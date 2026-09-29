"""Перепланирование дня по событию (README §6).

1. Заморозка на момент T по расписанию текущего плана. Фиксируются выполненные заявки, заявка в работе
   (текущая работа не прерывается) и заявка, к которой инженер уже выехал: он её доделывает. Это упрощение:
   считаем, что развернуть инженера в пути нельзя. Инженер освобождается в точке последней зафиксированной
   заявки после её окончания (но не раньше T). Инженер без зафиксированных заявок стоит в офисе.
2. Событие:
   - urgent — новая срочная заявка с окном [T, конец дня] и штрафом за пропуск как у аварии;
     координаты берутся из события или геокодируются, в матрицы добавляется новый узел (по дорогам — только для транспорта, который может её взять);
   - cancel — снять можно только ещё не начатую заявку. Если инженер уже ехал к ней, он освобождается
     в предыдущей точке в момент T;
   - unavailable — инженер больше не получает заявки. Выполненное остаётся за ним, текущая работа
     доделывается (finish_current) или возвращается в пул вместе со всеми будущими.
3. Оставшиеся заявки перерешиваются тем же алгоритмом, что строил план. Для оптимизатора стартовое решение —
   остаток прошлого плана, а перенос заявки к другому инженеру штрафуется (стабильность).
Инженеров вне смены не добавляем: на участке только инженеры, работающие в этот день.
"""
from __future__ import annotations

from ..catalogs import (Priority, duration_for, required_transport_for, skill_for)
from ..geo.matrix import extend_matrices
from ..models import Event, Plan, Point, Request, Route, Stop, hhmm
from .baseline import baseline_assignment
from .feasibility import can_serve
from .instance import OFFICE, Instance, Start
from .optimizer import solve_optimized
from .plan import assemble_plan

END_OF_DAY = 24 * 60 - 1


class ReplanError(ValueError):
    """Событие нельзя применить к плану (сообщение — для диспетчера)."""


def _frozen_count(route: Route, now: int, release: int) -> int:
    """Сколько первых остановок уже нельзя менять: выполнены, в работе или инженер к ним выехал.

    `release` — когда инженер освободился по прошлому перепланированию: к первой незафиксированной
    заявке он выезжает не по окончании предыдущей, а не раньше момента прошлого события.
    """
    n = route.frozen
    while n < len(route.stops):
        if n == 0:
            leg_start = route.departure
        elif n == route.frozen:
            leg_start = max(route.stops[n - 1].end, release)
        else:
            leg_start = route.stops[n - 1].end
        if leg_start >= now:
            break
        n += 1
    return n


def _prefix(route: Route, n: int) -> Route:
    stops = route.stops[:n]
    return Route(engineer_id=route.engineer_id, departure=route.departure, stops=stops,
                 km=round(sum(s.leg_km for s in stops), 3), travel_min=sum(s.leg_min for s in stops), frozen=n)


def _geocode(address: str) -> Point:
    import httpx

    from ..geo.geocode import Geocoder, load_cache  # сеть нужна только здесь

    hit = load_cache().get(address)
    if not hit or "lat" not in hit:
        geocoder = Geocoder()
        try:
            hit = geocoder.geocode(address)
        except httpx.HTTPError:
            raise ReplanError(f"Геокодер недоступен — укажите точку на карте для «{address}»") from None
        finally:
            geocoder.client.close()
    if not hit:
        raise ReplanError(f"Адрес «{address}» не найден — укажите точку на карте")
    return Point(lat=hit["lat"], lon=hit["lon"])


def _urgent_request(inst: Instance, plan: Plan, event: Event) -> Request:
    if event.location is None and not event.address:
        raise ReplanError("Для срочной заявки нужен адрес или точка на карте")
    try:
        skill = skill_for(event.type_bk)
    except KeyError:
        raise ReplanError(f"Неизвестный тип заявки «{event.type_bk}»") from None
    # отменённые заявки уже не в inst, но их id занят: иначе новая срочная совпадёт с отменённой
    taken = set(inst.request_by_id) | set(plan.cancelled) | {r.id for r in plan.extra_requests}
    rid = event.request_id or next(f"U{i}" for i in range(1, 10**6) if f"U{i}" not in taken)
    if rid in taken:
        raise ReplanError(f"Заявка {rid} уже есть")
    address = event.address or "точка на карте"
    return Request(
        id=rid, address=address, type_bk=event.type_bk, type_hd=event.type_hd,
        window_start=event.time, window_end=END_OF_DAY,
        duration=event.duration or duration_for(event.type_bk, event.type_hd, False),
        priority=Priority.URGENT, skill=skill,
        required_transport=required_transport_for(event.type_bk, event.type_hd, event.address or "Москва"),
        location=event.location or _geocode(event.address),
        geo_quality="exact" if event.location else "street",
    )


def _add_node(inst: Instance, req: Request, provider: str | None) -> tuple[dict, dict, list[Point]]:
    """Матрицы, node_of и точки с новым узлом заявки в конце."""
    if inst.points is None:
        raise ReplanError("У участка нет координат узлов — срочную заявку добавить нельзя")
    points = inst.points + [req.location]
    exact = {e.transport for e in inst.active_engineers if can_serve(e, req)}
    matrices = extend_matrices(inst.matrices, points, exact, provider)
    return matrices, {**inst.node_of, req.id: len(inst.points)}, points


def replan(inst: Instance, plan: Plan, event: Event, time_limit: float = 10.0,
           provider: str | None = None) -> tuple[Plan, Instance]:
    """Новый план дня после события и его Instance (нужен для объяснений и следующих событий)."""
    now = event.time
    if plan.now is not None and now < plan.now:
        raise ReplanError(f"Событие в {hhmm(now)} раньше предыдущего перепланирования ({hhmm(plan.now)})")
    routes = {r.engineer_id: r for r in plan.routes}
    cancelled = list(plan.cancelled)

    if event.type == "unavailable" and event.engineer_id not in inst.engineer_by_id:
        raise ReplanError(f"Нет инженера {event.engineer_id}")
    if event.type == "cancel":
        rid = event.request_id
        if rid not in inst.request_by_id or rid in cancelled:
            raise ReplanError(f"Нет заявки {rid}")
        stop = next((s for r in plan.routes for s in r.stops if s.request_id == rid), None)
        if stop and stop.start <= now:
            state = "уже выполнена" if stop.end <= now else "уже в работе"
            raise ReplanError(f"Заявку {rid} отменить нельзя: она {state}")
        cancelled.append(rid)

    frozen: dict[str, Route] = {}
    starts: dict[str, Start] = {}
    for eng in inst.engineers:
        route = routes.get(eng.id) or Route(engineer_id=eng.id, departure=eng.shift_start)
        n = _frozen_count(route, now, inst.start_of(eng).time)
        if event.type == "cancel":  # инженер ехал к отменённой заявке — она не фиксируется
            n = next((i for i, s in enumerate(route.stops[:n]) if s.request_id == event.request_id), n)
        if event.type == "unavailable" and eng.id == event.engineer_id:
            n = 0
            for s in route.stops:
                if not (s.end <= now or (event.finish_current and s.start <= now)):
                    break
                n += 1
        if n:
            frozen[eng.id] = _prefix(route, n)
            last: Stop = route.stops[n - 1]
            starts[eng.id] = Start(inst.node_of[last.request_id], max(now, last.end))
        else:
            starts[eng.id] = Start(OFFICE, max(now, eng.shift_start))

    fixed_ids = {rid for r in frozen.values() for rid in r.request_ids}
    fixed_requests = [r for r in inst.all_requests if r.id in fixed_ids]
    open_requests = [r for r in inst.all_requests if r.id not in fixed_ids and r.id not in cancelled]
    matrices, node_of, points = inst.matrices, inst.node_of, inst.points
    extra_requests = list(plan.extra_requests)
    if event.type == "urgent":
        new = _urgent_request(inst, plan, event)
        matrices, node_of, points = _add_node(inst, new, provider)
        open_requests.append(new)
        extra_requests.append(new)

    unavailable = set(inst.unavailable) | ({event.engineer_id} if event.type == "unavailable" else set())
    open_ids = {r.id for r in open_requests}
    remainder = {r.engineer_id: [rid for rid in r.request_ids if rid in open_ids] for r in plan.routes}
    new_inst = Instance(
        area=inst.area, requests=open_requests, engineers=inst.engineers, matrices=matrices, node_of=node_of,
        starts=starts, points=points, now=now, fixed_requests=fixed_requests, frozen=frozen,
        unavailable=unavailable, previous={rid: e for e, ids in remainder.items() for rid in ids},
    )

    if plan.algorithm == "baseline":
        new_plan = assemble_plan(new_inst, "baseline", baseline_assignment(new_inst), append_only=True)
    else:
        initial = {e: ids for e, ids in remainder.items() if e not in unavailable}
        new_plan = solve_optimized(new_inst, time_limit, reduce_engineers=False, initial=initial)
    new_plan.parent_id, new_plan.now, new_plan.event = plan.id, now, event.model_dump(mode="json")
    new_plan.extra_requests, new_plan.cancelled = extra_requests, cancelled
    new_plan.unavailable = sorted(unavailable)
    return new_plan, new_inst

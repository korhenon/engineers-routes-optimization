"""Проверка ограничений ТЗ §2.2 — единая для базового алгоритма, оптимизатора, объяснений и тестов.

Правила расписания:
  - инженер выезжает из точки старта не раньше начала смены (или момента освобождения при перепланировании);
  - приехать раньше окна можно — инженер ждёт; начать работу позже конца окна нельзя;
  - последняя работа заканчивается не позже конца смены; возвращаться в офис не нужно;
  - навык заявки должен быть у инженера, требуемый транспорт — совпадать с его транспортом.
Выезд к первой заявке сдвигается так, чтобы не ждать у клиента (на расписание это не влияет).
После перепланирования маршрут = зафиксированный префикс (inst.frozen, берётся как есть) + новые заявки,
которые считаются от точки и времени освобождения инженера (inst.start_of).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models import Engineer, Plan, Request, Route, Stop, hhmm
from .instance import Instance


def skill_ok(eng: Engineer, req: Request) -> bool:
    return req.skill in eng.skills


def transport_ok(eng: Engineer, req: Request) -> bool:
    return req.required_transport is None or req.required_transport == eng.transport


def can_serve(eng: Engineer, req: Request) -> bool:
    return skill_ok(eng, req) and transport_ok(eng, req)


@dataclass
class RouteSim:
    route: Route
    violations: list[str]

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def end_time(self) -> int:
        return self.route.stops[-1].end if self.route.stops else self.route.departure


def simulate_route(inst: Instance, eng: Engineer, request_ids: list[str]) -> RouteSim:
    """request_ids — только новая (незафиксированная) часть маршрута."""
    start = inst.start_of(eng)
    node, t = start.node, start.time
    frozen = inst.frozen.get(eng.id)
    stops: list[Stop] = list(frozen.stops) if frozen else []
    violations: list[str] = []
    who = f"{eng.name} ({eng.id})"

    for rid in request_ids:
        req = inst.request_by_id[rid]
        target = inst.node_of[rid]
        km, minutes = inst.leg(eng, node, target)
        arrival = t + minutes
        begin = max(arrival, req.window_start)
        if not skill_ok(eng, req):
            violations.append(f"{rid}: у {who} нет навыка «{req.skill.label}»")
        if not transport_ok(eng, req):
            violations.append(f"{rid}: нужен транспорт «{req.required_transport.label}», у {who} — «{eng.transport.label}»")
        if begin > req.window_end:
            violations.append(
                f"{rid}: {who} начинает в {hhmm(begin)}, окно {hhmm(req.window_start)}–{hhmm(req.window_end)} уже закрыто"
            )
        end = begin + req.duration
        stops.append(Stop(request_id=rid, arrival=arrival, start=begin, end=end, wait=begin - arrival,
                          leg_km=km, leg_min=minutes))
        node, t = target, end

    if request_ids and stops[-1].end > eng.shift_end:
        violations.append(f"{who}: работа заканчивается в {hhmm(stops[-1].end)}, смена до {hhmm(eng.shift_end)}")

    departure = start.time
    if frozen and frozen.stops:
        departure = frozen.departure
    elif stops:  # выезжаем «впритык» к первой заявке
        first = stops[0]
        departure = first.start - first.leg_min
        stops[0] = first.model_copy(update={"arrival": first.start, "wait": 0})

    route = Route(
        engineer_id=eng.id,
        departure=departure,
        stops=stops,
        km=round(sum(s.leg_km for s in stops), 3),
        travel_min=sum(s.leg_min for s in stops),
        frozen=len(frozen.stops) if frozen else 0,
    )
    return RouteSim(route, violations)


def best_insertion(inst: Instance, eng: Engineer, request_ids: list[str], rid: str) -> tuple[int, float] | None:
    """Лучшая допустимая позиция вставки заявки в маршрут: (позиция, прирост км) или None."""
    req = inst.request_by_id[rid]
    if not can_serve(eng, req):
        return None
    base_km = simulate_route(inst, eng, request_ids).route.km
    best: tuple[int, float] | None = None
    for pos in range(len(request_ids) + 1):
        sim = simulate_route(inst, eng, request_ids[:pos] + [rid] + request_ids[pos:])
        if sim.ok and (best is None or sim.route.km - base_km < best[1]):
            best = (pos, round(sim.route.km - base_km, 3))
    return best


def validate_plan(inst: Instance, plan: Plan) -> list[str]:
    """Независимая проверка плана: полнота и уникальность назначений + ограничения каждого маршрута."""
    violations: list[str] = []
    seen_engineers: set[str] = set()
    seen_requests: dict[str, str] = {}

    for route in plan.routes:
        eng = inst.engineer_by_id.get(route.engineer_id)
        if eng is None:
            violations.append(f"Неизвестный инженер {route.engineer_id}")
            continue
        if eng.id in seen_engineers:
            violations.append(f"Инженер {eng.id} встречается в плане дважды")
        seen_engineers.add(eng.id)

        ids = route.request_ids
        unknown = [rid for rid in ids if rid not in inst.request_by_id]
        if unknown:
            violations.append(f"{eng.id}: неизвестные заявки {unknown}")
            continue
        for rid in ids:
            if rid in seen_requests:
                violations.append(f"{rid}: назначена дважды ({seen_requests[rid]}, {eng.id})")
            seen_requests[rid] = eng.id

        fixed = inst.frozen_ids(eng.id)
        if ids[:len(fixed)] != fixed:
            violations.append(f"{eng.id}: зафиксированные заявки {fixed} изменены или переставлены")
            continue
        sim = simulate_route(inst, eng, ids[len(fixed):])
        if eng.id in inst.unavailable and len(ids) > len(fixed):
            violations.append(f"{eng.id}: инженер недоступен, но получил новые заявки")
        violations += sim.violations
        for got, expected in zip(route.stops, sim.route.stops):
            if (got.start, got.end) != (expected.start, expected.end):
                violations.append(
                    f"{got.request_id}: в плане {hhmm(got.start)}–{hhmm(got.end)}, "
                    f"по расчёту {hhmm(expected.start)}–{hhmm(expected.end)}"
                )

    for u in plan.unassigned:
        if u.request_id in seen_requests:
            violations.append(f"{u.request_id}: одновременно назначена и в списке неназначенных")
        seen_requests.setdefault(u.request_id, "—")
    missing = [r.id for r in inst.all_requests if r.id not in seen_requests]
    if missing:
        violations.append(f"Заявки потеряны (нет ни в маршрутах, ни в неназначенных): {missing}")
    return violations

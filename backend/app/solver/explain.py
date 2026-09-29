"""Короткие объяснения языком диспетчера (шаблоны, без LLM), README §7.

  unassigned_reason     — почему заявка не назначена (первая сработавшая причина);
  assignment_explanation — почему заявка у этого инженера и во сколько (2–4 строки);
  route_explanation     — одна строка про маршрут инженера;
  plan_summary          — «выполнено N из M» + сколько ещё инженеров нужно, чтобы закрыть остальное.
"""
from __future__ import annotations

import dataclasses

from ..catalogs import Skill, Transport
from ..models import Engineer, Plan, Request, Route, hhmm
from .feasibility import best_insertion, can_serve, simulate_route, skill_ok
from .instance import OFFICE, Instance, Start

SKILL_SHORT = {Skill.LOCAL: "локальные", Skill.CONNECTION: "подключения", Skill.EMERGENCY: "аварии"}


def _window(req: Request) -> str:
    return f"{hhmm(req.window_start)}–{hhmm(req.window_end)}"


def _names(engineers, limit: int = 3) -> str:
    names = [e.name for e in engineers]
    return ", ".join(names[:limit]) + (f" и ещё {len(names) - limit}" if len(names) > limit else "")


def unassigned_reason(inst: Instance, rid: str, routes: dict[str, list[str]], append_only: bool = False) -> str:
    """Первая сработавшая причина из списка (README §7).

    append_only — план базового алгоритма, который ставит заявку только в конец маршрута.
    """
    req = inst.request_by_id[rid]
    if req.window_end < inst.now:
        return f"Окно {_window(req)} уже прошло"
    with_skill = [e for e in inst.active_engineers if skill_ok(e, req)]
    if not with_skill:
        return f"Нет инженера с навыком «{req.skill.label}»"
    suitable = [e for e in with_skill if can_serve(e, req)]
    if not suitable:
        return f"Нет инженера с навыком «{req.skill.label}» и транспортом «{req.required_transport.label}»"

    def shift_fits(e) -> bool:
        begin = max(req.window_start, inst.start_of(e).time)
        return begin <= req.window_end and begin + req.duration <= e.shift_end

    on_shift = [e for e in suitable if shift_fits(e)]
    if not on_shift:
        return f"Ни у одного подходящего инженера смена не покрывает окно {_window(req)} (работа {req.duration} мин)"

    reachable = [e for e in on_shift if simulate_route(inst, e, [rid]).ok]
    if not reachable:
        travel = min(inst.leg(e, inst.start_of(e).node, inst.node_of[rid])[1] for e in on_shift)
        return f"Не успевает: дорога от точки старта не меньше {travel} мин, окно {_window(req)} и смена не позволяют"

    insertable = [e for e in reachable if best_insertion(inst, e, routes.get(e.id, []), rid)]
    if insertable and append_only:
        return (f"Базовый порядок: у подходящих инженеров ({_names(insertable)}) в конце маршрута уже стоят "
                f"более поздние заявки, а вставлять в середину он не умеет")
    if insertable:
        return (f"Можно поставить к {_names(insertable)}, но тогда не поместятся более важные заявки "
                f"или понадобится лишний инженер")
    return f"Все подходящие инженеры ({_names(reachable)}) заняты в окне {_window(req)}"


def open_assignment(plan: Plan) -> dict[str, list[str]]:
    """Незафиксированная часть маршрутов плана: инженер -> заявки."""
    return {r.engineer_id: r.request_ids[r.frozen:] for r in plan.routes}


def _engineer(e: Engineer) -> str:
    skills = ", ".join(SKILL_SHORT[s] for s in e.skills)
    return f"{e.name} ({e.transport.label.lower()}; {skills})"


def assignment_explanation(inst: Instance, plan: Plan, rid: str) -> list[str]:
    """Объяснение по любой заявке плана: назначенной — почему у этого инженера, иначе — причина."""
    req = inst.request_by_id[rid]
    for u in plan.unassigned:
        if u.request_id == rid:
            return [f"Не назначена: {u.reason}"]
    route = next(r for r in plan.routes if rid in r.request_ids)
    eng = inst.engineer_by_id[route.engineer_id]
    pos = route.request_ids.index(rid)
    stop = route.stops[pos]
    lines = [f"Назначена: {_engineer(eng)}, {pos + 1}-я в маршруте"]

    transport = f"транспорт «{req.required_transport.label}»" if req.required_transport else "транспорт любой"
    timing = f"начало в {hhmm(stop.start)}"
    if stop.wait:
        timing += f" (приезд в {hhmm(stop.arrival)}, ждёт {stop.wait} мин)"
    lines.append(f"✓ навык «{req.skill.label}» ✓ {transport} ✓ окно {_window(req)} — {timing}")

    if pos < route.frozen:
        state = "выполнена" if stop.end <= inst.now else "в работе" if stop.start <= inst.now else "инженер уже в пути"
        lines.append(f"Зафиксирована при перепланировании в {hhmm(inst.now)}: {state}")
        return lines

    assignment = open_assignment(plan)
    own = assignment[eng.id]
    without = [x for x in own if x != rid]
    cost = simulate_route(inst, eng, own).route.km - simulate_route(inst, eng, without).route.km
    alternatives = []
    for other in inst.active_engineers:
        if other.id == eng.id or not can_serve(other, req):
            continue
        ins = best_insertion(inst, other, assignment.get(other.id, []), rid)
        if ins:
            alternatives.append((ins[1], other))
    why = f"Почему он: заявка добавляет в его маршрут {cost:+.1f} км"
    if alternatives:
        delta, other = min(alternatives, key=lambda a: a[0])
        idle = "" if plan_uses(plan, other.id) else ", но это ещё один задействованный инженер"
        why += f". Ближайшая альтернатива — {other.name}, {delta:+.1f} км{idle}"
    else:
        why += ". У других подходящих инженеров нет свободного времени в этом окне"
    lines.append(why)
    return lines


def plan_uses(plan: Plan, eng_id: str) -> bool:
    return any(r.engineer_id == eng_id and r.stops for r in plan.routes)


def route_explanation(inst: Instance, route: Route) -> str:
    if not route.stops:
        return "Без заявок" if route.engineer_id not in inst.unavailable else "Недоступен, новых заявок нет"
    eng = inst.engineer_by_id[route.engineer_id]
    busy = sum(s.leg_min + (s.end - s.start) for s in route.stops)
    text = (f"{len(route.stops)} заяв. · {route.km:.1f} км · в пути {route.travel_min} мин · "
            f"{hhmm(route.departure)}–{hhmm(route.stops[-1].end)} · занятость смены "
            f"{round(100 * busy / (eng.shift_end - eng.shift_start))}%")
    if route.frozen:
        text += f" · зафиксировано {route.frozen}"
    if eng.id in inst.unavailable:
        text += " · недоступен, новых заявок нет"
    return text


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else many


def extra_engineers(inst: Instance, request_ids: list[str]) -> tuple[list[Route], list[Engineer], list[str]]:
    """Жадная оценка, сколько дополнительных инженеров закроют заявки: универсал (все навыки, автомобиль,
    из офиса) на одну из смен, которые уже есть на участке. Каждого следующего берём на ту смену, где он
    закрывает больше оставшихся заявок. Возвращает маршруты, самих инженеров и заявки, которые не закрыть."""
    transport = Transport.CAR if Transport.CAR in inst.matrices else next(iter(inst.matrices))
    shifts = sorted({(e.shift_start, e.shift_end) for e in inst.engineers if e.shift_end > inst.now})
    remaining = sorted(request_ids, key=lambda rid: (-inst.request_by_id[rid].priority,
                                                     inst.request_by_id[rid].window_start))
    work = inst
    extra: list[tuple[Engineer, list[str]]] = []
    while remaining:
        best = None
        for shift_start, shift_end in shifts:
            n = len(extra) + 1
            eng = Engineer(id=f"+{n}", name=f"Доп. инженер {n}", shift_start=shift_start, shift_end=shift_end,
                           skills=list(Skill), transport=transport)
            candidate = dataclasses.replace(
                work, engineers=work.engineers + [eng],
                starts={**work.starts, eng.id: Start(OFFICE, max(shift_start, inst.now))},
            )
            ids: list[str] = []
            for rid in remaining:
                if ins := best_insertion(candidate, eng, ids, rid):
                    ids.insert(ins[0], rid)
            if ids and (best is None or len(ids) > len(best[2])):
                best = (eng, candidate, ids)
        if best is None:
            break
        eng, work, ids = best
        extra.append((eng, ids))
        remaining = [rid for rid in remaining if rid not in ids]
    routes = [simulate_route(work, eng, ids).route for eng, ids in extra]
    return routes, [eng for eng, _ in extra], remaining


def plan_summary(inst: Instance, plan: Plan) -> str:
    m = plan.metrics
    text = f"Выполнено {m['assigned']} из {m['total']} ({round(100 * m['assigned_share'])}%)"
    if not plan.unassigned:
        return text + "."
    routes, extra, impossible = extra_engineers(inst, [u.request_id for u in plan.unassigned])
    by_shift: dict[tuple[int, int], tuple[int, set]] = {}
    for route, eng in zip(routes, extra):
        count, skills = by_shift.get((eng.shift_start, eng.shift_end), (0, set()))
        by_shift[eng.shift_start, eng.shift_end] = (count + 1, skills | {inst.request_by_id[r].skill for r in route.request_ids})
    parts = [
        f"{count} на смену {hhmm(a)}–{hhmm(b)} ({', '.join(SKILL_SHORT[s] for s in sorted(skills))})"
        for (a, b), (count, skills) in sorted(by_shift.items())
    ]
    if routes:
        n = len(routes)
        need = _plural(n, "нужен ещё", "нужны ещё", "нужны ещё")
        who = _plural(n, "инженер", "инженера", "инженеров")
        closable = len(plan.unassigned) - len(impossible)
        text += f". Чтобы закрыть оставшиеся {closable}, {need} {n} {who} на автомобиле: " + "; ".join(parts)
    if impossible:
        text += (f". {len(impossible)} {_plural(len(impossible), 'заявку', 'заявки', 'заявок')} "
                 f"не закрыть даже дополнительным инженером")
    return text + "."

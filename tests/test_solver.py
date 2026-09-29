import pytest

from backend.app.catalogs import AREAS, Priority, Skill, Transport
from backend.app.data.loader import load_area
from backend.app.geo.matrix import Matrix
from backend.app.models import Engineer, Request, Unassigned
from backend.app.solver.baseline import baseline_assignment, solve_baseline
from backend.app.solver.explain import unassigned_reason
from backend.app.solver.feasibility import best_insertion, simulate_route, validate_plan
from backend.app.solver.instance import Instance, build_instance
from backend.app.solver.optimizer import solve_optimized

H = 60


def req(rid, skill=Skill.LOCAL, window=(10 * H, 12 * H), duration=60, transport=None):
    return Request(id=rid, address=rid, type_bk="x", window_start=window[0], window_end=window[1],
                   duration=duration, priority=Priority.URGENT if skill is Skill.EMERGENCY else Priority.NORMAL,
                   skill=skill, required_transport=transport)


def eng(eid, skills=(Skill.LOCAL,), shift=(9 * H, 18 * H), transport=Transport.CAR):
    return Engineer(id=eid, name=f"Инженер {eid}", shift_start=shift[0], shift_end=shift[1],
                    skills=list(skills), transport=transport)


def tiny(requests, engineers, minutes=10):
    """Офис + заявки; между любыми двумя разными узлами `minutes` мин и 1 км, для всех видов транспорта."""
    n = len(requests) + 1
    m = Matrix([[0 if i == j else 1.0 for j in range(n)] for i in range(n)],
               [[0 if i == j else minutes for j in range(n)] for i in range(n)])
    return Instance(area="test", requests=requests, engineers=engineers,
                    matrices={t: m for t in Transport}, node_of={r.id: i + 1 for i, r in enumerate(requests)})


def test_simulate_waits_and_departs_just_in_time():
    inst = tiny([req("a", window=(12 * H, 14 * H)), req("b", window=(12 * H, 14 * H))], [eng("e")])
    sim = simulate_route(inst, inst.engineers[0], ["a", "b"])
    assert sim.ok
    a, b = sim.route.stops
    assert sim.route.departure == 12 * H - 10 and (a.start, a.end, a.wait) == (12 * H, 13 * H, 0)
    assert (b.arrival, b.start, b.end) == (13 * H + 10, 13 * H + 10, 14 * H + 10)
    assert sim.route.km == 2.0


def test_simulate_reports_every_violation():
    inst = tiny([req("late", window=(9 * H, 9 * H + 5)), req("car", skill=Skill.EMERGENCY, transport=Transport.CAR,
                                                            window=(17 * H, 18 * H), duration=120)],
                [eng("e", transport=Transport.FOOT)])
    text = " | ".join(simulate_route(inst, inst.engineers[0], ["late", "car"]).violations)
    assert "окно 09:00–09:05 уже закрыто" in text
    assert "нет навыка «Аварийные работы»" in text
    assert "нужен транспорт «Автомобиль»" in text
    assert "смена до 18:00" in text


def test_baseline_is_append_only_and_optimizer_beats_it():
    # Файл идёт не по времени: вечерняя заявка раньше утренней. Базовый алгоритм отдаёт вечернюю первому
    # инженеру, утреннюю поставить после неё нельзя — нужен второй инженер. Оптимизатору хватает одного.
    requests = [req("evening", window=(16 * H, 18 * H)), req("morning", window=(10 * H, 12 * H))]
    inst = tiny(requests, [eng("e1"), eng("e2")])
    assert baseline_assignment(inst) == {"e1": ["evening"], "e2": ["morning"]}
    base, opt = solve_baseline(inst), solve_optimized(inst, time_limit=2)
    assert base.metrics["engineers_used"] == 2 and opt.metrics["engineers_used"] == 1
    assert opt.metrics["assigned"] == 2 and not base.violations and not opt.violations
    assert [r.request_ids for r in opt.routes if r.stops] == [["morning", "evening"]]


def test_optimizer_prefers_emergency_over_locals():
    # Одна смена на 3 часа: либо авария (2 ч), либо две локальные — выбрать надо аварию.
    requests = [req("l1", window=(9 * H, 10 * H)), req("l2", window=(10 * H, 11 * H)),
                req("em", skill=Skill.EMERGENCY, window=(9 * H, 10 * H), duration=120, transport=Transport.CAR)]
    inst = tiny(requests, [eng("e", skills=(Skill.LOCAL, Skill.EMERGENCY), shift=(9 * H, 12 * H))])
    plan = solve_optimized(inst, time_limit=2, reduce_engineers=False)
    assert plan.routes[0].request_ids[0] == "em" and not plan.violations
    assert {u.request_id for u in plan.unassigned} >= {"l1"}


def test_validate_plan_catches_duplicates_and_losses():
    inst = tiny([req("a"), req("b")], [eng("e1"), eng("e2")])
    plan = solve_baseline(inst)
    assert plan.violations == []
    plan.routes[1] = simulate_route(inst, inst.engineers[1], ["a"]).route
    plan.unassigned = []
    violations = " | ".join(validate_plan(inst, plan))
    assert "a: назначена дважды" in violations
    plan.routes = [simulate_route(inst, inst.engineers[0], ["a"]).route]
    assert any("потеряны" in v and "'b'" in v for v in validate_plan(inst, plan))
    plan.unassigned = [Unassigned(request_id="b", reason="")]
    plan.routes[0].stops[0].start += 5
    assert any("по расчёту" in v for v in validate_plan(inst, plan))


def test_unassigned_reasons():
    requests = [
        req("no_skill", skill=Skill.EMERGENCY, transport=Transport.CAR),
        req("no_car", skill=Skill.CONNECTION, transport=Transport.CAR),
        req("night", window=(20 * H, 22 * H)),
        req("far", window=(9 * H, 9 * H + 5)),
        req("busy1", duration=100), req("busy2", duration=100),
    ]
    inst = tiny(requests, [eng("e", skills=(Skill.LOCAL, Skill.CONNECTION), transport=Transport.FOOT)], minutes=30)
    routes = {"e": ["busy1"]}
    reason = {rid: unassigned_reason(inst, rid, routes) for rid in ("no_skill", "no_car", "night", "far", "busy2")}
    assert reason["no_skill"] == "Нет инженера с навыком «Аварийные работы»"
    assert "транспортом «Автомобиль»" in reason["no_car"]
    assert "смена не покрывает окно 20:00–22:00" in reason["night"]
    assert reason["far"].startswith("Не успевает: дорога от точки старта не меньше 30 мин")
    assert reason["busy2"].startswith("Все подходящие инженеры (Инженер e) заняты")
    assert best_insertion(inst, inst.engineers[0], [], "busy2") == (0, 1.0)


@pytest.fixture(scope="module")
def area_plans():
    result = {}
    for area in AREAS:
        inst = build_instance(load_area(area))
        result[area] = (inst, solve_baseline(inst), solve_optimized(inst, time_limit=4, reduce_engineers=False))
    return result


@pytest.mark.parametrize("area", AREAS)
def test_area_plans_are_valid_and_optimizer_is_better(area, area_plans):
    inst, base, opt = area_plans[area]
    assert base.violations == [] and opt.violations == []
    for plan in (base, opt):
        assert plan.metrics["assigned"] + len(plan.unassigned) == len(inst.requests)
        assert all(u.reason for u in plan.unassigned)
    assert opt.metrics["assigned"] > base.metrics["assigned"]
    assert opt.metrics["engineers_used"] <= base.metrics["engineers_used"]
    assert opt.metrics["by_skill"]["emergency"]["assigned"] == opt.metrics["by_skill"]["emergency"]["total"]

from unittest.mock import patch

import httpx
import pytest

from backend.app.catalogs import Skill, Transport
from backend.app.geo.matrix import haversine_matrix
from backend.app.models import Event, Point
from backend.app.solver.baseline import solve_baseline
from backend.app.solver.explain import assignment_explanation, unassigned_reason
from backend.app.solver.instance import Instance
from backend.app.solver.metrics import plan_diff
from backend.app.solver.optimizer import solve_optimized
from backend.app.solver.plan import assemble_plan
from backend.app.solver.replan import ReplanError, replan
from tests.test_solver import H, eng, req, tiny

# Узлы в tiny: 10 мин и 1 км между любыми двумя. Утро e1: a 10:00–11:00, b (выезд 11:00, ждёт) 12:00–13:00.
MORNING = [req("a", window=(10 * H, 12 * H)), req("b", window=(12 * H, 14 * H)), req("c", window=(14 * H, 16 * H))]


def day(engineers=("e1", "e2")):
    inst = tiny([r.model_copy() for r in MORNING], [eng(e) for e in engineers])
    plan = assemble_plan(inst, "optimized", {"e1": ["a", "b", "c"]})
    assert plan.violations == []
    return inst, plan


def route(plan, eng_id):
    return next(r for r in plan.routes if r.engineer_id == eng_id)


def test_cancel_freezes_started_work_and_keeps_owners():
    inst, plan = day()
    new, new_inst = replan(inst, plan, Event(type="cancel", time=10 * H + 30, request_id="b"), time_limit=1)
    assert new.violations == [] and new.cancelled == ["b"] and new.parent_id == plan.id and new.now == 10 * H + 30
    e1 = route(new, "e1")
    assert e1.request_ids == ["a", "c"] and e1.frozen == 1  # a в работе — зафиксирована
    assert e1.stops[0] == route(plan, "e1").stops[0]
    assert new_inst.start_of(new_inst.engineer_by_id["e1"]).time == 11 * H
    diff = plan_diff(plan, new)
    assert diff["cancelled"] == ["b"] and diff["moved"] == [] and diff["changed_engineers"] == ["e1"]
    assert any("Зафиксирована при перепланировании в 10:30: в работе" in line
               for line in assignment_explanation(new_inst, new, "a"))


def test_cancel_rejects_started_work_and_earlier_events():
    inst, plan = day()
    with pytest.raises(ReplanError, match="уже в работе"):
        replan(inst, plan, Event(type="cancel", time=10 * H + 30, request_id="a"))
    new, new_inst = replan(inst, plan, Event(type="cancel", time=12 * H, request_id="c"), time_limit=1)
    with pytest.raises(ReplanError, match="раньше предыдущего"):
        replan(new_inst, new, Event(type="cancel", time=11 * H, request_id="b"))


def test_engineer_on_the_way_finishes_current_trip():
    # В 11:30 e1 уже выехал к b (выезд в 11:00, ждёт окна) — b фиксируется; при отмене b он свободен в точке a.
    inst, plan = day()
    new, _ = replan(inst, plan, Event(type="cancel", time=11 * H + 30, request_id="c"), time_limit=1)
    assert route(new, "e1").frozen == 2
    new, new_inst = replan(inst, plan, Event(type="cancel", time=11 * H + 30, request_id="b"), time_limit=1)
    assert route(new, "e1").frozen == 1 and new.violations == []
    assert new_inst.start_of(new_inst.engineer_by_id["e1"]).node == new_inst.node_of["a"]


@pytest.mark.parametrize("finish_current, kept", [(True, ["a"]), (False, [])])
def test_unavailable_engineer_hands_over_rest_of_route(finish_current, kept):
    inst, plan = day()
    event = Event(type="unavailable", time=10 * H + 30, engineer_id="e1", finish_current=finish_current)
    new, new_inst = replan(inst, plan, event, time_limit=1)
    assert new.violations == [] and new_inst.unavailable == {"e1"} and new.unavailable == ["e1"]
    assert route(new, "e1").request_ids == kept
    assert route(new, "e2").request_ids == [rid for rid in ("a", "b", "c") if rid not in kept]
    assert route(new, "e2").stops[0].start >= 10 * H + 30
    assert {m["request_id"] for m in plan_diff(plan, new)["moved"]} == {"b", "c"} | ({"a"} - set(kept))


def test_replan_keeps_assignments_stable():
    # Одному инженеру все заявки не успеть. Обмен p2 ↔ q2 (или p1 ↔ q1) экономит 1 км,
    # но это меньше штрафа за перенос двух заявок — после отмены x план не перетасовывается.
    requests = [req("p1"), req("q1"), req("p2", window=(12 * H, 14 * H)), req("q2", window=(12 * H, 14 * H)),
                req("x", window=(14 * H, 16 * H))]
    requests = [r.model_copy(update={"duration": 90}) for r in requests]
    inst = tiny(requests, [eng("e1"), eng("e2")])
    km = inst.matrices[Transport.CAR].dist_km  # одна матрица на все виды транспорта
    for a, b in (("p1", "q2"), ("q1", "p2")):
        km[inst.node_of[a]][inst.node_of[b]] = 0.5
    plan = assemble_plan(inst, "optimized", {"e1": ["p1", "p2", "x"], "e2": ["q1", "q2"]})
    event = Event(type="cancel", time=9 * H, request_id="x")
    new, _ = replan(inst, plan, event, time_limit=2)
    assert new.violations == [] and plan_diff(plan, new)["moved"] == []

    with patch("backend.app.solver.optimizer.STABILITY_COST", 0):  # без штрафа обмен выгоден
        new, _ = replan(inst, plan, event, time_limit=2)
    assert len(plan_diff(plan, new)["moved"]) == 2


def test_urgent_request_gets_new_node_and_is_served_after_event():
    office = Point(lat=55.75, lon=37.60)
    requests = [req("a", window=(10 * H, 12 * H)), req("b", window=(12 * H, 14 * H))]
    points = [office, Point(lat=55.76, lon=37.61), Point(lat=55.74, lon=37.62)]
    engineers = [eng("loc"), eng("em", skills=(Skill.LOCAL, Skill.EMERGENCY))]
    inst = Instance(area="test", requests=requests, engineers=engineers,
                    matrices={Transport.CAR: haversine_matrix(points, Transport.CAR)},
                    node_of={"a": 1, "b": 2}, points=points)
    plan = solve_optimized(inst, time_limit=1)
    event = Event(type="urgent", time=12 * H + 15, location=Point(lat=55.755, lon=37.605))
    new, new_inst = replan(inst, plan, event, time_limit=2, provider="haversine")

    assert new.violations == [] and [r.id for r in new.extra_requests] == ["U1"]
    urgent = new_inst.request_by_id["U1"]
    assert (urgent.skill, urgent.required_transport, urgent.window_start) == (Skill.EMERGENCY, Transport.CAR, 12 * H + 15)
    stop = next(s for r in new.routes for s in r.stops if s.request_id == "U1")
    assert route(new, "em").request_ids[-1] == "U1" and stop.start >= 12 * H + 15
    assert plan_diff(plan, new)["added"] == ["U1"] and new.metrics["total"] == 3

    with pytest.raises(ReplanError, match="адрес или точка"):
        replan(inst, plan, Event(type="urgent", time=12 * H))

    # id отменённой срочной не переиспользуется: иначе новая U1 выпала бы из плана при следующем событии
    new, new_inst = replan(new_inst, new, Event(type="cancel", time=12 * H + 15, request_id="U1"), time_limit=1)
    new, new_inst = replan(new_inst, new, event, time_limit=1, provider="haversine")
    assert [r.id for r in new.extra_requests] == ["U1", "U2"] and new.cancelled == ["U1"]
    new, new_inst = replan(new_inst, new, Event(type="unavailable", time=12 * H + 20, engineer_id="loc"), time_limit=1)
    assert "U2" in new_inst.request_by_id and new.violations == []
    assert any("U2" in r.request_ids for r in new.routes)


def test_urgent_address_uses_geocode_cache_and_survives_offline():
    inst = tiny([req("a")], [eng("e1", skills=(Skill.LOCAL, Skill.EMERGENCY))])
    inst.points = [Point(lat=55.75, lon=37.60), Point(lat=55.76, lon=37.61)]
    plan = solve_optimized(inst, time_limit=1)
    event = Event(type="urgent", time=12 * H, address="Москва, Тверская ул., 1")
    offline = patch("backend.app.geo.geocode.Geocoder._search", side_effect=httpx.ConnectError("offline"))
    with offline, pytest.raises(ReplanError, match="укажите точку на карте"):
        replan(inst, plan, event, time_limit=1, provider="haversine")
    cached = {event.address: {"lat": 55.757, "lon": 37.61, "quality": "exact"}}
    with offline, patch("backend.app.geo.geocode.load_cache", return_value=cached):
        new, new_inst = replan(inst, plan, event, time_limit=1, provider="haversine")
    assert new_inst.request_by_id["U1"].location == Point(lat=55.757, lon=37.61)


def test_second_event_at_same_time_does_not_freeze_new_stop():
    # В 11:30 e2 выбывает, b и c уходят к e1, который стоит в точке a с 11:00. Второе событие в те же 11:30:
    # e1 к b ещё не выехал (выезжает не раньше 11:30), так что b не фиксируется.
    inst, _ = day()
    plan = assemble_plan(inst, "optimized", {"e1": ["a"], "e2": ["b", "c"]})
    new, new_inst = replan(inst, plan, Event(type="unavailable", time=11 * H + 30, engineer_id="e2"), time_limit=1)
    assert route(new, "e1").request_ids == ["a", "b", "c"] and route(new, "e1").frozen == 1
    new, _ = replan(new_inst, new, Event(type="cancel", time=11 * H + 30, request_id="c"), time_limit=1)
    assert route(new, "e1").request_ids == ["a", "b"] and route(new, "e1").frozen == 1


def test_baseline_plan_is_replanned_by_baseline():
    inst, _ = day()
    base = solve_baseline(inst)
    new, _ = replan(inst, base, Event(type="unavailable", time=9 * H, engineer_id="e1"))
    assert new.algorithm == "baseline" and new.violations == []


def test_summary_and_passed_window():
    inst = tiny([req("a", window=(10 * H, 10 * H + 30)), req("b", window=(10 * H, 10 * H + 30))], [eng("e1")])
    plan = solve_optimized(inst, time_limit=1)
    assert plan.metrics["assigned"] == 1
    assert plan.summary.startswith("Выполнено 1 из 2 (50%). Чтобы закрыть оставшиеся 1, нужен ещё 1 инженер")
    inst.now = 13 * H
    assert unassigned_reason(inst, "a", {}) == "Окно 10:00–10:30 уже прошло"

"""Метрики плана (ТЗ §2.5) и сравнение двух планов."""
from __future__ import annotations

from ..catalogs import Skill
from ..models import Plan
from .instance import Instance


def plan_metrics(inst: Instance, plan: Plan) -> dict:
    used = [r for r in plan.routes if r.stops]
    assigned = {rid for r in used for rid in r.request_ids}
    by_skill = {}
    for s in Skill:
        total = [r for r in inst.all_requests if r.skill is s]
        by_skill[s.name.lower()] = {"assigned": sum(r.id in assigned for r in total), "total": len(total)}
    shift_load = {}
    for r in used:
        eng = inst.engineer_by_id[r.engineer_id]
        busy = sum(s.leg_min + (s.end - s.start) for s in r.stops)
        shift_load[r.engineer_id] = round(busy / (eng.shift_end - eng.shift_start), 3)
    return {
        "engineers_used": len(used),
        "engineers_total": len(inst.engineers),
        "total_km": round(sum(r.km for r in used), 1),
        "km_per_request": round(sum(r.km for r in used) / len(assigned), 2) if assigned else 0.0,
        "km_by_engineer": {r.engineer_id: round(r.km, 1) for r in used},
        "travel_min": sum(r.travel_min for r in used),
        "wait_min": sum(s.wait for r in used for s in r.stops),
        "assigned": len(assigned),
        "total": len(inst.all_requests),
        "assigned_share": round(len(assigned) / len(inst.all_requests), 3) if inst.all_requests else 1.0,
        "by_skill": by_skill,
        "shift_load": shift_load,
    }


def compare(base: dict, other: dict) -> dict:
    """Разница ключевых метрик: other − base (отрицательные км/инженеры — улучшение)."""
    keys = ("assigned", "engineers_used", "total_km", "km_per_request", "travel_min")
    return {k: {"base": base[k], "other": other[k], "delta": round(other[k] - base[k], 2)} for k in keys}


def _owners(plan: Plan) -> dict[str, tuple[str, int]]:
    """заявка -> (инженер, начало работы)."""
    return {s.request_id: (r.engineer_id, s.start) for r in plan.routes for s in r.stops}


def plan_diff(old: Plan, new: Plan) -> dict:
    """Что изменилось между планами (обычно new — результат перепланирования old), README §6."""
    was, now = _owners(old), _owners(new)
    old_ids = set(was) | {u.request_id for u in old.unassigned}
    new_ids = set(now) | {u.request_id for u in new.unassigned}
    moved = [
        {"request_id": rid, "from": was[rid][0], "to": now[rid][0]}
        for rid in now if rid in was and was[rid][0] != now[rid][0]
    ]
    retimed = [
        {"request_id": rid, "engineer_id": now[rid][0], "old_start": was[rid][1], "new_start": now[rid][1]}
        for rid in now if rid in was and was[rid][0] == now[rid][0] and was[rid][1] != now[rid][1]
    ]
    old_routes = {r.engineer_id: r.request_ids for r in old.routes}
    changed = [r.engineer_id for r in new.routes if r.request_ids != old_routes.get(r.engineer_id, [])]
    return {
        "added": sorted(new_ids - old_ids),
        "cancelled": sorted(old_ids - new_ids),
        "moved": moved,
        "retimed": retimed,
        "newly_assigned": sorted(rid for rid in now if rid not in was),
        "newly_unassigned": sorted(u.request_id for u in new.unassigned if u.request_id in was),
        "changed_engineers": changed,
        "metrics": compare(old.metrics, new.metrics),
    }

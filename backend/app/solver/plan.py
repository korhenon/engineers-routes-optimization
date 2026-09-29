"""Сборка Plan из назначений «инженер → порядок заявок»: расписание, причины, метрики, проверка."""
from __future__ import annotations

import uuid

from ..models import Plan, Unassigned
from .explain import plan_summary, route_explanation, unassigned_reason
from .feasibility import simulate_route, validate_plan
from .instance import Instance
from .metrics import plan_metrics


def assemble_plan(inst: Instance, algorithm: str, assignment: dict[str, list[str]],
                  append_only: bool = False) -> Plan:
    """assignment — только незафиксированные заявки; зафиксированный префикс берётся из inst.frozen."""
    routes = [simulate_route(inst, e, assignment.get(e.id, [])).route for e in inst.engineers]
    for route in routes:
        route.explanation = route_explanation(inst, route)
    assigned = {rid for ids in assignment.values() for rid in ids}
    unassigned = [
        Unassigned(request_id=r.id, reason=unassigned_reason(inst, r.id, assignment, append_only))
        for r in inst.requests if r.id not in assigned
    ]
    plan = Plan(id=uuid.uuid4().hex[:8], area=inst.area, algorithm=algorithm, routes=routes, unassigned=unassigned)
    plan.metrics = plan_metrics(inst, plan)
    plan.violations = validate_plan(inst, plan)
    plan.summary = plan_summary(inst, plan)
    return plan

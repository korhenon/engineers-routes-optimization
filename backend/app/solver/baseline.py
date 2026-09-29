"""Базовый алгоритм строго по ТЗ §2.3 — точка сравнения для оптимизатора.

Заявки берутся в порядке поступления (порядок строк файла), каждая отдаётся первому по списку
инженеру, у которого подходят навык и транспорт и для которого заявка, добавленная в конец
маршрута, не нарушает окно и смену. Порядок посещения = порядок назначения.
"""
from __future__ import annotations

from ..models import Plan
from .feasibility import can_serve, simulate_route
from .instance import Instance
from .plan import assemble_plan


def baseline_assignment(inst: Instance) -> dict[str, list[str]]:
    engineers = inst.active_engineers
    assignment: dict[str, list[str]] = {e.id: [] for e in engineers}
    for req in inst.requests:
        for eng in engineers:
            if can_serve(eng, req) and simulate_route(inst, eng, assignment[eng.id] + [req.id]).ok:
                assignment[eng.id].append(req.id)
                break
    return assignment


def solve_baseline(inst: Instance) -> Plan:
    return assemble_plan(inst, "baseline", baseline_assignment(inst), append_only=True)

"""Оптимизатор на OR-Tools Routing (VRPTW с разнотипным транспортом), README §5.

Модель:
  - транспортные средства = инженеры; маршрут открытый: старт — узел старта инженера (офис),
    конец — фиктивный узел END, дорога до него ничего не стоит;
  - измерение Time: транзит = длительность работы в исходном узле + время в пути по транспорту инженера,
    ожидание разрешено; cumul узла заявки = начало работы и лежит в окне; cumul старта ≥ начала смены,
    cumul END (конец последней работы) ≤ конца смены;
  - навык/транспорт — через VehicleVar(node) ∈ {допустимые инженеры}.
Иерархия целей — масштабом коэффициентов:
  пропуск заявки (авария 1e10 > подключение 1e9 > локальная 1e8) ≫ задействованный инженер (1e6) ≫ метры пути.
Срочные заявки дополнительно «тянутся» к началу окна мягкой верхней границей (выполнять как можно раньше).

При перепланировании (replan.py): старт у каждого инженера свой, недоступные и отработавшие смену инженеры
в модель не входят, инженер с зафиксированными заявками уже «задействован» (фиксированная стоимость 0),
а переход заявки к другому инженеру стоит STABILITY_COST — план не перетасовывается ради сотни метров.

Расписание готового решения пересчитывается через feasibility.simulate_route, а план проверяется
validate_plan — поэтому модель и проверка не могут незаметно разойтись.
"""
from __future__ import annotations

import time

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from ..catalogs import Priority, Skill, Transport
from ..models import Engineer, Plan, Request
from .baseline import baseline_assignment
from .feasibility import can_serve
from .instance import Instance
from .plan import assemble_plan

DROP_PENALTY = {Skill.EMERGENCY: 10**10, Skill.CONNECTION: 10**9, Skill.LOCAL: 10**8}
ENGINEER_FIXED_COST = 10**6
STABILITY_COST = 3000  # «метров» за перенос заявки к другому инженеру при перепланировании
URGENT_DELAY_COST = 10**4  # за минуту задержки срочной работы: весомее км, но час задержки дешевле лишнего инженера
HORIZON = 24 * 60


def drop_penalty(req: Request) -> int:
    """Штраф за невыполнение: срочная заявка (в т.ч. поступившая при перепланировании) — как авария."""
    return DROP_PENALTY[Skill.EMERGENCY] if req.priority is Priority.URGENT else DROP_PENALTY[req.skill]


def plannable_engineers(inst: Instance) -> list[Engineer]:
    """Инженеры, которым можно дать новые заявки: доступны и ещё не отработали смену."""
    return [e for e in inst.active_engineers if inst.start_of(e).time < e.shift_end]


class _Model:
    def __init__(self, inst: Instance, engineers: list, requests: list | None = None) -> None:
        self.inst = inst
        self.engineers = engineers
        self.requests = inst.requests if requests is None else requests
        # Узлы модели: узлы матриц (офис, заявки, точки старта) + END.
        n_matrix = len(next(iter(inst.matrices.values())).dist_km)
        self.end_node = n_matrix
        self.node_to_req = {inst.node_of[r.id]: r for r in self.requests}
        starts = [inst.start_of(e).node for e in engineers]
        self.manager = pywrapcp.RoutingIndexManager(n_matrix + 1, len(engineers), starts, [self.end_node] * len(engineers))
        self.routing = pywrapcp.RoutingModel(self.manager)
        self._build()

    def _service(self, node: int) -> int:
        req = self.node_to_req.get(node)
        return req.duration if req else 0

    def _cost_callback(self, eng: Engineer) -> int:
        """Метры пути; при перепланировании + STABILITY_COST за заявку, которую раньше вёл другой инженер."""
        m = self.inst.matrices[eng.transport]
        manager, end, previous = self.manager, self.end_node, self.inst.previous
        foreign = {self.inst.node_of[rid] for rid, owner in previous.items()
                   if owner != eng.id and rid in self.inst.node_of}

        def cost(i: int, j: int) -> int:
            a, b = manager.IndexToNode(i), manager.IndexToNode(j)
            if b == end or a == end:
                return 0
            return int(round(m.dist_km[a][b] * 1000)) + (STABILITY_COST if b in foreign else 0)

        return self.routing.RegisterTransitCallback(cost)

    def _callbacks(self, transport: Transport) -> tuple[int, int]:
        m = self.inst.matrices[transport]
        manager, end = self.manager, self.end_node

        def dist(i: int, j: int) -> int:
            a, b = manager.IndexToNode(i), manager.IndexToNode(j)
            return 0 if b == end or a == end else int(round(m.dist_km[a][b] * 1000))

        def travel_time(i: int, j: int) -> int:
            a, b = manager.IndexToNode(i), manager.IndexToNode(j)
            if a == end:
                return 0
            return self._service(a) + (0 if b == end else m.time_min[a][b])

        return self.routing.RegisterTransitCallback(dist), self.routing.RegisterTransitCallback(travel_time)

    def _build(self) -> None:
        routing, manager, inst = self.routing, self.manager, self.inst
        callbacks = {t: self._callbacks(t) for t in {e.transport for e in self.engineers}}
        self._callback_refs = callbacks  # колбэки должны жить, пока жива модель

        for v, eng in enumerate(self.engineers):
            cost = self._cost_callback(eng) if inst.previous else callbacks[eng.transport][0]
            routing.SetArcCostEvaluatorOfVehicle(cost, v)
            # Инженер с зафиксированными заявками уже задействован — его новые заявки не добавляют инженеров.
            routing.SetFixedCostOfVehicle(0 if inst.frozen_ids(eng.id) else ENGINEER_FIXED_COST, v)

        routing.AddDimensionWithVehicleTransits(
            [callbacks[e.transport][1] for e in self.engineers], HORIZON, HORIZON, False, "Time"
        )
        time_dim = routing.GetDimensionOrDie("Time")

        for v, eng in enumerate(self.engineers):
            start = inst.start_of(eng)
            time_dim.CumulVar(routing.Start(v)).SetRange(start.time, eng.shift_end)
            time_dim.CumulVar(routing.End(v)).SetRange(start.time, eng.shift_end)
            routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(routing.End(v)))

        for req in self.requests:
            index = manager.NodeToIndex(inst.node_of[req.id])
            time_dim.CumulVar(index).SetRange(req.window_start, req.window_end)
            allowed = [v for v, e in enumerate(self.engineers) if can_serve(e, req)]
            routing.VehicleVar(index).SetValues([-1] + allowed)
            routing.AddDisjunction([index], drop_penalty(req))
            if req.priority is Priority.URGENT:
                time_dim.SetCumulVarSoftUpperBound(index, req.window_start, URGENT_DELAY_COST)

        # Прочие узлы (офис, чужие точки старта, заявки не текущего этапа) не посещаются.
        start_nodes = {inst.start_of(e).node for e in self.engineers}
        for node in range(self.end_node):
            if node not in self.node_to_req and node not in start_nodes:
                index = manager.NodeToIndex(node)
                routing.AddDisjunction([index], 0)
                routing.ActiveVar(index).SetValue(0)

    def initial_routes(self, assignment: dict[str, list[str]]) -> list[list[int]]:
        return [
            [self.manager.NodeToIndex(self.inst.node_of[rid]) for rid in assignment.get(e.id, [])
             if self.inst.node_of[rid] in self.node_to_req]
            for e in self.engineers
        ]

    def solve(self, time_limit: float, initial: dict[str, list[str]] | None) -> dict[str, list[str]] | None:
        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        params.time_limit.FromMilliseconds(int(time_limit * 1000))

        solution = None
        if initial is not None:
            self.routing.CloseModelWithParameters(params)
            start = self.routing.ReadAssignmentFromRoutes(self.initial_routes(initial), True)
            if start is not None:
                solution = self.routing.SolveFromAssignmentWithParameters(start, params)
        if solution is None:
            solution = self.routing.SolveWithParameters(params)
        if solution is None:
            return None

        result: dict[str, list[str]] = {}
        for v, eng in enumerate(self.engineers):
            ids, index = [], solution.Value(self.routing.NextVar(self.routing.Start(v)))
            while not self.routing.IsEnd(index):
                req = self.node_to_req.get(self.manager.IndexToNode(index))
                if req:
                    ids.append(req.id)
                index = solution.Value(self.routing.NextVar(index))
            result[eng.id] = ids
        return result


def _assigned(assignment: dict[str, list[str]]) -> int:
    return sum(len(ids) for ids in assignment.values())


def _drop_penalty(inst: Instance, assignment: dict[str, list[str]]) -> int:
    assigned = {rid for ids in assignment.values() for rid in ids}
    return sum(drop_penalty(r) for r in inst.requests if r.id not in assigned)


def _used(assignment: dict[str, list[str]]) -> int:
    return sum(bool(ids) for ids in assignment.values())


# Поэтапное решение: сначала только аварии и срочные, затем + подключения, затем все заявки; каждый этап
# стартует с решения предыдущего. Иначе локальный поиск застревает в решениях, где авария вытеснена
# несколькими локальными заявками (на Юго-востоке одна авария терялась даже при лимите 60 с).
STAGES = (DROP_PENALTY[Skill.EMERGENCY], DROP_PENALTY[Skill.CONNECTION], 0)  # минимальный штраф заявок этапа
STAGE_SHARE = (0.15, 0.25, 0.6)
MAX_REDUCE_TRIES = 3


def _solve_staged(inst: Instance, engineers: list, time_limit: float,
                  initial: dict[str, list[str]] | None = None) -> dict[str, list[str]] | None:
    result = initial
    for min_penalty, share in zip(STAGES, STAGE_SHARE):
        requests = [r for r in inst.requests if drop_penalty(r) >= min_penalty]
        if not requests:
            continue
        result = _Model(inst, engineers, requests).solve(time_limit * share, result) or result
    return result


def optimize_assignment(inst: Instance, time_limit: float = 15.0, reduce_engineers: bool = True,
                        log=None, initial: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """Назначения оптимизатора (только незафиксированные заявки).

    reduce_engineers — «дожим»: пробуем убрать одного из наименее загруженных инженеров и перерешить;
    принимаем, если набор выполненных заявок не стал хуже (по штрафам) и инженеров стало меньше.
    initial — стартовое решение (при перепланировании — остаток прошлого плана); оно же служит страховкой.
    """
    deadline = time.monotonic() + time_limit
    log = log or (lambda *_: None)
    main_share = 0.6 if reduce_engineers else 1.0
    engineers = plannable_engineers(inst)
    if not engineers or not inst.requests:
        return {}

    best = _solve_staged(inst, engineers, time_limit * main_share, initial)
    for name, fallback in (("базового", baseline_assignment(inst)), ("стартового", initial)):  # страховка
        if fallback is not None and (best is None or _drop_penalty(inst, fallback) < _drop_penalty(inst, best)):
            log(f"оптимизатор хуже {name} решения — берём его как старт")
            best = _Model(inst, engineers).solve(time_limit * 0.2, fallback) or fallback
    log(f"основное решение: {_assigned(best)} заявок, {_used(best)} инженеров")

    banned: set[str] = set()
    failed: set[str] = set()
    while reduce_engineers:
        remaining = deadline - time.monotonic()
        used = {e: ids for e, ids in best.items() if ids and e not in failed and not inst.frozen_ids(e)}
        if remaining < 1 or len(used) <= 1 or len(failed) >= MAX_REDUCE_TRIES:
            break
        victim = min(used, key=lambda e: (len(used[e]), e))
        allowed = [e for e in engineers if e.id not in banned | {victim}]
        start = {e: ids for e, ids in best.items() if e != victim}
        candidate = _Model(inst, allowed).solve(min(remaining, max(2.0, time_limit / 8)), start)
        if (candidate is None or _drop_penalty(inst, candidate) > _drop_penalty(inst, best)
                or _used(candidate) >= _used(best)):
            failed.add(victim)
            log(f"без {victim}: не лучше")
            continue
        banned.add(victim)
        best = candidate
        log(f"без {victim}: {_assigned(best)} заявок, {_used(best)} инженеров")
    return best


def solve_optimized(inst: Instance, time_limit: float = 15.0, reduce_engineers: bool = True, log=None,
                    initial: dict[str, list[str]] | None = None) -> Plan:
    return assemble_plan(inst, "optimized", optimize_assignment(inst, time_limit, reduce_engineers, log, initial))

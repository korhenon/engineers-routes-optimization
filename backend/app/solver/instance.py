"""Экземпляр задачи для всех алгоритмов: заявки, инженеры, точки старта и матрицы по узлам.

Узлы нумеруются как в AreaMatrices: 0 — офис, далее заявки. При перепланировании (replan.py) инженер
стартует из точки последней зафиксированной заявки в момент освобождения, поэтому у каждого свой старт;
срочная заявка добавляет в матрицы новый узел.

Зафиксированные заявки (выполнены/в работе к моменту события) лежат в `fixed_requests` и `frozen`, а не в
`requests`: алгоритмы планируют только `requests`, а маршрут инженера = frozen‑префикс + новый хвост.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..catalogs import Transport
from ..geo.matrix import Matrix, build_area_matrices
from ..models import AreaData, Engineer, Point, Request, Route

OFFICE = 0


@dataclass(frozen=True)
class Start:
    node: int
    time: int  # не раньше этого времени инженер может выехать


@dataclass
class Instance:
    area: str
    requests: list[Request]
    engineers: list[Engineer]
    matrices: dict[Transport, Matrix]
    node_of: dict[str, int]  # id заявки -> индекс узла в матрицах
    starts: dict[str, Start] = field(default_factory=dict)  # id инженера -> старт (по умолчанию офис, начало смены)
    points: list[Point] | None = None  # координаты узлов (нужны, чтобы добавить узел срочной заявки)
    now: int = 0  # момент планирования: раньше него ничего не начинается
    fixed_requests: list[Request] = field(default_factory=list)
    frozen: dict[str, Route] = field(default_factory=dict)  # id инженера -> зафиксированная часть маршрута
    unavailable: set[str] = field(default_factory=set)  # инженеры, которым нельзя давать новые заявки
    previous: dict[str, str] = field(default_factory=dict)  # заявка -> инженер в прошлом плане (стабильность)

    def __post_init__(self) -> None:
        self.request_by_id = {r.id: r for r in self.fixed_requests + self.requests}
        self.engineer_by_id = {e.id: e for e in self.engineers}

    @property
    def all_requests(self) -> list[Request]:
        return self.fixed_requests + self.requests

    @property
    def active_engineers(self) -> list[Engineer]:
        return [e for e in self.engineers if e.id not in self.unavailable]

    def frozen_ids(self, eng_id: str) -> list[str]:
        route = self.frozen.get(eng_id)
        return route.request_ids if route else []

    def start_of(self, eng: Engineer) -> Start:
        return self.starts.get(eng.id) or Start(OFFICE, eng.shift_start)

    def leg(self, eng: Engineer, a: int, b: int) -> tuple[float, int]:
        """(км, мин) между узлами для транспорта инженера."""
        m = self.matrices[eng.transport]
        return m.dist_km[a][b], m.time_min[a][b]


def build_instance(data: AreaData, provider: str | None = None) -> Instance:
    matrices = build_area_matrices(data, provider=provider)
    node_of = {rid: i for i, rid in enumerate(matrices.node_ids) if i != OFFICE}
    return Instance(
        area=data.area,
        requests=list(data.requests),
        engineers=list(data.engineers),
        matrices=matrices.by_transport,
        node_of=node_of,
        points=[data.office.location] + [r.location for r in data.requests],
    )

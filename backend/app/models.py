"""Доменные модели. Время — минуты от 00:00 дня планирования, расстояния — км."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .catalogs import Priority, Skill, Transport


class Point(BaseModel):
    lat: float
    lon: float


class Request(BaseModel):
    id: str
    address: str
    district: str | None = None
    type_bk: str
    type_hd: str | None = None
    window_start: int
    window_end: int
    duration: int
    priority: Priority
    skill: Skill
    required_transport: Transport | None = None
    gigabit: bool = False
    connection: str | None = None
    location: Point | None = None
    # "exact" — дом найден, "street" — найдена улица, "approx" — центроид района/города
    geo_quality: str | None = None


class Engineer(BaseModel):
    id: str
    name: str
    shift_start: int
    shift_end: int
    skills: list[Skill] = Field(min_length=1, max_length=3)
    transport: Transport


class Office(BaseModel):
    address: str
    location: Point | None = None


class AreaData(BaseModel):
    area: str
    name: str
    office: Office
    requests: list[Request]
    engineers: list[Engineer] = []


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_hhmm(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


class Event(BaseModel):
    """Событие для перепланирования (README §6). time — момент события, минуты от 00:00."""
    type: Literal["urgent", "cancel", "unavailable"]
    time: int = Field(ge=0, lt=24 * 60)
    request_id: str | None = None  # cancel: какая заявка; urgent: свой id (иначе U1, U2, …)
    engineer_id: str | None = None  # unavailable
    finish_current: bool = True  # unavailable: текущую работу инженер доделывает (иначе она возвращается в пул)
    # urgent: адрес и/или точка на карте; тип работ как в CSV — от него навык, длительность и транспорт
    address: str | None = None
    location: Point | None = None
    type_bk: str = "Глобальная проблема"
    type_hd: str | None = None
    duration: int | None = Field(None, gt=0)


# --- План (ТЗ §2.4.2). Время — минуты от 00:00, форматирование в ЧЧ:ММ делает API/UI. ---


class Stop(BaseModel):
    request_id: str
    arrival: int  # когда инженер доезжает (может быть раньше окна — тогда ждёт)
    start: int  # начало работы, всегда внутри окна
    end: int
    wait: int = 0
    leg_km: float = 0.0
    leg_min: int = 0


class Route(BaseModel):
    engineer_id: str
    departure: int  # выезд из точки старта
    stops: list[Stop] = []
    km: float = 0.0
    travel_min: int = 0
    frozen: int = 0  # первые `frozen` остановок зафиксированы перепланированием (выполнены или уже в работе)
    explanation: str = ""

    @property
    def request_ids(self) -> list[str]:
        return [s.request_id for s in self.stops]


class Unassigned(BaseModel):
    request_id: str
    reason: str


class Plan(BaseModel):
    id: str
    area: str
    algorithm: str  # "baseline" | "optimized"
    routes: list[Route]
    unassigned: list[Unassigned] = []
    metrics: dict = {}
    violations: list[str] = []
    summary: str = ""
    # Перепланирование: от какого плана, в какой момент и по какому событию построен этот.
    parent_id: str | None = None
    now: int | None = None
    event: dict | None = None
    extra_requests: list[Request] = []  # заявки, которых нет в исходных данных участка (срочные)
    cancelled: list[str] = []
    unavailable: list[str] = []  # инженеры, выбывшие по событиям

"""Матрицы расстояний (км) и времени в пути (мин) между узлами участка, отдельно по типу транспорта.

Провайдеры:
  osrm       — публичные OSRM-серверы routing.openstreetmap.de (car / bike / foot), по дорогам;
  haversine  — прямая × коэффициент извилистости, фиксированные скорости. Запасной вариант без сети.

Допущения (описать в README):
  - у OSRM время без пробок, поэтому время на автомобиле умножается на CAR_TRAFFIC_FACTOR;
  - общественный транспорт: пешеходное расстояние по OSRM, время = dist / PUBLIC_SPEED + PUBLIC_OVERHEAD
    (ожидание и пересадки); свободного API для ОТ нет.
Результаты кэшируются в data/matrix_cache/ по хэшу координат (не в git): промах кэша — запрос к OSRM при загрузке участка.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import dataclass, field

import httpx

from ..catalogs import DATA_DIR, Transport
from ..models import AreaData, Point

CACHE_DIR = DATA_DIR / "matrix_cache"
OFFICE_NODE = "office"

DETOUR_FACTOR = 1.35
SPEED_KMH = {Transport.CAR: 25.0, Transport.FOOT: 4.5, Transport.BIKE: 12.0, Transport.PUBLIC: 20.0}
PUBLIC_SPEED_KMH = 20.0
PUBLIC_OVERHEAD_MIN = 10.0
CAR_TRAFFIC_FACTOR = 1.5
# лимиты публичного OSRM на один /table: sources × destinations ≤ 100² клеток (иначе 400 TooBig),
# URL до ~8 КБ (иначе 414) — около 20 символов на точку плюс списки sources/destinations
MAX_TABLE_CELLS = 100 * 100
MAX_URL_POINTS = 300
RETRY_DELAYS_S = (2, 5)  # паузы перед повторами при 429/5xx/обрыве соединения
USER_AGENT = "LCT2026-hackathon-route-planner/0.1"

OSRM_PROFILES = {
    Transport.CAR: "https://routing.openstreetmap.de/routed-car/table/v1/driving/",
    Transport.BIKE: "https://routing.openstreetmap.de/routed-bike/table/v1/driving/",
    Transport.FOOT: "https://routing.openstreetmap.de/routed-foot/table/v1/driving/",
}


@dataclass
class Matrix:
    dist_km: list[list[float]]
    time_min: list[list[int]]


@dataclass
class AreaMatrices:
    node_ids: list[str]  # node_ids[0] == OFFICE_NODE
    by_transport: dict[Transport, Matrix] = field(default_factory=dict)

    def index(self, node_id: str) -> int:
        return self.node_ids.index(node_id)


def haversine_km(a: Point, b: Point) -> float:
    r = 6371.0
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = p2 - p1, math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _time_from_dist(dist_km: float, transport: Transport, speed_kmh: float) -> int:
    if dist_km == 0:
        return 0
    minutes = dist_km / speed_kmh * 60
    if transport is Transport.PUBLIC:
        minutes += PUBLIC_OVERHEAD_MIN
    return math.ceil(minutes)


def haversine_matrix(points: list[Point], transport: Transport) -> Matrix:
    dist = [[round(haversine_km(a, b) * DETOUR_FACTOR, 3) for b in points] for a in points]
    time = [[_time_from_dist(d, transport, SPEED_KMH[transport]) for d in row] for row in dist]
    return Matrix(dist, time)


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    # обрыв уже установленного соединения; нет сети (ConnectError) и таймаут не повторяем
    return isinstance(exc, (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError))


def _osrm_table(points: list[Point], transport: Transport, client: httpx.Client,
                sources: list[int] | None = None, destinations: list[int] | None = None,
                ) -> tuple[list[list[float]], list[list[float]]]:
    coords = ";".join(f"{p.lon:.6f},{p.lat:.6f}" for p in points)
    params = {"annotations": "duration,distance"}
    if sources is not None:
        params["sources"] = ";".join(map(str, sources))
    if destinations is not None:
        params["destinations"] = ";".join(map(str, destinations))
    for delay in (*RETRY_DELAYS_S, None):
        try:
            resp = client.get(OSRM_PROFILES[transport] + coords, params=params)
            resp.raise_for_status()
            break
        except httpx.HTTPError as exc:
            if delay is None or not _retryable(exc):
                raise
            time.sleep(delay)
    body = resp.json()
    if body.get("code") != "Ok":
        raise RuntimeError(f"OSRM: {body.get('code')} {body.get('message')}")
    return body["distances"], body["durations"]


def _osrm_table_chunked(points: list[Point], transport: Transport, client: httpx.Client,
                        only: int | None = None) -> tuple[list[list[float | None]], list[list[float | None]]]:
    """Таблица n×n запросами в пределах лимитов публичного OSRM (клетки, которые не запрашивались, — None).

    only — нужны только строка и столбец этого узла (новая срочная заявка).
    """
    n = len(points)
    if n * n <= MAX_TABLE_CELLS:
        return _osrm_table(points, transport, client)
    dist: list[list[float | None]] = [[None] * n for _ in range(n)]
    dur: list[list[float | None]] = [[None] * n for _ in range(n)]

    def fill(rows: list[int], cols: list[int]) -> None:
        # точки — объединение строк и столбцов; список, совпадающий с ним целиком, в URL не передаётся
        idx = list(dict.fromkeys(rows + cols if len(rows) >= len(cols) else cols + rows))
        pos = {k: i for i, k in enumerate(idx)}
        d, t = _osrm_table([points[k] for k in idx], transport, client,
                           None if rows == idx else [pos[k] for k in rows],
                           None if cols == idx else [pos[k] for k in cols])
        for a, i in enumerate(rows):
            for b, j in enumerate(cols):
                dist[i][j], dur[i][j] = d[a][b], t[a][b]

    def rect(rows: list[int], cols: list[int]) -> None:
        # столбцы — все сразу, если влезают в URL; строки — сколько позволяет лимит клеток
        if n <= MAX_URL_POINTS:
            col_step = len(cols)
            row_step = MAX_TABLE_CELLS // col_step
        else:
            col_step = min(len(cols), max(MAX_URL_POINTS // 2, MAX_URL_POINTS - len(rows)))
            row_step = min(MAX_TABLE_CELLS // col_step, MAX_URL_POINTS - col_step)
        for c in range(0, len(cols), col_step):
            for r in range(0, len(rows), row_step):
                fill(rows[r:r + row_step], cols[c:c + col_step])

    everything = list(range(n))
    if only is not None:
        rect([only], everything)
        rect(everything, [only])
    else:
        rect(everything, everything)
    return dist, dur


def _short(exc: Exception) -> str:
    """Без URL: в нём все координаты участка."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    lines = str(exc).splitlines()
    return lines[0][:120] if lines and lines[0] else type(exc).__name__


def _osrm_cell(d_m: float | None, t_s: float | None, transport: Transport, fallback: tuple[float, int]) -> tuple[float, int]:
    if d_m is None or t_s is None:  # точка не привязалась к графу дорог
        return fallback
    d_km = round(d_m / 1000, 3)
    if transport is Transport.PUBLIC:
        return d_km, _time_from_dist(d_km, transport, PUBLIC_SPEED_KMH)
    if transport is Transport.CAR:
        return d_km, math.ceil(t_s / 60 * CAR_TRAFFIC_FACTOR)
    return d_km, math.ceil(t_s / 60)


def _osrm_profile(transport: Transport) -> Transport:
    return Transport.FOOT if transport is Transport.PUBLIC else transport


def osrm_matrix(points: list[Point], transport: Transport,
                table: tuple[list[list[float | None]], list[list[float | None]]]) -> Matrix:
    """Матрица транспорта из сырой таблицы OSRM его профиля (_osrm_profile)."""
    distances, durations = table
    fallback = haversine_matrix(points, transport)
    dist: list[list[float]] = []
    time: list[list[int]] = []
    for i in range(len(points)):
        cells = [_osrm_cell(distances[i][j], durations[i][j], transport,
                            (fallback.dist_km[i][j], fallback.time_min[i][j])) for j in range(len(points))]
        dist.append([c[0] for c in cells])
        time.append([c[1] for c in cells])
    return Matrix(dist, time)


def _cache_key(points: list[Point], transport: Transport, provider: str) -> str:
    # все константы, от которых зависят значения ячеек (SPEED_KMH — у ячеек без привязки к дорогам)
    params = [CAR_TRAFFIC_FACTOR, DETOUR_FACTOR, SPEED_KMH[transport], PUBLIC_SPEED_KMH, PUBLIC_OVERHEAD_MIN]
    payload = json.dumps(
        [provider, transport.name, *params, [(round(p.lat, 6), round(p.lon, 6)) for p in points]]
    )
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def _client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60)


def build_matrix(points: list[Point], transport: Transport, provider: str | None = None,
                 client: httpx.Client | None = None, tables: dict | None = None) -> Matrix:
    """tables — общий кэш сырых таблиц по профилю OSRM на один набор точек: PUBLIC и FOOT идут
    в один профиль, и таблица (для больших участков — несколько запросов) скачивается один раз."""
    provider = provider or os.environ.get("ROUTING", "osrm")
    if provider == "haversine":
        return haversine_matrix(points, transport)

    path = CACHE_DIR / f"{_cache_key(points, transport, provider)}.json"
    if path.exists():
        raw = json.loads(path.read_text())
        return Matrix(raw["dist_km"], raw["time_min"])
    tables = {} if tables is None else tables
    profile = _osrm_profile(transport)
    if profile not in tables:
        try:
            if client is None:
                with _client() as own:
                    tables[profile] = _osrm_table_chunked(points, profile, own)
            else:
                tables[profile] = _osrm_table_chunked(points, profile, client)
        except (httpx.HTTPError, RuntimeError) as exc:
            print(f"OSRM недоступен ({_short(exc)}); профиль {profile.name}: используется haversine")
            tables[profile] = None
    if tables[profile] is None:
        return haversine_matrix(points, transport)
    matrix = osrm_matrix(points, transport, tables[profile])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"dist_km": matrix.dist_km, "time_min": matrix.time_min}))
    return matrix


def extend_matrices(matrices: dict[Transport, Matrix], points: list[Point], exact: set[Transport],
                    provider: str | None = None) -> dict[Transport, Matrix]:
    """Матрицы для points = старые узлы + один новый в конце (срочная заявка при перепланировании).

    Старые значения не пересчитываются. По дорогам (OSRM) новый узел считается только для транспорта из
    `exact` — тех, кто может взять заявку; остальным хватает haversine, к этому узлу они не поедут.
    Публичный OSRM отвечает на повторные запросы ~10 с, поэтому запрашиваются только строка и столбец
    нового узла: до 100 узлов — один запрос на профиль, больше — два (1×n и n×1).
    """
    provider = provider or os.environ.get("ROUTING", "osrm")
    n = len(points) - 1
    osrm: dict[Transport, tuple[list, list]] = {}  # профиль -> (distances, durations)
    if provider != "haversine":
        profiles = {_osrm_profile(t) for t in exact if t in matrices}
        try:
            with _client() as client:
                for profile in profiles:
                    osrm[profile] = _osrm_table_chunked(points, profile, client, only=n)
        except (httpx.HTTPError, RuntimeError) as exc:
            print(f"OSRM недоступен ({_short(exc)}); новый узел считается по haversine")

    result = {}
    for transport, matrix in matrices.items():
        fallback = haversine_matrix(points, transport)
        table = osrm.get(_osrm_profile(transport)) if transport in exact else None

        def cell(i: int, j: int) -> tuple[float, int]:
            fb = (fallback.dist_km[i][j], fallback.time_min[i][j])
            return _osrm_cell(table[0][i][j], table[1][i][j], transport, fb) if table else fb

        col = [cell(i, n) for i in range(n)]
        row = [cell(n, j) for j in range(n)] + [(0.0, 0)]
        result[transport] = Matrix(
            [matrix.dist_km[i] + [col[i][0]] for i in range(n)] + [[c[0] for c in row]],
            [matrix.time_min[i] + [col[i][1]] for i in range(n)] + [[c[1] for c in row]],
        )
    return result


def area_nodes(data: AreaData) -> tuple[list[str], list[Point]]:
    missing = [r.id for r in data.requests if r.location is None]
    if data.office.location is None or missing:
        raise ValueError(f"Нет координат у офиса/заявок {missing[:5]} (геокодер не нашёл адрес)")
    return [OFFICE_NODE] + [r.id for r in data.requests], [data.office.location] + [r.location for r in data.requests]


def build_area_matrices(data: AreaData, transports: set[Transport] | None = None,
                        provider: str | None = None) -> AreaMatrices:
    """Матрицы для всех типов транспорта инженеров участка (или для явно заданных)."""
    node_ids, points = area_nodes(data)
    transports = transports or {e.transport for e in data.engineers} or set(Transport)
    result = AreaMatrices(node_ids=node_ids)
    tables: dict = {}
    with _client() as client:
        for t in sorted(transports):
            result.by_transport[t] = build_matrix(points, t, provider, client, tables)
    return result

"""Геометрия маршрутов по дорогам для карты (OSRM route). На расчёт плана не влияет: км и время берутся из матриц.

Кэш — по отрезкам «точка → точка» с профилем (data/route_cache.json, polyline с точностью 1e-5), поэтому
после перепланирования запрашиваются только новые отрезки. Недостающие отрезки одного профиля склеиваются
в одну цепочку точек и запрашиваются одним вызовом: публичный OSRM не любит частые и параллельные запросы.
Общественный транспорт рисуется пешеходным маршрутом (как и в матрице). Если OSRM недоступен или
ROUTING=haversine — отрезок без кэша рисуется прямой.
"""
from __future__ import annotations

import json
import os
import threading

import httpx

from ..catalogs import DATA_DIR, Transport
from ..models import Point

CACHE_PATH = DATA_DIR / "route_cache.json"
PROFILES = {Transport.CAR: "car", Transport.BIKE: "bike", Transport.FOOT: "foot", Transport.PUBLIC: "foot"}
ROUTE_URL = "https://routing.openstreetmap.de/routed-{profile}/route/v1/driving/"
MAX_WAYPOINTS = 100  # точек в одном запросе

LatLon = list[float]
_lock = threading.Lock()


def encode_polyline(coords: list[LatLon], precision: int = 5) -> str:
    factor, out, prev = 10 ** precision, [], (0, 0)
    for lat, lon in coords:
        cur = (round(lat * factor), round(lon * factor))
        for delta in (cur[0] - prev[0], cur[1] - prev[1]):
            v = ~(delta << 1) if delta < 0 else delta << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev = cur
    return "".join(out)


def decode_polyline(text: str, precision: int = 5) -> list[LatLon]:
    factor, coords, i, lat, lon = 10 ** precision, [], 0, 0, 0
    while i < len(text):
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(text[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat, lon = lat + deltas[0], lon + deltas[1]
        coords.append([lat / factor, lon / factor])
    return coords


def leg_key(profile: str, a: Point, b: Point) -> str:
    return f"{profile}:{a.lat:.6f},{a.lon:.6f};{b.lat:.6f},{b.lon:.6f}"


def _load_cache() -> dict[str, str]:
    return json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}


def _join(parts: list[list[LatLon]]) -> list[LatLon]:
    out: list[LatLon] = []
    for part in parts:
        out += part[1:] if out and part and out[-1] == part[0] else part
    return out


def fetch_legs(profile: str, legs: list[tuple[Point, Point]], client: httpx.Client) -> dict[str, list[LatLon]]:
    """Геометрия отрезков одного профиля: отрезки склеиваются в цепочки до MAX_WAYPOINTS точек."""
    result: dict[str, list[LatLon]] = {}
    i = 0
    while i < len(legs):
        waypoints: list[Point] = []
        index: list[tuple[int, str]] = []  # (номер отрезка OSRM, ключ нашего отрезка)
        while i < len(legs) and len(waypoints) + 2 <= MAX_WAYPOINTS:
            a, b = legs[i]
            if not waypoints or waypoints[-1] != a:
                waypoints.append(a)
            index.append((len(waypoints) - 1, leg_key(profile, a, b)))
            waypoints.append(b)
            i += 1
        coords = ";".join(f"{p.lon:.6f},{p.lat:.6f}" for p in waypoints)
        resp = client.get(ROUTE_URL.format(profile=profile) + coords,
                          params={"overview": "false", "steps": "true", "geometries": "polyline"})
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != "Ok":
            raise RuntimeError(f"OSRM: {body.get('code')} {body.get('message')}")
        osrm_legs = body["routes"][0]["legs"]
        for n, key in index:
            result[key] = _join([decode_polyline(s["geometry"]) for s in osrm_legs[n]["steps"]])
    return result


def road_geometry(paths: dict[str, tuple[Transport, list[Point]]], provider: str | None = None,
                  client: httpx.Client | None = None) -> dict:
    """{id: (транспорт, точки по порядку)} → {"routes": {id: [[lat, lon], …]}, "straight_legs": N}."""
    provider = provider or os.environ.get("ROUTING", "osrm")
    with _lock:
        cache = _load_cache()
    missing: dict[str, dict[str, tuple[Point, Point]]] = {}
    for transport, points in paths.values():
        profile = PROFILES[transport]
        for a, b in zip(points, points[1:]):
            key = leg_key(profile, a, b)
            if a != b and key not in cache:
                missing.setdefault(profile, {})[key] = (a, b)

    fetched: dict[str, str] = {}
    if provider != "haversine" and missing:
        own = client is None
        client = client or httpx.Client(headers={"User-Agent": "LCT2026-hackathon-route-planner/0.1"}, timeout=60)
        try:
            for profile, legs in missing.items():
                for key, coords in fetch_legs(profile, list(legs.values()), client).items():
                    fetched[key] = encode_polyline(coords)
        except (httpx.HTTPError, RuntimeError, KeyError, IndexError) as exc:
            print(f"OSRM route недоступен ({exc}); часть отрезков рисуется прямыми")
        finally:
            if own:
                client.close()
    if fetched:
        with _lock:
            cache = {**_load_cache(), **fetched}
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(json.dumps(cache, sort_keys=True, indent=0))

    routes, straight = {}, 0
    for rid, (transport, points) in paths.items():
        profile = PROFILES[transport]
        parts = []
        for a, b in zip(points, points[1:]):
            enc = cache.get(leg_key(profile, a, b))
            if enc is None and a != b:
                straight += 1
            parts.append(decode_polyline(enc) if enc else [[a.lat, a.lon], [b.lat, b.lon]])
        routes[rid] = [[round(lat, 5), round(lon, 5)] for lat, lon in _join(parts)]
    return {"routes": routes, "straight_legs": straight}

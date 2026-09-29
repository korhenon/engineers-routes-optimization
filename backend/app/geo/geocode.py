"""Геокодирование адресов через Nominatim (OSM) с файловым кэшем.

Кэш `data/geocode_cache.json` не хранится в git: адреса, которых в нём нет, геокодируются при загрузке
участка (`locate`), дальше сервис работает офлайн. Ручные правки делаются прямо в кэше:
запись с `"manual": true` никогда не перезаписывается.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

import httpx

from ..catalogs import DATA_DIR
from ..models import AreaData, Point
from .address import ParsedAddress, parse_address

CACHE_PATH = DATA_DIR / "geocode_cache.json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "LCT2026-hackathon-route-planner/0.1"
_LOCK = threading.Lock()  # один геокодер на процесс: лимит Nominatim и запись кэша
MIN_INTERVAL_S = 1.1  # политика Nominatim: не чаще 1 запроса в секунду

# Грубые границы Москвы и юга Подмосковья: отсекаем совпадения в других регионах.
BBOX = (54.5, 36.8, 56.1, 38.6)  # lat_min, lon_min, lat_max, lon_max


@dataclass(frozen=True)
class Query:
    params: dict
    quality: str  # exact | street | approx


def load_cache() -> dict[str, dict]:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict[str, dict]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def _street_variants(p: ParsedAddress) -> list[str]:
    """«Советский 1-й проезд» в OSM обычно записан как «1-й Советский проезд»."""
    variants = [p.street]
    m = re.fullmatch(r"(.+?)\s+(\d+-[йяе])", p.street_name)
    if m:
        name = f"{m.group(2)} {m.group(1)}"
        variants.append(f"{name} {p.street_type}" if p.street_type else name)
    return variants


def _district_name(district: str | None) -> str | None:
    if not district:
        return None
    return re.sub(r"^GPON\s+", "", district).strip() or None


def build_queries(address: str, district: str | None = None) -> Iterator[Query]:
    p = parse_address(address)
    base = {"city": p.city, "country": "Россия"}
    streets = _street_variants(p)
    if p.house:
        base_house = re.match(r"[\d/]+[А-Яа-я]?", p.house)
        houses = [p.house] + ([base_house.group(0)] if base_house and base_house.group(0) != p.house else [])
        for street in streets:
            for house in houses:
                yield Query({**base, "street": f"{house} {street}"}, "exact")
    for street in streets:
        yield Query({**base, "street": street}, "street")
    name = _district_name(district)
    if name and name != p.city:
        yield Query({"q": f"{name}, {p.city}"}, "approx")
        yield Query({"q": f"район {name}, Москва"}, "approx")
    yield Query({**base}, "approx")


def _in_bbox(lat: float, lon: float) -> bool:
    return BBOX[0] <= lat <= BBOX[2] and BBOX[1] <= lon <= BBOX[3]


class Geocoder:
    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30)
        self._last_call = 0.0

    def _search(self, params: dict) -> dict | None:
        wait = MIN_INTERVAL_S - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()
        resp = self.client.get(NOMINATIM_URL, params={**params, "format": "jsonv2", "limit": 3, "countrycodes": "ru"})
        resp.raise_for_status()
        for hit in resp.json():
            lat, lon = float(hit["lat"]), float(hit["lon"])
            if _in_bbox(lat, lon):
                return {"lat": lat, "lon": lon, "display_name": hit.get("display_name")}
        return None

    def geocode(self, address: str, district: str | None = None) -> dict | None:
        for q in build_queries(address, district):
            hit = self._search(q.params)
            if hit:
                return {**hit, "quality": q.quality, "query": q.params}
        return None


def geocode_all(items: list[tuple[str, str | None]], refresh: bool = False, log=print) -> dict[str, dict]:
    """Геокодирует пары (адрес, район), которых ещё нет в кэше. Кэш сохраняется после каждого адреса."""
    cache = load_cache()
    todo = [
        (a, d) for a, d in dict.fromkeys(items)
        if a not in cache or (refresh and not cache[a].get("manual"))
    ]
    if not todo:
        return cache
    geocoder = Geocoder()
    for i, (address, district) in enumerate(todo, 1):
        result = geocoder.geocode(address, district)
        cache[address] = result or {"quality": "failed"}
        log(f"[{i}/{len(todo)}] {cache[address]['quality']:7} {address}")
        save_cache(cache)
    return cache


def locate(data: AreaData, geocode: bool = True) -> None:
    """Координаты офиса и заявок; адреса без записи в кэше сначала геокодируются (httpx.HTTPError без сети)."""
    items = [(data.office.address, None)] + [(r.address, r.district) for r in data.requests]
    cache = load_cache()
    if geocode and any(a not in cache for a, _ in items):
        with _LOCK:
            cache = geocode_all(items)
    attach_locations(data, cache)


def attach_locations(data: AreaData, cache: dict[str, dict] | None = None) -> None:
    """Проставляет координаты из кэша. Адреса без координат остаются с location=None."""
    cache = load_cache() if cache is None else cache

    def point(address: str) -> tuple[Point | None, str | None]:
        hit = cache.get(address)
        if not hit or "lat" not in hit:
            return None, None
        return Point(lat=hit["lat"], lon=hit["lon"]), hit.get("quality")

    data.office.location, _ = point(data.office.address)
    for r in data.requests:
        r.location, r.geo_quality = point(r.address)

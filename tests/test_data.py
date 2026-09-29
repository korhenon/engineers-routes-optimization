import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from backend.app.catalogs import AREAS, Priority, Skill, Transport
from backend.app.data.loader import load_area, parse_requests_csv
from backend.app.data.synth import generate_engineers
from backend.app.geo.address import parse_address
from backend.app.geo.matrix import haversine_matrix
from backend.app.models import Point

EXPECTED_COUNTS = {"vostok": 66, "yugo_vostok": 83, "yugo_centr": 56}


@pytest.mark.parametrize("area", AREAS)
def test_load_area(area):
    data = load_area(area, with_geo=False)
    assert len(data.requests) == EXPECTED_COUNTS[area]
    assert data.office.address
    assert len({r.id for r in data.requests}) == len(data.requests)
    for r in data.requests:
        assert 0 <= r.window_start < r.window_end <= 24 * 60
        assert r.duration > 0
        if r.skill is Skill.EMERGENCY:
            assert r.priority is Priority.URGENT
            assert r.required_transport is Transport.CAR


def test_windows_kept_as_in_file():
    # Большинство аварий с окном на весь день, но часть — в обычном 2-часовом слоте:
    # по ответу организаторов такие планируются в рамках указанного интервала.
    windows = {r.id: (r.window_start, r.window_end) for r in load_area("yugo_vostok", with_geo=False).requests}
    assert windows["78673"] == (1, 1439)
    assert windows["7258"] == (20 * 60, 22 * 60)


def test_parse_utf8_csv_without_office():
    csv = (
        "Заявка;Тип заявки BK;Тип заявки HD;Начало;Окончание;Район;Адрес;Гигабитное подключение\n"
        "1;Локальная заявка;Нет линка;17.08.2026 10:00;17.08.2026 12:00;Кузьминки;Город Москва, ул.Юных Ленинцев, д. 1;Да\n"
    ).encode("utf-8")
    requests, office = parse_requests_csv(csv)
    assert office is None
    assert requests[0].window_start == 600 and requests[0].duration == 45 + 15


@pytest.mark.parametrize(
    "address, city, street, house",
    [
        ("Город Москва, пр-кт.Волгоградский, д. 128 к 5", "Москва", "Волгоградский проспект", "128к5"),
        ("г.Город Москва, наб.Семеновская, д. 3/1к2", "Москва", "Семеновская набережная", "3/1к2"),
        ("МО, г. Кашира Кржижановского ул. д. 7к2", "Кашира", "Кржижановского улица", "7к2"),
        ("Москва Булатниковский пр-зд. д. 6к1", "Москва", "Булатниковский проезд", "6к1"),
        ("обл.Московская область, г.Домодедово, пгт.Востряково-1, ул.Жуковского, д. 14/18",
         "Домодедово", "Жуковского улица", "14/18"),
        ("Город Москва, ул.Международная, д. 28 стр. 1", "Москва", "Международная улица", "28с1"),
        ("г. Москва, ул Бирюлёвская, д 1с1", "Москва", "Бирюлевская улица", "1с1"),
    ],
)
def test_parse_address(address, city, street, house):
    p = parse_address(address)
    assert (p.city, p.street, p.house) == (city, street, house)


@pytest.mark.parametrize("area", AREAS)
def test_engineers_cover_requirements(area):
    engineers = generate_engineers(area)
    assert 10 <= len(engineers) <= 15
    assert {s for e in engineers for s in e.skills} == set(Skill)
    assert any(Skill.EMERGENCY in e.skills and e.transport is Transport.CAR for e in engineers)
    assert max(e.shift_end for e in engineers) >= 22 * 60
    assert generate_engineers(area) == engineers  # детерминированность


def test_all_transport_types_present():
    assert {e.transport for e in generate_engineers("yugo_vostok")} == set(Transport)


def test_haversine_matrix():
    pts = [Point(lat=55.75, lon=37.60), Point(lat=55.76, lon=37.62)]
    car, foot = haversine_matrix(pts, Transport.CAR), haversine_matrix(pts, Transport.FOOT)
    assert car.dist_km[0][0] == 0 and car.time_min[1][1] == 0
    assert car.dist_km[0][1] == car.dist_km[1][0] > 0
    assert foot.time_min[0][1] > car.time_min[0][1]
    assert haversine_matrix(pts, Transport.PUBLIC).time_min[0][1] >= 10


@pytest.mark.parametrize("area", AREAS)
def test_prepared_geo_and_matrices(area):
    """Координаты и матрицы участка (из кэшей data/; без них — геокодер и OSRM)."""
    from backend.app.geo.matrix import build_area_matrices, haversine_km

    data = load_area(area)
    assert data.office.location and all(r.location for r in data.requests)
    assert all(haversine_km(data.office.location, r.location) < 120 for r in data.requests)
    m = build_area_matrices(data)
    assert set(m.by_transport) == set(Transport)
    n = len(data.requests) + 1
    for mx in m.by_transport.values():
        assert len(mx.dist_km) == n and all(len(row) == n for row in mx.time_min)
        assert all(mx.time_min[i][i] == 0 for i in range(n))


def test_matrix_cache_key_depends_on_speeds():
    from backend.app.geo import matrix

    points = [Point(lat=55.75, lon=37.60), Point(lat=55.76, lon=37.61)]
    key = matrix._cache_key(points, Transport.PUBLIC, "osrm")
    with patch.object(matrix, "PUBLIC_OVERHEAD_MIN", 15.0):
        assert matrix._cache_key(points, Transport.PUBLIC, "osrm") != key
    with patch.dict(matrix.SPEED_KMH, {Transport.PUBLIC: 30.0}):
        assert matrix._cache_key(points, Transport.PUBLIC, "osrm") != key


def test_engineers_fall_back_to_template(tmp_path):
    from backend.app.data import synth
    from backend.app.data.loader import load_engineers

    with patch.object(synth, "ENGINEERS_DIR", tmp_path):
        assert load_engineers("vostok") == synth.generate_engineers("vostok")


def test_missing_addresses_are_geocoded_on_load(tmp_path):
    """Без кэша координаты находятся при загрузке участка и дописываются в кэш."""
    from backend.app.geo import geocode

    hit = {"lat": 55.7, "lon": 37.7, "quality": "exact"}
    with patch.object(geocode, "CACHE_PATH", tmp_path / "geocode_cache.json"), \
            patch.object(geocode.Geocoder, "geocode", return_value=hit) as calls:
        data = load_area("yugo_centr")
        assert data.office.location and all(r.location for r in data.requests)
        assert calls.call_count == len({data.office.address, *(r.address for r in data.requests)})
        load_area("yugo_centr")
        assert calls.call_count == len(geocode.load_cache())  # второй раз — только из кэша


@pytest.mark.parametrize("n, only, max_requests", [
    (150, None, 3), (150, 149, 2), (450, None, 21), (450, 7, 4),
])
def test_osrm_table_split_into_blocks(n, only, max_requests):
    """Больше 100² клеток: каждый запрос в пределах лимитов OSRM, нужные клетки заполнены."""
    from backend.app.geo import matrix

    pts = [Point(lat=55.6 + i * 0.001, lon=37.6 + i * 0.0001) for i in range(n)]
    requests = []

    def fake_table(points, transport, client, sources=None, destinations=None):
        rows = sources if sources is not None else range(len(points))
        cols = destinations if destinations is not None else range(len(points))
        requests.append((len(points), len(rows) * len(cols)))
        d = [[matrix.haversine_km(points[i], points[j]) * 1000 for j in cols] for i in rows]
        return d, d

    with patch.object(matrix, "_osrm_table", side_effect=fake_table):
        dist, _ = matrix._osrm_table_chunked(pts, Transport.CAR, client=None, only=only)
    assert len(requests) <= max_requests
    assert all(p <= matrix.MAX_URL_POINTS and c <= matrix.MAX_TABLE_CELLS for p, c in requests)
    cells = [(i, j) for i in range(n) for j in range(n)] if only is None else \
        [(i, only) for i in range(n)] + [(only, j) for j in range(n)]
    for i, j in cells:
        assert dist[i][j] == pytest.approx(matrix.haversine_km(pts[i], pts[j]) * 1000)


def test_osrm_table_retries_rate_limit():
    from backend.app.geo import matrix

    ok = {"code": "Ok", "distances": [[0]], "durations": [[0]]}
    responses = [httpx.Response(429), httpx.Response(200, json=ok)]
    transport = httpx.MockTransport(lambda request: responses.pop(0))
    with httpx.Client(transport=transport) as client, patch.object(matrix.time, "sleep") as sleep:
        assert matrix._osrm_table([Point(lat=55.7, lon=37.6)], Transport.CAR, client) == ([[0]], [[0]])
    assert sleep.call_count == 1 and not responses


@pytest.mark.parametrize("exc", [httpx.ReadError(""), RuntimeError("")])
def test_osrm_error_without_message_falls_back(exc):
    """Пустое сообщение исключения не ломает запасной вариант (haversine)."""
    from backend.app.geo import matrix

    pts = [Point(lat=55.7, lon=37.6), Point(lat=55.75, lon=37.65)]
    with patch.object(matrix, "_osrm_table_chunked", side_effect=exc), \
            patch.object(matrix, "CACHE_DIR", Path("/nonexistent")):
        assert matrix.build_matrix(pts, Transport.CAR, "osrm") == matrix.haversine_matrix(pts, Transport.CAR)


def test_public_and_foot_share_osrm_table():
    from backend.app.geo import matrix

    pts = [Point(lat=55.7, lon=37.6), Point(lat=55.75, lon=37.65)]
    table = ([[0, 5000], [5000, 0]], [[0, 3600], [3600, 0]])
    tables: dict = {}
    with patch.object(matrix, "_osrm_table_chunked", return_value=table) as fetch, \
            patch.object(matrix, "CACHE_DIR", Path(tempfile.mkdtemp())):
        foot = matrix.build_matrix(pts, Transport.FOOT, "osrm", client=object(), tables=tables)
        public = matrix.build_matrix(pts, Transport.PUBLIC, "osrm", client=object(), tables=tables)
    assert fetch.call_count == 1
    assert foot.time_min[0][1] == 60 and public.dist_km[0][1] == 5.0

import json

from pytest import approx

from backend.app.catalogs import Transport
from backend.app.geo import routes
from backend.app.geo.routes import decode_polyline, encode_polyline, fetch_legs, road_geometry
from backend.app.models import Point

A, B, C, D = (Point(lat=55.7 + i / 100, lon=37.6 + i / 100) for i in range(4))


class FakeOSRM:
    """OSRM route: каждый отрезок между соседними точками — два шага с промежуточной точкой."""

    def __init__(self):
        self.calls = []

    def get(self, url, params):
        coords = [tuple(map(float, c.split(","))) for c in url.rsplit("/", 1)[1].split(";")]
        self.calls.append(coords)
        legs = []
        for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
            mid = [(lat1 + lat2) / 2 + 0.001, (lon1 + lon2) / 2]
            legs.append({"steps": [{"geometry": encode_polyline([[lat1, lon1], mid])},
                                   {"geometry": encode_polyline([mid, [lat2, lon2]])}]})
        body = {"code": "Ok", "routes": [{"legs": legs}]}
        return type("Resp", (), {"raise_for_status": lambda self: None, "json": lambda self: body})()


def test_polyline_round_trip():
    coords = [[55.75123, 37.61789], [55.7, 37.5], [-10.00001, 170.3]]
    assert decode_polyline(encode_polyline(coords)) == coords
    assert encode_polyline([[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]]) == "_p~iF~ps|U_ulLnnqC_mqNvxq`@"


def test_legs_are_chained_into_one_request():
    client = FakeOSRM()
    result = fetch_legs("car", [(A, B), (B, C), (D, A)], client)
    assert len(client.calls) == 1 and len(client.calls[0]) == 5  # A B C D A: переход C→D — лишний отрезок
    assert list(result) == [routes.leg_key("car", a, b) for a, b in ((A, B), (B, C), (D, A))]
    assert all(len(line) == 3 for line in result.values())  # стык шагов не дублируется
    assert result[routes.leg_key("car", D, A)][0] == approx([D.lat, D.lon])


def test_road_geometry_caches_legs(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "CACHE_PATH", tmp_path / "route_cache.json")
    paths = {"e1": (Transport.CAR, [A, B, C]), "e2": (Transport.PUBLIC, [A, B])}

    offline = road_geometry(paths, provider="haversine")  # без сети — прямые
    assert offline["straight_legs"] == 3 and offline["routes"]["e1"] == [[round(p.lat, 5), round(p.lon, 5)] for p in (A, B, C)]

    client = FakeOSRM()
    result = road_geometry(paths, provider="osrm", client=client)
    assert result["straight_legs"] == 0 and len(result["routes"]["e1"]) == 5
    assert len(client.calls) == 2  # car и foot (общественный транспорт рисуется пешеходным маршрутом)
    assert len(json.loads(routes.CACHE_PATH.read_text())) == 3

    again = road_geometry(paths, provider="osrm", client=FakeOSRM())
    assert again == result and road_geometry(paths, provider="haversine") == result  # из кэша, без запросов

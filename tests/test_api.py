import json

from fastapi.testclient import TestClient

from backend.app.catalogs import AREAS, DATASET_DIR
from backend.app.data.synth import engineers_path
from backend.app.main import app

client = TestClient(app)


def test_areas():
    areas = client.get("/api/areas").json()
    assert [a["id"] for a in areas] == list(AREAS)
    assert all(a["requests"] > 0 and 10 <= a["engineers"] <= 15 for a in areas)


def test_area_data():
    data = client.get("/api/areas/yugo_centr/data").json()
    assert data["office"]["location"] and data["engineers"]
    assert all(r["location"] for r in data["requests"])
    assert data["catalogs"]["skills"]["3"] == "Аварийные работы"
    assert "Глобальная проблема" in data["catalogs"]["types_bk"]
    assert client.get("/api/areas/nowhere/data").status_code == 404


def test_plan_and_compare():
    base = client.post("/api/plan", json={"area": "yugo_centr", "algorithm": "baseline"}).json()
    opt = client.post("/api/plan", json={"area": "yugo_centr", "algorithm": "optimized", "time_limit": 2}).json()
    assert base["violations"] == [] and opt["violations"] == []
    assert opt["metrics"]["assigned"] > base["metrics"]["assigned"]
    assert client.get(f"/api/plan/{opt['id']}").json()["id"] == opt["id"]

    diff = client.get(f"/api/plan/{base['id']}/compare/{opt['id']}").json()
    assert diff["assigned"]["delta"] == opt["metrics"]["assigned"] - base["metrics"]["assigned"]
    assert client.get("/api/plan/missing").status_code == 404
    assert client.post("/api/plan", json={"area": "yugo_centr", "algorithm": "magic"}).status_code == 422


def test_frontend_served():
    page = client.get("/")
    assert page.status_code == 200 and "leaflet" in page.text
    assert client.get("/app.js").status_code == 200


def test_replan_and_explain():
    opt = client.post("/api/plan", json={"area": "yugo_centr", "time_limit": 2}).json()
    rid = next(r for r in opt["routes"] if r["stops"])["stops"][-1]["request_id"]
    lines = client.get(f"/api/plan/{opt['id']}/explain/{rid}").json()["lines"]
    assert lines[0].startswith("Назначена:") and len(lines) == 3
    assert client.get(f"/api/plan/{opt['id']}/explain/nope").status_code == 404

    body = {"plan_id": opt["id"], "time_limit": 1, "event": {"type": "cancel", "time": 9 * 60, "request_id": rid}}
    result = client.post("/api/replan", json=body).json()
    assert result["plan"]["parent_id"] == opt["id"] and result["plan"]["violations"] == []
    assert result["diff"]["cancelled"] == [rid]
    assert client.get(f"/api/plan/{opt['id']}/diff/{result['plan']['id']}").json()["cancelled"] == [rid]

    body["event"]["time"] = 8 * 60  # раньше предыдущего перепланирования
    body["plan_id"] = result["plan"]["id"]
    assert client.post("/api/replan", json=body).status_code == 400
    body["event"] = {"type": "sleep", "time": 600}
    assert client.post("/api/replan", json=body).status_code == 422


def test_geometry(monkeypatch):
    monkeypatch.setenv("ROUTING", "haversine")  # без сети: отрезки не из кэша — прямые
    opt = client.post("/api/plan", json={"area": "yugo_centr", "time_limit": 1}).json()
    geo = client.get(f"/api/plan/{opt['id']}/geometry").json()
    used = {r["engineer_id"] for r in opt["routes"] if r["stops"]}
    assert set(geo["routes"]) == used and all(len(line) >= 2 for line in geo["routes"].values())


def test_upload():
    csv = (DATASET_DIR / AREAS["yugo_centr"][1]).read_bytes()  # адреса уже в кэше геокодера
    engineers = json.loads(engineers_path("yugo_centr").read_text(encoding="utf-8"))
    engineers[0]["shift_start"] = "09:00"
    files = {"requests": ("z.csv", csv, "text/csv"),
             "engineers": ("e.json", json.dumps(engineers).encode(), "application/json")}
    res = client.post("/api/upload", files=files, data={"name": "Мой участок"}).json()
    assert res["id"].startswith("upload-") and res["requests"] == 56 and res["warnings"] == []
    assert any(a["id"] == res["id"] and a["uploaded"] for a in client.get("/api/areas").json())
    data = client.get(f"/api/areas/{res['id']}/data").json()
    assert data["name"] == "Мой участок" and data["engineers"][0]["shift_start"] == 540
    plan = client.post("/api/plan", json={"area": res["id"], "algorithm": "baseline"}).json()
    assert plan["violations"] == [] and plan["metrics"]["total"] == 56

    only_engineers = client.post("/api/upload", files={"engineers": files["engineers"]}, data={"base_area": "yugo_centr"})
    assert only_engineers.status_code == 200 and only_engineers.json()["requests"] == 56
    assert client.post("/api/upload", data={"name": "пусто"}).status_code == 400
    bad = client.post("/api/upload", files={"requests": ("x.csv", b"a;b\n1;2\n", "text/csv")})
    assert bad.status_code == 400 and "колонки" in bad.json()["detail"]
    bad = client.post("/api/upload", files={"engineers": ("e.json", b"{}", "application/json")},
                      data={"base_area": "yugo_centr"})
    assert bad.status_code == 400 and "инженеров" in bad.json()["detail"]

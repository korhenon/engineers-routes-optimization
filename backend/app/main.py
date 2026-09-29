"""FastAPI: API диспетчера + раздача статической страницы из frontend/ (README §11).

    uvicorn backend.app.main:app --reload   →   http://localhost:8000

Время в ответах — минуты от 00:00 (как в моделях), в ЧЧ:ММ форматирует страница.
Планы хранятся в памяти процесса по plan.id вместе со своим Instance: после перепланирования у плана
свои старты инженеров, зафиксированные заявки и, возможно, новые узлы — объяснения и следующие события
считаются по нему.
Загруженные участки (/api/upload) тоже живут в памяти процесса.
Кэши в data/ не хранятся в git: при старте участки датасета прогреваются в фоне (геокодирование, матрицы OSRM),
первый запрос к ещё не готовому участку ждёт того же расчёта, а не запускает второй.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from functools import wraps
from typing import Literal

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .catalogs import AREAS, BK_TO_SKILL, ROOT, SKILL_LABELS, TRANSPORT_LABELS, Priority
from .data.loader import load_area
from .data.upload import UploadError, build_uploaded_area, upload_id
from .geo.routes import road_geometry
from .models import AreaData, Event, Plan
from .solver.baseline import solve_baseline
from .solver.instance import OFFICE, Instance, build_instance
from .solver.explain import assignment_explanation
from .solver.metrics import compare, plan_diff
from .solver.optimizer import solve_optimized
from .solver.replan import ReplanError, replan

PLANS: dict[str, Plan] = {}
INSTANCES: dict[str, Instance] = {}  # plan.id -> Instance, по которому план построен
UPLOADS: dict[str, AreaData] = {}  # загруженные участки по id


def _once_per_key(fn: Callable[[str], object]):
    """Кэш по ключу, в котором значение считается один раз, даже если его просят параллельно (прогрев + запрос)."""
    values: dict[str, object] = {}
    locks: dict[str, threading.Lock] = {}
    guard = threading.Lock()

    @wraps(fn)
    def wrapper(key: str):
        if key not in values:
            with guard:
                lock = locks.setdefault(key, threading.Lock())
            with lock:
                if key not in values:
                    values[key] = fn(key)
        return values[key]

    return wrapper


def area_data(area: str) -> AreaData:
    return UPLOADS[area] if area in UPLOADS else _dataset_area(area)


@_once_per_key
def _dataset_area(area: str) -> AreaData:
    return load_area(area)


@_once_per_key
def _dataset_summary(area: str) -> AreaData:
    """Заявки и инженеры без координат: списку участков не нужно ждать геокодирования."""
    return load_area(area, with_geo=False)


@_once_per_key
def area_instance(area: str) -> Instance:
    return build_instance(area_data(area))


def _warm_up() -> None:
    for area in AREAS:
        try:
            area_instance(area)
        except Exception as exc:  # без сети: участок попробует снова первый запрос к нему
            print(f"Прогрев участка {area} не удался: {exc}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    threading.Thread(target=_warm_up, name="warm-up", daemon=True).start()
    yield


app = FastAPI(title="Помощник диспетчера выездных инженеров", lifespan=lifespan)


@app.exception_handler(httpx.HTTPError)
def _network_error(_: Request, exc: httpx.HTTPError) -> JSONResponse:
    return JSONResponse({"detail": f"Нет координат для участка, а геокодер недоступен ({exc})"}, status_code=503)


def _check_area(area: str) -> None:
    if area not in AREAS and area not in UPLOADS:
        raise HTTPException(404, f"Неизвестный участок {area!r}; есть: {', '.join([*AREAS, *UPLOADS])}")


def _plan(plan_id: str) -> Plan:
    if plan_id not in PLANS:
        raise HTTPException(404, f"План {plan_id} не найден (планы живут до перезапуска сервера)")
    return PLANS[plan_id]


@app.get("/api/areas")
def areas() -> list[dict]:
    result = []
    for key in [*AREAS, *UPLOADS]:
        data = UPLOADS[key] if key in UPLOADS else _dataset_summary(key)
        result.append({"id": key, "name": data.name, "requests": len(data.requests), "engineers": len(data.engineers),
                       "uploaded": key in UPLOADS})
    return result


@app.post("/api/upload")
def upload(requests: UploadFile | None = File(None), engineers: UploadFile | None = File(None),
           base_area: str | None = Form(None), name: str | None = Form(None),
           office_address: str | None = Form(None)) -> dict:
    """Свой участок: CSV заявок и/или JSON инженеров (недостающее — из base_area). Новые адреса геокодируются."""
    if base_area:
        _check_area(base_area)
    csv_raw = requests.file.read() if requests and requests.filename else None
    eng_raw = engineers.file.read() if engineers and engineers.filename else None
    area_id = upload_id(csv_raw, eng_raw, base_area, name, office_address)
    try:
        data, warnings = build_uploaded_area(area_id, name, csv_raw, eng_raw,
                                             area_data(base_area) if base_area else None, office_address)
        UPLOADS[area_id] = data
        area_instance(area_id)  # матрицы считаются сразу: ошибки видны при загрузке, а не при планировании
    except UploadError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"id": area_id, "name": data.name, "requests": len(data.requests), "engineers": len(data.engineers),
            "warnings": warnings}



@app.get("/api/areas/{area}/data")
def area(area: str) -> dict:
    _check_area(area)
    data = area_data(area)
    return {
        **data.model_dump(),
        "catalogs": {
            "skills": {int(k): v for k, v in SKILL_LABELS.items()},
            "transports": {int(k): v for k, v in TRANSPORT_LABELS.items()},
            "priorities": {int(p): p.label for p in Priority},
            "types_bk": list(BK_TO_SKILL),  # для формы срочной заявки
        },
    }


class PlanRequest(BaseModel):
    area: str
    algorithm: Literal["optimized", "baseline"] = "optimized"
    time_limit: float = Field(30.0, ge=1, le=120)  # как в поле «Лимит, с» на странице


@app.post("/api/plan")
def make_plan(body: PlanRequest) -> Plan:
    # Обычный def: FastAPI выполняет его в пуле потоков, солвер не блокирует остальные запросы.
    _check_area(body.area)
    inst = area_instance(body.area)
    plan = solve_baseline(inst) if body.algorithm == "baseline" else solve_optimized(inst, body.time_limit)
    PLANS[plan.id], INSTANCES[plan.id] = plan, inst
    return plan


class ReplanRequest(BaseModel):
    plan_id: str
    event: Event
    time_limit: float = Field(20.0, ge=1, le=120)  # страница передаёт min(лимит, 20)


@app.post("/api/replan")
def make_replan(body: ReplanRequest) -> dict:
    """Новый план после события и что изменилось относительно исходного."""
    old = _plan(body.plan_id)
    try:
        plan, inst = replan(INSTANCES[old.id], old, body.event, body.time_limit)
    except ReplanError as exc:
        raise HTTPException(400, str(exc)) from None
    PLANS[plan.id], INSTANCES[plan.id] = plan, inst
    return {"plan": plan, "diff": plan_diff(old, plan)}


@app.get("/api/plan/{plan_id}")
def get_plan(plan_id: str) -> Plan:
    return _plan(plan_id)


@app.get("/api/plan/{plan_id}/compare/{other_id}")
def compare_plans(plan_id: str, other_id: str) -> dict:
    """Метрики other относительно plan_id (обычно plan_id — базовый)."""
    base, other = _plan(plan_id), _plan(other_id)
    if base.area != other.area:
        raise HTTPException(400, "Планы относятся к разным участкам")
    return compare(base.metrics, other.metrics)


@app.get("/api/plan/{plan_id}/explain/{request_id}")
def explain(plan_id: str, request_id: str) -> dict:
    plan, inst = _plan(plan_id), INSTANCES[plan_id]
    if request_id not in inst.request_by_id:
        raise HTTPException(404, f"В плане {plan_id} нет заявки {request_id}")
    return {"request_id": request_id, "lines": assignment_explanation(inst, plan, request_id)}


@app.get("/api/plan/{plan_id}/geometry")
def geometry(plan_id: str) -> dict:
    """Линии маршрутов по дорогам для карты: {"routes": {engineer_id: [[lat, lon], …]}, "straight_legs": N}."""
    plan, inst = _plan(plan_id), INSTANCES[plan_id]
    if inst.points is None:
        raise HTTPException(400, "У плана нет координат узлов")
    paths = {}
    for route in plan.routes:
        if route.stops:
            eng = inst.engineer_by_id[route.engineer_id]
            paths[eng.id] = (eng.transport, [inst.points[OFFICE]] + [inst.points[inst.node_of[s.request_id]]
                                                                   for s in route.stops])
    return road_geometry(paths)


@app.get("/api/plan/{plan_id}/diff/{other_id}")
def diff_plans(plan_id: str, other_id: str) -> dict:
    """Что изменилось в other_id относительно plan_id (заявки, инженеры, метрики)."""
    old, new = _plan(plan_id), _plan(other_id)
    if old.area != new.area:
        raise HTTPException(400, "Планы относятся к разным участкам")
    return plan_diff(old, new)


class FrontendFiles(StaticFiles):
    """Статика страницы без эвристического кэша браузера: иначе после правок фронтенда подхватывается
    новый index.html со старыми app.js/style.css. no-cache — проверка по ETag, неизменённое отдаётся как 304."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", FrontendFiles(directory=ROOT / "frontend", html=True), name="frontend")

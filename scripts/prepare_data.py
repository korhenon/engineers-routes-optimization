"""Необязательный прогрев кэшей и перегенерация инженеров.

    python -m scripts.prepare_data                 # все шаги
    python -m scripts.prepare_data --steps geo     # только геокодирование
    python -m scripts.prepare_data --steps engineers matrix --routing haversine

Сервис сам геокодирует адреса и считает матрицы при загрузке участка (кэши в data/, не в git) —
скрипт нужен, чтобы сделать это заранее, перегеокодировать (--refresh-geo) или пересоздать
справочник инженеров dataset/engineers/<участок>.json (--steps engineers --seed N).
"""
from __future__ import annotations

import argparse

from backend.app.catalogs import AREAS
from backend.app.data.loader import load_area

STEPS = ("geo", "engineers", "matrix")


def step_geo(refresh: bool) -> None:
    from backend.app.geo.geocode import geocode_all

    items = []
    for area in AREAS:
        data = load_area(area, with_geo=False)
        items.append((data.office.address, None))
        items += [(r.address, r.district) for r in data.requests]
    cache = geocode_all(items, refresh=refresh)
    stats: dict[str, int] = {}
    for address, _ in dict.fromkeys(items):
        q = cache.get(address, {}).get("quality", "missing")
        stats[q] = stats.get(q, 0) + 1
    print("Итог геокодирования:", stats)


def step_engineers(seed: int) -> None:
    from backend.app.data.synth import write_engineers

    for area in AREAS:
        engineers = write_engineers(area, seed=seed)
        print(f"{area}: {len(engineers)} инженеров")


def step_matrix(routing: str) -> None:
    from backend.app.geo.matrix import build_area_matrices

    for area in AREAS:
        m = build_area_matrices(load_area(area), provider=routing)
        print(f"{area}: {len(m.node_ids)} узлов, профили {sorted(t.name for t in m.by_transport)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--steps", nargs="+", choices=STEPS, default=list(STEPS))
    parser.add_argument("--refresh-geo", action="store_true", help="перегеокодировать (кроме ручных правок)")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--routing", choices=("osrm", "haversine"), default="osrm")
    args = parser.parse_args()

    if "geo" in args.steps:
        step_geo(args.refresh_geo)
    if "engineers" in args.steps:
        step_engineers(args.seed)
    if "matrix" in args.steps:
        step_matrix(args.routing)


if __name__ == "__main__":
    main()

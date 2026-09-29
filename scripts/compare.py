"""Базовый vs оптимизированный план по участкам (контрольная точка дня 1).

    python -m scripts.compare                    # все участки, лимит 15 с
    python -m scripts.compare yugo_centr --time-limit 30 -v
"""
from __future__ import annotations

import argparse

from backend.app.catalogs import AREAS
from backend.app.data.loader import load_area
from backend.app.solver.baseline import solve_baseline
from backend.app.solver.instance import build_instance
from backend.app.solver.metrics import compare
from backend.app.solver.optimizer import solve_optimized


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("areas", nargs="*", choices=list(AREAS), help="по умолчанию — все участки")
    parser.add_argument("--time-limit", type=float, default=15.0)
    parser.add_argument("-v", "--verbose", action="store_true", help="показать причины неназначения")
    args = parser.parse_args()

    for area in args.areas or AREAS:
        inst = build_instance(load_area(area))
        base = solve_baseline(inst)
        opt = solve_optimized(inst, args.time_limit)
        print(f"\n== {AREAS[area][0]}: {len(inst.requests)} заявок, {len(inst.engineers)} инженеров ==")
        print(f"{'метрика':<16}{'базовый':>10}{'оптим.':>10}{'Δ':>10}")
        for key, row in compare(base.metrics, opt.metrics).items():
            print(f"{key:<16}{row['base']:>10}{row['other']:>10}{row['delta']:>10}")
        for name, plan in (("базовый", base), ("оптим.", opt)):
            print(f"{name}: нарушений {len(plan.violations)}; по навыкам {plan.metrics['by_skill']}")
            for v in plan.violations:
                print("  !", v)
        if args.verbose:
            for u in opt.unassigned:
                print(f"  не назначена {u.request_id}: {u.reason}")


if __name__ == "__main__":
    main()

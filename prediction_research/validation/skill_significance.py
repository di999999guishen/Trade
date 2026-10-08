"""Block-bootstrap the Brier skill for the ONE positive-skill combo (commodity h20).

Why blocking: each h20 sample spans 20 trading days, so consecutive samples overlap heavily
and are NOT independent. A naive bootstrap would massively overstate significance.
We resample whole feature_date blocks instead.
"""
from __future__ import annotations

import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # .../prediction_research/validation/<file> -> TradingAgents
sys.path.insert(0, str(REPO))
from prediction_research.config import load_config  # noqa: E402
from prediction_research.data import load_universe  # noqa: E402
from prediction_research.features import build_samples  # noqa: E402
from prediction_research.evaluation import walk_forward  # noqa: E402

cfg = load_config()
model_cfg = cfg["model"]


def run_block(universe: str, horizon: int):
    _, snaps = load_universe(cfg, universe)
    samples = []
    for snap in snaps.values():
        samples.extend(build_samples(snap, horizon))
    results, folds = walk_forward(samples, model_cfg)
    # grouped by feature_date -> these are the resampling blocks
    blocks = defaultdict(list)
    for r in results:
        blocks[r.feature_date].append(r)
    return results, blocks, folds


def brier_skill(rows):
    if not rows:
        return 0.0
    brier = sum((r.probability_up - r.actual_up) ** 2 for r in rows) / len(rows)
    base = sum((r.baseline_probability - r.actual_up) ** 2 for r in rows) / len(rows)
    return base - brier


def main(argv: list[str] | None = None):
    import argparse
    ap = argparse.ArgumentParser(description="Block-bootstrap Brier skill (overlap-aware).")
    ap.add_argument("--out", default="brier_skill_bootstrap.json", help="output json path")
    args = ap.parse_args(argv)
    random.seed(20261005)
    out = {}
    for universe, horizon in [("commodity", 20), ("cached", 20), ("commodity", 5)]:
        results, blocks, folds = run_block(universe, horizon)
        keys = list(blocks.keys())
        point = brier_skill(results)
        n_blocks = len(keys)

        boot = []
        for _ in range(400):
            picked = [random.choice(keys) for _ in range(n_blocks)]
            rows = [r for k in picked for r in blocks[k]]
            boot.append(brier_skill(rows))
        lo, hi = sorted(boot)[int(0.025 * len(boot))], sorted(boot)[int(0.975 * len(boot))]
        frac_neg = sum(1 for b in boot if b <= 0) / len(boot)
        out[f"{universe}_h{horizon}"] = {
            "rows": len(results), "blocks(n_feature_dates)": n_blocks, "folds": len(folds),
            "point_skill": round(point, 6),
            "bootstrap_ci95": [round(lo, 6), round(hi, 6)],
            "mean_skill": round(statistics.fmean(boot), 6),
            "P(skill<=0)": round(frac_neg, 4),
        }
        print(f"{universe}_h{horizon}: skill={point:+.6f}  CI95=[{lo:+.6f},{hi:+.6f}]  "
              f"P(skill<=0)={frac_neg:.3f}  blocks={n_blocks} rows={len(results)}", flush=True)

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written -> {args.out}")


if __name__ == "__main__":
    main()

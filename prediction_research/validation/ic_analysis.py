"""信息系数（IC）与分位数组收益分析。

为什么补这个
------------
evaluation.metrics 目前只有 Brier / log_loss / accuracy / decisive_*，
全都是**分类/校准**口径。它们回答的是"概率给得准不准"，
回答不了"按这个概率排序能不能赚到钱"。

能把预测能力翻译成收益的是信息系数：
    IC_t = corr(probability_up, actual_return)  在第 t 个 feature_date 的横截面上
    ICIR = mean(IC) / std(IC)                   稳定性

同时做概率分 5 组的收益单调性检验：若模型有选股价值，
Q5（最高概率组）平均收益应显著高于 Q1。
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # .../prediction_research/validation/<file> -> TradingAgents
sys.path.insert(0, str(REPO))


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def spearman(xs: list[float], ys: list[float]) -> float | None:
    return pearson(rank(xs), rank(ys))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="commodity")
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--data-dir", default=None,
                    help="覆盖 market_data_dir；用于读 datasets/market_full 全历史缓存")
    ap.add_argument("--out", default="ic_analysis.json")
    args = ap.parse_args()

    from prediction_research.config import load_config
    from prediction_research.data import configured_data_dirs, find_cache
    from prediction_research.evaluation import (
        walk_forward, signal_verdict, ICIR_GATE,
    )
    from prediction_research.features import build_samples

    cfg = load_config()
    if args.data_dir:
        cfg["market_data_dir"] = args.data_dir
    symbols = [str(i["symbol"]) for i in cfg["universes"][args.universe] if i.get("symbol")]
    dirs = configured_data_dirs(cfg)

    snaps = {}
    for sym in symbols:
        try:
            snaps[sym] = find_cache(sym, dirs)
        except (FileNotFoundError, ValueError):
            pass

    samples = []
    for sym, snap in snaps.items():
        samples.extend(build_samples(snap, args.horizon))
    predictions, _ = walk_forward(samples, cfg["model"])

    # 按 feature_date 分组做横截面 IC
    by_date: dict[str, list] = {}
    for p in predictions:
        key = p.feature_date.isoformat() if hasattr(p.feature_date, "isoformat") else str(p.feature_date)
        by_date.setdefault(key, []).append((p.probability_up, p.actual_return, p.symbol))

    ics: list[float] = []
    rank_ics: list[float] = []
    skipped = 0
    for _day, rows in sorted(by_date.items()):
        if len(rows) < 3:
            skipped += 1
            continue
        probs = [r[0] for r in rows]
        rets = [r[1] for r in rows]
        ic = pearson(probs, rets)
        ric = spearman(probs, rets)
        if ic is not None:
            ics.append(ic)
        if ric is not None:
            rank_ics.append(ric)

    def summarize(vals: list[float], name: str) -> dict:
        if not vals:
            return {"metric": name, "n": 0}
        mean = statistics.fmean(vals)
        sd = statistics.pstdev(vals)
        # t 统计量（考虑 overlaps 会被高估，仅作参考）
        tstat = mean / (sd / math.sqrt(len(vals))) if sd > 0 else 0.0
        return {
            "metric": name,
            "n_dates": len(vals),
            "mean": round(mean, 6),
            "std": round(sd, 6),
            "icir_naive": round(mean / sd, 4) if sd > 0 else None,
            "t_stat_naive": round(tstat, 3),
            "positive_ratio": round(sum(1 for v in vals if v > 0) / len(vals), 4),
        }

    # 概率分 5 组
    allrows = [(p.probability_up, p.actual_return) for p in predictions]
    allrows.sort(key=lambda r: r[0])
    n = len(allrows)
    quantiles = {}
    for q in range(5):
        lo, hi = int(n * q / 5), int(n * (q + 1) / 5)
        bucket = allrows[lo:hi]
        if bucket:
            quantiles[f"Q{q+1}"] = {
                "n": len(bucket),
                "prob_range": [round(bucket[0][0], 4), round(bucket[-1][0], 4)],
                "mean_return": round(statistics.fmean(r[1] for r in bucket), 6),
                "win_rate": round(sum(1 for r in bucket if r[1] > 0) / len(bucket), 4),
            }

    monotonic = None
    if len(quantiles) == 5:
        mr = [quantiles[f"Q{i}"]["mean_return"] for i in range(1, 6)]
        monotonic = bool(mr[4] > mr[0])

    result = {
        "universe": args.universe,
        "horizon": args.horizon,
        "predictions": len(predictions),
        "cross_section_dates": len(by_date),
        "dates_skipped_lt3": skipped,
        "ic": summarize(ics, "pearson_IC"),
        "rank_ic": summarize(rank_ics, "spearman_rank_IC"),
        "quantiles": quantiles,
        "q5_gt_q1": monotonic,
        "spread_q5_minus_q1": (
            round(quantiles["Q5"]["mean_return"] - quantiles["Q1"]["mean_return"], 6)
            if len(quantiles) == 5 else None
        ),
    }

    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

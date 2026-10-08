from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from datetime import date

from .features import Sample
from .model import LogisticModel, _sigmoid


@dataclass(frozen=True)
class PredictionResult:
    symbol: str
    feature_date: date
    target_end_date: date
    probability_up: float
    actual_up: int
    actual_return: float
    calibrated: bool
    baseline_probability: float
    momentum_prediction: int
    raw_probability_up: float | None = None
    discrimination_status: str = "legacy_unrecorded"


def _fit_model(training: list[Sample], model_cfg: dict) -> LogisticModel:
    training = sorted(training, key=lambda s: (s.feature_date, s.symbol))
    dates = sorted({sample.feature_date for sample in training})
    if not dates:
        raise ValueError("no labeled training dates")
    calibration_dates = max(1, int(len(dates) * float(model_cfg["calibration_fraction"])))
    cutoff = dates[-calibration_dates]
    core = [sample for sample in training if sample.feature_date < cutoff
            and sample.target_end_date and sample.target_end_date < cutoff]
    calibration = [sample for sample in training if sample.feature_date >= cutoff]
    purged = sum(sample.feature_date < cutoff and sample.target_end_date >= cutoff
                 for sample in training if sample.target_end_date)
    fallback = False
    if len(core) < 40 or len({sample.target_up for sample in core}) < 2:
        core, calibration = training, []
        fallback = True
    cap = int(model_cfg.get("max_fit_rows", 2500))
    if len(core) > cap:
        step = len(core) / cap
        core = [core[int(index * step)] for index in range(cap)]
    model = LogisticModel.fit(
        core,
        learning_rate=float(model_cfg["learning_rate"]),
        iterations=int(model_cfg["iterations"]),
        l2=float(model_cfg["l2"]),
    )
    if len(calibration) >= int(model_cfg["min_calibration_rows"]):
        model.calibrate(calibration)
    else:
        model.calibration_details = {"status": "insufficient_calibration_rows", "input_rows": len(calibration)}
    model.training_details = {
        "split_policy": "date_grouped_purged_v1", "calibration_start": cutoff.isoformat(),
        "input_rows": len(training), "core_rows": len(core), "calibration_rows": len(calibration),
        "purged_rows": 0 if fallback else purged, "uncalibrated_fallback": fallback,
        "core_latest_target": max(s.target_end_date for s in core).isoformat(),
    }
    return model


def walk_forward(samples: list[Sample], model_cfg: dict) -> tuple[list[PredictionResult], list[dict]]:
    samples = [sample for sample in samples if sample.target_end_date and sample.target_up is not None]
    dates = sorted({sample.feature_date for sample in samples})
    min_dates = int(model_cfg["min_train_dates"])
    max_dates = int(model_cfg["max_train_dates"])
    window = int(model_cfg["test_window_dates"])
    results: list[PredictionResult] = []
    folds: list[dict] = []
    for start in range(min_dates, len(dates), window):
        test_dates = dates[start : start + window]
        if not test_dates:
            break
        test_start = test_dates[0]
        eligible = [sample for sample in samples if sample.target_end_date < test_start]
        eligible_dates = sorted({sample.feature_date for sample in eligible})[-max_dates:]
        eligible_set = set(eligible_dates)
        training = [sample for sample in eligible if sample.feature_date in eligible_set]
        testing = [sample for sample in samples if sample.feature_date in set(test_dates)]
        if len(training) < 80 or len({sample.target_up for sample in training}) < 2:
            continue
        model = _fit_model(training, model_cfg)
        base_rate = sum(int(sample.target_up) for sample in training) / len(training)
        for sample in testing:
            results.append(PredictionResult(
                symbol=sample.symbol,
                feature_date=sample.feature_date,
                target_end_date=sample.target_end_date,
                probability_up=model.predict_proba(sample.features),
                actual_up=int(sample.target_up),
                actual_return=float(sample.target_return),
                calibrated=model.calibrated,
                baseline_probability=base_rate,
                momentum_prediction=int(sample.features[1] > 0),
                raw_probability_up=_sigmoid(model.raw_score(sample.features)),
                discrimination_status=model.discrimination_status,
            ))
        folds.append({
            "train_rows": len(training),
            "train_start": min(sample.feature_date for sample in training).isoformat(),
            "latest_observed_target": max(sample.target_end_date for sample in training).isoformat(),
            "test_start": test_dates[0].isoformat(),
            "test_end": test_dates[-1].isoformat(),
            "test_rows": len(testing),
            "calibrated": model.calibrated,
            "model_diagnostics": model.diagnostics(),
        })
    return results, folds


def metrics(results: list[PredictionResult]) -> dict:
    if not results:
        return {"rows": 0}
    n = len(results)
    brier = sum((row.probability_up - row.actual_up) ** 2 for row in results) / n
    base_brier = sum((row.baseline_probability - row.actual_up) ** 2 for row in results) / n
    log_loss = -sum(row.actual_up * math.log(row.probability_up) + (1 - row.actual_up) * math.log(1 - row.probability_up) for row in results) / n
    accuracy = sum(int((row.probability_up >= 0.5) == bool(row.actual_up)) for row in results) / n
    momentum_accuracy = sum(int(row.momentum_prediction == row.actual_up) for row in results) / n
    decisive = [row for row in results if row.probability_up >= 0.55 or row.probability_up <= 0.45]
    decisive_accuracy = (
        sum(int((row.probability_up >= 0.5) == bool(row.actual_up)) for row in decisive) / len(decisive)
        if decisive else None
    )
    return {
        "rows": n,
        "start": min(row.feature_date for row in results).isoformat(),
        "end": max(row.feature_date for row in results).isoformat(),
        "brier": round(brier, 6),
        "historical_rate_brier": round(base_brier, 6),
        "log_loss": round(log_loss, 6),
        "accuracy": round(accuracy, 6),
        "momentum_accuracy": round(momentum_accuracy, 6),
        "decisive_coverage": round(len(decisive) / n, 6),
        "decisive_accuracy": round(decisive_accuracy, 6) if decisive_accuracy is not None else None,
        "calibrated_rows": sum(int(row.calibrated) for row in results),
        "baseline_only_rows": sum(row.discrimination_status == "baseline_only" for row in results),
        "unique_dates": len({row.feature_date for row in results}),
        "probability_min": round(min(row.probability_up for row in results), 6),
        "probability_max": round(max(row.probability_up for row in results), 6),
    }


def _autocov(series: list[float], lag: int) -> float:
    """总体自协方差（与朴素 ICIR 用 pstdev 的口径一致，分母用 n）。"""
    n = len(series)
    if lag <= 0:
        return statistics.pvariance(series)
    if n <= lag:
        return 0.0
    mean = statistics.fmean(series)
    return sum((series[i] - mean) * (series[i - lag] - mean) for i in range(lag, n)) / n


def ic_tstat_newey_west(ics: list[float], max_lag: int | None = None) -> float | None:
    """Newey-West(1987) 异方差-自相关稳健 t 统计量，检验 H0: mean(IC)=0。

    与 selection_metrics 里的 icir(=mean/std，信息比率/一致性指标) 是**不同口径**：
    这里算的是**显著性**：t = mean(IC) / SE_mean，其中
        SE_mean = sqrt( (gamma0 + 2*Σ_{l=1..L} gamma_l*(1 - l/(L+1))) / n )
    h20 标签相邻重叠 19 天 → IC 序列强自相关 → 朴素 SE=std/√n 会**低估标准误**、
    **高估 t 值**。本函数给出去重叠后的真实显著性，用于判断"均值 IC 是否真的 ≠ 0"。
    """
    if len(ics) < 10:
        return None
    if max_lag is None:
        max_lag = min(len(ics) - 2, 20)  # 覆盖 h20 的 19 天重叠
    if max_lag < 1:
        return None
    mean = statistics.fmean(ics)
    gamma0 = _autocov(ics, 0)
    if gamma0 <= 0:
        return None
    var = gamma0
    for lag in range(1, max_lag + 1):
        w = 1 - lag / (max_lag + 1)
        var += 2 * w * _autocov(ics, lag)
    if var <= 0:
        return None
    se = math.sqrt(var / len(ics))
    if se == 0:
        return None
    return mean / se


def selection_metrics(results: list[PredictionResult], quantiles: int = 5) -> dict:
    """选股能力指标：IC / Rank IC / ICIR + 概率分位组收益单调性。

    为什么必须有这个
    ----------------
    metrics() 里的 Brier / log_loss / accuracy 全是**校准与分类**口径，
    回答的是"概率值给得准不准"。它们无法回答"按这个概率排序能不能赚钱"。

    两者可以彻底脱钩 —— 2026-10-05 实测 commodity h20 的 Brier skill 显著为正
    (+0.00528, block bootstrap CI 全在正区间)，但 Pearson IC 仅 0.0267、
    ICIR 0.051、分位组不单调；同期 cached h20 的 Q5−Q1 甚至是 −3.83pp 的反向。
    结论：模型学到的是市场整体基准率(校准)，不是个股排序(选股)。
    只靠 Brier 会把"概率校准得好"误判成"有 alpha"。

    判读参考：ICIR > 0.5 可用，0.3~0.5 勉强，< 0.1 基本等于零。
    实务上还应要求分位组收益单调递增，否则 IC 可能是少数极端样本撑起来的。
    """
    if not results:
        return {"rows": 0}

    def _pearson(xs: list[float], ys: list[float]) -> float | None:
        n = len(xs)
        if n < 3:
            return None
        mx = sum(xs) / n
        my = sum(ys) / n
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        dy = math.sqrt(sum((y - my) ** 2 for y in ys))
        return num / (dx * dy) if dx > 0 and dy > 0 else None

    def _ranks(values: list[float]) -> list[float]:
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

    # 按 feature_date 分组做横截面相关
    by_date: dict = {}
    for row in results:
        by_date.setdefault(row.feature_date, []).append(
            (row.probability_up, row.actual_return))

    ics: list[float] = []
    rank_ics: list[float] = []
    skipped = 0
    for _day, pairs in by_date.items():
        if len(pairs) < 3:
            skipped += 1
            continue
        probs = [p for p, _ in pairs]
        rets = [r for _, r in pairs]
        ic = _pearson(probs, rets)
        ric = _pearson(_ranks(probs), _ranks(rets))
        if ic is not None:
            ics.append(ic)
        if ric is not None:
            rank_ics.append(ric)

    tstat_nw = ic_tstat_newey_west(ics)

    def _summarize(vals: list[float]) -> dict:
        if not vals:
            return {"n_dates": 0}
        mean = sum(vals) / len(vals)
        # 用样本标准差(pstdev)而非无偏估计：这里描述的是已观测序列本身的离散度
        sd = statistics.pstdev(vals)
        return {
            "n_dates": len(vals),
            "mean": round(mean, 6),
            "std": round(sd, 6) if sd else 0.0,
            # 两个键都给：signal_verdict 与独立脚本 ic_analysis.py 历史上
            # 用的是 icir_naive，这里若只给 icir 会让闸门恒定读到 None。
            "icir": round(mean / sd, 4) if sd > 0 else None,
            "icir_naive": round(mean / sd, 4) if sd > 0 else None,
            "positive_ratio": round(sum(1 for v in vals if v > 0) / len(vals), 4),
        }

    # 概率分位组
    ordered = sorted((row.probability_up, row.actual_return) for row in results)
    n = len(ordered)
    buckets: dict[str, dict] = {}
    for q in range(quantiles):
        lo, hi = int(n * q / quantiles), int(n * (q + 1) / quantiles)
        part = ordered[lo:hi]
        if not part:
            continue
        buckets[f"Q{q + 1}"] = {
            "n": len(part),
            "prob_range": [round(part[0][0], 6), round(part[-1][0], 6)],
            "mean_return": round(sum(r for _, r in part) / len(part), 6),
            "win_rate": round(sum(1 for _, r in part if r > 0) / len(part), 6),
        }

    spread = None
    if f"Q{quantiles}" in buckets and "Q1" in buckets:
        spread = round(
            buckets[f"Q{quantiles}"]["mean_return"] - buckets["Q1"]["mean_return"], 6)

    means = [buckets[f"Q{i}"]["mean_return"] for i in range(1, quantiles + 1)
             if f"Q{i}" in buckets]
    monotonic = len(means) == quantiles and all(
        means[i] < means[i + 1] for i in range(len(means) - 1))

    ic_summary = _summarize(ics)
    ic_summary["icir_newey_west"] = round(tstat_nw, 4) if tstat_nw is not None else None
    return {
        "rows": n,
        "cross_section_dates": len(by_date),
        "dates_skipped_lt3": skipped,
        "ic": ic_summary,
        "rank_ic": _summarize(rank_ics),
        "icir_newey_west": tstat_nw,
        "quantile_buckets": buckets,
        "spread_top_minus_bottom": spread,
        "monotonic_quantiles": monotonic,
    }


# ==========================================================================
# 决策口径：超额收益优先、IC 判定选股、滑点默认计入
# --------------------------------------------------------------------------
# 2026-10-06 补。背景：commodity h20 的 Brier skill +0.00528 通过了 block
# bootstrap 与 Bonferroni 六重校正，但做成策略后全面跑输买入持有
# （选股 top3 +27.1% vs 买入持有 +86.1%）。原因是 Brier 衡量的是**校准**，
# IC 衡量的才是**排序**——模型学会的是市场整体基准上涨率，不是区分个股。
#
# 下面三个常量与三个函数把那次教训固化为可执行的检查，避免重复误判。
# ==========================================================================

ICIR_GATE = 0.30
"""ICIR 行业门槛。低于此值视为无选股能力。

实测对照：commodity h20 ICIR=0.051，cached h20 ICIR=0.062 —— 两者都是零。
即便 Brier skill 显著为正，也不能拿去做选股。
"""

IC_MEAN_GATE = 0.02
"""IC 均值参考门槛。"""

DEFAULT_SLIPPAGE_BPS = 10.0
"""回测默认滑点（单边，bps）。

为什么默认开启：commodity top3 实测，手续费 10bps 让收益掉 4.9pp，
滑点 10bps 再掉 9.2pp —— 滑点的杀伤是手续费的近两倍。
把它设为 0 的回测结果一律不可用于决策。
"""


def excess_metrics(strategy: dict, benchmark: dict) -> dict:
    """把策略表现换算成相对基准的超额口径。

    为什么必须有这个函数
    --------------------
    绝对收益会骗人：在 commodity 那种 8 只里 3 只黄金、整段单边牛市的环境里，
    任何策略的绝对收益都是正的，看上去"有效"，实际全部跑输"买了不动"。
    只有超额收益（相对买入持有）才是决策依据。

    几何超额用 (1+s)/(1+b)-1 而非简单相减，避免百分比口径混淆。
    """
    if not benchmark:
        raise ValueError(
            "excess_metrics 必须提供 benchmark（买入持有）。"
            "没有基准线的回测结论一律不可用于决策——这正是 commodity h20 "
            "那次误判的成因。"
        )

    s_ret = float(strategy.get("total_return", 0.0))
    b_ret = float(benchmark.get("total_return", 0.0))
    s_cagr = float(strategy.get("cagr", 0.0))
    b_cagr = float(benchmark.get("cagr", 0.0))

    denom = 1.0 + b_ret
    geo_excess = ((1.0 + s_ret) / denom - 1.0) if denom > 0 else None

    return {
        "strategy_total_return": round(s_ret, 6),
        "benchmark_total_return": round(b_ret, 6),
        "arithmetic_excess": round(s_ret - b_ret, 6),
        "geometric_excess": round(geo_excess, 6) if geo_excess is not None else None,
        "cagr_excess": round(s_cagr - b_cagr, 6),
        "sharpe_excess": round(
            float(strategy.get("sharpe", 0.0)) - float(benchmark.get("sharpe", 0.0)), 4),
        "drawdown_delta": round(
            float(strategy.get("max_drawdown", 0.0))
            - float(benchmark.get("max_drawdown", 0.0)), 6),
        "beats_benchmark": bool(geo_excess is not None and geo_excess > 0),
    }


def signal_verdict(selection: dict) -> dict:
    """用 IC（不是 Brier）判断这个信号能不能用来选股。

    返回中的 usages 显式列出该信号允许/禁止的用法，供上层直接采纳。
    """
    ic = selection.get("ic") or {}
    ric = selection.get("rank_ic") or {}
    # 兼容两种字段名，避免再出现各模块命名漂移导致闸门静默失效
    icir = ic.get("icir_naive", ic.get("icir"))
    rank_icir = ric.get("icir_naive", ric.get("icir"))
    icir_nw = ic.get("icir_newey_west", selection.get("icir_newey_west"))
    mean = ic.get("mean")
    spread = selection.get("spread_top_minus_bottom")
    monotone = bool(selection.get("monotonic_quantiles"))

    usable = bool(
        icir is not None and abs(icir) >= ICIR_GATE
        and mean is not None and abs(mean) >= IC_MEAN_GATE
    )

    # h20 标签重叠会让朴素 ICIR 高估显著性：若朴素过闸但 NW 调整后没过，
    # 说明"显著性"主要来自重叠标签的重复计数，不是真实选股能力。
    # 仅在 NW 估计可用时才判定——缺 NW 时退回朴素口径，不臆断。
    overlap_inflates = bool(
        icir is not None and abs(icir) >= ICIR_GATE
        and icir_nw is not None and abs(icir_nw) < ICIR_GATE
    )
    if overlap_inflates:
        usable = False

    return {
        "ic_mean": mean,
        "icir": icir,
        "icir_newey_west": icir_nw,
        "rank_icir": rank_icir,
        "spread_top_minus_bottom": spread,
        "monotonic_quantiles": monotone,
        "icir_gate": ICIR_GATE,
        "overlap_inflates_significance": overlap_inflates,
        "selection_usable": usable,
        "usages": {
            "stock_picking": "允许" if usable else "禁用：ICIR 未过闸",
            "market_timing": "允许：概率均值可用于判断整体方向",
        },
        "reason": (
            f"ICIR={icir} (Newey-West 去重叠后 {icir_nw}) {'>=' if (icir is not None and abs(icir) >= ICIR_GATE) else '<'} 门槛 {ICIR_GATE}"
            f"，IC 均值 {mean}；"
            f"{'可用于区分个股' if usable else ('仅朴素口径过闸、NW 调整后未过闸，显著性由重叠标签高估' if overlap_inflates else '只学到市场整体上涨率，无排序能力')}"
        ),
    }


def decision_report(results: list, strategy_stats: dict,
                    benchmark_stats: dict) -> dict:
    """一次性产出「这套信号该怎么用」的完整判决。

    强制要求 benchmark —— 漏传直接抛错，而不是给出没有参照的漂亮数字。
    """
    selection = selection_metrics(results)
    verdict = signal_verdict(selection)
    excess = excess_metrics(strategy_stats, benchmark_stats)

    return {
        "selection_metrics": selection,
        "signal_verdict": verdict,
        "excess_vs_buy_hold": excess,
        "decision": (
            "可用" if excess["beats_benchmark"] else "跑输基准，不应上线"
        ),
        "note": (
            "决策依据为相对买入持有的几何超额与 ICIR，"
            "不是绝对收益也不是 Brier skill。"
        ),
    }


def serialize_results(results: list[PredictionResult]) -> list[dict]:
    serialized = []
    for result in results:
        row = asdict(result)
        row["feature_date"] = result.feature_date.isoformat()
        row["target_end_date"] = result.target_end_date.isoformat()
        serialized.append(row)
    return serialized

from __future__ import annotations

import math
import random
import statistics
from datetime import date, timedelta

import pytest

from prediction_research.evaluation import (
    DEFAULT_SLIPPAGE_BPS,
    ICIR_GATE,
    IC_MEAN_GATE,
    PredictionResult,
    excess_metrics,
    ic_tstat_newey_west,
    selection_metrics,
    signal_verdict,
)


def _row(symbol: str, day: int, prob: float, ret: float) -> PredictionResult:
    feature_date = date(2024, 1, 1) + timedelta(days=day)
    return PredictionResult(
        symbol=symbol, feature_date=feature_date,
        target_end_date=feature_date + timedelta(days=20),
        probability_up=prob, actual_up=1 if ret > 0 else 0, actual_return=ret,
        calibrated=True, baseline_probability=0.5, momentum_prediction=1,
        raw_probability_up=prob,
    )


def _aligned_results(day: int, invert: bool = False) -> list[PredictionResult]:
    # 4 只标的，概率与收益严格单调；invert 时把收益顺序颠倒，让 IC 变负。
    probs = [0.2, 0.4, 0.6, 0.8]
    rets = [-0.05, -0.01, 0.04, 0.10]
    if invert:
        rets = rets[::-1]
    return [_row(f"s{i}", day, p, r) for i, (p, r) in enumerate(zip(probs, rets))]


def test_empty_results_short_circuits():
    assert selection_metrics([]) == {"rows": 0}


def test_selection_metrics_positive_ic_when_aligned():
    results = [_r for d in range(3) for _r in _aligned_results(d, invert=False)]
    sel = selection_metrics(results)
    assert sel["rows"] == 12
    assert sel["ic"]["mean"] > 0.9          # 完美单调 → IC≈1
    assert sel["rank_ic"]["mean"] > 0.9
    assert sel["monotonic_quantiles"] is True
    assert sel["spread_top_minus_bottom"] > 0


def test_selection_metrics_negative_ic_when_inverted():
    results = [_r for d in range(3) for _r in _aligned_results(d, invert=True)]
    sel = selection_metrics(results)
    assert sel["ic"]["mean"] < -0.9         # 概率升、收益降 → IC≈−1
    assert sel["monotonic_quantiles"] is False


def test_cross_section_grouping_skips_days_with_lt3_symbols():
    # 单日少于 3 只会被跳过，且不计入 IC。
    results = [_row("a", 0, 0.2, -0.1), _row("b", 0, 0.8, 0.1)]
    sel = selection_metrics(results)
    assert sel["cross_section_dates"] == 1
    assert sel["dates_skipped_lt3"] == 1
    assert sel["ic"]["n_dates"] == 0


def test_signal_verdict_gate_passes_when_icir_and_mean_clear():
    sel = {"ic": {"mean": 0.10, "icir": 0.55, "icir_naive": 0.55},
           "rank_ic": {"mean": 0.09, "icir": 0.52, "icir_naive": 0.52},
           "spread_top_minus_bottom": 0.04, "monotonic_quantiles": True}
    verdict = signal_verdict(sel)
    assert verdict["selection_usable"] is True
    assert verdict["usages"]["stock_picking"] == "允许"
    assert verdict["usages"]["market_timing"] == "允许：概率均值可用于判断整体方向"


def test_signal_verdict_gate_blocks_weak_icir():
    sel = {"ic": {"mean": 0.10, "icir": 0.20, "icir_naive": 0.20},
           "rank_ic": {"mean": 0.09, "icir": 0.18, "icir_naive": 0.18},
           "spread_top_minus_bottom": 0.04, "monotonic_quantiles": True}
    verdict = signal_verdict(sel)
    # 0.20 < 0.30 门槛 → 禁用选股，但允许择时口径。
    assert verdict["selection_usable"] is False
    assert verdict["usages"]["stock_picking"].startswith("禁用")
    assert verdict["usages"]["market_timing"].startswith("允许")


def test_signal_verdict_gate_blocks_zero_mean():
    sel = {"ic": {"mean": 0.0, "icir": 0.9, "icir_naive": 0.9},
           "rank_ic": {"mean": 0.0, "icir": 0.9, "icir_naive": 0.9},
           "spread_top_minus_bottom": 0.0, "monotonic_quantiles": False}
    assert signal_verdict(sel)["selection_usable"] is False


def test_signal_verdict_reads_icir_naive_when_only_legacy_key_present():
    # 字段名漂移修复：历史脚本只写 icir_naive，缺 icir，闸门绝不能读到 None。
    sel = {"ic": {"mean": 0.10, "icir_naive": 0.55},
           "rank_ic": {"mean": 0.09, "icir_naive": 0.52},
           "spread_top_minus_bottom": 0.04, "monotonic_quantiles": True}
    verdict = signal_verdict(sel)
    assert verdict["icir"] == 0.55
    assert verdict["selection_usable"] is True


def test_signal_verdict_naive_wins_when_both_keys_present():
    # icir 写错（None/漂移）时仍以 icir_naive 为准，锁住"闸门恒 None"的回归。
    sel = {"ic": {"mean": 0.10, "icir": None, "icir_naive": 0.55},
           "rank_ic": {"mean": 0.09, "icir": None, "icir_naive": 0.52},
           "spread_top_minus_bottom": 0.04, "monotonic_quantiles": True}
    assert signal_verdict(sel)["icir"] == 0.55


def test_constants_exposed_with_expected_values():
    assert ICIR_GATE == 0.30
    assert IC_MEAN_GATE == 0.02
    assert DEFAULT_SLIPPAGE_BPS == 10.0


def test_excess_metrics_requires_benchmark():
    with pytest.raises(ValueError):
        excess_metrics({"total_return": 0.3}, {})


def test_excess_metrics_geometric_vs_arithmetic():
    strategy = {"total_return": 0.5, "cagr": 0.2, "sharpe": 1.1, "max_drawdown": -0.1}
    benchmark = {"total_return": 0.3, "cagr": 0.15, "sharpe": 1.3, "max_drawdown": -0.2}
    ex = excess_metrics(strategy, benchmark)
    assert ex["arithmetic_excess"] == pytest.approx(0.2)
    # 几何超额 = (1.5/1.3)-1 ≈ 0.153846
    assert ex["geometric_excess"] == pytest.approx(0.153846, rel=1e-4)
    assert ex["beats_benchmark"] is True
    assert ex["cagr_excess"] == pytest.approx(0.05)
    assert ex["sharpe_excess"] == pytest.approx(-0.2)
    assert ex["drawdown_delta"] == pytest.approx(0.1)  # 策略回撤更小 → 差值正


def test_excess_metrics_losing_to_benchmark_reports_false():
    ex = excess_metrics({"total_return": 0.3}, {"total_return": 0.5})
    assert ex["beats_benchmark"] is False
    assert ex["geometric_excess"] == pytest.approx((1.3 / 1.5) - 1, rel=1e-4)


def test_icir_newey_west_requires_minimum_series():
    assert ic_tstat_newey_west([0.1, 0.2, 0.3]) is None


def _ar1(phi: float, n: int, seed: int, mu: float = 0.05) -> list[float]:
    """平稳 AR(1) 序列：x_t = mu + phi*(x_{t-1}-mu) + eps_t。

    phi 越接近 1，IC 序列自相关越强 —— 这正是 h20 标签相邻重叠 19 天的近似。
    注意旧版测试用 `ics=[0.30]; 0.9*x+0.03` 构造，那会收敛到常数 0.30
    （0.9*0.30+0.03=0.30），序列方差≈0，除法直接爆炸，必须加随机扰动。
    """
    rng = random.Random(seed)
    out: list[float] = []
    x = mu
    for _ in range(n):
        x = mu + phi * (x - mu) + rng.gauss(0.0, 0.1)
        out.append(x)
    return out


def _naive_tstat(ics: list[float]) -> float:
    """朴素 t 统计量 t = mean / (std / sqrt(n))，假设样本独立、忽略自相关。

    这是 ic_tstat_newey_west 唯一正确的对照口径 —— 不能拿 ICIR(=mean/std,
    信息比率) 去比 NW 的 t 统计量(显著性)，两者量纲与含义都不同。
    """
    n = len(ics)
    return statistics.fmean(ics) / (statistics.pstdev(ics) / math.sqrt(n))


def test_tstat_newey_west_close_to_naive_when_independent():
    # 白噪声（phi=0）：Bartlett 核的滞后修正项≈0，NW-t 应与朴素 t 同量级。
    # 这是 NW 调整的"中性基准"—— 没有重叠时它不该凭空改变结论。
    ics = _ar1(0.0, n=240, seed=7)
    naive = _naive_tstat(ics)
    nw = ic_tstat_newey_west(ics)
    assert nw is not None
    assert naive > 0
    assert 0.5 < nw / naive < 2.0


def test_tstat_newey_west_is_more_conservative_under_positive_autocorrelation():
    # 强正自相关（phi=0.9）：长程方差被放大 → 标准误变大 → |t| 必须被压低。
    # 若此处 NW-t 反而更大，说明去重叠逻辑写反了，h20 的显著性会被高估。
    ics = _ar1(0.9, n=240, seed=11)
    naive = _naive_tstat(ics)
    nw = ic_tstat_newey_west(ics)
    assert nw is not None
    assert naive > 0 and nw > 0
    # 截断 lags=20 时理论衰减比约 1/2.9，留足余量只断言"明显更保守"。
    assert nw < naive * 0.7


def test_selection_metrics_includes_icir_newey_west():
    rows = []
    day = 0
    # 多日、每日 4 只，概率与收益对齐 -> 正 IC；用足够多天让 NW 可估。
    for d in range(40):
        rows.extend(_aligned_results(day))
        day += 1
    sel = selection_metrics(rows)
    assert "icir_newey_west" in sel
    assert "icir_newey_west" in sel["ic"]


def test_signal_verdict_flags_overlap_inflation():
    # 朴素 ICIR 过闸、但 Newey-West 调整后未过 -> 由重叠标签高估，闸门应禁用选股。
    selection = {
        "ic": {"mean": 0.05, "icir_naive": 0.60, "icir_newey_west": 0.10},
        "spread_top_minus_bottom": 0.01,
        "monotonic_quantiles": True,
    }
    verdict = signal_verdict(selection)
    assert verdict["overlap_inflates_significance"] is True
    assert verdict["selection_usable"] is False
    assert verdict["icir_newey_west"] == 0.10


def test_signal_verdict_no_nw_field_falls_back_to_naive():
    # 缺 icir_newey_west 时不臆断重叠，退回朴素口径。
    selection = {
        "ic": {"mean": 0.05, "icir_naive": 0.60},
        "spread_top_minus_bottom": 0.01,
        "monotonic_quantiles": True,
    }
    verdict = signal_verdict(selection)
    assert verdict["overlap_inflates_significance"] is False
    assert verdict["selection_usable"] is True

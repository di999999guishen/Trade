from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date

from .data import SeriesSnapshot


FEATURE_NAMES = (
    "ret_5",
    "ret_20",
    "ret_60",
    "vol_20",
    "vol_60",
    "close_ma20",
    "close_ma60",
    "drawdown_20",
    "range_20",
    "volume_ratio_20",
)

EXTERNAL_FEATURE_NAMES = (
    "underlying_ret_5",
    "underlying_ret_20",
    "underlying_ret_60",
    "underlying_vol_20",
    "underlying_breadth_20",
    "underlying_coverage",
)

# ⛔ 禁止把任何**成交额 / turnover（amount）类**量加进 FEATURE_NAMES / EXTERNAL_FEATURE_NAMES。
#
# 记忆 §5「停用成交额类特征必须动手」的落地。核查结论（2026-10-10）：
#   - 本模块的特征里**从来没有**成交额类量（`git log -S"amount" -- features.py` 为空），
#     真正的成交量维度只有 `volume_ratio_20`，取自 `bar.volume`。
#   - 日线缓存的 `amount` 列在本管道里**恒为常量占位**：新浪 K 线不返回成交额
#     （`adapters/sina.py` 写死 "0" 并标注 `amount_status=unavailable`），腾讯源同样
#     `df["amount"] = 0`（`etf_data_source.py`）。常量列对模型是纯噪声，还会把
#     「残缺输入」伪装成有效特征——这正是记忆要求停用它的原因。
#   - 真实成交额只存在于**资金流快照**（东财 `f6` / 新浪 `amount`），且只供粗筛
#     `liquidity` 因子使用，**不进模型特征**（`screening.py`）。
#
# 下面这个导入期守卫把该不变量钉死：将来谁误把成交额类量加回特征列表，import 即失败。
_AMOUNT_FEATURE_GUARD = ("amount", "turnover", "成交额", "成交金额")


def _assert_no_amount_features(*groups: tuple[str, ...]) -> None:
    for group in groups:
        for name in group:
            if any(token in name.lower() for token in _AMOUNT_FEATURE_GUARD):
                raise AssertionError(
                    f"feature '{name}' looks like an amount/turnover feature. The daily cache's "
                    "`amount` column is a constant placeholder (sina/tencent return no turnover), "
                    "so it is a corrupt input — see the note above EXTERNAL_FEATURE_NAMES."
                )


_assert_no_amount_features(FEATURE_NAMES, EXTERNAL_FEATURE_NAMES)


@dataclass(frozen=True)
class Sample:
    symbol: str
    feature_date: date
    target_end_date: date | None
    features: tuple[float, ...]
    target_return: float | None
    target_up: int | None


def _returns(values: list[float]) -> list[float]:
    return [values[i] / values[i - 1] - 1.0 for i in range(1, len(values))]


def build_samples(snapshot: SeriesSnapshot, horizon: int, include_unlabeled: bool = False) -> list[Sample]:
    bars = snapshot.bars
    output: list[Sample] = []
    for idx in range(60, len(bars)):
        close = [bar.close for bar in bars[: idx + 1]]
        volume = [bar.volume for bar in bars[: idx + 1]]
        recent_returns = _returns(close)
        mean20 = statistics.fmean(close[-20:])
        mean60 = statistics.fmean(close[-60:])
        high20 = max(bar.high for bar in bars[idx - 19 : idx + 1])
        low20 = min(bar.low for bar in bars[idx - 19 : idx + 1])
        vol20_mean = statistics.fmean(volume[-20:])
        values = (
            close[-1] / close[-6] - 1.0,
            close[-1] / close[-21] - 1.0,
            close[-1] / close[-61] - 1.0,
            statistics.pstdev(recent_returns[-20:]) * math.sqrt(252),
            statistics.pstdev(recent_returns[-60:]) * math.sqrt(252),
            close[-1] / mean20 - 1.0,
            close[-1] / mean60 - 1.0,
            close[-1] / max(close[-20:]) - 1.0,
            (high20 - low20) / close[-1],
            volume[-1] / vol20_mean if vol20_mean > 0 else 1.0,
        )
        exit_idx = idx + horizon
        if exit_idx < len(bars):
            entry = bars[idx + 1].open
            result = bars[exit_idx].close / entry - 1.0
            output.append(Sample(snapshot.symbol, bars[idx].trading_date, bars[exit_idx].trading_date, values, result, int(result > 0)))
        elif include_unlabeled and idx == len(bars) - 1:
            output.append(Sample(snapshot.symbol, bars[idx].trading_date, None, values, None, None))
    return output


def augment_with_external(
    samples: list[Sample],
    external_snapshots: dict[str, SeriesSnapshot],
    series_definitions: dict,
    mapped_symbols: list[str],
) -> list[Sample]:
    indexes = {
        symbol: ([bar.trading_date for bar in snapshot.bars], snapshot.bars)
        for symbol, snapshot in external_snapshots.items()
        if symbol in mapped_symbols
    }
    output = []
    for sample in samples:
        observations = []
        for symbol in mapped_symbols:
            indexed = indexes.get(symbol)
            if not indexed:
                continue
            dates, bars = indexed
            lag = int(series_definitions[symbol].get("availability_lag_days", 0))
            position = bisect_right(dates, sample.feature_date) - 1 - lag
            if position < 60:
                continue
            closes = [bar.close for bar in bars[position - 60 : position + 1]]
            returns20 = _returns(closes[-21:])
            observations.append((
                closes[-1] / closes[-6] - 1,
                closes[-1] / closes[-21] - 1,
                closes[-1] / closes[-61] - 1,
                statistics.pstdev(returns20) * math.sqrt(252),
            ))
        if observations:
            count = len(observations)
            external = (
                statistics.fmean(item[0] for item in observations),
                statistics.fmean(item[1] for item in observations),
                statistics.fmean(item[2] for item in observations),
                statistics.fmean(item[3] for item in observations),
                sum(int(item[1] > 0) for item in observations) / count,
                count / max(1, len(mapped_symbols)),
            )
        else:
            external = (0.0, 0.0, 0.0, 0.0, 0.5, 0.0)
        output.append(Sample(
            sample.symbol, sample.feature_date, sample.target_end_date,
            sample.features + external, sample.target_return, sample.target_up,
        ))
    return output

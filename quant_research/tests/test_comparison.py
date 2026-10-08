import numpy as np
import pandas as pd
import pytest

from quant_research.calendar import calendar, decision_at
from quant_research.comparison import quarterly_splits
from quant_research.config import load_config
from quant_research.portfolio import desired_weights
from quant_research.validation import TimeSplit


def long_samples():
    cal = calendar()
    dates = cal.sessions_in_range("2020-01-02", "2026-09-17")
    return pd.DataFrame({"sample_id": [str(s.date()) for s in dates], "session": dates,
                         "decision_at": [decision_at(s) for s in dates],
                         "exit_at": [cal.session_close(cal.session_offset(s, 5)) for s in dates],
                         "label_available_at": [decision_at(cal.session_offset(s, 5)) for s in dates]})


def test_quarterly_six_year_history_purge_and_partial_tail():
    samples = long_samples()
    folds = quarterly_splits(samples)
    assert [f["boundaries"][2] for f in folds] == ["2026-01-01", "2026-04-01", "2026-07-01"]
    assert folds[-1]["boundaries"][-1] == "2026-09-18"
    assert folds[-1]["test"][-1] == "2026-09-17"
    seen = set()
    indexed = samples.set_index("sample_id")
    for fold in folds:
        assert not seen.intersection(fold["test"])
        seen.update(fold["test"])
        train, valid, test = (indexed.loc[fold[k]] for k in ("train", "validation", "test"))
        assert train.label_available_at.max() < valid.decision_at.min()
        assert valid.label_available_at.max() < test.decision_at.min()
    assert len(seen) == len(samples[samples.session >= "2026-01-01"])


def test_split_accepts_holiday_start_and_exclusive_end():
    samples = long_samples()
    spec = TimeSplit("2020-01-01", "2025-01-01", "2026-01-01", "2026-09-18", 5)
    assert spec.select(samples)["status"] == "ready"
    assert spec.select(samples.iloc[1:])["status"] == "insufficient_data"


@pytest.mark.parametrize("strategy", ["s3_lightgbm", "s8_fixed_ensemble"])
def test_model_scores_change_ranking_but_preserve_trend_exit(strategy):
    day = pd.Timestamp("2026-01-02")
    symbols = ["SPY", "QQQ", "IWM", "MDY", "GLD", "TLT", "IEF"]
    frames = {s: pd.DataFrame({"eligible": [True]}, index=[day]) for s in symbols}
    factors = {s: pd.DataFrame({"trend200": [.1], "mom126": [.1], "mom252": [.2]}, index=[day]) for s in symbols}
    factors["QQQ"].loc[day, "trend200"] = -.1
    scores = {s: float(i) for i, s in enumerate(symbols)}
    scores["QQQ"] = 100.
    desired, _, _, forced, _ = desired_weights(strategy, day, frames, factors,
                                               dict.fromkeys(symbols, 0.), load_config(), model_scores=scores)
    assert desired["QQQ"] == 0 and "QQQ" in forced
    assert desired["SPY"] == 0 and desired["IEF"] == .2
    assert sum(desired.values()) <= 1
    scores["IEF"] = np.nan
    with pytest.raises(ValueError, match="missing causal"):
        desired_weights(strategy, day, frames, factors, {}, load_config(), model_scores=scores)

import numpy as np
import pandas as pd
import pytest

from quant_research.diagnostics import factor_diagnostics
from quant_research.model import train_fold


def training_fixture():
    # Explicit synthetic training-contract test; no strategy performance claim.
    dates = pd.bdate_range("2010-01-01", periods=950, tz="UTC")
    rng = np.random.default_rng(42)
    ids = [f"SPY:{d.date()}" for d in dates]
    features = pd.DataFrame(rng.normal(size=(950, 10)), columns=[f"factor_{i}" for i in range(10)], index=ids)
    samples = pd.DataFrame({"sample_id": ids, "session": dates.tz_localize(None), "symbol": "SPY",
                            "decision_at": dates + pd.Timedelta(hours=22),
                            "entry_at": dates + pd.Timedelta(days=1), "exit_at": dates + pd.Timedelta(days=6),
                            "label_available_at": dates + pd.Timedelta(days=7),
                            "return_value": features.factor_0.to_numpy() * .01 + rng.normal(0, .001, 950)})
    split = {"status": "ready", "train": ids[:600], "validation": ids[630:800], "test": ids[830:]}
    return features, samples, split


@pytest.mark.parametrize("estimator", ["lightgbm", "ridge"])
def test_repeat_seed_and_test_label_independence(tmp_path, estimator):
    features, samples, split = training_fixture()
    first, predictions = train_fold(features, samples, split, tmp_path, estimator=estimator)
    samples.loc[830:, "return_value"] = np.nan
    second, other = train_fold(features, samples, split, tmp_path, estimator=estimator)
    np.testing.assert_array_equal(predictions.score, other.score)
    assert first["model_hash"] == second["model_hash"]
    assert first["test_rows"] == 120


def test_training_rejects_unmatured_and_shared_dates(tmp_path):
    features, samples, split = training_fixture()
    samples.loc[599, "label_available_at"] = samples.loc[640, "decision_at"]
    with pytest.raises(ValueError, match="unmatured"):
        train_fold(features, samples, split, tmp_path)
    _, samples, split = training_fixture()
    samples.loc[630, "session"] = samples.loc[599, "session"]
    with pytest.raises(ValueError, match="same decision date"):
        train_fold(features, samples, split, tmp_path)


def test_small_cross_section_is_insufficient():
    features, samples, _ = training_fixture()
    diagnostics = factor_diagnostics(features, samples)
    assert diagnostics["factors"]["factor_0"]["status"] == "insufficient_cross_section"
    assert diagnostics["factors"]["factor_0"]["ic_mean"] is None

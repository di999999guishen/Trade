from datetime import date, timedelta
import random

import pytest

from prediction_research.evaluation import _fit_model, walk_forward
from prediction_research.features import Sample
from prediction_research.model import LogisticModel
from prediction_research.pipeline import _model_version, run_prediction
from prediction_research.tests.test_evidence import cfg, write_histories


def samples(days=240, horizon=5):
    start = date(2023, 1, 1)
    return [Sample(symbol, start + timedelta(days=i), start + timedelta(days=i + horizon),
                   (i / 100, float((i + j) % 7)), 0.01 if (i + j) % 2 else -0.01, (i + j) % 2)
            for j, symbol in enumerate(("A", "B", "C")) for i in range(days)]


def settings():
    return dict(min_train_dates=90, max_train_dates=200, calibration_fraction=0.4,
                min_calibration_rows=20, max_fit_rows=180, learning_rate=0.05,
                iterations=8, l2=0.01, test_window_dates=20)


@pytest.mark.parametrize("horizon", [5, 20])
def test_both_inner_boundaries_purge_crossing_labels(horizon):
    model = _fit_model(samples(horizon=horizon), settings())
    train, cal = model.training_details, model.calibration_details
    assert train["core_latest_target"] < train["calibration_start"]
    assert train["purged_rows"] == horizon * 3
    assert cal["fit_latest_target"] < cal["blend_start"]
    assert cal["purged_rows"] == horizon * 3


def test_symbol_blocks_and_random_input_order_produce_identical_model():
    original = samples()
    shuffled = original[:]
    random.Random(73).shuffle(shuffled)
    first, second = _fit_model(original, settings()), _fit_model(shuffled, settings())
    assert first == second


def test_baseline_fallback_is_explicit_not_a_buy_signal():
    model = LogisticModel([0, 0], [1, 1], [0, 0], 0, 0.5)
    model.calibrate(samples())
    assert model.calibration_blend == 0
    assert model.discrimination_status == "baseline_only"
    assert model.predict_proba((100, -10)) == model.predict_proba((-4, 500)) == 0.5


def test_insufficient_calibration_clears_previous_state():
    model = LogisticModel([0, 0], [1, 1], [0, 0], 0, 0.5, 1, 0, 0)
    model.calibrate(samples(days=1))
    assert not model.calibrated
    assert model.discrimination_status == "uncalibrated"
    assert model.calibration_details["status"] == "insufficient_calibration_dates"


def test_future_samples_cannot_change_prior_walk_forward_predictions():
    rows = samples(days=170)
    before, _ = walk_forward(rows, settings())
    later = [row for row in samples(days=210) if row.feature_date > max(r.feature_date for r in rows)]
    after, _ = walk_forward(rows + later, settings())
    by_key = {(row.symbol, row.feature_date): row for row in after}
    assert all(row == by_key[row.symbol, row.feature_date] for row in before)


def test_model_spec_version_separates_training_pool_and_parameters(cfg):
    before = _model_version("commodity", "base", "pooled", cfg)
    cfg["universes"]["commodity"].reverse()
    assert before == _model_version("commodity", "base", "pooled", cfg)
    cfg["model"]["iterations"] += 1
    assert before != _model_version("commodity", "base", "pooled", cfg)
    second = _model_version("commodity", "base", "pooled", cfg)
    cfg["universes"]["commodity"].pop()
    assert second != _model_version("commodity", "base", "pooled", cfg)


def test_frozen_forecast_contains_replayable_model_state(cfg):
    write_histories(cfg)
    _, payload = run_prediction(cfg, "commodity", 5, "base")
    for row in payload["predictions"]:
        model = LogisticModel(**row["model_state"])
        assert model.diagnostics() == row["model_diagnostics"]
        assert 0 < row["raw_probability_up"] < 1
        assert row["probability_status"] == "research_only_failed_validation"
        assert row["model_diagnostics"]["training"]["split_policy"] == "date_grouped_purged_v1"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_features_cannot_become_extreme_probability(bad):
    model = LogisticModel([0, 0], [1, 1], [1, 1], 0, 0.5)
    with pytest.raises(ValueError, match="nonfinite"):
        model.predict_proba((bad, 0))


def test_unannounced_missing_asset_cannot_change_the_fitted_pool(cfg):
    write_histories(cfg)
    cfg["universes"]["commodity"].append({"symbol": "MISSING", "name": "missing", "asset_type": "test", "exposure": "gold"})
    with pytest.raises(ValueError, match="training pool is incomplete"):
        run_prediction(cfg, "commodity", 5, "base")

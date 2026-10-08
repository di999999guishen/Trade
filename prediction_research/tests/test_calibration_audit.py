from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from prediction_research import calibration_audit as audit


def row(symbol="A", day="2026-01-01", probability=0.6, actual_return=0.01, **kwargs):
    return {"symbol": symbol, "feature_date": day, "target_end_date": "2026-02-01",
            "probability_up": probability, "actual_return": actual_return, "actual_up": int(actual_return > 0),
            "discrimination_status": "model_differentiated", **kwargs}


def test_pairing_requires_identical_unique_keys_and_outcomes():
    first = row()
    with pytest.raises(ValueError, match="key mismatch"):
        audit.paired_rows([first], [row(symbol="B")])
    with pytest.raises(ValueError, match="duplicate"):
        audit.paired_rows([first, first], [first])
    with pytest.raises(ValueError, match="label mismatch"):
        audit.paired_rows([first], [row(actual_return=-0.01)])
    assert audit.paired_rows([first], [row(probability=0.7)])[0][1]["probability_up"] == 0.7


def test_date_pairing_aggregates_cross_section_before_bootstrap():
    old = [row("A", probability=0.5), row("B", probability=0.5), row(day="2026-01-02", probability=0.5)]
    new = [row("A", probability=1), row("B", probability=1), row(day="2026-01-02", probability=0)]
    daily = audit.paired_date_brier(old, new)
    assert [item["rows"] for item in daily] == [2, 1]
    assert [item["new_minus_old"] for item in daily] == [-0.25, 0.75]
    result = audit.moving_block_bootstrap(daily, 1, replicates=100)
    assert result["point_estimate"] == 0.25


def test_bootstrap_requires_horizon_blocks_and_two_blocks_of_dates():
    daily = [{"feature_date": f"2026-01-{day:02}", "new_minus_old": -0.01} for day in range(1, 10)]
    assert audit.moving_block_bootstrap([], 5)["point_estimate"] is None
    assert audit.moving_block_bootstrap(daily, 5)["ci95"] is None
    with pytest.raises(ValueError, match="at least"):
        audit.moving_block_bootstrap(daily, 5, block_dates=4)
    with pytest.raises(ValueError, match="one paired"):
        audit.moving_block_bootstrap(daily + daily[:1], 5)
    with pytest.raises(ValueError, match="positive"):
        audit.moving_block_bootstrap(daily, 5, replicates=0)
    daily.append({"feature_date": "2026-01-10", "new_minus_old": -0.01})
    result = audit.moving_block_bootstrap(daily, 5, replicates=100)
    assert result["status"] == "exploratory_interval"
    assert result["ci95"] == pytest.approx([-0.01, -0.01])


def test_bootstrap_reproducible_and_finite():
    daily = [{"feature_date": f"2026-01-{day:02}", "new_minus_old": (day % 7 - 3) / 100} for day in range(1, 31)]
    first = audit.moving_block_bootstrap(daily, 5, replicates=100, seed=42)
    assert first == audit.moving_block_bootstrap(daily, 5, replicates=100, seed=42)
    assert first["ci95"][0] <= first["ci95"][1]
    with pytest.raises(ValueError, match="finite"):
        audit.moving_block_bootstrap([{"feature_date": "2026-01-01", "new_minus_old": float("nan")}], 1)


def test_cohort_cost_and_date_weighting_are_observation_diagnostics():
    rows = [row("A", probability=0.55, actual_return=0.02), row("B", actual_return=0),
            row("C", day="2026-01-02", actual_return=-0.005), row("D", probability=0.5499, actual_return=1)]
    result = audit.long_cohort_diagnostics(rows)
    assert result["selected_rows"] == 3
    assert result["selected_dates"] == 2
    assert result["mean_gross_cohort_return"] == pytest.approx(0.0025)
    assert [item["round_trip_cost_bps"] for item in result["costs"]] == [0, 10, 30, 50]
    for item in result["costs"]:
        assert item["mean_net_cohort_return"] == pytest.approx(0.0025 - item["round_trip_cost_bps"] / 10000)
    assert "sharpe" not in result and "annualized_return" not in result
    with pytest.raises(ValueError, match="invalid"):
        audit.long_cohort_diagnostics(rows, costs_bps=(-1,))


def test_baseline_only_is_counted_but_excluded_from_differentiated_cohort():
    rows = [row("A", discrimination_status="baseline_only"), row("B"),
            row("C", discrimination_status="uncalibrated")]
    all_thresholds = audit.long_cohort_diagnostics(rows)
    effective = audit.long_cohort_diagnostics(rows, differentiated_only=True)
    assert all_thresholds["selected_rows"] == 3 and all_thresholds["selected_baseline_only_rows"] == 1
    assert effective["selected_rows"] == 1 and effective["selected_baseline_only_rows"] == 0
    assert effective["observation_coverage"] == pytest.approx(1 / 3)
    empty = audit.long_cohort_diagnostics([row(probability=0.5)])
    assert empty["selected_rows"] == 0
    assert all(item["mean_net_cohort_return"] is None for item in empty["costs"])


def test_saved_engine_is_loaded_independently_and_hash_checked(tmp_path):
    sources = {"features.py": "from .data import SeriesSnapshot\nMARKER = 'legacy'\n",
               "model.py": "from .features import MARKER\nclass LogisticModel: pass\n",
               "evaluation.py": "from .model import LogisticModel, MARKER\n",
               "pipeline.py": "# original pipeline is recorded, not imported\n"}
    for filename, source in sources.items():
        (tmp_path / filename).write_text(source, encoding="utf-8")
    manifest = {filename: audit.file_hash(tmp_path / filename) for filename in sources}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with audit.legacy_engine(tmp_path) as modules:
        assert modules["evaluation"].MARKER == "legacy"
        assert modules["evaluation"].LogisticModel is modules["model"].LogisticModel
        assert modules["evaluation"].LogisticModel is not audit.evaluation.LogisticModel
        private_names = {module.__name__ for module in modules.values()}
    assert not private_names.intersection(sys.modules)
    (tmp_path / "model.py").write_text("# changed", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        with audit.legacy_engine(tmp_path):
            pass


def test_snapshot_mismatch_rejected_before_fitting(tmp_path, monkeypatch):
    reference = tmp_path / "ref.json"
    reference.write_text(json.dumps({"feature_set": "base", "model_scope": "pooled", "horizon": 5,
                                    "snapshots": {"A": {"path": str(tmp_path / "cache_A.csv"), "sha256": "expected"}}}), encoding="utf-8")
    monkeypatch.setattr(audit.data, "load_cache", lambda *_: SimpleNamespace(sha256="wrong"))
    with pytest.raises(ValueError, match="snapshot hash mismatch"):
        audit.load_reference(reference)


def test_audit_refuses_output_outside_authorized_ablation_tree(tmp_path):
    with pytest.raises(ValueError, match="must stay within"):
        audit.run_audit(tmp_path, tmp_path, [], {})
    assert list(tmp_path.iterdir()) == []


def test_fit_observer_is_removed_on_engine_failure():
    original = lambda *args: None

    def failing(*args):
        raise RuntimeError("synthetic failure")

    engine = SimpleNamespace(_fit_model=original, walk_forward=failing)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        audit.run_engine(engine, [], {})
    assert engine._fit_model is original

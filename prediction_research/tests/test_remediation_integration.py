import csv
import json
from pathlib import Path

import pytest

from prediction_research.archives import ArchiveError, verify_archive
from prediction_research.cli import main, parser
from prediction_research.cycle import candidate_history_status, run_cycle
from prediction_research.data import load_cache
from prediction_research.pipeline import _validation_status
from prediction_research.tests.test_evidence import cfg, write_histories
from prediction_research.tests.test_cycle_remediation import snapshot, flow_row


def test_default_expands_quantitative_coverage_without_expanding_llm(cfg):
    assert cfg["workflow"]["prediction_top_n"] == 20
    assert parser().parse_args(["cycle"]).top is None
    assert parser().parse_args(["cycle", "--top", "5"]).top == 5
    assert parser().parse_args(["run-agents"]).top == 5


def test_missing_one_history_does_not_block_two_ready_candidates(cfg, monkeypatch):
    write_histories(cfg)
    missing = {**flow_row("515220"), "name": "煤炭ETF测试"}
    snapshot(cfg, [flow_row(), flow_row("159934"), missing])
    monkeypatch.setattr("prediction_research.cycle.now_utc", lambda: "2024-08-17T12:00:00+00:00")
    result = run_cycle(cfg, 3, True)
    assert result["data_readiness"] == "ready_with_exclusions"
    assert result["prediction_eligible_symbols"] == ["159934", "518880"]
    assert len(result["prediction_ids"]) == 2
    assert any(row.get("symbol") == "515220" for row in result["prediction_exclusions"])
    assert result["archive"]["status"] == "complete"
    assert verify_archive(result["archive"]["archive_path"])["complete"]


def test_archive_failure_is_persisted_as_operational_failure(cfg, monkeypatch):
    write_histories(cfg)
    snapshot(cfg)
    monkeypatch.setattr("prediction_research.cycle.now_utc", lambda: "2024-08-17T12:00:00+00:00")
    def fail(*args):
        raise ArchiveError("disk failure")
    monkeypatch.setattr("prediction_research.archives.archive_cycle", fail)
    result = run_cycle(cfg, 2, True)
    saved = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))
    assert saved["archive"]["status"] == "failed"
    assert saved["outcome"] == "partial_failure"
    assert len(saved["prediction_ids"]) == 2


def test_archive_cli_reuses_runtime_candidate_config(cfg, monkeypatch):
    write_histories(cfg)
    snapshot(cfg)
    monkeypatch.setattr("prediction_research.cycle.now_utc", lambda: "2024-08-17T12:00:00+00:00")
    result = run_cycle(cfg, 2, True)
    monkeypatch.setattr("prediction_research.cli.load_config", lambda _: cfg.copy())
    assert main(["archive-cycle", result["result_path"]]) == 0
    assert main(["verify-archive", result["archive"]["archive_path"]]) == 0


def test_loader_accepts_feature_minimum_but_rejects_nonfinite_bars(tmp_path):
    path = tmp_path / "cache_A.csv"
    from datetime import date, timedelta
    fields = ["date", "open", "high", "low", "close", "volume", "amount"]
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        for day in range(61):
            writer.writerow([date(2024, 1, 1) + timedelta(days=day), 1, 1.1, 0.9, 1, 100, 100])
        writer.writerow(["2024-06-01", "nan", 2, 1, 1, 1, 1])
        writer.writerow(["2024-06-02", 1, 2, 1, "inf", 1, 1])
    assert len(load_cache("A", tmp_path).bars) == 61


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf")])
def test_invalid_validation_metrics_cannot_pass_gate(cfg, tmp_path, bad):
    path = tmp_path / "backtest.json"
    path.write_text(json.dumps({"universe": "commodity", "horizon": 5, "feature_set": "base",
                                "model_scope": "pooled", "model_version": "test",
                                "metrics": {"rows": 2000, "brier": bad, "historical_rate_brier": 0.25}}))
    status = _validation_status(cfg, "commodity", 5, "base", "pooled", "test", path)
    assert not status["passed"]
    assert "nonfinite" in status["reason"]


def test_backtest_snapshot_change_invalidates_probability_gate(cfg, tmp_path):
    path = tmp_path / "bt.json"
    path.write_text(json.dumps({"universe": "commodity", "horizon": 5, "feature_set": "base",
        "model_scope": "pooled", "model_version": "test", "snapshots": {"A": {"sha256": "old"}},
        "metrics": {"rows": 2000, "unique_dates": 200, "brier": 0.2, "historical_rate_brier": 0.25}}))
    result = _validation_status(cfg, "commodity", 5, "base", "pooled", "test", path, {"A": "changed"})
    assert not result["passed"] and "snapshots differ" in result["reason"]


def test_report_uses_same_date_quality_gate(cfg, tmp_path):
    from prediction_research.reporting import build_research_report
    path = tmp_path / "bt.json"
    path.write_text(json.dumps({"metrics": {"rows": 2000, "unique_dates": 2, "brier": 0.2, "historical_rate_brier": 0.25}}))
    report = build_research_report(cfg, {"artifacts": {"backtest_5d": str(path)}})
    assert "| 5日 | 2000 | 0.2 | 0.25 | 未通过 |" in report.read_text(encoding="utf-8")


def test_feature_history_without_mature_training_labels_waits(cfg, monkeypatch):
    write_histories(cfg)
    for path in (Path(cfg["_project_dir"]) / cfg["market_data_dir"]).glob("cache_*.csv"):
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[:1] + lines[-61:]) + "\n", encoding="utf-8")
    snapshot(cfg)
    monkeypatch.setattr("prediction_research.cycle.now_utc", lambda: "2024-08-17T12:00:00+00:00")
    result = run_cycle(cfg, 2, True)
    assert result["data_readiness"] == "ready"
    assert not result.get("prediction_ids")
    assert any(row["name"] == "training_readiness_5d" and row["status"] == "waiting_for_data" for row in result["steps"])
    assert result["outcome"] == "complete_with_data_waits"

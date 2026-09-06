from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from prediction_research.cycle import run_cycle
from prediction_research.evidence import utc
from prediction_research.flow_backtest import reconstruct_screens, run_flow_backtest
from prediction_research.flow_context import flow_context
from prediction_research.pipeline import run_prediction, settle_predictions
from prediction_research.reporting import build_research_report
from prediction_research.screening import ingest_etf_snapshot, rule_identity, screen_etfs, select_etfs
from prediction_research.settlement import decision_outcome, pending_history_assets
from prediction_research.store import connect, frozen_prediction, insert_prediction
from prediction_research.order_divergence import order_divergence
from prediction_research.etf_evidence import sync_etf_evidence
from prediction_research.evidence import load_records
from prediction_research.tests.test_core import synthetic
from prediction_research.tests.test_evidence import cfg, write_histories


def flow_row(symbol="518880", net=10000000.0, pct=10.0, epoch=None):
    return {"symbol": symbol, "exchange": "SSE", "name": "黄金ETF测试", "price": 1.0,
            "change_pct": 2.0, "amount": 100000000.0, "market_cap": 1000000000.0,
            "quote_epoch": epoch or int(utc("2024-08-17T09:00:00Z").timestamp()),
            "main_net_inflow": net, "main_net_inflow_pct": pct}


def snapshot(cfg, rows=None, received="2024-08-17T09:10:00Z", sha="a" * 64):
    directory = Path(cfg["_project_dir"]) / cfg["etf_market"]["snapshot_dir"]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"snapshot_{sha}.json"
    rows = rows if rows is not None else [flow_row(), flow_row("159934", -10000000, -10)]
    path.write_text(json.dumps({"records": rows, "flow_definition": "synthetic test only"}), encoding="utf-8")
    manifest = {"path": str(path), "sha256": sha, "retrieved_at_utc": received, "records": len(rows)}
    (directory / "latest.json").write_text(json.dumps(manifest), encoding="utf-8")
    ingest_etf_snapshot(cfg, manifest)
    cfg["etf_market"]["screen"]["max_per_group"] = 2
    return manifest


def prediction(**kwargs):
    return {"symbol": "518880", "feature_date": "2024-08-17", "horizon": 5,
            "probability_up": 0.6, "probability_status": "research_only_failed_validation", "model_version": "test-v2",
            "data_snapshot": {}, "evidence": [{"original": True}], "decision_at_utc": "2024-08-17T12:00:00Z", **kwargs}


def test_frozen_duplicate_returns_original_probability_evidence_and_time(cfg):
    with connect(Path(cfg["_project_dir"]) / cfg["state_db"]) as connection:
        first = frozen_prediction(connection, prediction())
        second = frozen_prediction(connection, prediction(probability_up=0.2, evidence=[{"later": True}],
                                                           decision_at_utc="2024-08-18T12:00:00Z"))
    assert first["inserted"] and not second["inserted"]
    assert {k: v for k, v in first.items() if k != "inserted"} == {k: v for k, v in second.items() if k != "inserted"}


def test_duplicate_freeze_releases_write_transaction_for_other_connection(cfg):
    db_path = Path(cfg["_project_dir"]) / cfg["state_db"]
    with connect(db_path) as connection:
        frozen_prediction(connection, prediction())
        duplicate = frozen_prediction(connection, prediction(probability_up=0.2))
        with connect(db_path) as reader:
            assert reader.execute("SELECT count(*) FROM predictions").fetchone()[0] == 1
        another = frozen_prediction(connection, prediction(symbol="159934"))
    assert not duplicate["inserted"] and another["inserted"]


def test_legacy_duplicate_never_acquires_new_metadata(cfg):
    with connect(Path(cfg["_project_dir"]) / cfg["state_db"]) as connection:
        insert_prediction(connection, prediction())
        result = frozen_prediction(connection, prediction(probability_up=0.2, name="new fake name"))
    assert result["freeze_schema_version"] == 1
    assert result["probability_up"] == 0.6
    assert "name" not in result and "decision_at_utc" not in result


def test_snapshot_availability_boundary_and_flow_window_missing(cfg):
    manifest = snapshot(cfg)
    screen = {"source_snapshot": manifest, "selected": [flow_row()]}
    early = flow_context(cfg, "518880", screen, utc("2024-08-17T09:09:59Z"))
    on_time = flow_context(cfg, "518880", screen, utc("2024-08-17T09:10:00Z"))
    assert early["status"] == "unavailable_at_decision" and early["main_net_inflow"] is None
    assert on_time["status"] == "available" and on_time["main_net_inflow"] == 10000000
    assert on_time["history_windows"]["5"]["net_inflow_sum"] is None
    assert on_time["history_windows"]["5"]["observed_days"] == 1
    assert on_time["used_for_probability"] is False


def test_missing_flow_excluded_but_zero_allowed(cfg):
    eligible, _ = select_etfs([flow_row(pct=None), flow_row("159934", net=0, pct=0)], cfg["etf_market"]["screen"])
    assert [row["symbol"] for row in eligible] == ["159934"]


def test_super_large_vs_large_divergence_is_separate_screen_factor(cfg):
    rules = cfg["etf_market"]["screen"]
    bullish = flow_row() | {"super_large_net_inflow": 20_000_000,
                            "super_large_net_inflow_pct": 8.0,
                            "large_net_inflow": -10_000_000, "large_net_inflow_pct": -4.0}
    bearish = flow_row("159934") | {"super_large_net_inflow": -20_000_000,
                                    "super_large_net_inflow_pct": -8.0,
                                    "large_net_inflow": 10_000_000, "large_net_inflow_pct": 4.0}
    assert order_divergence(bullish, rules)["signal"] == "bullish_divergence"
    assert order_divergence(bearish, rules)["signal"] == "bearish_divergence"
    eligible, _ = select_etfs([bullish, bearish], rules, 2)
    by_symbol = {row["symbol"]: row for row in eligible}
    assert by_symbol["518880"]["screen_components"]["order_divergence"] > 0
    assert by_symbol["159934"]["screen_components"]["order_divergence"] < 0
    assert by_symbol["518880"]["screen_contributions"]["order_divergence"] > 0


def test_missing_divergence_is_not_zero_and_preserves_original_score_weights(cfg):
    row = flow_row()
    eligible, _ = select_etfs([row], cfg["etf_market"]["screen"])
    assert eligible[0]["order_divergence"]["status"] == "missing_data"
    assert eligible[0]["screen_components"]["order_divergence"] is None
    expected = 0.50 * (10 / 30) + 0.35 * 0.5 + 0.15 * 0.2
    assert eligible[0]["screen_score"] == pytest.approx(expected)


def test_divergence_is_saved_as_standard_evidence(cfg):
    row = flow_row() | {"super_large_net_inflow": 20_000_000,
                        "super_large_net_inflow_pct": 8.0,
                        "large_net_inflow": -10_000_000, "large_net_inflow_pct": -4.0}
    snapshot(cfg, [row])
    result = sync_etf_evidence(cfg)
    evidence = [item for item in load_records(cfg) if item["source"] == "etf_order_divergence"]
    assert result["order_divergence_records"] == 1
    assert len(evidence) == 1
    assert evidence[0]["kind"] == "anomaly"
    assert evidence[0]["direction"] == "up"
    assert evidence[0]["metrics"]["signal"] == "bullish_divergence"


def test_future_screen_rejected_and_same_day_selection_immutable(cfg):
    snapshot(cfg)
    with pytest.raises(ValueError, match="unavailable"):
        screen_etfs(cfg, decision_at_utc="2024-08-17T09:00:00Z")
    _, first = screen_etfs(cfg, decision_at_utc="2024-08-17T12:00:00Z")
    snapshot(cfg, [flow_row(net=30000000, pct=30)], received="2024-08-17T12:10:00Z", sha="b" * 64)
    _, second = screen_etfs(cfg, decision_at_utc="2024-08-17T13:00:00Z")
    assert first["selected"] == second["selected"]
    assert second["reused_frozen_screen"]
    assert second["source_snapshot"]["sha256"] == "a" * 64


def test_flow_observation_window_deduplicates_same_day(cfg):
    for idx in range(5):
        stamp = utc("2024-08-10T09:00:00Z") + timedelta(days=idx)
        snapshot(cfg, [flow_row(net=100, epoch=int(stamp.timestamp()))], (stamp + timedelta(minutes=10)).isoformat(), f"{idx:064x}")
    stamp = utc("2024-08-14T09:00:00Z")
    snapshot(cfg, [flow_row(net=200, epoch=int(stamp.timestamp()))], (stamp + timedelta(minutes=20)).isoformat(), "f" * 64)
    result = flow_context(cfg, "518880", None, utc("2024-08-15T00:00:00Z"))
    assert result["history_windows"]["5"]["net_inflow_sum"] == 600
    assert result["history_windows"]["20"]["net_inflow_sum"] is None


def test_actual_decision_uses_later_open(cfg):
    bars = synthetic("518880", 90)
    feature = bars.bars[60].trading_date.isoformat()
    decision_day = bars.bars[62].trading_date.isoformat()
    result = decision_outcome(cfg, bars, feature, 5, decision_day + "T10:00:00+08:00")
    assert result["entry_date"] == bars.bars[63].trading_date.isoformat()
    assert result["actual_return"] == pytest.approx(bars.bars[67].close / bars.bars[63].open - 1)


def test_removed_candidate_still_tracked_and_settled(cfg):
    write_histories(cfg)
    with connect(Path(cfg["_project_dir"]) / cfg["state_db"]) as connection:
        frozen_prediction(connection, prediction(feature_date="2024-04-01", decision_at_utc="2024-04-01T10:00:00Z"))
    cfg["universes"] = {"commodity": []}
    assert pending_history_assets(cfg, []) == [{"symbol": "518880"}]
    assert settle_predictions(cfg)["settled"] == 1
    assert settle_predictions(cfg)["settled"] == 0


def test_replay_excludes_late_snapshots_and_future_changes(cfg):
    snapshot(cfg, received="2024-08-18T09:00:00Z")
    result = reconstruct_screens(cfg)
    assert result["observed_days"] == 1 and result["timely_screen_days"] == 0
    assert result["late_snapshots_excluded"] == 1
    snapshot(cfg, received="2024-08-17T09:30:00Z", sha="b" * 64)
    first = reconstruct_screens(cfg)["cohorts"]
    stamp = utc("2024-08-19T09:00:00Z")
    snapshot(cfg, [flow_row(net=999999999, epoch=int(stamp.timestamp()))], "2024-08-19T09:30:00Z", "c" * 64)
    second = reconstruct_screens(cfg)["cohorts"]
    assert first[0] == second[0]


def test_replay_uses_actual_frozen_decision_for_after_close_collection(cfg):
    snapshot(cfg, received="2024-08-18T09:00:00Z")
    assert reconstruct_screens(cfg)["timely_screen_days"] == 0
    screen_etfs(cfg, decision_at_utc="2024-08-18T12:00:00Z")
    rebuilt = reconstruct_screens(cfg)
    assert rebuilt["timely_screen_days"] == 1
    assert rebuilt["cohorts"][0]["decision_basis"] == "frozen_live_decision"
    assert rebuilt["cohorts"][0]["decision_at_utc"] == "2024-08-18T12:00:00+00:00"


def test_cohort_costs_and_missing_prices_are_not_silently_dropped(cfg):
    write_histories(cfg)
    stamp = utc("2024-06-15T09:00:00Z")
    snapshot(cfg, [flow_row(epoch=int(stamp.timestamp())), flow_row("159934", epoch=int(stamp.timestamp()))], "2024-06-15T09:30:00Z")
    identity = rule_identity(cfg["etf_market"]["screen"], cfg["etf_market"]["screen"]["top_n"])
    with connect(Path(cfg["_project_dir"]) / cfg["state_db"]) as connection:
        connection.execute("INSERT INTO screen_rule_versions VALUES (?, ?, ?)", (identity, "2024-01-01T00:00:00Z", "{}"))
        connection.commit()
    cfg["evidence"]["minimum_observation_days"] = 1
    _, result = run_flow_backtest(cfg)
    assert result["status"] == "evaluated_research_only"
    assert result["probability_metrics_after_historical_screen"]["5"]["rows"] == 2
    for item in result["cohorts"]:
        assert item["status"] == "paired"
        assert item["returns"]["money_flow"]["gross_return"] - item["returns"]["money_flow"]["net_return"] == pytest.approx(0.001)
    snapshot(cfg, [flow_row("159999", epoch=int(stamp.timestamp()))], "2024-06-15T09:40:00Z", "b" * 64)
    _, missing = run_flow_backtest(cfg)
    assert missing["metrics"] == {}
    assert "159999" in missing["missing_histories"]


def test_report_uses_only_this_runs_ids_and_renders_more_than_six(cfg, tmp_path):
    with connect(Path(cfg["_project_dir"]) / cfg["state_db"]) as connection:
        rows = [frozen_prediction(connection, prediction(symbol=f"5188{idx:02d}")) for idx in range(8)]
        unrelated = frozen_prediction(connection, prediction(symbol="599999"))
    artifact = tmp_path / "predictions.json"
    artifact.write_text(json.dumps({"predictions": rows}), encoding="utf-8")
    report = build_research_report(cfg, {"cycle_id": "test", "outcome": "complete", "artifacts": {"predict_5d": str(artifact)}})
    text = report.read_text(encoding="utf-8")
    assert all(row["prediction_id"] in text for row in rows)
    assert unrelated["prediction_id"] not in text
    assert "数据不足" in text and "参与概率计算：否" in text


def test_cycle_failure_does_not_read_old_screen_and_still_reports(cfg, monkeypatch):
    snapshot(cfg)
    screen_etfs(cfg, decision_at_utc="2024-08-17T12:00:00Z")
    def fail(*args):
        raise RuntimeError("simulated network failure")
    monkeypatch.setattr("prediction_research.cycle.fetch_etf_snapshot", fail)
    monkeypatch.setattr("prediction_research.cycle.fetch_news", lambda _: {})
    monkeypatch.setattr("prediction_research.cycle.fetch_universe", lambda *args: {"failures": {}})
    monkeypatch.setattr("prediction_research.cycle.run_prediction", lambda *args: pytest.fail("must not predict from old screen"))
    result = run_cycle(cfg)
    assert result["outcome"] == "partial_failure"
    assert "screen" not in result["artifacts"] and "prediction_ids" not in result
    assert Path(result["result_path"]).exists()
    assert Path(result["artifacts"]["report"]).exists()
    assert any(row["name"] == "settle_predictions" for row in result["steps"])


def test_cached_cycle_returns_waiting_and_does_not_mutate_config(cfg, monkeypatch):
    write_histories(cfg)
    snapshot(cfg)
    monkeypatch.setattr("prediction_research.cycle.now_utc", lambda: "2024-08-17T12:00:00+00:00")
    result = run_cycle(cfg, 2, True)
    assert result["outcome"] == "complete_with_data_waits"
    assert len(result["prediction_ids"]) == 2
    assert "screened_current" not in cfg["universes"]
    report = Path(result["artifacts"]["report"]).read_text(encoding="utf-8")
    assert "净流出" in report and "首次可得" in report and "本轮模型验证" in report


@pytest.mark.parametrize("failure", ["screen", "empty", "histories", "report"])
def test_cycle_failure_matrix_persists_outcome(cfg, monkeypatch, failure):
    write_histories(cfg)
    snapshot(cfg, rows=[] if failure == "empty" else None)
    monkeypatch.setattr("prediction_research.cycle.now_utc", lambda: "2024-08-17T12:00:00+00:00")
    def fail(*args, **kwargs):
        raise RuntimeError("simulated failure")
    if failure == "screen":
        monkeypatch.setattr("prediction_research.cycle.screen_etfs", fail)
    elif failure == "histories":
        monkeypatch.setattr("prediction_research.cycle.candidate_history_status", lambda *args: {"status": "waiting_for_history", "issues": [{"reason": "missing_history"}]})
    elif failure == "report":
        monkeypatch.setattr("prediction_research.cycle.build_research_report", fail)
    result = run_cycle(cfg, 2, True)
    assert result["outcome"] == "partial_failure"
    saved = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))
    assert saved["outcome"] == "partial_failure"
    if failure != "report":
        assert not result.get("prediction_ids")
        assert "report" in result["artifacts"]


def test_cycle_cli_failure_has_nonzero_exit(monkeypatch):
    from prediction_research.cli import main

    monkeypatch.setattr("prediction_research.cycle.run_cycle", lambda *args: {"outcome": "partial_failure"})
    assert main(["cycle", "--skip-fetch"]) == 2


def test_report_validation_gate_requires_minimum_rows(cfg, tmp_path):
    path = tmp_path / "bt.json"
    path.write_text(json.dumps({"metrics": {"rows": 10, "brier": 0.1, "historical_rate_brier": 0.2}}), encoding="utf-8")
    report = build_research_report(cfg, {"artifacts": {"backtest_5d": str(path)}})
    assert "| 5日 | 10 | 0.1 | 0.2 | 未通过 |" in report.read_text(encoding="utf-8")


def test_backtest_binding_rejects_wrong_universe(cfg, tmp_path):
    from prediction_research.pipeline import _validation_status

    path = tmp_path / "wrong.json"
    path.write_text(json.dumps({"universe": "wrong", "metrics": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        _validation_status(cfg, "commodity", 5, "base", "pooled", "test", path)


def test_late_coarse_flow_cannot_bypass_standard_evidence_cutoff(cfg):
    write_histories(cfg)
    snapshot(cfg, received="2024-08-17T11:00:00Z")
    screen = screen_etfs(cfg, decision_at_utc="2024-08-17T12:00:00Z")
    cfg["universes"]["screened_current"] = cfg["universes"]["commodity"]
    _, result = run_prediction(cfg, "screened_current", 5, "base", screen_context=screen)
    for row in result["predictions"]:
        assert row["fund_flow"]["status"] == "unavailable_at_decision"
        assert row["fund_flow"]["main_net_inflow"] is None
        assert not any(e["type"] == "money_flow_coarse_screen" for e in row["evidence"])


def test_failed_live_history_refresh_cannot_use_fresh_old_cache(cfg, monkeypatch):
    write_histories(cfg)
    manifest = snapshot(cfg)
    monkeypatch.setattr("prediction_research.cycle.now_utc", lambda: "2024-08-17T12:00:00+00:00")
    monkeypatch.setattr("prediction_research.cycle.fetch_etf_snapshot", lambda *args: manifest)
    monkeypatch.setattr("prediction_research.cycle.fetch_news", lambda *args: {})
    def fail(*args):
        raise RuntimeError("simulated failed price refresh")
    monkeypatch.setattr("prediction_research.cycle.fetch_universe", fail)
    monkeypatch.setattr("prediction_research.cycle.run_prediction", lambda *args: pytest.fail("cannot reuse cache after failed live refresh"))
    result = run_cycle(cfg, 2, False)
    assert result["outcome"] == "partial_failure"
    assert result["data_readiness"] == "waiting_for_history"
    assert not result.get("prediction_ids")


def test_status_observation_count_does_not_imply_strategy_ready(cfg):
    from prediction_research.workflow import workflow_status

    for idx in range(60):
        stamp = utc("2024-06-01T09:00:00Z") + timedelta(days=idx)
        snapshot(cfg, [flow_row(epoch=int(stamp.timestamp()))], stamp.isoformat(), f"{idx:064x}")
    stage = workflow_status(cfg)["stages"][2]
    assert stage["historical_observation_days"] == 60
    assert not stage["historical_screen_validation_ready"]
    runs = Path(cfg["_project_dir"]) / cfg["runs_dir"]
    runs.mkdir(parents=True, exist_ok=True)
    strategy_path = runs / "flow_strategy_test.json"
    strategy_path.write_text(json.dumps({"status": "waiting_for_history_or_prices"}), encoding="utf-8")
    (runs / "cycle_test.json").write_text(json.dumps({"outcome": "complete_with_data_waits", "artifacts": {"flow_strategy": str(strategy_path)}}), encoding="utf-8")
    assert not workflow_status(cfg)["stages"][2]["historical_screen_validation_ready"]
    strategy_path.write_text(json.dumps({"status": "evaluated_research_only"}), encoding="utf-8")
    assert workflow_status(cfg)["stages"][2]["historical_screen_validation_ready"]

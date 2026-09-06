from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from prediction_research.config import load_config
from prediction_research.etf_evidence import etf_profiles, sync_etf_evidence, sync_news_chains
from prediction_research.etf_integration import run_etf_integration
from prediction_research.evidence import (
    cutoff_utc,
    digest,
    eligible_records,
    import_evidence,
    load_records,
    save_records,
    utc,
    validate_record,
)
from prediction_research.evidence_experiments import group_features, run_evidence_experiments, training_observation_days
from prediction_research.features import Sample
from prediction_research.store import connect, upsert_event
from prediction_research.tests.test_core import synthetic


@pytest.fixture
def cfg(tmp_path):
    config = load_config()
    config["_project_dir"] = str(tmp_path)
    config["universes"] = {"commodity": [
        {"symbol": "518880", "name": "测试黄金ETF", "asset_type": "commodity_spot_etf", "exposure": "gold"},
        {"symbol": "159934", "name": "测试黄金ETF2", "asset_type": "commodity_spot_etf", "exposure": "gold"},
    ]}
    config["horizons"] = [5]
    config["model"].update(min_train_dates=60, max_train_dates=130, iterations=5,
                           min_calibration_rows=20, test_window_dates=20)
    config["evidence"].update(minimum_observation_days=2, minimum_test_rows=5,
                              minimum_train_observation_days=5)
    return config


def record(**changes):
    row = {"source": "fingenius", "source_key": "synthetic-test-only", "symbol": "518880", "kind": "analysis",
           "epistemic": "inference", "claim": "合成测试数据，不是真实研究结果", "source_ref": "https://example.test/report",
           "snapshot_sha256": "a" * 64, "observed_at_utc": "2024-04-01T09:00:00+00:00",
           "available_at_utc": "2024-04-01T10:00:00+00:00", "score": 0.5, "direction": "unknown"}
    return {**row, **changes}


def write_histories(cfg):
    from pathlib import Path

    directory = Path(cfg["_project_dir"]) / cfg["market_data_dir"]
    directory.mkdir(parents=True)
    for symbol in ("518880", "159934"):
        with (directory / f"cache_{symbol}.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("date", "open", "high", "low", "close", "volume", "amount"))
            for bar in synthetic(symbol, 230).bars:
                writer.writerow((bar.trading_date, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.amount))


@pytest.mark.parametrize("changes", [
    {"symbol": "600519"}, {"observed_at_utc": "2024-04-01T09:00:00"},
    {"available_at_utc": "2024-03-01T00:00:00+00:00"}, {"score": float("nan")},
    {"score": 2}, {"probability_up": 0.8}, {"snapshot_sha256": "bad"},
    {"published_at_utc": "2025-01-01T00:00:00+00:00"},
])
def test_invalid_or_stock_evidence_rejected(cfg, changes):
    with pytest.raises(ValueError):
        validate_record(cfg, record(**changes))


def test_import_receipt_prevents_backdated_llm_leakage_and_is_idempotent(cfg, tmp_path, monkeypatch):
    path = tmp_path / "input.json"
    path.write_text(json.dumps({"schema_version": 1, "records": [record()]}), encoding="utf-8")
    monkeypatch.setattr("prediction_research.evidence.now_utc", lambda: "2026-09-06T00:00:00+00:00")
    assert import_evidence(cfg, path)["inserted"] == 1
    monkeypatch.setattr("prediction_research.evidence.now_utc", lambda: "2026-09-07T00:00:00+00:00")
    assert import_evidence(cfg, path)["inserted"] == 0
    rows = load_records(cfg)
    assert rows[0]["available_at_utc"] == "2026-09-06T00:00:00+00:00"
    assert not eligible_records(cfg, rows, "518880", utc("2024-04-02T00:00:00Z"))


def test_batch_validation_and_immutable_identity(cfg):
    with pytest.raises(ValueError):
        save_records(cfg, [record(), record(symbol="600519")])
    assert load_records(cfg) == []
    save_records(cfg, [record()])
    with pytest.raises(ValueError, match="conflict"):
        save_records(cfg, [record(score=-0.5)])
    assert load_records(cfg)[0]["score"] == 0.5


def test_after_cutoff_and_stale_evidence_excluded(cfg):
    rows = [validate_record(cfg, record(available_at_utc="2024-04-01T10:00:01+00:00"))]
    cutoff = cutoff_utc(cfg, date(2024, 4, 1))
    assert cutoff.hour == 10
    assert not eligible_records(cfg, rows, "518880", cutoff)
    assert eligible_records(cfg, rows, "518880", cutoff + timedelta(seconds=1))
    assert not eligible_records(cfg, rows, "518880", cutoff + timedelta(days=15))
    old_article = validate_record(cfg, record(published_at_utc="2024-01-01T00:00:00Z"))
    assert not eligible_records(cfg, [old_article], "518880", cutoff)


def test_news_mapping_cannot_backfill_and_reprints_deduplicate(cfg, monkeypatch):
    from pathlib import Path

    event = {"canonical_hash": digest("gold news"), "title": "黄金供应合成新闻", "summary": "测试",
             "event_type": "news", "reliability_score": 0.9, "relevance_score": 0.8,
             "directness_score": 0.8, "evidence_score": 0.8, "exposures": ["gold"],
             "evidence_status": "source_fact_unreviewed_impact"}
    source = {"source_id": "test", "source_url": "https://example.test", "article_url": "https://example.test/a",
              "published_at": "Mon, 01 Apr 2024 00:00:00 GMT", "fetched_at_utc": "2024-04-01T10:00:00+00:00",
              "raw_snapshot_path": "test.xml", "raw_snapshot_sha256": "b" * 64}
    with connect(Path(cfg["_project_dir"]) / cfg["state_db"]) as connection:
        upsert_event(connection, event, source)
        upsert_event(connection, event, {**source, "source_id": "reprint"})
    monkeypatch.setattr("prediction_research.etf_evidence.now_utc", lambda: "2024-04-02T12:00:00+00:00")
    first = sync_news_chains(cfg)
    assert first["inserted"] == 2
    assert sync_news_chains(cfg)["inserted"] == 0
    rows = load_records(cfg)
    assert all(row["available_at_utc"] == "2024-04-02T12:00:00+00:00" for row in rows)
    assert not eligible_records(cfg, rows, "518880", utc("2024-04-01T12:00:00Z"))
    assert rows[0]["direction"] == "unknown"
    assert [node["type"] for node in rows[0]["chain"]] == ["news", "commodity_or_sector", "etf"]


def test_flow_snapshots_not_actor_identity_or_duplicate_days(cfg):
    from pathlib import Path

    with connect(Path(cfg["_project_dir"]) / cfg["state_db"]) as connection:
        for sha, pct in (("a" * 64, 25.0), ("b" * 64, None)):
            connection.execute("INSERT INTO etf_flow_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (sha, "518880", "SH", "测试ETF", 1711969200, "2024-04-01T12:00:00+00:00",
                                "commodity", "spot", "domestic", 1.0, 1.0, 100, 1000, None, pct))
        connection.commit()
    assert sync_etf_evidence(cfg)["inserted"] == 2
    assert sync_etf_evidence(cfg)["inserted"] == 0
    profiles = etf_profiles(cfg, utc("2024-04-02T00:00:00Z"))["profiles"]
    profile = next(row for row in profiles if row["symbol"] == "518880")
    assert profile["observed_days"] == 1
    assert profile["net_inflow_sum"] is None
    assert profile["actor_identity"] == "unobservable"


def test_revised_news_does_not_increase_independent_count(cfg):
    a = validate_record(cfg, record(source="news_chain", source_key="v1", independence_key="same-news"))
    b = validate_record(cfg, record(source="news_chain", source_key="v2", independence_key="same-news"))
    sample = Sample("518880", date(2024, 4, 2), None, (0.0,), None, None)
    assert group_features(cfg, [a], sample, "news_chain") == group_features(cfg, [a, b], sample, "news_chain")


def test_carrying_one_observation_across_samples_is_still_one_training_day(cfg):
    samples = [Sample("518880", date(2024, 4, 1) + timedelta(days=idx),
                      date(2024, 4, 6) + timedelta(days=idx), (0.0,), 0.1, 1) for idx in range(10)]
    fold = {"train_start": "2024-04-01", "test_start": "2024-05-01"}
    assert training_observation_days(cfg, [validate_record(cfg, record())], samples, fold) == 1


def test_real_engine_ablation_pairs_and_cache_on_synthetic_data(cfg, monkeypatch):
    write_histories(cfg)
    rows = []
    for idx in range(140):
        observed = datetime(2024, 2, 25, 9, tzinfo=timezone.utc) + timedelta(days=idx)
        for symbol in ("518880", "159934"):
            rows.append(record(source_key=f"synthetic-{idx}", symbol=symbol, observed_at_utc=observed.isoformat(),
                               available_at_utc=observed.isoformat(), score=0.5 if idx % 2 else -0.5))
    save_records(cfg, rows)
    path, result = run_evidence_experiments(cfg, "commodity", 5)
    variant = next(row for row in result["variants"] if row["variant"] == "base+fingenius")
    assert variant["status"] == "evaluated_research_only"
    assert variant["metrics"]["rows"] == variant["baseline_metrics"]["rows"] > 5
    assert variant["promotion"].startswith("disabled")
    for fold in result["folds"]:
        assert fold["latest_observed_target"] < fold["test_start"]
    monkeypatch.setattr("prediction_research.evidence_experiments.walk_forward",
                        lambda *args: pytest.fail("unchanged input should reuse its experiment"))
    cached_path, cached = run_evidence_experiments(cfg, "commodity", 5)
    assert cached_path == path and cached == json.loads(path.read_text(encoding="utf-8"))


def test_stage_runner_continues_through_data_waits_and_writes_markdown(cfg):
    from pathlib import Path

    write_histories(cfg)
    result = run_etf_integration(cfg)
    assert result["outcome"] == "complete_with_data_waits"
    assert [row["stage"] for row in result["stages"]] == list(range(1, 7))
    assert result["stages"][4]["status"] == "waiting_for_evidence_or_history"
    for row in result["stages"]:
        assert Path(row["markdown_path"]).exists()


def test_stage_failure_blocks_dependents_but_keeps_status_report(cfg, monkeypatch):
    def failure(_):
        raise ValueError("secret-not-for-reports")

    monkeypatch.setattr("prediction_research.etf_integration.sync_etf_evidence", failure)
    result = run_etf_integration(cfg)
    assert result["outcome"] == "partial_failure"
    assert result["stages"][2]["status"] == "blocked_dependency"
    assert result["stages"][4]["status"] == "blocked_dependency"
    assert result["stages"][5]["status"] == "complete"
    assert "secret-not-for-reports" not in json.dumps(result)


def test_prediction_freezes_only_evidence_available_at_its_cutoff(cfg):
    from prediction_research.pipeline import run_prediction

    write_histories(cfg)
    day = synthetic("518880", 230).bars[-1].trading_date.isoformat()
    timely = record(source_key="timely", observed_at_utc=f"{day}T09:00:00+00:00",
                    available_at_utc=f"{day}T10:00:00+00:00")
    late = record(source_key="late", observed_at_utc=f"{day}T09:00:00+00:00",
                  available_at_utc=f"{day}T10:00:01+00:00")
    save_records(cfg, [timely, late])
    _, payload = run_prediction(cfg, "commodity", 5, "base")
    item = next(row for row in payload["predictions"] if row["symbol"] == "518880")
    frozen = next(row for row in item["evidence"] if row["type"] == "standard_etf_evidence")
    assert frozen["evidence_ids"] == [validate_record(cfg, timely)["evidence_id"]]
    assert frozen["probability_effect"] == "none_pending_independent_validation"
    assert item["probability_status"] == "research_only_failed_validation"

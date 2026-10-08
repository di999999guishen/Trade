from __future__ import annotations

import json
import csv
import hashlib
import os
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from prediction_research.data import Bar, SeriesSnapshot
from prediction_research.config import market_data_dir
from prediction_research.evidence import utc
from prediction_research.opportunity import build_opportunity_observation, classify_observation
from prediction_research.reporting import build_research_report
from prediction_research.screening import rule_identity, screen_etfs, select_etfs
from prediction_research.store import connect, frozen_prediction
from prediction_research.tests.test_cycle_remediation import flow_row, prediction, snapshot
from prediction_research.tests.test_evidence import cfg


DECISION = "2024-08-17T12:00:00Z"
RECEIVED = "2024-08-17T10:00:00Z"


def history(closes=None, end=date(2024, 8, 17)):
    closes = closes if closes is not None else [100 + idx * 0.4 for idx in range(80)]
    bars = tuple(Bar(end - timedelta(days=len(closes) - idx - 1), value, value + 1, value - 1,
                     value, 1000, 100000) for idx, value in enumerate(closes))
    return SeriesSnapshot("518880", bars, "synthetic.csv", "a" * 64)


def classify(cfg, row=None, prices=None, **kwargs):
    return classify_observation(row or flow_row(), DECISION, cfg, prices or history(),
                                kwargs.get("available", RECEIVED), kwargs.get("basis", "recorded_receipt"))


def test_single_day_gives_observation_not_invented_trend_or_hold(cfg):
    result = classify_observation(flow_row(), DECISION, cfg)
    assert result["snapshot_category"] == "inflow_price_confirmation"
    assert result["category"] == "insufficient_data"
    assert result["trade_action"] is None
    assert result["watchlists"] == ["opportunity_watch"]
    assert result["quality"]["model_validation"] == "not_evaluated_by_screen"
    assert result["quality"]["flow_observation_days"] == 1
    assert "hold" not in result["category"].lower()


@pytest.mark.parametrize("changes,expected", [
    ({"quote_epoch": None}, "missing_timestamp"),
    ({"quote_epoch": utc("2024-08-18T09:00:00Z").timestamp()}, "future_quote"),
    ({"quote_epoch": utc("2024-08-01T09:00:00Z").timestamp()}, "stale_quote"),
])
def test_missing_stale_future_quotes_never_generate_watch_actions(cfg, changes, expected):
    result = classify(cfg, flow_row() | changes)
    assert result["category"] == "insufficient_data"
    assert result["quality"]["quote_freshness"] == expected
    assert result["watchlists"] == []


@pytest.mark.parametrize("value", [None, float("nan"), float("inf")])
def test_missing_flow_does_not_become_neutral_or_hold(cfg, value):
    result = classify(cfg, flow_row() | {"main_net_inflow": value})
    assert result["snapshot_category"] == "insufficient_data"
    assert result["category"] == "insufficient_data"


@pytest.mark.parametrize("prices,net,pct,expected", [
    ([100 + idx * 0.4 for idx in range(80)], 10_000_000, 10, "trend_continuation"),
    ([100 + idx * 0.5 for idx in range(75)] + [136, 135, 134, 133, 132], 10_000_000, 10, "pullback_watch"),
    ([100] * 60 + [98 - idx * 1.5 for idx in range(15)] + [78, 79, 80, 81, 82], 10_000_000, 10, "oversold_recovery"),
    ([100 + idx * 1.5 for idx in range(80)], -10_000_000, -10, "high_level_divergence"),
    ([160 - idx * 0.5 for idx in range(80)], -10_000_000, -10, "trend_weakening"),
    ([100] * 80, 0, 0, "range_no_edge"),
])
def test_six_historical_states_use_explicit_features(cfg, prices, net, pct, expected):
    result = classify(cfg, flow_row(net=net, pct=pct), history(prices))
    assert result["category"] == expected
    assert result["quality"]["category_validation"] == "heuristic_not_backtested"
    assert result["evidence"]["history_feature_date"] == "2024-08-17"
    assert result["evidence"]["history_bars"] == 80
    assert result["followup_condition"] and result["invalidation_condition"]
    assert result["trade_action"] is None


@pytest.mark.parametrize("available,basis,status", [
    (None, "missing", "unknown_history_availability"),
    ("bad-time", "recorded_receipt", "unknown_history_availability"),
    ("2024-08-17T12:00:01Z", "recorded_receipt", "history_unavailable_at_decision"),
    ("2024-08-17T12:00:01Z", "filesystem_mtime_only", "history_unavailable_at_decision"),
])
def test_history_availability_boundary(cfg, available, basis, status):
    result = classify(cfg, available=available, basis=basis)
    assert result["category"] == "insufficient_data"
    assert result["quality"]["history_status"] == status


def test_filesystem_mtime_is_never_claimed_as_historical_pit_proof(cfg):
    result = classify(cfg, basis="filesystem_mtime_only")
    assert result["category"] == "trend_continuation"
    assert result["quality"]["point_in_time_verified"] is False


def test_future_bars_do_not_change_past_category_or_feature_values(cfg):
    first = classify(cfg)
    original = history()
    extra = Bar(date(2024, 8, 18), 999, 1000, 998, 999, 1000, 100000)
    result = classify(cfg, prices=replace(original, bars=original.bars + (extra,)))
    assert result["category"] == first["category"]
    for key in ("close", "ma20", "ma60", "return_5", "return_20", "drawdown_20", "drawdown_60"):
        assert result["evidence"][key] == first["evidence"][key]
    assert result["evidence"]["future_or_unclosed_bars_excluded"] == 1


def test_unclosed_today_bar_is_excluded(cfg):
    row = flow_row(epoch=int(utc("2024-08-17T06:00:00Z").timestamp()))
    result = classify_observation(row, "2024-08-17T07:00:00Z", cfg, history(),
                                  "2024-08-17T06:30:00Z", "recorded_receipt")
    assert result["evidence"]["history_feature_date"] == "2024-08-16"
    assert result["evidence"]["future_or_unclosed_bars_excluded"] == 1


def test_short_and_stale_history_do_not_become_hold(cfg):
    for prices, expected in ((history([100] * 60), "too_few_history_bars"),
                             (history(end=date(2024, 8, 1)), "stale_history")):
        result = classify(cfg, prices=prices)
        assert result["category"] == "insufficient_data"
        assert result["quality"]["history_status"] == expected


def test_invalid_and_duplicate_bars_are_explicit_data_failures(cfg):
    original = history()
    invalid = replace(original, bars=original.bars[:-1] + (replace(original.bars[-1], close=float("nan")),))
    duplicate = replace(original, bars=original.bars + original.bars[-1:])
    assert classify(cfg, prices=invalid)["quality"]["history_status"] == "invalid_history_bars"
    assert classify(cfg, prices=duplicate)["quality"]["history_status"] == "duplicate_history_dates"


def test_risk_watch_covers_outflow_outside_selected_top_n(cfg):
    records = [flow_row(), flow_row("159934", -30_000_000, -30), flow_row("159935", 0, 0)]
    rules = cfg["etf_market"]["screen"]
    eligible, selected = select_etfs(records, rules, 1)
    manifest = {"sha256": "a" * 64, "retrieved_at_utc": RECEIVED}
    result = build_opportunity_observation(cfg, eligible, selected, manifest, DECISION)
    assert selected[0]["symbol"] == "518880"
    assert result["risk_watch"] == ["159934"]
    assert result["opportunity_watch"] == ["518880"]
    assert result["coverage"]["eligible"] == 3
    assert sum(result["category_counts"].values()) == 3
    assert result["selection_effect"] == "none"
    assert all(row["asset_class_label"] == "商品" for row in result["rows"])
    assert records[0].keys() == flow_row().keys()


def test_observation_builder_rejects_future_snapshot_even_without_screen_wrapper(cfg):
    with pytest.raises(ValueError, match="unavailable"):
        build_opportunity_observation(cfg, [], [], {"retrieved_at_utc": "2024-08-18T12:00:00Z"}, DECISION)


def test_legacy_frozen_screen_is_not_backfilled_or_resorted(cfg):
    initial_manifest = snapshot(cfg)
    path, first = screen_etfs(cfg, decision_at_utc=DECISION)
    identity = rule_identity(cfg["etf_market"]["screen"], cfg["etf_market"]["screen"]["top_n"])
    # Reproduce an old version that never recorded categories.
    legacy = {key: value for key, value in first.items() if key != "opportunity_observation"}
    db = Path(cfg["_project_dir"]) / cfg["state_db"]
    with connect(db) as connection:
        connection.execute("UPDATE screen_batches SET payload_json=? WHERE rule_hash=?", (json.dumps(legacy), identity))
        connection.commit()
    current_manifest = snapshot(cfg, [flow_row("159935", -50_000_000, -30)], "2024-08-17T12:10:00Z", "b" * 64)
    _, second = screen_etfs(cfg, decision_at_utc="2024-08-17T13:00:00Z")
    assert second["selected"] == first["selected"]
    assert second["rule_hash"] == identity
    assert "opportunity_observation" not in second
    assert second["current_opportunity_observation"]["source_snapshot"]["sha256"] == current_manifest["sha256"]
    assert second["source_snapshot"]["sha256"] == initial_manifest["sha256"]
    with connect(db) as connection:
        saved = json.loads(connection.execute("SELECT payload_json FROM screen_batches WHERE rule_hash=?", (identity,)).fetchone()[0])
    assert saved == legacy
    assert json.loads(path.read_text(encoding="utf-8")) == first


def test_report_preserves_all_twenty_and_marks_unpredicted_observations(cfg):
    rows = [flow_row(str(518800 + idx), pct=idx + 1) | {"name": f"黄金ETF测试{idx}"} for idx in range(25)]
    snapshot(cfg, rows)
    cfg["etf_market"]["screen"].update(top_n=20, max_per_group=100)
    path, screened = screen_etfs(cfg, decision_at_utc=DECISION)
    report = build_research_report(cfg, {"requested_prediction_top_n": 5, "prediction_candidate_count": 5,
                                       "artifacts": {"screen": str(path)}})
    content = report.read_text(encoding="utf-8")
    assert all(row["symbol"] in content for row in screened["selected"])
    assert "全部冻结候选的观察覆盖（20只）" in content
    assert "全量合格 25 只" in content
    assert "仅粗筛观察" in content
    assert "风险榜不等于减持指令" in content
    assert "深度预测范围：粗筛前 5 名" in content


def test_report_explains_baseline_fallback_and_prediction_exclusions(cfg, tmp_path):
    with connect(Path(cfg["_project_dir"]) / cfg["state_db"]) as connection:
        item = frozen_prediction(connection, prediction(raw_probability_up=0.63, discrimination_status="baseline_only"))
    path = tmp_path / "prediction.json"
    path.write_text(json.dumps({"predictions": [item]}), encoding="utf-8")
    report = build_research_report(cfg, {"prediction_candidate_count": 20, "prediction_eligible_symbols": ["518880"],
                                       "prediction_exclusions": [{"symbol": "159934", "reason": "missing_history"}],
                                       "artifacts": {"predict_5d": str(path)}})
    text = report.read_text(encoding="utf-8")
    assert "申请候选 20 只；数据检查后可预测 1 只；本轮实际引用冻结预测 1 只 / 1 条" in text
    assert "历史基准回退" in text
    assert "63.00%" in text
    assert "缺少可用历史日线" in text


def _write_observation_history(cfg):
    directory = market_data_dir(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "cache_518880.csv"
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("date", "open", "high", "low", "close", "volume", "amount"))
        for bar in history().bars:
            writer.writerow((bar.trading_date, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.amount))
    epoch = utc(RECEIVED).timestamp()
    os.utime(source, (epoch, epoch))
    return source


def test_history_copy_survives_cache_refresh(cfg):
    source = _write_observation_history(cfg)
    original = source.read_bytes()
    sha = hashlib.sha256(original).hexdigest()
    rows, selected = select_etfs([flow_row()], cfg["etf_market"]["screen"])
    result = build_opportunity_observation(cfg, rows, selected, {"retrieved_at_utc": RECEIVED}, DECISION)
    item = result["rows"][0]
    reference = item["evidence"]["history_snapshot"]
    frozen = Path(reference["path"])
    assert frozen == market_data_dir(cfg) / "snapshots" / sha / "cache_518880.csv"
    assert reference["sha256"] == sha
    assert item["evidence"]["history_original_path"] == str(source.resolve())
    assert item["category"] == "trend_continuation"
    assert utc(item["evidence"]["history_available_at_utc"]) == utc(RECEIVED)
    assert not item["quality"]["point_in_time_verified"]
    # A later provider refresh must never change this run's archived input.
    source.write_text("new provider response", encoding="utf-8")
    assert frozen.read_bytes() == original
    assert hashlib.sha256(frozen.read_bytes()).hexdigest() == reference["sha256"]


def test_existing_history_copy_corruption_is_rejected(cfg):
    source = _write_observation_history(cfg)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    target = market_data_dir(cfg) / "snapshots" / sha / "cache_518880.csv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupted previous snapshot")
    rows, selected = select_etfs([flow_row()], cfg["etf_market"]["screen"])
    with pytest.raises(ValueError, match="snapshot hash mismatch"):
        build_opportunity_observation(cfg, rows, selected, {"retrieved_at_utc": RECEIVED}, DECISION)
    assert target.read_bytes() == b"corrupted previous snapshot"


def test_history_changed_between_load_and_freeze_is_rejected(cfg, monkeypatch):
    from prediction_research import opportunity
    from prediction_research.data import load_cache

    source = _write_observation_history(cfg)
    loaded = load_cache("518880", market_data_dir(cfg))
    source.write_text("provider changed file during observation", encoding="utf-8")
    monkeypatch.setattr(opportunity, "_cached_history", lambda *_: (loaded, RECEIVED, "filesystem_mtime_only"))
    with pytest.raises(ValueError, match="changed while being read"):
        build_opportunity_observation(cfg, [flow_row()], [], {"retrieved_at_utc": RECEIVED}, DECISION)

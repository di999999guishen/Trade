"""Regression locks for the observe-only coarse-screen diagnostics merge.

Reference: ZhuLinsen/daily_stock_analysis screening engine (Apache-2.0).
The point of these tests is not the diagnostics themselves - it is proving
that the merge does not change any production decision.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from prediction_research import screen_diagnostics as sd
from prediction_research.screening import ingest_etf_snapshot, rule_identity, screen_etfs, select_etfs
from prediction_research.tests.test_evidence import cfg  # noqa: F401 - shared pytest fixture

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "research.json"
# Deliberately pinned: changing etf_market.screen changes the frozen-screen
# batch key. Update this constant only together with a frozen-screen migration.
EXPECTED_RULE_HASH = "8c8e1e44515bec188b1552d26728066c52f3dbe81a3230e14d85bf5cde827ff4"

RULES = {
    "asset_classes": ["equity", "commodity", "multi_asset"],
    "min_amount": 5_000_000,
    "min_market_cap": 50_000_000,
    "top_n": None,
    "max_per_group": 1,
    "order_divergence_min_leg_pct": 1.0,
    "order_divergence_clip_pct": 15.0,
    "exclude_money_market_etfs": True,
    "money_market_max_atr_pct": 0.05,
    "money_market_price_band": 0.5,
    "screen_mode": "composite",
    "overheat_threshold_pct": 7.0,
    "score_weights": {
        "main_flow": 0.30, "liquidity": 0.25, "momentum": 0.10,
        "order_divergence": 0.10, "flow_to_cap": 0.10,
        "relative_strength": 0.15, "overheat_penalty": 0.10,
    },
}


def row(symbol: str, **overrides):
    base = {
        "symbol": symbol, "exchange": "SSE", "name": "黄金ETF测试", "price": 1.0,
        "change_pct": 2.0, "amount": 100_000_000.0, "market_cap": 1_000_000_000.0,
        "main_net_inflow": 10_000_000.0, "main_net_inflow_pct": 10.0,
    }
    base.update(overrides)
    return base


def sample_records():
    return [
        row("518880"),
        row("518881", change_pct=1.0),                        # same screen_group -> group cap
        row("518882", change_pct=0.5),                        # same screen_group -> group cap
        row("518883", price=100.0),                           # par-price peg exclusion
        row("518884", main_net_inflow=None, main_net_inflow_pct=None),  # red-line missing flow
        row("518885", amount=100.0),                          # below min_amount
    ]


def test_rejections_do_not_change_selection_or_scores():
    records = sample_records()
    eligible_a, selected_a = select_etfs(records, RULES, 3)
    rejections: list = []
    eligible_b, selected_b = select_etfs(records, RULES, 3, rejections=rejections)

    assert [r["symbol"] for r in selected_a] == [r["symbol"] for r in selected_b]
    assert [r["screen_score"] for r in eligible_a] == [r["screen_score"] for r in eligible_b]
    assert [r["symbol"] for r in eligible_a] == [r["symbol"] for r in eligible_b]
    assert rejections
    assert all({"symbol", "name", "stage", "reason", "field_status"} <= set(r) for r in rejections)
    assert {"group_cap_exceeded", "missing_money_flow", "eligibility",
            "money_market_exclusion"} <= {r["stage"] for r in rejections}


def test_missing_flow_is_never_a_pass():
    records = [row("518880"), row("518884", main_net_inflow=None, main_net_inflow_pct=None)]
    rejections: list = []
    eligible, selected = select_etfs(records, RULES, 5, rejections=rejections)
    assert "518884" not in {r["symbol"] for r in eligible}
    assert "518884" not in {r["symbol"] for r in selected}
    record = next(r for r in rejections if r["symbol"] == "518884")
    assert record["stage"] == "missing_money_flow"
    assert record["field_status"] == "missing"
    assert record["missing_fields"] == ["main_net_inflow", "main_net_inflow_pct"]


def test_waterfall_and_effective_weights():
    records = sample_records()
    rejections: list = []
    eligible, selected = select_etfs(records, RULES, 3, rejections=rejections)
    manifest = {"records": len(records), "with_flow": 5, "source": "eastmoney push2",
                "fallback_used": False, "sha256": "a" * 64,
                "retrieved_at_utc": "2026-11-12T23:24:40+00:00"}
    snapshot = {"flow_definition": "eastmoney f62/f184", "records": records}
    diag = sd.build_diagnostics(
        manifest=manifest, snapshot=snapshot, fresh=records, rules=RULES,
        eligible=eligible, selected=selected, rejections=rejections,
        decision_at_utc="2026-11-12T23:30:00+00:00",
    )
    waterfall = diag["filter_waterfall"]
    assert waterfall["total_rejections"] == len(rejections)
    assert waterfall["missing_by_field"]["main_net_inflow"] == 1
    assert waterfall["missing_by_field"]["main_net_inflow_pct"] == 1
    assert waterfall["missing_policy"].startswith("a missing field is never a pass")
    assert diag["source_chain"]["actual_source"] == "eastmoney"
    assert diag["effective_factor_weights"]["basis"] == "eligible_row_screen_weights"
    assert diag["effective_factor_weights"]["weights"]["flow"] == 0.30
    assert diag["not_a_candidate_source"] is True
    assert diag["stock_picking"] == "disabled_while_icir_below_0.30"
    assert "selected" not in diag  # no candidate list can leak into diagnostics
    json.dumps(diag, ensure_ascii=False)  # must stay serialisable


def test_liquidity_only_records_zeroed_factors():
    rules = {**RULES, "screen_mode": "liquidity_only"}
    records = sample_records()
    eligible, _ = select_etfs(records, rules, 3)
    weights = sd.build_effective_weights(rules, eligible)
    assert weights["screen_mode"] == "liquidity_only"
    assert {"flow", "momentum", "flow_to_cap"} <= set(weights["zeroed_factors"])
    assert weights["weights"]["flow"] == 0.0


def test_source_chain_captures_sina_fallback_degradation():
    degraded = ["large_net_inflow", "large_net_inflow_pct", "medium_net_inflow",
                "medium_net_inflow_pct", "small_net_inflow", "small_net_inflow_pct",
                "super_large_net_inflow_pct"]
    manifest = {"source": "sina:Market_Center.getHQNodeData(etf_hq_fund) + MoneyFlow",
                "records": 927, "with_flow": 917, "fallback_used": True,
                "sha256": "b" * 64, "retrieved_at_utc": "2026-11-12T23:24:40+00:00"}
    snapshot = {"fallback": {"reason": "eastmoney quote api unavailable",
                             "degraded_fields": degraded},
                "flow_definition": "Sina MoneyFlow netamount/ratioamount"}
    chain = sd.build_source_chain(manifest, snapshot)
    assert chain["actual_source"] == "sina"
    assert chain["priority_position"] == 1
    assert chain["fallback_used"] is True
    assert chain["degraded_fields"] == degraded
    assert chain["flow_coverage"] == round(917 / 927, 6)


def test_last_good_is_diagnostics_only_and_keeps_previous_ok(tmp_path):
    path = tmp_path / "screen_last_good.json"
    diag = sd.build_diagnostics(
        manifest={"records": 2, "with_flow": 2, "source": "eastmoney"},
        snapshot={}, fresh=[row("518880")], rules=RULES,
        eligible=[], selected=[], rejections=[],
        decision_at_utc="2026-11-13T00:00:00+00:00", last_good_path=path,
    )
    ok = sd.commit_last_good(path, diag, status="ok")
    assert ok["not_a_candidate_source"] is True
    assert ok["usage"] == "diagnostics_only"
    assert ok["stale"] is False
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "ok"

    failed = sd.commit_last_good(path, diag, status="failed", failure_reason="no eligible ETF candidates")
    assert failed["stale"] is True
    assert failed["previous_ok"]["decision_at_utc"] == "2026-11-13T00:00:00+00:00"
    assert failed["failure_reason"] == "no eligible ETF candidates"

    again = sd.build_diagnostics(
        manifest={"records": 2, "with_flow": 2, "source": "eastmoney"},
        snapshot={}, fresh=[row("518880")], rules=RULES,
        eligible=[], selected=[], rejections=[],
        decision_at_utc="2026-11-13T00:01:00+00:00", last_good_path=path,
    )
    assert again["last_good_binding"]["previous_record_present"] is True
    assert again["last_good_binding"]["priority_match"] is True


def test_source_priority_mismatch_is_observation_only(tmp_path):
    path = tmp_path / "screen_last_good.json"
    path.write_text(json.dumps({"status": "ok", "source_chain": {
        "priority": ["sina", "eastmoney"], "actual_source": "sina"}}), encoding="utf-8")
    diag = sd.build_diagnostics(
        manifest={"source": "eastmoney", "records": 1, "with_flow": 1},
        snapshot={}, fresh=[row("518880")], rules=RULES,
        eligible=[], selected=[], rejections=[],
        decision_at_utc="2026-11-13T00:00:00+00:00", last_good_path=path,
    )
    binding = diag["last_good_binding"]
    assert binding["priority_match"] is False
    assert binding["actual_source_match"] is False
    assert "never re-selected" in binding["policy"]


def test_config_switch_stays_out_of_rules_and_preserves_rule_hash():
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    assert isinstance(cfg.get("screen_diagnostics"), dict)
    assert cfg["screen_diagnostics"]["mode"] == "observe_only"
    rules = cfg["etf_market"]["screen"]
    assert not any(key in rules for key in
                   ("enabled", "mode", "last_good_path", "screen_diagnostics", "source_priority"))
    assert rule_identity(rules, None) == EXPECTED_RULE_HASH
    without = {key: value for key, value in cfg.items() if key != "screen_diagnostics"}
    assert rule_identity(without["etf_market"]["screen"], None) == EXPECTED_RULE_HASH


def test_diagnostics_module_has_no_pandas_dependency():
    source = Path(sd.__file__).read_text(encoding="utf-8")
    assert "import pandas" not in source
    assert "from pandas" not in source


def _seed_snapshot(cfg, rows, received="2024-08-17T09:10:00Z", sha="c" * 64):
    directory = Path(cfg["_project_dir"]) / cfg["etf_market"]["snapshot_dir"]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"etf_snapshot_{sha}.json"
    path.write_text(json.dumps({
        "records": rows, "flow_definition": "synthetic test only",
        "fallback": {"reason": "eastmoney quote api unavailable",
                     "degraded_fields": ["large_net_inflow", "large_net_inflow_pct"]},
    }), encoding="utf-8")
    manifest = {
        "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "retrieved_at_utc": received, "records": len(rows), "with_flow": len(rows),
        "source": "sina:Market_Center.getHQNodeData(etf_hq_fund)", "fallback_used": True,
    }
    (directory / "latest.json").write_text(json.dumps(manifest), encoding="utf-8")
    ingest_etf_snapshot(cfg, manifest)
    return manifest


def test_end_to_end_report_carries_diagnostics_and_frozen_payload_is_intact(cfg):
    epoch = int(datetime(2024, 8, 17, 9, tzinfo=timezone.utc).timestamp())
    rows = [
        row("518880", quote_epoch=epoch),
        row("159934", quote_epoch=epoch, change_pct=1.0),      # same group -> group cap
        row("518884", quote_epoch=epoch, main_net_inflow=None, main_net_inflow_pct=None),
    ]
    _seed_snapshot(cfg, rows)
    decision = "2024-08-17T12:00:00Z"

    path, first = screen_etfs(cfg, decision_at_utc=decision)
    assert "screen_diagnostics" in first
    diag = first["screen_diagnostics"]
    assert diag["mode"] == "observe_only"
    assert diag["source_chain"]["actual_source"] == "sina"
    assert diag["source_chain"]["fallback_used"] is True
    assert diag["filter_waterfall"]["missing_by_field"]["main_net_inflow"] == 1
    assert diag["filter_waterfall"]["steps"][0]["stage"] == "money_category_excluded" or \
        any(step["stage"] == "group_cap_exceeded" for step in diag["filter_waterfall"]["steps"])
    assert json.loads(path.read_text(encoding="utf-8")) == first

    last_good = Path(cfg["_project_dir"]) / cfg["screen_diagnostics"]["last_good_path"]
    assert last_good.is_file()
    saved = json.loads(last_good.read_text(encoding="utf-8"))
    assert saved["not_a_candidate_source"] is True
    assert saved["status"] == "ok"

    # Re-running the same decision reuses the frozen batch: historical payload
    # must not be backfilled; only a clearly-labelled current observation is added.
    _, second = screen_etfs(cfg, decision_at_utc=decision)
    assert second["selected"] == first["selected"]
    assert second.get("reused_frozen_screen") is True
    assert second["screen_diagnostics"] == diag
    assert second["current_screen_diagnostics"]["binding"] == \
        "current_observation_not_frozen_decision_evidence"


def test_disabling_switch_restores_the_original_payload(cfg):
    epoch = int(datetime(2024, 8, 17, 9, tzinfo=timezone.utc).timestamp())
    _seed_snapshot(cfg, [row("518880", quote_epoch=epoch)])
    cfg["screen_diagnostics"]["enabled"] = False
    _, payload = screen_etfs(cfg, decision_at_utc="2024-08-17T12:00:00Z")
    assert "screen_diagnostics" not in payload
    assert "current_screen_diagnostics" not in payload
    assert payload["selected"]


# ---------------------------------------------------------------------------
# Schema v2: additional observation fields.
# ---------------------------------------------------------------------------

V2_BLOCKS = (
    "field_coverage", "quote_freshness", "score_distribution",
    "factor_availability", "guard_counts", "group_competition",
    "staleness", "run_contract", "observation_policy",
)


def _v2_diag():
    records = sample_records()
    rejections: list = []
    eligible, selected = select_etfs(records, RULES, 3, rejections=rejections)
    manifest = {"records": len(records), "with_flow": 5, "source": "sina fallback",
                "fallback_used": True, "sha256": "d" * 64,
                "retrieved_at_utc": "2026-11-12T23:24:40+00:00"}
    snapshot = {"fallback": {"reason": "eastmoney quote api unavailable",
                             "degraded_fields": ["large_net_inflow", "medium_net_inflow"]},
                "flow_definition": "Sina MoneyFlow", "records": records}
    return sd.build_diagnostics(
        manifest=manifest, snapshot=snapshot, fresh=records, rules=RULES,
        eligible=eligible, selected=selected, rejections=rejections,
        decision_at_utc="2026-11-12T23:30:00+00:00",
        max_age_days=4.0, selection_cap=3,
        diagnostics_config={"enabled": True, "mode": "observe_only"},
    )


def test_v2_blocks_present_serialisable_and_contain_no_candidate_list():
    diag = _v2_diag()
    assert diag["schema_version"] == 2
    for block in V2_BLOCKS:
        assert block in diag, block
    text = json.dumps(diag, ensure_ascii=False)
    assert "selected" not in diag
    assert "eligible" not in diag
    assert len(text) < 200_000  # aggregates only, never a copied universe
    assert diag["observation_policy"].startswith("aggregates only")


def test_field_coverage_reports_red_line_and_degraded_fields():
    diag = _v2_diag()
    coverage = diag["field_coverage"]
    assert coverage["records"] == 6
    assert coverage["red_line_fields"] == ["main_net_inflow", "main_net_inflow_pct"]
    assert coverage["red_line_violations"] == 1
    assert coverage["by_field"]["main_net_inflow"]["missing"] == 1
    assert coverage["by_field"]["main_net_inflow"]["coverage"] == round(5 / 6, 6)
    assert coverage["degraded_fields_declared"] == ["large_net_inflow", "medium_net_inflow"]
    assert "large_net_inflow" in coverage["degraded_fields_absent_in_records"]


def test_guard_counts_assert_red_line_was_enforced():
    diag = _v2_diag()
    guards = diag["guard_counts"]
    assert guards["red_line_enforced"] is True
    assert guards["missing_money_flow_intercepts"] == 1
    assert guards["selection_limit"] == 3
    assert guards["max_per_group"] == 1
    assert guards["by_stage"]["missing_money_flow"] == 1


def test_factor_availability_and_denominator_drift():
    diag = _v2_diag()
    availability = diag["factor_availability"]
    assert availability["eligible_records"] == 3  # 518880/518881/518882; 518883 peg, 518884 flow, 518885 amount
    assert "flow" in availability["by_factor"]
    assert availability["by_factor"]["overheat_penalty"]["availability"] is not None
    assert availability["denominator"]["count"] == 3
    assert availability["denominator"]["distinct"] >= 1


def test_score_distribution_and_group_competition_are_coherent():
    diag = _v2_diag()
    scores = diag["score_distribution"]
    assert scores["eligible"]["count"] >= scores["selected"]["count"] > 0
    assert scores["selected"]["min"] <= scores["selected"]["max"]
    assert scores["selection_cutoff_score"] == scores["selected"]["min"]
    competition = diag["group_competition"]
    assert competition["group_cap_rejected"] == diag["guard_counts"]["group_cap_intercepts"]
    assert competition["near_miss_rejections"] >= 0
    assert competition["eligible_groups"] >= 1
    assert competition["policy"].startswith("diagnoses the cost")


def test_quote_freshness_uses_epochs_and_policy():
    epoch = int(datetime(2026, 11, 12, 9, tzinfo=timezone.utc).timestamp())
    rows = [row("518880", quote_epoch=epoch), row("518881", quote_epoch=epoch + 3600)]
    diag = sd.build_diagnostics(
        manifest={"records": 2, "with_flow": 2, "source": "eastmoney",
                  "retrieved_at_utc": "2026-11-12T09:10:00+00:00"},
        snapshot={"records": rows}, fresh=rows, rules=RULES,
        eligible=[], selected=[], rejections=[],
        decision_at_utc="2026-11-12T12:00:00+00:00",
        max_age_days=4.0, selection_cap=3,
    )
    fresh = diag["quote_freshness"]
    assert fresh["records_with_epoch"] == 2
    assert fresh["records_without_epoch"] == 0
    assert fresh["span_hours"] == 1.0
    assert fresh["decision_lag_hours"] == 2.0
    assert fresh["retrieved_before_decision"] is True
    assert fresh["max_age_days_policy"] == 4.0
    assert fresh["dropped_stale_or_future_records"] == 0


def test_staleness_and_run_contract_are_metadata_only():
    diag = _v2_diag()
    assert diag["staleness"]["previous_record_present"] is False
    assert diag["staleness"]["currently_stale"] is False
    contract = diag["run_contract"]
    assert len(contract["rules_sha256"]) == 64
    assert "NOT the frozen" in contract["rules_sha256_note"]
    assert contract["selection_limit"] == 3
    assert contract["screen_mode"] == "composite"
    assert "last_good_path" not in contract["diagnostics_config"]


def test_v2_does_not_change_production_selection_or_rule_hash():
    records = sample_records()
    eligible_a, selected_a = select_etfs(records, RULES, 3)
    rejections: list = []
    eligible_b, selected_b = select_etfs(records, RULES, 3, rejections=rejections)
    assert [r["symbol"] for r in selected_a] == [r["symbol"] for r in selected_b]
    assert [r["screen_score"] for r in eligible_a] == [r["screen_score"] for r in eligible_b]
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    assert rule_identity(cfg["etf_market"]["screen"], None) == EXPECTED_RULE_HASH


def test_last_good_tracks_consecutive_failures_and_v2_fields(tmp_path):
    path = tmp_path / "screen_last_good.json"
    diag = _v2_diag()
    ok = sd.commit_last_good(path, diag, status="ok")
    assert ok["schema_version"] == 2
    assert ok["consecutive_failures"] == 0
    assert ok["red_line_violations"] == 1
    first = sd.commit_last_good(path, diag, status="failed", failure_reason="no candidates")
    assert first["consecutive_failures"] == 1
    second = sd.commit_last_good(path, diag, status="failed", failure_reason="still none")
    assert second["consecutive_failures"] == 2
    assert second["stale"] is True
    again = sd.build_diagnostics(
        manifest={"records": 1, "with_flow": 1, "source": "eastmoney"},
        snapshot={}, fresh=[], rules=RULES, eligible=[], selected=[], rejections=[],
        decision_at_utc="2026-11-13T01:00:00+00:00", last_good_path=path,
    )
    assert again["staleness"]["previous_consecutive_failures"] == 2
    assert again["staleness"]["currently_stale"] is True


def test_end_to_end_payload_carries_v2_blocks(cfg):
    epoch = int(datetime(2024, 8, 17, 9, tzinfo=timezone.utc).timestamp())
    _seed_snapshot(cfg, [row("518880", quote_epoch=epoch)])
    _, payload = screen_etfs(cfg, decision_at_utc="2024-08-17T12:00:00Z")
    diag = payload["screen_diagnostics"]
    assert diag["schema_version"] == 2
    for block in V2_BLOCKS:
        assert block in diag, block
    assert diag["guard_counts"]["red_line_enforced"] is True
    assert diag["run_contract"]["rules_sha256"]
    saved = json.loads((Path(cfg["_project_dir"]) / cfg["screen_diagnostics"]["last_good_path"]).read_text(encoding="utf-8"))
    assert saved["schema_version"] == 2
    assert saved["consecutive_failures"] == 0

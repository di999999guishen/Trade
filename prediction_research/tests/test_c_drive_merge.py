import sqlite3
from pathlib import Path

import pytest

from prediction_research.screening import select_etfs, screen_etfs, selection_limit
from prediction_research.flow_backtest import reconstruct_screens
from prediction_research.store import connect
from prediction_research.tests.test_cycle_remediation import flow_row, snapshot
from prediction_research.tests.test_evidence import cfg


def test_unlimited_screen_freeze_and_explicit_limit(cfg):
    rules = cfg["etf_market"]["screen"]
    rules["top_n"] = None
    rows = [flow_row(str(i)) | {"name": f"独立主题{i}ETF"} for i in range(27)]
    snapshot(cfg, rows)
    _, full = screen_etfs(cfg, decision_at_utc="2024-08-17T12:00:00Z")
    _, limited = screen_etfs(cfg, limit=5, decision_at_utc="2024-08-17T12:00:00Z")
    assert len(full["selected"]) == 27
    assert len(limited["selected"]) == 5
    assert full["rule_hash"] != limited["rule_hash"]
    history = reconstruct_screens(cfg)
    assert len(history["cohorts"][0]["groups"]["money_flow"]) == 27


@pytest.mark.parametrize("limit", [0, -1])
def test_invalid_limit_is_not_silently_unlimited(limit):
    with pytest.raises(ValueError):
        selection_limit({"top_n": None}, limit)


def test_dedup_keeps_best_fund_for_same_track(cfg):
    rows = [flow_row("a", pct=1) | {"name": "创新药ETF银华"},
            flow_row("b", pct=20) | {"name": "生物科技ETF易方达"},
            flow_row("c", pct=5) | {"name": "农业ETF富国"}]
    rules = cfg["etf_market"]["screen"] | {"top_n": None, "max_per_group": 1}
    _, selected = select_etfs(rows, rules)
    assert {row["symbol"] for row in selected} == {"b", "c"}


def test_five_leg_migration_preserves_old_rows_and_ingests_new_fields(cfg):
    path = Path(cfg["_project_dir"]) / cfg["state_db"]
    path.parent.mkdir(parents=True, exist_ok=True)
    # Start from the existing schema without just the new medium/small columns.
    from prediction_research.store import SCHEMA
    legacy = "\n".join(line for line in SCHEMA.splitlines()
                        if not any(leg + "_net_inflow" in line for leg in ("medium", "small")))
    with sqlite3.connect(path) as db:
        db.executescript(legacy)
    legs = {"medium_net_inflow": -123.0, "medium_net_inflow_pct": -1.2,
            "small_net_inflow": 456.0, "small_net_inflow_pct": 4.5}
    snapshot(cfg, [flow_row() | legs])
    with connect(path) as db:
        stored = db.execute("SELECT medium_net_inflow,medium_net_inflow_pct,small_net_inflow,small_net_inflow_pct FROM etf_flow_snapshots").fetchone()
        assert tuple(stored) == tuple(legs.values())
    # Reopening/reingesting must neither fail migration nor duplicate history.
    snapshot(cfg, [flow_row() | legs])
    with connect(path) as db:
        assert db.execute("SELECT count(*) FROM etf_flow_snapshots").fetchone()[0] == 1

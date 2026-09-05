from __future__ import annotations

import json
from pathlib import Path

from .config import resolve_project_path
from .store import connect


def workflow_status(cfg: dict) -> dict:
    project = Path(cfg["_project_dir"])
    snapshot_manifest = resolve_project_path(cfg, cfg["etf_market"]["snapshot_dir"]) / "latest.json"
    runs = resolve_project_path(cfg, cfg["runs_dir"])
    screens = sorted(runs.glob("screen_etf_flow_*.json"), reverse=True)
    batches = sorted(runs.glob("tradingagents_batch_*.json"), reverse=True)
    cycle_paths = sorted(runs.glob("cycle_*.json"), reverse=True)
    snapshot = json.loads(snapshot_manifest.read_text(encoding="utf-8")) if snapshot_manifest.exists() else None
    screen = json.loads(screens[0].read_text(encoding="utf-8")) if screens else None
    batch = json.loads(batches[0].read_text(encoding="utf-8")) if batches else None
    agent_items = batch.get("items", []) if batch else []
    agent_successes = sum(item.get("status") == "ok" for item in agent_items)
    auth_blocked = any(item.get("error_class") == "authentication" for item in agent_items)
    agent_skipped = bool(cfg.get("workflow", {}).get("skip_tradingagents"))
    db_path = resolve_project_path(cfg, cfg["state_db"])
    selected_symbols = [row["symbol"] for row in screen.get("selected", [])] if screen else []
    with connect(db_path) as connection:
        if selected_symbols:
            placeholders = ",".join("?" for _ in selected_symbols)
            frozen_predictions = connection.execute(
                f"SELECT count(1) FROM predictions WHERE symbol IN ({placeholders}) AND settled_at_utc IS NULL",
                selected_symbols,
            ).fetchone()[0]
        else:
            frozen_predictions = 0
        all_open_predictions = connection.execute(
            "SELECT count(1) FROM predictions WHERE settled_at_utc IS NULL"
        ).fetchone()[0]
        flow_observation_days = connection.execute("SELECT count(DISTINCT date(quote_epoch, 'unixepoch', '+8 hours')) FROM etf_flow_snapshots WHERE quote_epoch IS NOT NULL").fetchone()[0]
        open_screen_selections = connection.execute(
            "SELECT count(1) FROM screen_selections WHERE settled_at_utc IS NULL"
        ).fetchone()[0]
        recorded_cycle_runs = connection.execute("SELECT count(1) FROM runs WHERE run_type='cycle'").fetchone()[0]
    successful_cycle_runs = 0
    for cycle_path in cycle_paths:
        cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
        outcome = cycle.get("outcome")
        if outcome == "complete" or outcome is None and all(
            item.get("status") in {"ok", "skipped_by_configuration"} for item in cycle.get("steps", [])
        ):
            successful_cycle_runs += 1
    stages = [
        {"stage": 1, "name": "ETF分类口径", "status": "complete", "evidence": str((project / "docs" / "ETF_TAXONOMY_AND_LABELS.md").resolve())},
        {"stage": 2, "name": "ETF行情板块快照", "status": "complete" if snapshot else "pending", "records": snapshot.get("records") if snapshot else 0, "coverage_caveat": "尚缺交易所合同级债券/多资产清单"},
        {"stage": 3, "name": "资金流粗筛与候选冻结", "status": "complete" if screen else "pending", "eligible": screen.get("eligible_records") if screen else 0, "selected": len(screen.get("selected", [])) if screen else 0, "open_forward_selections": open_screen_selections, "historical_observation_days": flow_observation_days, "historical_screen_validation_ready": flow_observation_days >= 60, "report": str(screens[0].resolve()) if screens else None},
        {"stage": 4, "name": "TradingAgents深度分析", "status": "skipped_by_user" if agent_skipped else "blocked_authentication" if auth_blocked and not agent_successes else "complete" if agent_successes else "pending", "successful": agent_successes, "attempted": len(agent_items), "report": str(batches[0].resolve()) if batches else None},
        {"stage": 5, "name": "智能体结论入证据层", "status": "skipped_no_agent_evidence" if agent_skipped and not agent_successes else "complete" if agent_successes else "waiting_for_stage_4"},
        {"stage": 6, "name": "预测冻结与到期复盘", "status": "research_predictions_frozen" if frozen_predictions else "quantitative_path_ready" if agent_skipped else "waiting_for_stage_5" if not agent_successes else "ready", "current_screen_open_predictions": frozen_predictions, "all_open_predictions": all_open_predictions, "recorded_cycle_runs": recorded_cycle_runs, "successful_cycle_runs": successful_cycle_runs, "latest_cycle": str(cycle_paths[0].resolve()) if cycle_paths else None},
        {"stage": 7, "name": "TradingAgents独立增益验证", "status": "deferred_while_agents_skipped" if agent_skipped else "pending", "note": "不得把多角色一致意见直接当作概率"},
    ]
    return {"current_stage": 6 if agent_skipped else 4 if not agent_successes else 5, "active_path": "quantitative_without_tradingagents" if agent_skipped else "full", "blocking_reason": None if agent_skipped else "DeepSeek authentication failed (HTTP 401)" if auth_blocked and not agent_successes else None, "stages": stages}

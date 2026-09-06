from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .adapters.eastmoney import fetch_universe
from .adapters.eastmoney_etf import fetch_etf_snapshot
from .config import market_data_dir, resolve_project_path
from .data import configured_data_dirs, find_cache
from .events import fetch_news
from .evidence import now_utc, utc
from .flow_backtest import run_flow_backtest
from .pipeline import run_backtest, run_prediction, settle_predictions
from .reporting import build_research_report
from .screening import ingest_etf_snapshot, screen_etfs, settle_screen_selections
from .settlement import pending_history_assets
from .store import connect, record_run
from .tradingagents_adapter import run_tradingagents, settle_agent_analyses


def candidate_history_status(cfg: dict, candidates: list[dict], decision: str) -> dict:
    issues, dates = [], {}
    local_date = utc(decision).astimezone(ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))).date()
    for asset in candidates:
        try:
            snapshot = find_cache(asset["symbol"], configured_data_dirs(cfg))
            last = snapshot.bars[-1].trading_date
            dates[asset["symbol"]] = last.isoformat()
            age = (local_date - last).days
            if age < 0 or age > int(cfg.get("workflow", {}).get("max_quote_age_days", 4)):
                issues.append({"symbol": asset["symbol"], "reason": "stale_or_future_history", "last_date": last.isoformat()})
        except (FileNotFoundError, ValueError):
            issues.append({"symbol": asset["symbol"], "reason": "missing_history"})
    if len(set(dates.values())) > 1:
        issues.append({"reason": "candidate_history_dates_disagree"})
    if len(candidates) < 2:
        issues.append({"reason": "pooled_model_requires_two_candidates"})
    return {"status": "waiting_for_history" if issues else "ready", "issues": issues, "last_dates": dates}


def run_cycle(cfg: dict, top_n: int = 3, skip_fetch: bool = False) -> dict:
    if top_n < 1:
        raise ValueError("top must be positive")
    cfg = copy.deepcopy(cfg)
    runs = resolve_project_path(cfg, cfg["runs_dir"])
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / f"cycle_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    report = {"run_type": "end_to_end_research_cycle", "cycle_id": uuid.uuid4().hex,
              "started_at_utc": now_utc(), "data_mode": "cached_validation" if skip_fetch else "network_refresh",
              "steps": [], "artifacts": {}, "outcome": "running", "result_path": str(path.resolve())}

    def persist():
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def blocked(name, reason):
        report["steps"].append({"name": name, "status": "blocked_dependency", "reason": reason})
        persist()

    def step(name, action):
        try:
            value = action()
            status = "ok"
            if isinstance(value, dict):
                if value.get("outcome") == "partial_failure" or value.get("failures") or any(
                    row.get("status") == "failed" for row in value.get("sources", {}).values()
                ):
                    status = "partial"
                elif value.get("outcome") == "complete_with_data_waits" or str(value.get("status", "")).startswith("waiting") or value.get("waiting"):
                    status = "waiting_for_data"
            report["steps"].append({"name": name, "status": status, "result": value})
            persist()
            return value
        except Exception as exc:
            report["steps"].append({"name": name, "status": "failed", "error_type": type(exc).__name__,
                                    "error": "step failed; inspect the referenced input and run the corresponding CLI command"})
            persist()
            return None

    persist()
    can_screen = True
    if not skip_fetch:
        def fetch_snapshot():
            manifest = fetch_etf_snapshot(resolve_project_path(cfg, cfg["etf_market"]["snapshot_dir"]))
            return {**manifest, "storage": ingest_etf_snapshot(cfg, manifest)}
        can_screen = step("fetch_etf_snapshot", fetch_snapshot) is not None
        step("fetch_news", lambda: fetch_news(cfg))
    else:
        step("ingest_cached_snapshot", lambda: ingest_etf_snapshot(cfg))
    # A snapshot fetched in this run must exist before the decision is made.
    report["decision_at_utc"] = now_utc()
    screen_context = None
    candidates = []
    if can_screen:
        def screen_action():
            screen_path, screen = screen_etfs(cfg, cfg["etf_market"]["screen"]["top_n"], report["decision_at_utc"])
            report["artifacts"]["screen"] = str(screen_path.resolve())
            return {"path": str(screen_path.resolve()), "screen": screen}
        screened = step("screen_etfs", screen_action)
        if screened:
            screen_context = (Path(screened["path"]), screened["screen"])
            candidates = screened["screen"]["selected"][:top_n]
    else:
        blocked("screen_etfs", "current_snapshot_fetch_failed")
    cfg["universes"]["screened_current"] = [
        {"symbol": row["symbol"], "name": row["name"], "asset_type": f"{row['subtype']}_etf", "exposure": row["screen_group"]}
        for row in candidates
    ]
    report["candidate_symbols"] = [row["symbol"] for row in candidates]
    tracked = step("pending_history_assets", lambda: {"assets": pending_history_assets(cfg, candidates)})
    history_fetch = None
    if not skip_fetch and tracked:
        history_fetch = step("fetch_tracked_histories", lambda: fetch_universe(tracked["assets"], market_data_dir(cfg)))
    def readiness():
        value = candidate_history_status(cfg, candidates, report["decision_at_utc"])
        if not skip_fetch and candidates and (history_fetch is None or
                set(history_fetch.get("failures", {})) & set(report["candidate_symbols"])):
            value["status"] = "waiting_for_history"
            value["issues"].append({"reason": "current_candidate_refresh_failed"})
        return value
    ready = step("candidate_history_status", readiness)
    history_ready = bool(candidates and ready and ready["status"] == "ready")
    report["data_readiness"] = ready["status"] if ready else "failed"

    if cfg.get("evidence", {}).get("enabled", False) and screen_context:
        from .etf_integration import run_etf_integration
        def integration():
            payload = run_etf_integration(cfg)
            report["artifacts"]["integration"] = payload["summary_path"]
            return {"outcome": payload["outcome"], "summary_path": payload["summary_path"]}
        step("etf_evidence_integration", integration)

    def historical_flow():
        flow_path, payload = run_flow_backtest(cfg)
        report["artifacts"]["flow_strategy"] = str(flow_path.resolve())
        return {"status": payload["status"], "path": str(flow_path.resolve()),
                "timely_screen_days": payload["timely_screen_days"], "missing_histories": payload["missing_histories"]}
    step("historical_flow_screen", historical_flow)

    for horizon in cfg["horizons"]:
        if not history_ready:
            blocked(f"backtest_{horizon}d", "current_candidates_or_histories_not_ready")
            blocked(f"predict_{horizon}d", "current_candidates_or_histories_not_ready")
            continue
        def backtest(horizon=horizon):
            output, _ = run_backtest(cfg, "screened_current", horizon, "base", "pooled")
            report["artifacts"][f"backtest_{horizon}d"] = str(output.resolve())
            return str(output.resolve())
        validated = step(f"backtest_{horizon}d", backtest)
        if validated is None:
            blocked(f"predict_{horizon}d", "current_backtest_failed")
            continue
        def predict(horizon=horizon):
            output, payload = run_prediction(cfg, "screened_current", horizon, "base", "pooled",
                                              report["decision_at_utc"], screen_context, Path(validated))
            report["artifacts"][f"predict_{horizon}d"] = str(output.resolve())
            report.setdefault("prediction_ids", []).extend(row["prediction_id"] for row in payload["predictions"])
            report.setdefault("validation", {})[str(horizon)] = payload["validation"]
            return str(output.resolve())
        step(f"predict_{horizon}d", predict)

    if cfg.get("workflow", {}).get("skip_tradingagents"):
        report["steps"].append({"name": "tradingagents", "status": "skipped_by_configuration"})
    elif screen_context and history_ready:
        step("tradingagents", lambda: str(run_tradingagents(cfg, top_n)[0]))
    else:
        blocked("tradingagents", "current_candidates_not_ready")
    step("settle_predictions", lambda: settle_predictions(cfg))
    step("settle_agent_analyses", lambda: settle_agent_analyses(cfg))
    step("settle_screen_selections", lambda: settle_screen_selections(cfg))

    def outcome():
        statuses = {row["status"] for row in report["steps"]}
        if statuses & {"failed", "partial", "blocked_dependency"}:
            return "partial_failure"
        return "complete_with_data_waits" if "waiting_for_data" in statuses else "complete"
    report["outcome"] = outcome()
    output = step("build_report", lambda: str(build_research_report(cfg, report)))
    if output:
        report["artifacts"]["report"] = output
    report["outcome"] = outcome()
    report["finished_at_utc"] = now_utc()
    persist()
    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        record_run(connection, "cycle", cfg, path)
    return report

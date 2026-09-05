from __future__ import annotations

import json
from datetime import datetime, timezone

from .adapters.eastmoney import fetch_universe
from .adapters.eastmoney_etf import fetch_etf_snapshot
from .config import market_data_dir, resolve_project_path
from .events import fetch_news
from .pipeline import run_backtest, run_prediction, settle_predictions
from .reporting import build_research_report
from .screening import ingest_etf_snapshot, latest_screen, screen_etfs, settle_screen_selections
from .store import connect, record_run
from .tradingagents_adapter import run_tradingagents, settle_agent_analyses


def run_cycle(cfg: dict, top_n: int = 3, skip_fetch: bool = False) -> dict:
    report = {"run_type": "end_to_end_research_cycle", "started_at_utc": datetime.now(timezone.utc).isoformat(), "steps": []}

    def step(name, action):
        try:
            value = action()
            status = "ok"
            if isinstance(value, dict):
                source_states = [item.get("status") for item in value.get("sources", {}).values()]
                if "failed" in source_states or value.get("failures"):
                    status = "partial"
            report["steps"].append({"name": name, "status": status, "result": value})
            return value
        except Exception as exc:
            report["steps"].append({"name": name, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            return None

    if not skip_fetch:
        def fetch_etf_step():
            manifest = fetch_etf_snapshot(resolve_project_path(cfg, cfg["etf_market"]["snapshot_dir"]))
            manifest["storage"] = ingest_etf_snapshot(cfg, manifest)
            return manifest

        step("fetch_etf_snapshot", fetch_etf_step)
        step("fetch_news", lambda: fetch_news(cfg))
    def screen_step():
        path, payload = screen_etfs(cfg, cfg["etf_market"]["screen"]["top_n"])
        return {"path": str(path.resolve()), "eligible": payload["eligible_records"], "selected": len(payload["selected"])}

    step("screen_etfs", screen_step)
    _, screen_payload = latest_screen(cfg)
    candidate_rows = screen_payload["selected"][:top_n]
    cfg["universes"]["screened_current"] = [
        {
            "symbol": row["symbol"], "name": row["name"],
            "asset_type": f"{row['subtype']}_etf", "exposure": row["screen_group"],
        }
        for row in candidate_rows
    ]
    assets = [{"symbol": row["symbol"]} for row in candidate_rows]
    if not skip_fetch:
        step("fetch_candidate_histories", lambda: fetch_universe(assets, market_data_dir(cfg)))
    for horizon in cfg["horizons"]:
        step(f"backtest_{horizon}d", lambda horizon=horizon: str(run_backtest(cfg, "screened_current", horizon, "base", "pooled")[0]))
        step(f"predict_{horizon}d", lambda horizon=horizon: str(run_prediction(cfg, "screened_current", horizon, "base", "pooled")[0]))
    if not cfg.get("workflow", {}).get("skip_tradingagents"):
        step("tradingagents", lambda: str(run_tradingagents(cfg, top_n)[0]))
    else:
        report["steps"].append({"name": "tradingagents", "status": "skipped_by_configuration"})
    step("settle_predictions", lambda: settle_predictions(cfg))
    step("settle_agent_analyses", lambda: settle_agent_analyses(cfg))
    step("settle_screen_selections", lambda: settle_screen_selections(cfg))
    step("build_report", lambda: str(build_research_report(cfg)))
    report["outcome"] = "complete" if all(
        item["status"] in {"ok", "skipped_by_configuration"} for item in report["steps"]
    ) else "partial_failure"
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    runs = resolve_project_path(cfg, cfg["runs_dir"])
    path = runs / f"cycle_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    report["result_path"] = str(path.resolve())
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        record_run(connection, "cycle", cfg, path)
    return report

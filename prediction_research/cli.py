from __future__ import annotations

import argparse
import json
import sys

from .adapters.eastmoney import fetch_universe
from .config import external_data_dir, load_config, market_data_dir
from .pipeline import doctor, run_backtest, run_prediction, settle_predictions


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Point-in-time ETF prediction research")
    root.add_argument("--config", help="independent research JSON config")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="inspect data coverage without changing source data")
    commands.add_parser("status", help="show the end-to-end workflow checkpoint")
    commands.add_parser("report", help="build a consolidated auditable research report")
    commands.add_parser("backtest-flow", help="reconstruct historical ETF screens with paired cohort baselines")
    integrate = commands.add_parser("integrate-etfs", help="run ETF evidence stages with per-stage Markdown")
    integrate.add_argument("--universe", help="configured ETF experiment universe")
    commands.add_parser("evidence-status", help="show ETF evidence and deferred external modules")
    commands.add_parser("evidence-profiles", help="describe ETF flow observations without inferring account identity")
    commands.add_parser("evidence-chains", help="show available news-sector-ETF evidence paths")
    evidence_import = commands.add_parser("import-evidence", help="import ETF-only standardized external JSON")
    evidence_import.add_argument("path")
    evidence_test = commands.add_parser("backtest-evidence", help="paired evidence ablations against price-volume v2")
    evidence_test.add_argument("--universe", default="commodity")
    evidence_test.add_argument("--horizon", type=int, default=5)
    cycle = commands.add_parser("cycle", help="run the end-to-end research workflow")
    cycle.add_argument("--top", type=int, default=3)
    cycle.add_argument("--skip-fetch", action="store_true", help="reuse frozen network snapshots")
    fetch = commands.add_parser("fetch", help="fetch daily bars into the isolated dataset directory")
    fetch.add_argument("--universe", default="commodity")
    commands.add_parser("fetch-external", help="fetch mapped commodity futures series")
    commands.add_parser("fetch-news", help="snapshot and score configured news sources")
    commands.add_parser("fetch-etfs", help="snapshot the complete exchange ETF quote universe")
    commands.add_parser("etf-summary", help="summarize current ETF types and unclassified records")
    commands.add_parser("ingest-etfs", help="idempotently store the latest ETF flow snapshot in SQLite")
    screen = commands.add_parser("screen-etfs", help="coarse-screen ETFs by defined money-flow and liquidity rules")
    screen.add_argument("--limit", type=int)
    agents_plan = commands.add_parser("agents-plan", help="show TradingAgents candidates without calling an LLM")
    agents_plan.add_argument("--top", type=int, default=3)
    agents = commands.add_parser("run-agents", help="run TradingAgents only for the frozen coarse-screen candidates")
    agents.add_argument("--top", type=int, default=3)
    screened = commands.add_parser("fetch-screened", help="fetch daily histories for frozen screen candidates")
    screened.add_argument("--top", type=int, default=20)
    commands.add_parser("settle-agents", help="settle successful TradingAgents directions at configured horizons")
    commands.add_parser("settle-screen", help="settle frozen money-flow screen selections")
    events = commands.add_parser("events", help="show recently stored evidence events")
    events.add_argument("--limit", type=int, default=20)
    backtest = commands.add_parser("backtest", help="run strict walk-forward validation")
    backtest.add_argument("--universe", default="cached")
    backtest.add_argument("--horizon", type=int, default=5)
    backtest.add_argument("--feature-set", choices=("auto", "base", "external"), default="auto")
    backtest.add_argument("--model-scope", choices=("pooled", "exposure"), default="pooled")
    predict = commands.add_parser("predict", help="freeze latest predictions")
    predict.add_argument("--universe", default="commodity")
    predict.add_argument("--horizon", type=int, default=5)
    predict.add_argument("--feature-set", choices=("auto", "base", "external"), default="auto")
    predict.add_argument("--model-scope", choices=("pooled", "exposure"), default="pooled")
    commands.add_parser("settle", help="settle predictions whose horizon has elapsed")
    return root


def main(argv: list[str] | None = None) -> int:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.command == "integrate-etfs":
            from .etf_integration import run_etf_integration

            payload = run_etf_integration(cfg, args.universe)
            result = {key: payload[key] for key in ("outcome", "summary_path", "scope")}
            result["stages"] = [{key: row[key] for key in ("stage", "name", "status", "markdown_path")}
                                for row in payload["stages"]]
        elif args.command == "backtest-flow":
            from .flow_backtest import run_flow_backtest

            path, payload = run_flow_backtest(cfg)
            result = {"result_path": str(path.resolve()), **{key: value for key, value in payload.items()
                       if key not in {"cohorts", "price_snapshots"}}}
        elif args.command == "evidence-status":
            from .evidence import evidence_status

            result = evidence_status(cfg)
        elif args.command == "evidence-profiles":
            from .etf_evidence import etf_profiles

            result = etf_profiles(cfg)
        elif args.command == "evidence-chains":
            from .etf_evidence import chain_report

            result = chain_report(cfg)
        elif args.command == "import-evidence":
            from pathlib import Path

            from .evidence import import_evidence

            result = import_evidence(cfg, Path(args.path))
        elif args.command == "backtest-evidence":
            from .evidence_experiments import run_evidence_experiments

            path, payload = run_evidence_experiments(cfg, args.universe, args.horizon)
            result = {"result_path": str(path.resolve()), "status": payload["status"],
                      "baseline_metrics": payload["baseline_metrics"], "readiness": payload["readiness"]}
        elif args.command == "doctor":
            result = doctor(cfg)
        elif args.command == "status":
            from .workflow import workflow_status

            result = workflow_status(cfg)
        elif args.command == "report":
            from .reporting import build_research_report

            path = build_research_report(cfg)
            result = {"result_path": str(path.resolve())}
        elif args.command == "cycle":
            from .cycle import run_cycle

            result = run_cycle(cfg, args.top, args.skip_fetch)
        elif args.command == "fetch":
            universe = cfg["universes"].get(args.universe)
            if not universe:
                raise ValueError(f"unknown or empty universe: {args.universe}")
            result = fetch_universe(universe, market_data_dir(cfg))
        elif args.command == "fetch-external":
            from .adapters.sina_futures import fetch_all

            result = fetch_all(cfg["external_series"], external_data_dir(cfg))
        elif args.command == "fetch-news":
            from .events import fetch_news

            result = fetch_news(cfg)
        elif args.command == "fetch-etfs":
            from .adapters.eastmoney_etf import fetch_etf_snapshot
            from .config import resolve_project_path
            from .screening import ingest_etf_snapshot

            result = fetch_etf_snapshot(resolve_project_path(cfg, cfg["etf_market"]["snapshot_dir"]))
            result["storage"] = ingest_etf_snapshot(cfg, result)
        elif args.command == "etf-summary":
            from .screening import summarize_etfs

            result = summarize_etfs(cfg)
        elif args.command == "ingest-etfs":
            from .screening import ingest_etf_snapshot

            result = ingest_etf_snapshot(cfg)
        elif args.command == "screen-etfs":
            from .screening import screen_etfs

            path, payload = screen_etfs(cfg, args.limit)
            result = {"result_path": str(path.resolve()), "universe_records": payload["universe_records"], "eligible_records": payload["eligible_records"], "selected": payload["selected"]}
        elif args.command == "agents-plan":
            from .tradingagents_adapter import build_plan

            result = build_plan(cfg, args.top)
        elif args.command == "run-agents":
            from .tradingagents_adapter import run_tradingagents

            path, payload = run_tradingagents(cfg, args.top)
            result = {"result_path": str(path.resolve()), "items": payload["items"]}
        elif args.command == "fetch-screened":
            from .screening import latest_screen

            _, screen_payload = latest_screen(cfg)
            assets = [{"symbol": row["symbol"]} for row in screen_payload["selected"][:args.top]]
            result = fetch_universe(assets, market_data_dir(cfg))
        elif args.command == "settle-agents":
            from .tradingagents_adapter import settle_agent_analyses

            result = settle_agent_analyses(cfg)
        elif args.command == "settle-screen":
            from .screening import settle_screen_selections

            result = settle_screen_selections(cfg)
        elif args.command == "events":
            from .events import recent_events

            result = recent_events(cfg, args.limit)
        elif args.command == "backtest":
            if args.horizon not in cfg["horizons"]:
                raise ValueError(f"unsupported horizon: {args.horizon}")
            path, payload = run_backtest(cfg, args.universe, args.horizon, args.feature_set, args.model_scope)
            result = {"result_path": str(path.resolve()), "metrics": payload["metrics"], "folds": len(payload["folds"])}
        elif args.command == "predict":
            if args.horizon not in cfg["horizons"]:
                raise ValueError(f"unsupported horizon: {args.horizon}")
            path, payload = run_prediction(cfg, args.universe, args.horizon, args.feature_set, args.model_scope)
            result = {"result_path": str(path.resolve()), "predictions": payload["predictions"]}
        else:
            result = settle_predictions(cfg)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command in {"integrate-etfs", "cycle"} and result["outcome"] == "partial_failure":
            return 2
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

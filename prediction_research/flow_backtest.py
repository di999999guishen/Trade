"""Point-in-time ETF screen reconstruction and paired, overlapping cohort diagnostics."""
from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .config import resolve_project_path
from .data import configured_data_dirs, find_cache
from .evidence import cutoff_utc, now_utc, utc
from .evaluation import metrics, serialize_results, walk_forward
from .features import build_samples
from .screening import rule_identity, select_etfs
from .settlement import decision_outcome
from .store import connect, record_run


def reconstruct_screens(cfg: dict) -> dict:
    rules = cfg["etf_market"]["screen"]
    limit = int(rules["top_n"])
    identity = rule_identity(rules, limit)
    zone = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))
    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        connection.execute("INSERT OR IGNORE INTO screen_rule_versions VALUES (?, ?, ?)",
                           (identity, now_utc(), json.dumps({"rules": rules, "selection_limit": limit}, sort_keys=True)))
        registered = connection.execute("SELECT first_seen_at_utc FROM screen_rule_versions WHERE rule_hash=?", (identity,)).fetchone()[0]
        batches = {row[0]: json.loads(row[1]) for row in connection.execute(
            "SELECT feature_date,payload_json FROM screen_batches WHERE rule_hash=?", (identity,)).fetchall()}
        connection.commit()
        rows = connection.execute("SELECT snapshot_sha256,symbol,name,quote_epoch,retrieved_at_utc,price,change_pct,"
                                  "amount,market_cap,main_net_inflow,main_net_inflow_pct,super_large_net_inflow,"
                                  "super_large_net_inflow_pct,large_net_inflow,large_net_inflow_pct FROM etf_flow_snapshots "
                                  "ORDER BY retrieved_at_utc,snapshot_sha256,symbol").fetchall()
    snapshots = {}
    observed_days = set()
    for row in rows:
        if row[3] is None:
            continue
        day = datetime.fromtimestamp(row[3], zone).date()
        observed_days.add(day.isoformat())
        key = (day.isoformat(), row[0])
        snapshot = snapshots.setdefault(key, {"day": day, "sha256": row[0], "available": utc(row[4]), "rows": []})
        snapshot["available"] = max(snapshot["available"], utc(row[4]))
        snapshot["rows"].append(dict(zip(("symbol", "name", "quote_epoch", "price", "change_pct", "amount",
                                           "market_cap", "main_net_inflow", "main_net_inflow_pct",
                                           "super_large_net_inflow", "super_large_net_inflow_pct",
                                           "large_net_inflow", "large_net_inflow_pct"),
                                          (row[1], row[2], row[3], *row[5:]))))
    daily = {}
    late = 0
    for snapshot in snapshots.values():
        batch = batches.get(snapshot["day"].isoformat())
        if batch and batch["source_snapshot"]["sha256"] != snapshot["sha256"]:
            continue
        cutoff = utc(batch["decision_at_utc"]) if batch else cutoff_utc(cfg, snapshot["day"])
        if snapshot["available"] > cutoff:
            late += 1
            continue
        rows = [row for row in snapshot["rows"] if row["quote_epoch"] <= cutoff.timestamp()]
        if not rows:
            continue
        key = snapshot["day"].isoformat()
        if key not in daily or (snapshot["available"], snapshot["sha256"]) > (daily[key]["available"], daily[key]["sha256"]):
            daily[key] = {**snapshot, "rows": rows, "cutoff": cutoff,
                          "decision_basis": "frozen_live_decision" if batch else "configured_historical_cutoff"}
    cohorts = []
    for day, snapshot in sorted(daily.items()):
        eligible, selected = select_etfs(snapshot["rows"], rules, limit)
        if not selected:
            continue
        momentum = sorted(eligible, key=lambda row: (row.get("change_pct") or 0, row["symbol"]), reverse=True)[:limit]
        cutoff = snapshot["cutoff"]
        cohorts.append({"feature_date": day, "decision_at_utc": cutoff.isoformat(), "snapshot_sha256": snapshot["sha256"],
                        "decision_basis": snapshot["decision_basis"],
                        "snapshot_available_at_utc": snapshot["available"].isoformat(),
                        "rule_known_at_decision": utc(registered) <= cutoff,
                        "groups": {"money_flow": [row["symbol"] for row in selected],
                                   "equal_weight_eligible": sorted(row["symbol"] for row in eligible),
                                   "momentum": [row["symbol"] for row in momentum]}})
    return {"rule_hash": identity, "rule_registered_at_utc": registered, "observed_days": len(observed_days),
            "timely_screen_days": len(cohorts), "late_snapshots_excluded": late, "cohorts": cohorts}


def run_flow_backtest(cfg: dict) -> tuple:
    rebuilt = reconstruct_screens(cfg)
    directories = configured_data_dirs(cfg)
    cache, missing = {}, set()
    cohorts = []
    cost_bps = float(cfg.get("workflow", {}).get("flow_round_trip_cost_bps", 10))
    if cost_bps < 0:
        raise ValueError("round-trip cost cannot be negative")
    for cohort in rebuilt["cohorts"]:
        symbols = set().union(*map(set, cohort["groups"].values()))
        for symbol in sorted(symbols):
            if symbol not in cache:
                try:
                    cache[symbol] = find_cache(symbol, directories)
                except (FileNotFoundError, ValueError):
                    cache[symbol] = None
                    missing.add(symbol)
        for horizon in cfg["horizons"]:
            outcomes = {symbol: decision_outcome(cfg, cache[symbol], cohort["feature_date"], horizon,
                                                cohort["decision_at_utc"]) if cache[symbol] else None for symbol in symbols}
            unavailable = [symbol for symbol, result in outcomes.items() if result is None]
            groups = {}
            if not unavailable:
                for name, assets in cohort["groups"].items():
                    gross = statistics.fmean(outcomes[symbol]["actual_return"] for symbol in assets)
                    groups[name] = {"count": len(assets), "gross_return": gross,
                                    "net_return": gross - cost_bps / 10000}
            cohorts.append({**cohort, "horizon": horizon, "status": "waiting_for_bars_or_horizon" if unavailable else "paired",
                            "unavailable_symbols": sorted(unavailable), "returns": groups})
    minimum = int(cfg.get("evidence", {}).get("minimum_observation_days", 60))
    ready = len({row["feature_date"] for row in cohorts if row["rule_known_at_decision"] and row["status"] == "paired"}) >= minimum
    summary = {}
    if ready:
        for horizon in cfg["horizons"]:
            paired = [row for row in cohorts if row["horizon"] == horizon and row["status"] == "paired" and row["rule_known_at_decision"]]
            if len(paired) < minimum:
                ready = False
                break
            summary[str(horizon)] = {name: {"cohorts": len(paired), "mean_net_cohort_return": statistics.fmean(row["returns"][name]["net_return"] for row in paired)}
                                     for name in ("money_flow", "equal_weight_eligible", "momentum")}
    probability_results, model_cache = {}, {}
    model_waits = []
    if ready:
        # Train/evaluate on the universe known in each historical screen, never a future candidate pool.
        for cohort in cohorts:
            if cohort["status"] != "paired" or not cohort["rule_known_at_decision"]:
                continue
            pool = tuple(cohort["groups"]["equal_weight_eligible"])
            key = (pool, cohort["horizon"])
            if key not in model_cache:
                samples = [sample for symbol in pool for sample in build_samples(cache[symbol], cohort["horizon"])]
                predictions, _ = walk_forward(samples, cfg["model"])
                model_cache[key] = {(row.symbol, row.feature_date.isoformat()): row for row in predictions}
            selected = []
            for symbol in cohort["groups"]["money_flow"]:
                predicted = model_cache[key].get((symbol, cohort["feature_date"]))
                outcome = decision_outcome(cfg, cache[symbol], cohort["feature_date"], cohort["horizon"], cohort["decision_at_utc"])
                if predicted is None or predicted.target_end_date.isoformat() != outcome["target_end_date"]:
                    model_waits.append({"symbol": symbol, "feature_date": cohort["feature_date"],
                                        "horizon": cohort["horizon"], "reason": "no_matching_out_of_sample_prediction"})
                else:
                    selected.append(predicted)
            if len(selected) == len(cohort["groups"]["money_flow"]):
                probability_results.setdefault(cohort["horizon"], []).extend(selected)
        if model_waits:
            ready = False
    payload = {"run_type": "historical_etf_flow_screen", "created_at_utc": now_utc(),
               **{key: value for key, value in rebuilt.items() if key != "cohorts"},
               "status": "evaluated_research_only" if ready else "waiting_for_history_or_prices",
               "minimum_paired_days_per_horizon": minimum, "round_trip_cost_bps": cost_bps,
               "screen_rules": cfg["etf_market"]["screen"], "model_config": cfg["model"],
               "missing_histories": sorted(missing), "cohorts": cohorts, "metrics": summary if ready else {},
               "probability_metrics_after_historical_screen": {str(h): metrics(rows) for h, rows in probability_results.items()} if ready else {},
               "out_of_sample_predictions_after_screen": {str(h): serialize_results(rows) for h, rows in probability_results.items()},
               "model_waits": model_waits,
               "price_snapshots": {symbol: snapshot.sha256 for symbol, snapshot in cache.items() if snapshot},
               "label": "first session open after decision to horizon close",
               "interpretation": "重叠的独立等权 cohort；不是连续组合净值，不报告年化收益或 Sharpe；当前规则登记前的重建仅作事后诊断",
               "cost_limitations": "统一往返费率假设，不保证涨跌停成交，未模拟市场冲击"}
    runs = resolve_project_path(cfg, cfg["runs_dir"])
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / f"flow_strategy_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        record_run(connection, "flow_strategy", cfg, path)
    return path, payload

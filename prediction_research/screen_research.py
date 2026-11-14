"""Offline, read-only screen comparison. No fitted weights or automatic promotion.

Reconstructs the first post-close snapshot of each day; uses only history <=
that date for features and buys at the first open AFTER snapshot availability.
The historical price caches are not a certified point-in-time archive.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import statistics
from collections import Counter
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import resolve_project_path
from .data import configured_data_dirs, find_cache
from .screening import select_etfs, atr14_pct
from .settlement import decision_outcome


def price_features(bars, as_of):
    history = [bar for bar in bars if bar.trading_date <= as_of]
    if len(history) < 61 or history[-1].trading_date != as_of:
        return None
    closes = [bar.close for bar in history[-61:]]
    returns = [b / a - 1 for a, b in zip(closes, closes[1:])]
    if any(abs(r) > .22 for r in returns):
        return None  # unresolved corporate action / bad tick; do not invent an adjustment
    vol = statistics.pstdev(returns[-20:])
    return {"momentum20": closes[-1] / closes[-21] - 1,
            "momentum60": closes[-1] / closes[0] - 1,
            "trend60": closes[-1] / statistics.fmean(closes[-60:]) - 1,
            "volatility20": vol,
            "drawdown60": closes[-1] / max(closes) - 1}


def percentiles(values):
    ordered = sorted(set(values))
    if len(values) <= 1:
        return [0.5] * len(values)
    counts = Counter(values)
    ranks, prior = {}, 0
    for value in ordered:
        ranks[value] = (prior + (counts[value] - 1) / 2) / (len(values) - 1)
        prior += counts[value]
    return [ranks[v] for v in values]


def rank_candidates(rows, strategy):
    rows = [dict(row) for row in rows]
    if strategy == "current":
        for row in rows:
            row["research_score"] = row["screen_score"]
    elif strategy == "liquidity":
        for row in rows:
            row["research_score"] = row["amount"]
    elif strategy == "momentum20":
        for row in rows:
            row["research_score"] = row["price_features"]["momentum20"]
    elif strategy == "trend_risk":
        # Fixed hypothesis, not weights selected by observed backtest performance.
        cohorts = {}
        for row in rows:
            cohorts.setdefault((row["asset_class"], row["market_scope"]), []).append(row)
        for cohort in cohorts.values():
            for row in cohort:
                row["research_score"] = 0.0
            for factor, weight, sign in (("momentum20", .30, 1), ("momentum60", .25, 1),
                                        ("trend60", .20, 1), ("volatility20", .15, -1),
                                        ("amount", .10, 1)):
                values = [sign * (row["amount"] if factor == "amount" else row["price_features"][factor]) for row in cohort]
                for row, value in zip(cohort, percentiles(values)):
                    row["research_score"] += weight * value
    else:
        raise ValueError(strategy)
    return sorted(rows, key=lambda r: (-r["research_score"], r["symbol"]))


def choose(rows, top, per_group):
    result, counts = [], Counter()
    for row in rows:
        group = row["screen_group"]
        if counts[group] < per_group:
            result.append(row)
            counts[group] += 1
        if len(result) == top:
            break
    return result


def run_screen_review(cfg, output, top=10):
    if top < 1:
        raise ValueError("top must be positive")
    db = resolve_project_path(cfg, cfg["state_db"])
    con = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    try:
        records = [dict(row) for row in con.execute('SELECT * FROM etf_flow_snapshots ORDER BY retrieved_at_utc')]
    finally:
        con.close()
    zone = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))
    snapshots = {}
    for row in records:
        if not row.get("quote_epoch"):
            continue
        day = datetime.fromtimestamp(row["quote_epoch"], zone).date()
        stamp = datetime.fromisoformat(row["retrieved_at_utc"])
        if stamp < datetime.combine(day, time(15), zone):
            continue
        key = (day, row["snapshot_sha256"])
        value = snapshots.setdefault(key, {"day": day, "available": stamp, "rows": [], "sha": key[1]})
        value["available"] = max(value["available"], stamp)
        value["rows"].append(row)
    daily = {}
    for value in sorted(snapshots.values(), key=lambda v: (v["available"], v["sha"])):
        daily.setdefault(value["day"], value)
    cache, missing, hashes = {}, set(), {}
    def history(symbol):
        if symbol not in cache:
            try:
                cache[symbol] = find_cache(symbol, configured_data_dirs(cfg))
                hashes[symbol] = cache[symbol].sha256
            except (ValueError, FileNotFoundError):
                cache[symbol] = None
                missing.add(symbol)
        return cache[symbol]
    methods = ("current", "liquidity", "momentum20", "trend_risk")
    cohorts, coverage = [], []
    rules = cfg["etf_market"]["screen"]
    for day, snapshot in sorted(daily.items()):
        def atr(symbol, as_of):
            series = history(symbol)
            return atr14_pct(series.bars, as_of) if series else None
        eligible, _ = select_etfs(snapshot["rows"], rules, top, atr_lookup=atr, as_of=day)
        common = []
        for row in eligible:
            series = history(row["symbol"])
            features = price_features(series.bars, day) if series else None
            if features is not None:
                common.append({**row, "price_features": features})
        groups = {name: choose(rank_candidates(common, name), top, int(rules.get("max_per_group", 1))) for name in methods}
        coverage.append({"date": day.isoformat(), "eligible": len(eligible), "common_feature_pool": len(common),
                         "snapshot_sha256": snapshot["sha"], "available_at": snapshot["available"].isoformat()})
        for horizon in cfg.get("horizons", [5, 20]):
            symbols = {row["symbol"] for rows in groups.values() for row in rows}
            outcomes = {sym: decision_outcome(cfg, history(sym), day.isoformat(), horizon, snapshot["available"].isoformat()) for sym in symbols}
            unavailable = sorted(sym for sym, result in outcomes.items() if result is None)
            # No removal of failed / unpriced selections after ranking.
            paired = not unavailable and all(len(rows) == top for rows in groups.values())
            cohort = {"date": day.isoformat(), "horizon": horizon, "status": "paired" if paired else "incomplete",
                      "missing_outcomes": unavailable, "selections": {k:[r['symbol'] for r in rows] for k,rows in groups.items()}}
            if paired:
                gross = {name: statistics.fmean(outcomes[r["symbol"]]["actual_return"] for r in rows) for name, rows in groups.items()}
                cohort.update(gross_returns=gross, net_returns={k:v-.003 for k,v in gross.items()},
                              entry_dates=sorted({v['entry_date'] for v in outcomes.values()}),
                              exit_dates=sorted({v['target_end_date'] for v in outcomes.values()}))
            cohorts.append(cohort)
    summary = {}
    for horizon in cfg.get("horizons", [5, 20]):
        paired = [c for c in cohorts if c['horizon']==horizon and c['status']=='paired']
        result = {"paired_days": len(paired), "methods": {}}
        for method in methods:
            returns = [c['net_returns'][method] for c in paired]
            excess = [c['net_returns'][method]-c['net_returns']['liquidity'] for c in paired]
            result['methods'][method] = {"mean_net_cohort_return": statistics.fmean(returns) if returns else None,
                                         "mean_excess_vs_liquidity": statistics.fmean(excess) if excess else None,
                                         "positive_cohort_fraction": statistics.fmean(r>0 for r in returns) if returns else None}
        summary[str(horizon)] = result
    payload = {"status": "research_only_not_validated", "created_at": datetime.now(timezone.utc).isoformat(),
               "top": top, "methods": list(methods), "summary": summary, "coverage": coverage,
               "cohorts": cohorts, "missing_history_symbols": sorted(missing), "price_hashes": hashes,
               "rules": rules, "round_trip_cost_bps": 30,
               "limitations": ["Overlapping cohort diagnostics, not continuous portfolio NAV; no CAGR or Sharpe.",
                   "Retrospective fixed hypotheses; not an untouched holdout or certified point-in-time price universe.",
                   "Compare on the same history-available pool; this is not full-universe current production performance.",
                   "Unresolved >22% daily discontinuities excluded using only predecision history; other corporate actions remain unverified.",
                   "30bp is a flat assumed round-trip cost, not a liquidity/limit/slippage execution model.",
                   "No historical news signal tested: old reports only cover holdings and lack complete point-in-time event archives."],
               "promotion_allowed": False}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return {"path": str(output.resolve()), "status": payload['status'], "summary": summary,
            "observation_days": len(coverage), "promotion_allowed": False}

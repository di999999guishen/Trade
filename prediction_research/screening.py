from __future__ import annotations

import json
import math
import hashlib
import uuid
import statistics
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .config import resolve_project_path
from .taxonomy import classify_etf, market_scope, screen_group
from .store import connect
from .config import market_data_dir
from .data import load_cache
from .features import build_samples
from .evidence import utc, now_utc
from .flow_context import finite
from .order_divergence import order_divergence
from .opportunity import build_opportunity_observation


def _number(value, default=0.0) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _snapshot_path(directory: Path, manifest: dict) -> Path:
    recorded = Path(manifest["path"])
    if recorded.exists():
        return recorded
    migrated = directory / recorded.name
    if migrated.exists():
        return migrated
    raise FileNotFoundError(f"ETF snapshot not found at recorded or migrated path: {recorded}")


def selection_limit(rules: dict, limit: int | None = None) -> int | None:
    value = rules.get("top_n") if limit is None else limit
    if value is None:
        return None
    value = int(value)
    if value < 1:
        raise ValueError("selection limit must be positive or null for unlimited")
    return value


def rule_identity(rules: dict, limit: int | None) -> str:
    return hashlib.sha256(json.dumps({"rules": rules, "selection_limit": limit,
                                     "algorithm_version": 5}, sort_keys=True).encode("utf-8")).hexdigest()


def select_etfs(records: list[dict], rules: dict, limit: int | None = None) -> tuple[list, list]:
    cap = selection_limit(rules, limit)
    eligible = []
    for row in records:
        row = {**row, **classify_etf(row["name"]), "market_scope": market_scope(row["name"])}
        amount = finite(row.get("amount")) or 0.0
        market_cap = finite(row.get("market_cap")) or 0.0
        price = finite(row.get("price")) or -1.0
        if row["asset_class"] not in rules["asset_classes"] or amount <= 0 or market_cap <= 0 or amount < rules["min_amount"] or market_cap < rules["min_market_cap"] or price <= 0:
            continue
        if finite(row.get("main_net_inflow_pct")) is None or finite(row.get("main_net_inflow")) is None:
            continue
        flow_pct = max(-30.0, min(30.0, _number(row.get("main_net_inflow_pct")))) / 30.0
        change = finite(row.get("change_pct"))
        momentum = max(-10.0, min(10.0, change)) / 10.0 if change is not None else None
        liquidity = min(1.0, max(0.0, (math.log10(amount) - 6.0) / 4.0))
        divergence = order_divergence(row, rules)
        weights = {"flow": 0.50, "liquidity": 0.35, "momentum": 0.15,
                   "order_divergence": 0.15}
        configured = rules.get("score_weights", {})
        weights.update({"flow": float(configured.get("main_flow", weights["flow"])),
                        "liquidity": float(configured.get("liquidity", weights["liquidity"])),
                        "momentum": float(configured.get("momentum", weights["momentum"])),
                        "order_divergence": float(configured.get("order_divergence", weights["order_divergence"]))})
        weights.update({name: float(configured.get(name, 0.0))
                        for name in ("flow_to_cap", "relative_strength")})
        if any(not math.isfinite(value) or value < 0 for value in weights.values()):
            raise ValueError("screen score weights cannot be negative")
        components = {"flow": flow_pct, "liquidity": liquidity, "momentum": momentum,
                      "order_divergence": divergence["factor"],
                      "flow_to_cap": max(-1.0, min(1.0, row["main_net_inflow"] / market_cap / 0.01)),
                      "relative_strength": None}
        eligible.append({**row, "screen_group": screen_group(row["name"], row["subtype"]),
                         "screen_components": components, "screen_weights": weights,
                         "order_divergence": divergence})
    # Compare only eligible instruments in the same asset class and market scope.
    # A singleton has no relative-strength evidence; it must not receive a bonus.
    peers = {}
    for row in eligible:
        change = finite(row.get("change_pct"))
        if change is not None:
            peers.setdefault((row["asset_class"], row["market_scope"]), []).append(change)
    for row in eligible:
        cohort = peers.get((row["asset_class"], row["market_scope"]), [])
        change = finite(row.get("change_pct"))
        baseline = statistics.median(cohort) if len(cohort) >= 3 else None
        components, weights = row["screen_components"], row["screen_weights"]
        components["relative_strength"] = (round(max(-1.0, min(1.0, (change - baseline) / 5.0)), 6)
                                           if baseline is not None and change is not None else None)
        row["relative_strength_context"] = {"peer_count": len(cohort), "median_change_pct": baseline,
                                            "basis": "eligible_same_asset_class_and_market_scope"}
        denominator = sum(weights[name] for name, value in components.items() if value is not None)
        if denominator <= 0:
            raise ValueError("screen score has no available positive-weight components")
        row["screen_contributions"] = {name: round(weights[name] * value / denominator, 6)
                                        if value is not None else None for name, value in components.items()}
        row["screen_components"] = {name: round(value, 6) if value is not None else None
                                    for name, value in components.items()}
        row["screen_score"] = round(sum(value for value in row["screen_contributions"].values() if value is not None), 6)
    eligible.sort(key=lambda row: (row["screen_score"], _number(row.get("amount"))), reverse=True)
    selected = []
    group_counts: dict[str, int] = {}
    for row in eligible:
        group = row["screen_group"]
        if group_counts.get(group, 0) >= int(rules.get("max_per_group", 1)):
            continue
        selected.append(row)
        group_counts[group] = group_counts.get(group, 0) + 1
        if cap is not None and len(selected) >= cap:
            break
    return eligible, selected


def screen_etfs(cfg: dict, limit: int | None = None, decision_at_utc: str | None = None) -> tuple[Path, dict]:
    directory = resolve_project_path(cfg, cfg["etf_market"]["snapshot_dir"])
    manifest = json.loads((directory / "latest.json").read_text(encoding="utf-8"))
    snapshot = json.loads(_snapshot_path(directory, manifest).read_text(encoding="utf-8"))
    decision = utc(decision_at_utc or now_utc())
    if utc(manifest["retrieved_at_utc"]) > decision:
        raise ValueError("ETF snapshot was unavailable at decision time")
    max_age = float(cfg.get("workflow", {}).get("max_quote_age_days", 4)) * 86400
    fresh = [row for row in snapshot["records"] if finite(row.get("quote_epoch")) is not None
             and 0 <= decision.timestamp() - row["quote_epoch"] <= max_age]
    if fresh:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))
        quote_day = lambda row: datetime.fromtimestamp(row["quote_epoch"], zone).date()
        latest_quote_day = max(quote_day(row) for row in fresh)
        fresh = [row for row in fresh if quote_day(row) == latest_quote_day]
    rules = cfg["etf_market"]["screen"]
    cap = selection_limit(rules, limit)
    eligible, selected = select_etfs(fresh, rules, cap)
    if not selected:
        raise ValueError("no eligible ETF candidates with fresh, complete money-flow data")
    observation = build_opportunity_observation(cfg, eligible, selected, manifest, decision.isoformat())
    payload = {
        "run_type": "etf_money_flow_coarse_screen", "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_snapshot": manifest, "universe_records": len(snapshot["records"]), "eligible_records": len(eligible),
        "rules": rules,
        "score_formula": "weighted mean of available main flow, liquidity, momentum, order divergence, flow/market-cap (1% clip) and peer-relative daily strength (5 percentage-point clip); missing factors excluded; heuristic weights, not calibrated probabilities",
        "flow_caveat": snapshot["flow_definition"], "selected": selected,
        "decision_at_utc": decision.isoformat(), "fresh_records": len(fresh),
        "screen_freeze_policy": "first_daily_selection_wins",
        "opportunity_observation": observation,
        "order_divergence_coverage": {
            "eligible_with_data": sum(row["order_divergence"]["status"] == "available" for row in eligible),
            "selected_with_data": sum(row["order_divergence"]["status"] == "available" for row in selected),
            "selected_bullish": sum(row["order_divergence"]["signal"] == "bullish_divergence" for row in selected),
            "selected_bearish": sum(row["order_divergence"]["signal"] == "bearish_divergence" for row in selected),
        },
    }
    runs = resolve_project_path(cfg, cfg["runs_dir"])
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / f"screen_etf_flow_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    rule_hash = rule_identity(rules, cap)
    epochs = [int(row["quote_epoch"]) for row in selected if row.get("quote_epoch")]
    feature_date = datetime.fromtimestamp(max(epochs), timezone(timedelta(hours=8))).date().isoformat() if epochs else datetime.now().date().isoformat()
    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        connection.execute("INSERT OR IGNORE INTO screen_rule_versions VALUES (?, ?, ?)",
                           (rule_hash, now_utc(), json.dumps({"rules": rules, "selection_limit": cap}, sort_keys=True)))
        previous = connection.execute("SELECT payload_json FROM screen_batches WHERE feature_date=? AND rule_hash=?",
                                      (feature_date, rule_hash)).fetchone()
        if previous:
            payload = json.loads(previous[0])
            if utc(payload["decision_at_utc"]) > decision:
                raise ValueError("frozen screen was created after the requested decision")
            selected = payload["selected"]
            payload["reused_frozen_screen"] = True
            # Keep the historical screen byte-for-byte in the database. New
            # observations have their own source and time, never inherited by
            # old selections or presented as past decision evidence.
            frozen_symbols = {row["symbol"] for row in selected}
            for row in observation["rows"]:
                row["in_frozen_selection"] = row["symbol"] in frozen_symbols
            observation["coverage"]["selected"] = len(selected)
            observation["binding"] = "current_observation_not_frozen_decision_evidence"
            payload["current_opportunity_observation"] = observation
        else:
            payload["rule_hash"] = rule_hash
            connection.execute("INSERT INTO screen_batches VALUES (?, ?, ?)",
                               (feature_date, rule_hash, json.dumps(payload, ensure_ascii=False)))
        for rank, row in enumerate(selected, 1):
            for horizon in cfg["horizons"]:
                selection_id = uuid.uuid4().hex
                before = connection.total_changes
                connection.execute(
                    """INSERT INTO screen_selections
                    (selection_id,snapshot_sha256,rule_hash,screen_report,feature_date,rank,symbol,screen_score,horizon)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(feature_date,rule_hash,symbol,horizon) DO NOTHING""",
                    (selection_id, payload["source_snapshot"]["sha256"], rule_hash, str(path.resolve()), feature_date,
                     rank, row["symbol"], row["screen_score"], horizon),
                )
                if connection.total_changes > before:
                    connection.execute("INSERT INTO screen_selection_times VALUES (?, ?)",
                                       (selection_id, payload["decision_at_utc"]))
        connection.commit()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, payload


def latest_screen(cfg: dict) -> tuple[Path, dict]:
    runs = resolve_project_path(cfg, cfg["runs_dir"])
    paths = sorted(runs.glob("screen_etf_flow_*.json"), reverse=True)
    if not paths:
        raise FileNotFoundError("no ETF screen report; run fetch-etfs and screen-etfs first")
    return paths[0], json.loads(paths[0].read_text(encoding="utf-8"))


def summarize_etfs(cfg: dict) -> dict:
    directory = resolve_project_path(cfg, cfg["etf_market"]["snapshot_dir"])
    manifest = json.loads((directory / "latest.json").read_text(encoding="utf-8"))
    snapshot = json.loads(_snapshot_path(directory, manifest).read_text(encoding="utf-8"))
    rows = [{**row, **classify_etf(row["name"]), "market_scope": market_scope(row["name"])} for row in snapshot["records"]]
    return {
        "snapshot": manifest,
        "records": len(rows),
        "asset_classes": dict(sorted(Counter(row["asset_class"] for row in rows).items())),
        "subtypes": dict(sorted(Counter(row["subtype"] for row in rows).items())),
        "market_scopes": dict(sorted(Counter(row["market_scope"] for row in rows).items())),
        "unknown_examples": [{"symbol": row["symbol"], "name": row["name"]} for row in rows if row["asset_class"] == "unknown"][:20],
        "classification_warning": "Counts cover the configured Eastmoney ETF quote boards and use name inference. Bond and multi-asset types exist in the official taxonomy but are absent from this source snapshot; exchange contract-level reconciliation is required before claiming a complete listed-product inventory.",
    }


def ingest_etf_snapshot(cfg: dict, manifest: dict | None = None) -> dict:
    directory = resolve_project_path(cfg, cfg["etf_market"]["snapshot_dir"])
    manifest = manifest or json.loads((directory / "latest.json").read_text(encoding="utf-8"))
    snapshot = json.loads(_snapshot_path(directory, manifest).read_text(encoding="utf-8"))
    inserted = 0
    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        for source_row in snapshot["records"]:
            row = {**source_row, **classify_etf(source_row["name"]), "market_scope": market_scope(source_row["name"])}
            before = connection.total_changes
            connection.execute(
                """INSERT OR IGNORE INTO etf_flow_snapshots
                (snapshot_sha256,symbol,exchange,name,quote_epoch,retrieved_at_utc,asset_class,subtype,market_scope,
                 price,change_pct,amount,market_cap,main_net_inflow,main_net_inflow_pct,
                 super_large_net_inflow,super_large_net_inflow_pct,large_net_inflow,large_net_inflow_pct,
                 medium_net_inflow,medium_net_inflow_pct,small_net_inflow,small_net_inflow_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (manifest["sha256"], row["symbol"], row["exchange"], row["name"], row.get("quote_epoch"),
                 manifest["retrieved_at_utc"], row["asset_class"], row["subtype"], row["market_scope"],
                 row.get("price"), row.get("change_pct"), row.get("amount"), row.get("market_cap"),
                 row.get("main_net_inflow"), row.get("main_net_inflow_pct"), row.get("super_large_net_inflow"),
                 row.get("super_large_net_inflow_pct"), row.get("large_net_inflow"), row.get("large_net_inflow_pct"),
                 row.get("medium_net_inflow"), row.get("medium_net_inflow_pct"),
                 row.get("small_net_inflow"), row.get("small_net_inflow_pct")),
            )
            inserted += int(connection.total_changes > before)
        connection.commit()
        observations = connection.execute("SELECT count(DISTINCT date(quote_epoch, 'unixepoch', '+8 hours')) FROM etf_flow_snapshots WHERE quote_epoch IS NOT NULL").fetchone()[0]
        rows = connection.execute("SELECT count(1) FROM etf_flow_snapshots").fetchone()[0]
    return {"snapshot_sha256": manifest["sha256"], "inserted": inserted, "stored_rows": rows, "observation_days": observations}


def settle_screen_selections(cfg: dict) -> dict:
    settled, waiting = [], []
    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        rows = connection.execute(
            "SELECT selection_id,symbol,feature_date,horizon FROM screen_selections WHERE settled_at_utc IS NULL"
        ).fetchall()
        for selection_id, symbol, feature_date, horizon in rows:
            try:
                snapshot = load_cache(symbol, market_data_dir(cfg))
            except (FileNotFoundError, ValueError):
                waiting.append({"selection_id": selection_id, "symbol": symbol, "status": "waiting_for_bars"})
                continue
            from .settlement import decision_outcome

            stored = connection.execute("SELECT decision_at_utc FROM screen_selection_times WHERE selection_id=?", (selection_id,)).fetchone()
            outcome = decision_outcome(cfg, snapshot, feature_date, horizon, stored[0] if stored else None)
            if outcome is None:
                waiting.append({"selection_id": selection_id, "symbol": symbol, "status": "waiting_for_horizon"})
                continue
            connection.execute(
                """UPDATE screen_selections SET target_end_date=?,actual_return=?,actual_up=?,settled_at_utc=?
                WHERE selection_id=? AND settled_at_utc IS NULL""",
                (outcome["target_end_date"], outcome["actual_return"], outcome["actual_up"], datetime.now(timezone.utc).isoformat(), selection_id),
            )
            settled.append({"selection_id": selection_id, "symbol": symbol, "horizon": horizon, **outcome})
        connection.commit()
    return {"open_selections": len(rows), "settled": len(settled), "items": settled, "waiting": waiting}

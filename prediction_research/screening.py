from __future__ import annotations

import json
import math
import hashlib
import uuid
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


def rule_identity(rules: dict, limit: int) -> str:
    return hashlib.sha256(json.dumps({"rules": rules, "selection_limit": limit,
                                     "algorithm_version": 3}, sort_keys=True).encode("utf-8")).hexdigest()


def select_etfs(records: list[dict], rules: dict, limit: int | None = None) -> tuple[list, list]:
    selection_limit = int(limit or rules["top_n"])
    if selection_limit < 1:
        raise ValueError("selection limit must be positive")
    eligible = []
    for row in records:
        row = {**row, **classify_etf(row["name"]), "market_scope": market_scope(row["name"])}
        amount = _number(row.get("amount"))
        market_cap = _number(row.get("market_cap"))
        price = _number(row.get("price"), -1)
        if row["asset_class"] not in rules["asset_classes"] or amount < rules["min_amount"] or market_cap < rules["min_market_cap"] or price <= 0:
            continue
        if finite(row.get("main_net_inflow_pct")) is None or finite(row.get("main_net_inflow")) is None:
            continue
        flow_pct = max(-30.0, min(30.0, _number(row.get("main_net_inflow_pct")))) / 30.0
        momentum = max(-10.0, min(10.0, _number(row.get("change_pct")))) / 10.0
        liquidity = min(1.0, max(0.0, (math.log10(amount) - 6.0) / 4.0))
        divergence = order_divergence(row, rules)
        weights = {"flow": 0.50, "liquidity": 0.35, "momentum": 0.15,
                   "order_divergence": 0.15}
        configured = rules.get("score_weights", {})
        weights.update({"flow": float(configured.get("main_flow", weights["flow"])),
                        "liquidity": float(configured.get("liquidity", weights["liquidity"])),
                        "momentum": float(configured.get("momentum", weights["momentum"])),
                        "order_divergence": float(configured.get("order_divergence", weights["order_divergence"]))})
        if any(value < 0 for value in weights.values()):
            raise ValueError("screen score weights cannot be negative")
        components = {"flow": flow_pct, "liquidity": liquidity, "momentum": momentum,
                      "order_divergence": divergence["factor"]}
        available = [name for name, value in components.items() if value is not None and weights[name] > 0]
        denominator = sum(weights[name] for name in available)
        if denominator <= 0:
            raise ValueError("screen score has no available positive-weight components")
        contributions = {name: round(weights[name] * value / denominator, 6) if name in available else None
                         for name, value in components.items()}
        score = sum(value for value in contributions.values() if value is not None)
        eligible.append({**row, "screen_group": screen_group(row["name"], row["subtype"]),
                         "screen_score": round(score, 6),
                         "screen_components": {name: round(value, 6) if value is not None else None
                                               for name, value in components.items()},
                         "screen_contributions": contributions, "screen_weights": weights,
                         "order_divergence": divergence})
    eligible.sort(key=lambda row: (row["screen_score"], _number(row.get("amount"))), reverse=True)
    selected = []
    group_counts: dict[str, int] = {}
    for row in eligible:
        group = row["screen_group"]
        if group_counts.get(group, 0) >= int(rules.get("max_per_group", 1)):
            continue
        selected.append(row)
        group_counts[group] = group_counts.get(group, 0) + 1
        if len(selected) >= selection_limit:
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
    selection_limit = int(limit or rules["top_n"])
    eligible, selected = select_etfs(fresh, rules, selection_limit)
    if not selected:
        raise ValueError("no eligible ETF candidates with fresh, complete money-flow data")
    payload = {
        "run_type": "etf_money_flow_coarse_screen", "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_snapshot": manifest, "universe_records": len(snapshot["records"]), "eligible_records": len(eligible),
        "rules": rules,
        "score_formula": "weighted mean of main flow, liquidity, momentum and available super-large-vs-large divergence; missing divergence is excluded from the denominator",
        "flow_caveat": snapshot["flow_definition"], "selected": selected,
        "decision_at_utc": decision.isoformat(), "fresh_records": len(fresh),
        "screen_freeze_policy": "first_daily_selection_wins",
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
    rule_hash = rule_identity(rules, selection_limit)
    epochs = [int(row["quote_epoch"]) for row in selected if row.get("quote_epoch")]
    feature_date = datetime.fromtimestamp(max(epochs), timezone(timedelta(hours=8))).date().isoformat() if epochs else datetime.now().date().isoformat()
    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        connection.execute("INSERT OR IGNORE INTO screen_rule_versions VALUES (?, ?, ?)",
                           (rule_hash, now_utc(), json.dumps({"rules": rules, "selection_limit": selection_limit}, sort_keys=True)))
        previous = connection.execute("SELECT payload_json FROM screen_batches WHERE feature_date=? AND rule_hash=?",
                                      (feature_date, rule_hash)).fetchone()
        if previous:
            payload = json.loads(previous[0])
            if utc(payload["decision_at_utc"]) > decision:
                raise ValueError("frozen screen was created after the requested decision")
            selected = payload["selected"]
            payload["reused_frozen_screen"] = True
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
                 super_large_net_inflow,super_large_net_inflow_pct,large_net_inflow,large_net_inflow_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (manifest["sha256"], row["symbol"], row["exchange"], row["name"], row.get("quote_epoch"),
                 manifest["retrieved_at_utc"], row["asset_class"], row["subtype"], row["market_scope"],
                 row.get("price"), row.get("change_pct"), row.get("amount"), row.get("market_cap"),
                 row.get("main_net_inflow"), row.get("main_net_inflow_pct"), row.get("super_large_net_inflow"),
                 row.get("super_large_net_inflow_pct"), row.get("large_net_inflow"), row.get("large_net_inflow_pct")),
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

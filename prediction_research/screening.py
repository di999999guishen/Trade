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


def screen_etfs(cfg: dict, limit: int | None = None) -> tuple[Path, dict]:
    directory = resolve_project_path(cfg, cfg["etf_market"]["snapshot_dir"])
    manifest = json.loads((directory / "latest.json").read_text(encoding="utf-8"))
    snapshot = json.loads(_snapshot_path(directory, manifest).read_text(encoding="utf-8"))
    rules = cfg["etf_market"]["screen"]
    selection_limit = int(limit or rules["top_n"])
    eligible = []
    for row in snapshot["records"]:
        row = {**row, **classify_etf(row["name"]), "market_scope": market_scope(row["name"])}
        amount = _number(row.get("amount"))
        market_cap = _number(row.get("market_cap"))
        price = _number(row.get("price"), -1)
        if row["asset_class"] not in rules["asset_classes"] or amount < rules["min_amount"] or market_cap < rules["min_market_cap"] or price <= 0:
            continue
        flow_pct = max(-30.0, min(30.0, _number(row.get("main_net_inflow_pct")))) / 30.0
        momentum = max(-10.0, min(10.0, _number(row.get("change_pct")))) / 10.0
        liquidity = min(1.0, max(0.0, (math.log10(amount) - 6.0) / 4.0))
        score = 0.50 * flow_pct + 0.35 * liquidity + 0.15 * momentum
        eligible.append({**row, "screen_group": screen_group(row["name"], row["subtype"]), "screen_score": round(score, 6), "screen_components": {"flow": round(flow_pct, 6), "liquidity": round(liquidity, 6), "momentum": round(momentum, 6)}})
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
    payload = {
        "run_type": "etf_money_flow_coarse_screen", "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_snapshot": manifest, "universe_records": len(snapshot["records"]), "eligible_records": len(eligible),
        "rules": rules, "score_formula": "0.50*clipped_main_flow_ratio + 0.35*log_amount_liquidity + 0.15*clipped_daily_return",
        "flow_caveat": snapshot["flow_definition"], "selected": selected,
    }
    runs = resolve_project_path(cfg, cfg["runs_dir"])
    path = runs / f"screen_etf_flow_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    rule_hash = hashlib.sha256(
        json.dumps(
            {"rules": rules, "selection_limit": selection_limit, "formula": payload["score_formula"]},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    epochs = [int(row["quote_epoch"]) for row in selected if row.get("quote_epoch")]
    feature_date = datetime.fromtimestamp(max(epochs), timezone(timedelta(hours=8))).date().isoformat() if epochs else datetime.now().date().isoformat()
    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        for rank, row in enumerate(selected, 1):
            for horizon in cfg["horizons"]:
                connection.execute(
                    """INSERT INTO screen_selections
                    (selection_id,snapshot_sha256,rule_hash,screen_report,feature_date,rank,symbol,screen_score,horizon)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(feature_date,rule_hash,symbol,horizon) DO UPDATE SET
                    snapshot_sha256=excluded.snapshot_sha256,
                    screen_report=excluded.screen_report,
                    rank=excluded.rank,
                    screen_score=excluded.screen_score""",
                    (uuid.uuid4().hex, manifest["sha256"], rule_hash, str(path.resolve()), feature_date,
                     rank, row["symbol"], row["screen_score"], horizon),
                )
        connection.commit()
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
                """INSERT OR IGNORE INTO etf_flow_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (manifest["sha256"], row["symbol"], row["exchange"], row["name"], row.get("quote_epoch"),
                 manifest["retrieved_at_utc"], row["asset_class"], row["subtype"], row["market_scope"],
                 row.get("price"), row.get("change_pct"), row.get("amount"), row.get("market_cap"),
                 row.get("main_net_inflow"), row.get("main_net_inflow_pct")),
            )
            inserted += int(connection.total_changes > before)
        connection.commit()
        observations = connection.execute("SELECT count(DISTINCT date(quote_epoch, 'unixepoch', '+8 hours')) FROM etf_flow_snapshots WHERE quote_epoch IS NOT NULL").fetchone()[0]
        rows = connection.execute("SELECT count(1) FROM etf_flow_snapshots").fetchone()[0]
    return {"snapshot_sha256": manifest["sha256"], "inserted": inserted, "stored_rows": rows, "observation_days": observations}


def settle_screen_selections(cfg: dict) -> dict:
    settled = []
    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        rows = connection.execute(
            "SELECT selection_id,symbol,feature_date,horizon FROM screen_selections WHERE settled_at_utc IS NULL"
        ).fetchall()
        for selection_id, symbol, feature_date, horizon in rows:
            try:
                snapshot = load_cache(symbol, market_data_dir(cfg))
            except (FileNotFoundError, ValueError):
                continue
            sample = next((item for item in build_samples(snapshot, int(horizon)) if item.feature_date.isoformat() == feature_date), None)
            if not sample or sample.target_return is None:
                continue
            connection.execute(
                """UPDATE screen_selections SET target_end_date=?,actual_return=?,actual_up=?,settled_at_utc=?
                WHERE selection_id=? AND settled_at_utc IS NULL""",
                (sample.target_end_date.isoformat(), sample.target_return, sample.target_up, datetime.now(timezone.utc).isoformat(), selection_id),
            )
            settled.append({"selection_id": selection_id, "symbol": symbol, "horizon": horizon, "actual_return": sample.target_return})
        connection.commit()
    return {"open_selections": len(rows), "settled": len(settled), "items": settled}

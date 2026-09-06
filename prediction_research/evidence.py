"""ETF-only evidence exchange. Scores are observations, never probabilities."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import resolve_project_path
from .store import connect

EXTERNAL_SOURCES = ("fingenius", "smart-money-profiler")
FEATURE_GROUPS = ("etf_flow", "etf_order_divergence", "news_chain", "tradingagents", *EXTERNAL_SOURCES)
KINDS = {"flow", "news", "actor_profile", "anomaly", "analysis"}


def utc(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("evidence timestamps must include a timezone")
    return stamp.astimezone(timezone.utc)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def etf_assets(cfg: dict) -> dict:
    return {asset["symbol"]: asset for assets in cfg["universes"].values() for asset in assets
            if "etf" in asset.get("asset_type", "").lower()
            or "fund" in asset.get("asset_type", "").lower()}


def cutoff_utc(cfg: dict, day) -> datetime:
    return datetime.combine(day, time.fromisoformat(cfg.get("data_cutoff", "18:00")),
                            ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))).astimezone(timezone.utc)


def validate_record(cfg: dict, item: dict) -> dict:
    row = dict(item)
    if row.get("symbol") not in etf_assets(cfg):
        raise ValueError("only configured ETFs/funds are accepted; individual stocks are deferred")
    for key in ("source", "source_key", "kind", "claim", "source_ref", "snapshot_sha256"):
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise ValueError(f"missing evidence field: {key}")
    if row["kind"] not in KINDS or row.get("epistemic") not in {"fact", "inference", "hypothesis"}:
        raise ValueError("invalid evidence kind or epistemic status")
    sha = row["snapshot_sha256"]
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("snapshot_sha256 must be a lowercase SHA-256 digest")
    available, observed = utc(row["available_at_utc"]), utc(row["observed_at_utc"])
    if available < observed:
        raise ValueError("availability precedes observation")
    row["available_at_utc"], row["observed_at_utc"] = available.isoformat(), observed.isoformat()
    if row.get("published_at_utc") and utc(row["published_at_utc"]) > available:
        raise ValueError("availability precedes publication")
    direction = row.get("direction", "unknown")
    if direction not in {"up", "down", "neutral", "unknown"}:
        raise ValueError("invalid direction")
    row["direction"] = direction
    score = row.get("score")
    if score is not None and (isinstance(score, bool) or not isinstance(score, (float, int))
                              or not math.isfinite(score) or not -1 <= score <= 1):
        raise ValueError("score must be null or finite within [-1, 1]")
    if any(key in row for key in ("probability_up", "win_probability")):
        raise ValueError("uncalibrated external probabilities are not accepted as evidence")
    row["schema_version"] = 1
    row["evidence_id"] = digest([row[k] for k in ("source", "source_key", "symbol", "kind")])
    return row


def save_records(cfg: dict, records: list[dict]) -> dict:
    # Validate the entire batch before changing the database.
    records = [validate_record(cfg, row) for row in records]
    inserted = 0
    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        for row in records:
            serialized = json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False)
            prior = connection.execute("SELECT payload_json FROM research_evidence WHERE evidence_id=?",
                                       (row["evidence_id"],)).fetchone()
            if prior:
                # Same identity is immutable. A correction needs a new source_key/revision.
                previous = json.loads(prior[0])
                ignored = {"available_at_utc", "received_at_utc", "import_sha256"}
                if {k: v for k, v in previous.items() if k not in ignored} != {
                    k: v for k, v in row.items() if k not in ignored
                }:
                    raise ValueError("evidence identity conflict; give revisions a new source_key")
                continue
            connection.execute("INSERT INTO research_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (row["evidence_id"], row["source"], row["source_key"], row["symbol"],
                                row["kind"], row["available_at_utc"], row["observed_at_utc"], serialized))
            inserted += 1
        connection.commit()
    return {"records": len(records), "inserted": inserted, "duplicates": len(records) - inserted}


def import_evidence(cfg: dict, path: Path) -> dict:
    """Read our versioned interchange format, not an invented upstream API."""
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8-sig"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("records"), list):
        raise ValueError("expected schema_version=1 and records array")
    received = now_utc()
    records = []
    for original in payload["records"]:
        row = dict(original)
        if row.get("source") not in EXTERNAL_SOURCES:
            raise ValueError("unsupported external source; TradingAgents-Astock is deferred")
        # Local receipt is authoritative: backdated generated reports cannot enter past folds.
        row["available_at_utc"] = max(utc(received), utc(row["observed_at_utc"]),
                                       utc(row.get("available_at_utc", received))).isoformat()
        row["received_at_utc"] = received
        row["import_sha256"] = hashlib.sha256(raw).hexdigest()
        records.append(row)
    records = [validate_record(cfg, row) for row in records]
    archive = resolve_project_path(cfg, cfg.get("evidence", {}).get("raw_dir", "datasets/evidence"))
    archive.mkdir(parents=True, exist_ok=True)
    target = archive / f"{hashlib.sha256(raw).hexdigest()}.json"
    if not target.exists():
        target.write_bytes(raw)
    result = save_records(cfg, records)
    return {**result, "snapshot": str(target.resolve()), "availability_policy": "local_receipt_or_later"}


def load_records(cfg: dict) -> list[dict]:
    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        rows = connection.execute("SELECT payload_json FROM research_evidence ORDER BY available_at_utc,evidence_id").fetchall()
    return [json.loads(row[0]) for row in rows]


def eligible_records(cfg: dict, records: list[dict], symbol: str, as_of: datetime) -> list[dict]:
    max_age = timedelta(days=int(cfg.get("evidence", {}).get("max_age_days", 14)))
    return [row for row in records if row["symbol"] == symbol
            and utc(row["available_at_utc"]) <= as_of
            and as_of - max_age <= utc(row.get("published_at_utc") or row["observed_at_utc"]) <= as_of]


def evidence_status(cfg: dict) -> dict:
    rows = load_records(cfg)
    counts = {source: sum(row["source"] == source for row in rows) for source in FEATURE_GROUPS}
    return {"scope": "etf_only", "records": len(rows), "by_source": counts,
            "independent_news_etf_pairs": len({(row["symbol"], row.get("independence_key", row["source_key"]))
                                              for row in rows if row["source"] == "news_chain"}),
            "external_modules": {source: "evidence_received" if counts[source] else "waiting_for_evidence"
                                 for source in EXTERNAL_SOURCES},
            "tradingagents_astock": "deferred_by_user",
            "identity_caveat": "ETF transaction-size flow does not identify accounts or creations/redemptions"}

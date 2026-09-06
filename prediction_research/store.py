from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from collections.abc import Iterator
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS research_evidence (
  evidence_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  source_key TEXT NOT NULL,
  symbol TEXT NOT NULL,
  kind TEXT NOT NULL,
  available_at_utc TEXT NOT NULL,
  observed_at_utc TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  UNIQUE(source, source_key, symbol, kind)
);
CREATE INDEX IF NOT EXISTS ix_research_evidence_time
ON research_evidence(symbol, available_at_utc);
CREATE TABLE IF NOT EXISTS evidence_mapping_versions (
  mapping_hash TEXT PRIMARY KEY,
  first_seen_at_utc TEXT NOT NULL,
  mapping_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS predictions (
  prediction_id TEXT PRIMARY KEY,
  symbol TEXT NOT NULL,
  feature_date TEXT NOT NULL,
  horizon INTEGER NOT NULL,
  expected_target_end TEXT,
  probability_up REAL NOT NULL,
  probability_status TEXT NOT NULL,
  model_version TEXT NOT NULL,
  data_snapshot_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  settled_at_utc TEXT,
  target_end_date TEXT,
  actual_return REAL,
  actual_up INTEGER,
  UNIQUE(symbol, feature_date, horizon, model_version)
);
CREATE TABLE IF NOT EXISTS prediction_payloads (
  prediction_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS screen_rule_versions (
  rule_hash TEXT PRIMARY KEY,
  first_seen_at_utc TEXT NOT NULL,
  rules_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS screen_batches (
  feature_date TEXT NOT NULL,
  rule_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY(feature_date, rule_hash)
);
CREATE TABLE IF NOT EXISTS screen_selection_times (
  selection_id TEXT PRIMARY KEY,
  decision_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  run_type TEXT NOT NULL,
  config_json TEXT NOT NULL,
  result_path TEXT NOT NULL,
  created_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  canonical_hash TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  event_type TEXT NOT NULL,
  first_seen_at_utc TEXT NOT NULL,
  last_seen_at_utc TEXT NOT NULL,
  reliability_score REAL NOT NULL,
  relevance_score REAL NOT NULL,
  directness_score REAL NOT NULL,
  evidence_score REAL NOT NULL,
  exposures_json TEXT NOT NULL,
  evidence_status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_sources (
  occurrence_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(event_id),
  source_id TEXT NOT NULL,
  source_url TEXT NOT NULL,
  article_url TEXT NOT NULL,
  published_at TEXT,
  fetched_at_utc TEXT NOT NULL,
  raw_snapshot_path TEXT NOT NULL,
  raw_snapshot_sha256 TEXT NOT NULL,
  UNIQUE(event_id, source_id, article_url)
);
CREATE TABLE IF NOT EXISTS agent_analyses (
  analysis_id TEXT PRIMARY KEY,
  screen_report TEXT NOT NULL,
  ticker TEXT NOT NULL,
  symbol TEXT NOT NULL,
  analysis_date TEXT NOT NULL,
  agent_system TEXT NOT NULL,
  status TEXT NOT NULL,
  decision_text TEXT,
  report_dir TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  UNIQUE(screen_report, ticker, analysis_date, agent_system)
);
CREATE TABLE IF NOT EXISTS agent_outcomes (
  outcome_id TEXT PRIMARY KEY,
  analysis_id TEXT NOT NULL REFERENCES agent_analyses(analysis_id),
  horizon INTEGER NOT NULL,
  direction TEXT NOT NULL,
  target_end_date TEXT NOT NULL,
  actual_return REAL NOT NULL,
  actual_up INTEGER NOT NULL,
  direction_correct INTEGER,
  settled_at_utc TEXT NOT NULL,
  UNIQUE(analysis_id, horizon)
);
CREATE TABLE IF NOT EXISTS etf_flow_snapshots (
  snapshot_sha256 TEXT NOT NULL,
  symbol TEXT NOT NULL,
  exchange TEXT NOT NULL,
  name TEXT NOT NULL,
  quote_epoch INTEGER,
  retrieved_at_utc TEXT NOT NULL,
  asset_class TEXT NOT NULL,
  subtype TEXT NOT NULL,
  market_scope TEXT NOT NULL,
  price REAL,
  change_pct REAL,
  amount REAL,
  market_cap REAL,
  main_net_inflow REAL,
  main_net_inflow_pct REAL,
  super_large_net_inflow REAL,
  super_large_net_inflow_pct REAL,
  large_net_inflow REAL,
  large_net_inflow_pct REAL,
  PRIMARY KEY(snapshot_sha256, symbol)
);
CREATE TABLE IF NOT EXISTS screen_selections (
  selection_id TEXT PRIMARY KEY,
  snapshot_sha256 TEXT NOT NULL,
  rule_hash TEXT NOT NULL,
  screen_report TEXT NOT NULL,
  feature_date TEXT NOT NULL,
  rank INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  screen_score REAL NOT NULL,
  horizon INTEGER NOT NULL,
  target_end_date TEXT,
  actual_return REAL,
  actual_up INTEGER,
  settled_at_utc TEXT,
  UNIQUE(snapshot_sha256, rule_hash, symbol, horizon)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_screen_selection_daily
ON screen_selections(feature_date, rule_hash, symbol, horizon);
UPDATE predictions
SET probability_status = 'legacy_development_pre_gate'
WHERE model_version = 'price-volume-logistic-v1'
  AND probability_status = 'calibrated';
"""


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(etf_flow_snapshots)")}
        for name in ("super_large_net_inflow", "super_large_net_inflow_pct",
                     "large_net_inflow", "large_net_inflow_pct"):
            if name not in columns:
                connection.execute(f"ALTER TABLE etf_flow_snapshots ADD COLUMN {name} REAL")
        connection.commit()
        yield connection
    finally:
        connection.close()


def insert_prediction(connection: sqlite3.Connection, payload: dict, commit: bool = True) -> tuple[str, bool]:
    prediction_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    try:
        connection.execute(
            """INSERT INTO predictions
            (prediction_id, symbol, feature_date, horizon, expected_target_end,
             probability_up, probability_status, model_version, data_snapshot_json,
             evidence_json, created_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                prediction_id, payload["symbol"], payload["feature_date"], payload["horizon"],
                payload.get("expected_target_end"), payload["probability_up"],
                payload["probability_status"], payload["model_version"],
                json.dumps(payload["data_snapshot"], ensure_ascii=False, sort_keys=True),
                json.dumps(payload.get("evidence", []), ensure_ascii=False, sort_keys=True), now,
            ),
        )
        if commit:
            connection.commit()
        return prediction_id, True
    except sqlite3.IntegrityError:
        row = connection.execute(
            "SELECT prediction_id FROM predictions WHERE symbol=? AND feature_date=? AND horizon=? AND model_version=?",
            (payload["symbol"], payload["feature_date"], payload["horizon"], payload["model_version"]),
        ).fetchone()
        return str(row[0]), False


def record_run(connection: sqlite3.Connection, run_type: str, cfg: dict, result_path: Path) -> str:
    run_id = str(uuid.uuid4())
    public_cfg = {key: value for key, value in cfg.items() if not key.startswith("_")}
    connection.execute(
        "INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
        (run_id, run_type, json.dumps(public_cfg, ensure_ascii=False, sort_keys=True), str(result_path.resolve()), datetime.now(timezone.utc).isoformat()),
    )
    connection.commit()
    return run_id


def frozen_prediction(connection: sqlite3.Connection, payload: dict) -> dict:
    prediction_id, inserted = insert_prediction(connection, payload, commit=False)
    if inserted:
        frozen = {**payload, "prediction_id": prediction_id,
                  "frozen_at_utc": datetime.now(timezone.utc).isoformat(), "freeze_schema_version": 2}
        connection.execute("INSERT INTO prediction_payloads VALUES (?, ?)",
                           (prediction_id, json.dumps(frozen, ensure_ascii=False, sort_keys=True, allow_nan=False)))
        connection.commit()
    else:
        frozen = load_frozen_prediction(connection, prediction_id)
        # The failed UNIQUE insert opened a write transaction.  End it before
        # the next asset opens a separate read connection for flow context.
        connection.rollback()
    return {**frozen, "inserted": inserted}


def load_frozen_prediction(connection: sqlite3.Connection, prediction_id: str) -> dict:
    record = connection.execute("SELECT payload_json FROM prediction_payloads WHERE prediction_id=?", (prediction_id,)).fetchone()
    if record:
        return json.loads(record[0])
    row = connection.execute("SELECT symbol,feature_date,horizon,probability_up,probability_status,model_version,"
                             "data_snapshot_json,evidence_json,created_at_utc FROM predictions WHERE prediction_id=?",
                             (prediction_id,)).fetchone()
    if row is None:
        raise ValueError("referenced frozen prediction does not exist")
    frozen = dict(zip(("symbol", "feature_date", "horizon", "probability_up", "probability_status", "model_version"), row[:6]))
    frozen.update(prediction_id=prediction_id, data_snapshot=json.loads(row[6]), evidence=json.loads(row[7]),
                  frozen_at_utc=row[8], freeze_schema_version=1, legacy_time_status="decision_time_not_recorded")
    return frozen


def upsert_event(connection: sqlite3.Connection, event: dict, occurrence: dict) -> tuple[str, bool]:
    row = connection.execute("SELECT event_id FROM events WHERE canonical_hash=?", (event["canonical_hash"],)).fetchone()
    inserted = row is None
    event_id = str(uuid.uuid4()) if inserted else str(row[0])
    if inserted:
        connection.execute(
            """INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, event["canonical_hash"], event["title"], event["summary"], event["event_type"],
             occurrence["fetched_at_utc"], occurrence["fetched_at_utc"], event["reliability_score"],
             event["relevance_score"], event["directness_score"], event["evidence_score"],
             json.dumps(event["exposures"], ensure_ascii=False), event["evidence_status"]),
        )
    else:
        connection.execute(
            """UPDATE events SET title=?,summary=?,event_type=?,last_seen_at_utc=?,reliability_score=?,
            relevance_score=?,directness_score=?,evidence_score=?,exposures_json=?,evidence_status=? WHERE event_id=?""",
            (event["title"], event["summary"], event["event_type"], occurrence["fetched_at_utc"],
             event["reliability_score"], event["relevance_score"], event["directness_score"],
             event["evidence_score"], json.dumps(event["exposures"], ensure_ascii=False),
             event["evidence_status"], event_id),
        )
    try:
        connection.execute(
            "INSERT INTO event_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), event_id, occurrence["source_id"], occurrence["source_url"], occurrence["article_url"],
             occurrence.get("published_at"), occurrence["fetched_at_utc"], occurrence["raw_snapshot_path"], occurrence["raw_snapshot_sha256"]),
        )
    except sqlite3.IntegrityError:
        connection.execute(
            """UPDATE event_sources SET published_at=COALESCE(published_at, ?)
            WHERE event_id=? AND source_id=? AND article_url=?""",
            (occurrence.get("published_at"), event_id, occurrence["source_id"], occurrence["article_url"]),
        )
    connection.commit()
    return event_id, inserted


def record_agent_analysis(connection: sqlite3.Connection, screen_report: str, item: dict, analysis_date: str) -> str:
    analysis_id = str(uuid.uuid4())
    connection.execute(
        """INSERT INTO agent_analyses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(screen_report,ticker,analysis_date,agent_system) DO UPDATE SET
        status=excluded.status,decision_text=excluded.decision_text,report_dir=excluded.report_dir,created_at_utc=excluded.created_at_utc""",
        (analysis_id, screen_report, item["ticker"], item["symbol"], analysis_date, "TradingAgents",
         item["status"], item.get("decision"), item["report_dir"], datetime.now(timezone.utc).isoformat()),
    )
    connection.commit()
    row = connection.execute(
        "SELECT analysis_id FROM agent_analyses WHERE screen_report=? AND ticker=? AND analysis_date=? AND agent_system='TradingAgents'",
        (screen_report, item["ticker"], analysis_date),
    ).fetchone()
    return str(row[0])

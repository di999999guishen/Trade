from __future__ import annotations

import json
import os
import subprocess
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import resolve_project_path
from .config import market_data_dir
from .data import load_cache
from .features import build_samples
from .screening import latest_screen
from .store import connect, record_agent_analysis


def _ticker(row: dict) -> str:
    return row["symbol"] + (".SS" if row["exchange"] == "SSE" else ".SZ")


def _safe_tail(value: str) -> str:
    value = re.sub(r"(?i)(api[ _-]?key[^\n:]*[:=]\s*)[^\s,'\"}]+", r"\1[REDACTED]", value)
    return value[-2000:]


def _decision(stdout: str) -> str | None:
    match = re.search(r"Final decision:\s*(.+)", stdout)
    return match.group(1).strip() if match else None


def decision_direction(value: str | None) -> str:
    upper = (value or "").upper()
    if any(token in upper for token in ("SELL", "卖出", "看空")):
        return "down"
    if any(token in upper for token in ("BUY", "买入", "看多")):
        return "up"
    if any(token in upper for token in ("HOLD", "持有", "观望")):
        return "neutral"
    return "unclassified"


def build_plan(cfg: dict, top_n: int) -> dict:
    screen_path, screen = latest_screen(cfg)
    project = resolve_project_path(cfg, cfg["tradingagents"]["project_dir"])
    candidates = [{"ticker": _ticker(row), "symbol": row["symbol"], "name": row["name"], "screen_score": row["screen_score"], "screen_group": row["screen_group"], "asset_class": row["asset_class"], "subtype": row["subtype"]} for row in screen["selected"][:top_n]]
    epochs = [int(row["quote_epoch"]) for row in screen["selected"] if row.get("quote_epoch")]
    china_time = timezone(timedelta(hours=8))
    trade_date = datetime.fromtimestamp(max(epochs), china_time).date().isoformat() if epochs else date.today().isoformat()
    return {"screen_report": str(screen_path.resolve()), "project_dir": str(project), "project_available": (project / "main.py").exists(), "trade_date": trade_date, "candidates": candidates}


def run_tradingagents(cfg: dict, top_n: int) -> tuple[Path, dict]:
    plan = build_plan(cfg, top_n)
    if not plan["project_available"]:
        raise FileNotFoundError(plan["project_dir"])
    project = Path(plan["project_dir"])
    interpreter = project / ".venv" / "Scripts" / "python.exe"
    if not interpreter.exists():
        raise FileNotFoundError(f"TradingAgents interpreter not found: {interpreter}")
    report_root = resolve_project_path(cfg, cfg["tradingagents"]["reports_dir"])
    items = []
    stop_reason = None
    for candidate in plan["candidates"]:
        target = report_root / f"{candidate['ticker']}_{plan['trade_date']}"
        if stop_reason:
            items.append({**candidate, "status": "skipped_after_batch_failure", "returncode": None, "decision": None, "report_dir": str(target.resolve()), "error_class": stop_reason})
            continue
        command = [str(interpreter), "main.py", candidate["ticker"], "--date", plan["trade_date"], "--reports-dir", str(report_root), "--quiet"]
        completed = subprocess.run(command, cwd=project, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=int(cfg["tradingagents"]["timeout_seconds"]), env=os.environ.copy())
        stderr_tail = _safe_tail(completed.stderr)
        error_class = "authentication" if "Authentication" in stderr_tail or "401" in stderr_tail else "runtime" if completed.returncode else None
        item = {**candidate, "status": "ok" if completed.returncode == 0 else "failed", "returncode": completed.returncode, "decision": _decision(completed.stdout), "report_dir": str(target.resolve()), "error_class": error_class, "stdout_tail": _safe_tail(completed.stdout), "stderr_tail": stderr_tail}
        items.append(item)
        if error_class == "authentication":
            stop_reason = error_class
    payload = {**plan, "run_type": "tradingagents_after_etf_screen", "created_at_utc": datetime.now(timezone.utc).isoformat(), "items": items}
    output = resolve_project_path(cfg, cfg["runs_dir"]) / f"tradingagents_batch_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    db_path = resolve_project_path(cfg, cfg["state_db"])
    with connect(db_path) as connection:
        for item in items:
            item["analysis_id"] = record_agent_analysis(connection, plan["screen_report"], item, plan["trade_date"])
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output, payload


def settle_agent_analyses(cfg: dict) -> dict:
    db_path = resolve_project_path(cfg, cfg["state_db"])
    settled = []
    with connect(db_path) as connection:
        analyses = connection.execute(
            "SELECT analysis_id,symbol,analysis_date,decision_text FROM agent_analyses WHERE status='ok' AND decision_text IS NOT NULL"
        ).fetchall()
        for analysis_id, symbol, analysis_date, decision_text in analyses:
            try:
                snapshot = load_cache(symbol, market_data_dir(cfg))
            except (FileNotFoundError, ValueError):
                continue
            direction = decision_direction(decision_text)
            for horizon in cfg["horizons"]:
                exists = connection.execute("SELECT 1 FROM agent_outcomes WHERE analysis_id=? AND horizon=?", (analysis_id, horizon)).fetchone()
                if exists:
                    continue
                sample = next((row for row in build_samples(snapshot, int(horizon)) if row.feature_date.isoformat() == analysis_date), None)
                if not sample or sample.target_return is None:
                    continue
                correct = None if direction in {"neutral", "unclassified"} else int((direction == "up") == bool(sample.target_up))
                outcome_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO agent_outcomes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (outcome_id, analysis_id, horizon, direction, sample.target_end_date.isoformat(), sample.target_return,
                     sample.target_up, correct, datetime.now(timezone.utc).isoformat()),
                )
                settled.append({"analysis_id": analysis_id, "symbol": symbol, "horizon": horizon, "direction": direction, "actual_return": sample.target_return, "direction_correct": correct})
        connection.commit()
    return {"successful_analyses": len(analyses), "settled": len(settled), "items": settled}

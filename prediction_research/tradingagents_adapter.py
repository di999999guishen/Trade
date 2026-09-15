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
    matches = re.findall(r"^[ \t]*Final decision:[ \t]*(.*)$", stdout, re.IGNORECASE | re.MULTILINE)
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0].strip() or None
    # Keep conflicting final fields visible to the parser instead of choosing one.
    return "\n".join(f"Final decision: {value.strip()}" for value in matches)


_RATING_ALIASES = {
    "buy": "Buy", "买入": "Buy",
    "overweight": "Overweight", "增持": "Overweight",
    "hold": "Hold", "持有": "Hold", "观望": "Hold",
    "underweight": "Underweight", "减持": "Underweight",
    "sell": "Sell", "卖出": "Sell",
}
_RATING_INTENTS = {
    "Buy": ("enter_or_add", "up"),
    "Overweight": ("increase_exposure", "unclassified"),
    "Hold": ("maintain_or_wait", "neutral"),
    "Underweight": ("reduce_exposure", "unclassified"),
    "Sell": ("exit_or_avoid", "down"),
}
_DECISION_FIELD = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*|__)?\s*"
    r"(?:final decision|rating|recommendation|action|最终决策|最终评级|评级|建议|操作)"
    r"\s*(?:\*\*|__)?\s*[:：]\s*(?:\*\*|__)?\s*(.*?)\s*$",
    re.IGNORECASE,
)


def _rating_token(value: str) -> str | None:
    token = value.strip().rstrip("。.!！").strip(" *_`\t")
    if token.startswith("建议"):
        token = token[2:].strip(" *_`\t")
    return _RATING_ALIASES.get(token.casefold())


def decision_semantics(value: str | None) -> dict:
    """Read explicit ratings; never infer an action from words inside a narrative.

    Overweight/Underweight express exposure changes, not predictions of positive
    or negative absolute returns. Only explicit Buy/Sell retain the legacy
    up/down direction used by outcome evaluation. Unknown text stays unknown.
    """
    result = {"rating": None, "action_intent": "unknown", "direction": "unclassified",
              "parse_status": "missing" if not value or not value.strip() else "unrecognized"}
    if result["parse_status"] == "missing":
        return result
    rating = _rating_token(value)
    if rating is None:
        fields = [match.group(1) for line in value.splitlines()
                  if (match := _DECISION_FIELD.fullmatch(line))]
        ratings = [_rating_token(field) for field in fields]
        if not ratings or any(item is None for item in ratings):
            return result
        if len(set(ratings)) != 1:
            return {**result, "parse_status": "ambiguous"}
        rating = ratings[0]
    intent, direction = _RATING_INTENTS[rating]
    return {"rating": rating, "action_intent": intent, "direction": direction, "parse_status": "parsed"}


def decision_direction(value: str | None) -> str:
    return decision_semantics(value)["direction"]


def build_plan(cfg: dict, top_n: int) -> dict:
    screen_path, screen = latest_screen(cfg)
    project = resolve_project_path(cfg, cfg["tradingagents"]["project_dir"])
    candidates = [{"ticker": _ticker(row), "symbol": row["symbol"], "name": row["name"],
                   "screen_score": row["screen_score"], "screen_group": row["screen_group"],
                   "asset_class": row["asset_class"], "subtype": row["subtype"],
                   "order_divergence": row.get("order_divergence", {"status": "missing_data", "signal": "no_data"}),
                   "evidence_handoff_status": "available_in_plan_not_injected_into_current_upstream_cli"}
                  for row in screen["selected"][:top_n]]
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
            item["decision_semantics"] = decision_semantics(item.get("decision") if item["status"] == "ok" else None)
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
            semantics = decision_semantics(decision_text)
            direction = semantics["direction"]
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
                settled.append({"analysis_id": analysis_id, "symbol": symbol, "horizon": horizon,
                                "decision_semantics": semantics, "direction": direction,
                                "actual_return": sample.target_return, "direction_correct": correct})
        connection.commit()
    return {"successful_analyses": len(analyses), "settled": len(settled), "items": settled}

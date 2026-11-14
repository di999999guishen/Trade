"""Point-in-time, instrument-specific handoff from the existing news flow.

News remains evidence: this module does not turn sentiment into a calibrated
probability, nor use price-derived sentiment as an independent news factor.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def timestamp(value, zone="Asia/Shanghai"):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=ZoneInfo(zone)) if result.tzinfo is None else result
    except (TypeError, ValueError):
        return None


def news_for_candidate(path, ticker, decision_at, max_age_hours=18, candidate_name=None):
    decision = timestamp(decision_at)
    if decision is None:
        raise ValueError("news requires an explicit decision timestamp")
    empty = {"status": "missing", "events": [], "scoring_eligible": False}
    if not path or not Path(path).is_file():
        return empty
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    generated = timestamp(data.get("generated_at"))
    base = {**empty, "source_path": str(Path(path).resolve()),
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "generated_at": generated.isoformat() if generated else None,
            "decision_at": decision.isoformat()}
    if generated is None:
        return {**base, "status": "unknown_availability"}
    age = (decision - generated).total_seconds() / 3600
    if not 0 <= age <= max_age_hours:
        return {**base, "status": "future" if age < 0 else "stale", "age_hours": age}
    seen, events, rejected = set(), [], 0
    flow = data.get("cls_telegraph") or {}
    buckets = [etf.get("strong_news", []) for etf in flow.get("etfs", []) if etf.get("ticker") == ticker]
    if candidate_name:
        from .taxonomy import _strip_fund_company
        theme = _strip_fund_company(candidate_name)
        if len(theme) >= 2:
            # A name match is explicitly weaker than verified constituent exposure.
            buckets.append([{**r, "via": "candidate_name_theme", "match_detail": theme}
                            for r in flow.get("events", [])
                            if theme in str(r.get("title", "")) or theme == r.get("subject")])
    for bucket in buckets:
        for row in bucket:
            published = timestamp(row.get("time"))
            title = str(row.get("title") or "").strip()
            key = str(row.get("id") or row.get("url") or title)
            if not title or key in seen:
                continue
            if published is None or published > generated or not 0 <= (decision - published).total_seconds() <= max_age_hours * 3600:
                rejected += 1
                continue
            seen.add(key)
            events.append({k: row.get(k) for k in ("title", "time", "url", "id", "via", "match_detail", "sentiment", "sentiment_src", "level", "shared")})
    independent = [event for event in events if event.get("sentiment_src") != "market"]
    return {**base, "status": "available" if events else "no_matching_events",
            "events": events[:12], "rejected_events": rejected,
            "independent_text_events": len(independent),
            "price_derived_events": len(events) - len(independent),
            "scoring_eligible": False,
            "note": "Unvalidated contextual evidence; market-derived sentiment is not an independent news signal. No matching news means unknown coverage, not neutral news. 13F is excluded from short-horizon evidence."}


def configured_candidate_news(cfg, ticker, decision_at, candidate_name=None):
    path = cfg.get("workflow", {}).get("news_context_path")
    if not path:
        project = Path(cfg["_project_dir"]).parent
        paths = sorted((project / "reports").glob("news_flow_*.json"), reverse=True)
        path = paths[0] if paths else None
    return news_for_candidate(path, ticker, decision_at,
                              float(cfg.get("workflow", {}).get("news_max_age_hours", 18)), candidate_name)


def validate_screen_date(screen, cfg, now=None):
    """Reject stale/mixed-day candidates, including standalone run-agents."""
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))
    rows = screen.get("selected") or []
    epochs = [row.get("quote_epoch") for row in rows]
    if not epochs or any(not isinstance(t, (float, int)) or not math.isfinite(t) for t in epochs):
        raise ValueError("frozen screen is missing quote timestamps")
    days = {datetime.fromtimestamp(t, zone).date().isoformat() for t in epochs}
    if len(days) != 1:
        raise ValueError("frozen screen contains mixed quote dates")
    trade_date = next(iter(days))
    expected = cfg.get("workflow", {}).get("expected_feature_date")
    if expected and trade_date != expected:
        raise ValueError(f"screen quote date {trade_date} differs from requested {expected}")
    age_days = (now - datetime.fromtimestamp(max(epochs), timezone.utc)).total_seconds() / 86400
    if not 0 <= age_days <= float(cfg.get("workflow", {}).get("max_quote_age_days", 4)):
        raise ValueError(f"stale or future screen ({trade_date}); run a fresh cycle before run-agents")
    return trade_date

"""Decision-time ETF money-flow context shared by prediction and reporting."""
from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import resolve_project_path
from .evidence import utc
from .store import connect

FLOW_BASIS = "成交单大小分类估计；不代表 ETF 申赎或账户身份"


def finite(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def flow_context(cfg: dict, symbol: str, screen: dict | None, decision_at: datetime) -> dict:
    row = next((r for r in (screen or {}).get("selected", []) if r["symbol"] == symbol), {})
    manifest = (screen or {}).get("source_snapshot", {})
    epoch = finite(row.get("quote_epoch"))
    observed = datetime.fromtimestamp(epoch, decision_at.tzinfo) if epoch else None
    received = utc(manifest["retrieved_at_utc"]) if manifest.get("retrieved_at_utc") else None
    available = max(observed, received) if observed and received else None
    max_age = float(cfg.get("workflow", {}).get("max_quote_age_days", 4))
    if not row:
        status = "no_screen_evidence"
    elif not available:
        status = "missing_source_time"
    elif available > decision_at:
        status = "unavailable_at_decision"
    elif (decision_at - observed).total_seconds() > max_age * 86400:
        status = "stale_at_decision"
    elif finite(row.get("main_net_inflow")) is None or finite(row.get("main_net_inflow_pct")) is None:
        status = "missing_flow_values"
    else:
        status = "available"
    usable = status == "available"
    # Never attach later snapshot values to a historical decision.
    net = finite(row.get("main_net_inflow")) if usable else None
    pct = finite(row.get("main_net_inflow_pct")) if usable else None
    change = finite(row.get("change_pct")) if usable else None
    alignment = "unknown" if net is None or change is None else "neutral" if net == 0 or change == 0 else "same_direction" if (net > 0) == (change > 0) else "opposite_direction"
    divergence = row.get("order_divergence", {}) if usable else {}
    daily = {}
    zone = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))
    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        history = connection.execute(
            "SELECT quote_epoch,retrieved_at_utc,main_net_inflow FROM etf_flow_snapshots "
            "WHERE symbol=? AND quote_epoch IS NOT NULL ORDER BY quote_epoch,retrieved_at_utc,snapshot_sha256", (symbol,)
        ).fetchall()
    for quote_epoch, retrieved, amount in history:
        quote = datetime.fromtimestamp(quote_epoch, decision_at.tzinfo)
        if max(quote, utc(retrieved)) <= decision_at:
            daily[quote.astimezone(zone).date().isoformat()] = finite(amount)
    days = sorted(daily)
    windows = {}
    for size in (5, 20):
        selected = days[-size:]
        complete = len(selected) == size and all(daily[d] is not None for d in selected)
        windows[str(size)] = {"observed_days": len(selected), "net_inflow_sum": sum(daily[d] for d in selected) if complete else None,
                              "status": "complete_observation_window" if complete else "insufficient_data",
                              "dates": selected, "definition": "已观测交易日期，未保证连续交易日"}
    return {"status": status, "decision_at_utc": decision_at.isoformat(),
            "observed_at_utc": observed.isoformat() if observed else None,
            "available_at_utc": available.isoformat() if available else None,
            "snapshot_sha256": manifest.get("sha256"), "main_net_inflow": net,
            "main_net_inflow_pct": pct, "price_change_pct": change, "price_flow_alignment": alignment,
            "screen_score": row.get("screen_score") if usable else None,
            "screen_flow_contribution": row.get("screen_contributions", {}).get("flow") if usable else None,
            "screen_order_divergence_contribution": row.get("screen_contributions", {}).get("order_divergence") if usable else None,
            "order_divergence": divergence,
            "used_for_screening": usable, "used_for_probability": False,
            "history_windows": windows, "definition": FLOW_BASIS}

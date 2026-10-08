"""Auditable super-large-order versus large-order divergence signal."""
from __future__ import annotations

import math


def _finite(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def order_divergence(row: dict, rules: dict) -> dict:
    """Return a signed factor only when the two order-size legs truly diverge."""
    super_pct = _finite(row.get("super_large_net_inflow_pct"))
    large_pct = _finite(row.get("large_net_inflow_pct"))
    super_amount = _finite(row.get("super_large_net_inflow"))
    large_amount = _finite(row.get("large_net_inflow"))
    threshold = float(rules.get("order_divergence_min_leg_pct", 1.0))
    clip_pct = float(rules.get("order_divergence_clip_pct", 15.0))
    if threshold < 0 or clip_pct <= 0:
        raise ValueError("invalid order-divergence thresholds")
    result = {
        "status": "missing_data", "signal": "no_data", "factor": None,
        "super_large_net_inflow": super_amount, "super_large_net_inflow_pct": super_pct,
        "large_net_inflow": large_amount, "large_net_inflow_pct": large_pct,
        "spread_pct_points": None, "minimum_leg_pct": threshold, "clip_pct": clip_pct,
        "definition": "超大单与大单净流入占比方向相反，且两腿绝对值均达到阈值",
    }
    if super_pct is None or large_pct is None:
        return result
    spread = super_pct - large_pct
    result["spread_pct_points"] = round(spread, 6)
    opposite = super_pct * large_pct < 0
    above_threshold = abs(super_pct) >= threshold and abs(large_pct) >= threshold
    if not opposite:
        result.update(status="available", signal="same_direction_or_flat", factor=0.0)
    elif not above_threshold:
        result.update(status="available", signal="opposite_below_threshold", factor=0.0)
    else:
        factor = max(-1.0, min(1.0, spread / (2.0 * clip_pct)))
        result.update(status="available", signal="bullish_divergence" if factor > 0 else "bearish_divergence",
                      factor=round(factor, 6))
    return result

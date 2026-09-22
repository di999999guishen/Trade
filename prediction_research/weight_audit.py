"""Read-only cross-sectional weight diagnostics; never a return backtest.

Usage: python -m prediction_research.weight_audit FROZEN_SCREEN OUTPUT_JSON
"""
from __future__ import annotations

import copy
import hashlib
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .screening import select_etfs


def audit(screen_path: Path) -> dict:
    screen = json.loads(screen_path.read_text(encoding="utf-8"))
    source = Path(screen["source_snapshot"]["path"])
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != screen["source_snapshot"]["sha256"]:
        raise ValueError("source snapshot hash mismatch")
    snapshot = json.loads(raw)
    decision = datetime.fromisoformat(screen["decision_at_utc"])
    # Infer the already frozen quote date, then replay exactly that date's rows.
    zone = ZoneInfo("Asia/Shanghai")
    quote_day = lambda row: datetime.fromtimestamp(row["quote_epoch"], zone).date()
    day = max(quote_day(row) for row in screen["selected"])
    rows = [row for row in snapshot["records"] if row.get("quote_epoch") is not None
            and row["quote_epoch"] <= decision.timestamp() and quote_day(row) == day]
    rules = screen["rules"]
    eligible, selected = select_etfs(rows, rules)
    frozen_symbols = [row["symbol"] for row in screen["selected"]]
    if (len(eligible) != screen["eligible_records"] or [row["symbol"] for row in selected] != frozen_symbols
            or [row["screen_score"] for row in selected] != [row["screen_score"] for row in screen["selected"]]):
        raise ValueError("replay differs from frozen screen; audit requires matching algorithm and limits")
    factors = list(eligible[0]["screen_components"])
    stats = {}
    for factor in factors:
        values = [r["screen_components"][factor] for r in eligible if r["screen_components"][factor] is not None]
        contributions = [r["screen_contributions"][factor] for r in eligible if r["screen_contributions"][factor] is not None]
        stats[factor] = {"available": len(values), "missing": len(eligible) - len(values),
                         "factor_std": statistics.pstdev(values) if values else None,
                         "contribution_std": statistics.pstdev(contributions) if contributions else None,
                         "mean_absolute_contribution": statistics.mean(map(abs, contributions)) if contributions else None,
                         "saturated": sum(abs(v) >= 0.999999 for v in values)}
    correlations = []
    for i, left in enumerate(factors):
        for right in factors[i + 1:]:
            pairs = [(r["screen_components"][left], r["screen_components"][right]) for r in eligible
                     if r["screen_components"][left] is not None and r["screen_components"][right] is not None]
            try:
                value = statistics.correlation(*zip(*pairs)) if len(pairs) >= 2 else None
            except statistics.StatisticsError:
                value = None
            correlations.append({"left": left, "right": right, "n": len(pairs), "pearson": value})
    profiles = {"current": rules["score_weights"]}
    profiles["flow_reduced_shadow"] = {"main_flow": .25, "liquidity": .25, "momentum": .10,
                                       "order_divergence": .05, "flow_to_cap": .10, "relative_strength": .25}
    for name in rules["score_weights"]:
        profiles[f"without_{name}"] = {**rules["score_weights"], name: 0.0}
    scenarios = {}
    for name, weights in profiles.items():
        scenario = copy.deepcopy(rules)
        scenario["score_weights"] = weights
        _, candidates = select_etfs(rows, scenario)
        scenarios[name] = {"weights": weights, "selected_count": len(candidates),
                           "top20_overlap": len(set(frozen_symbols[:20]) & {r["symbol"] for r in candidates[:20]}),
                           "top20": [{"symbol": r["symbol"], "name": r["name"], "score": r["screen_score"]}
                                     for r in candidates[:20]]}
    return {"scope": "cross_section_only_not_return_validation", "screen_path": str(screen_path.resolve()),
            "source_sha256": screen["source_snapshot"]["sha256"], "quote_day": day.isoformat(),
            "decision_at_utc": screen["decision_at_utc"], "eligible": len(eligible), "selected": len(selected),
            "replay_matches_frozen": True, "factor_statistics": stats, "correlations": correlations,
            "scenarios": scenarios, "production_weights_changed": False}


if __name__ == "__main__":
    payload = audit(Path(sys.argv[1]))
    target = Path(sys.argv[2])
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print(json.dumps({key: payload[key] for key in ("eligible", "selected", "factor_statistics", "correlations")}, ensure_ascii=False, indent=2))
    print({name: data["top20_overlap"] for name, data in payload["scenarios"].items()})

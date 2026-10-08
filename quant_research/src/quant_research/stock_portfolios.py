"""S4 sector allocation and S5 event target planning; no synthetic return claims."""
import numpy as np
from scipy.optimize import linprog

from .fundamental_signals import aware


def quality_portfolio(ranked, benchmark):
    """Reduce top-100 equal weights only; report infeasible lower sector bands."""
    required = {"security_id", "industry", "score"}
    if not required.issubset(ranked) or ranked.security_id.duplicated().any():
        raise ValueError("invalid ranked stock panel")
    if (not benchmark or not np.isfinite(list(benchmark.values())).all()
            or min(benchmark.values()) < 0 or abs(sum(benchmark.values())-1) > 1e-8):
        raise ValueError("benchmark sector weights must sum to one")
    if ranked.industry.isna().any() or not np.isfinite(ranked.score).all():
        raise ValueError("unknown industry or score")
    selected = ranked.sort_values(["score", "security_id"], ascending=[False, True]).head(100)
    if selected.empty:
        return {"status": "infeasible_cash", "weights": {}, "cash": 1.0}
    industries = sorted(set(benchmark) | set(selected.industry))
    matrix, limits = [], []
    for industry in industries:
        indicator = (selected.industry == industry).astype(float).to_numpy()
        matrix.extend([indicator, -indicator])
        limits.extend([benchmark.get(industry, 0)+.05, -max(0, benchmark.get(industry, 0)-.05)])
    result = linprog(-np.ones(len(selected)), A_ub=np.array(matrix), b_ub=limits,
                     bounds=[(0, min(.02, 1/len(selected)))]*len(selected), method="highs")
    if result.status == 2:
        return {"status": "infeasible_cash", "weights": {}, "cash": 1.0,
                "reason": "top100_downward_only_cannot_meet_sector_lower_bounds"}
    if not result.success:
        raise RuntimeError(result.message)
    weights = {s: float(w) for s, w in zip(selected.security_id, result.x, strict=True) if w > 1e-10}
    return {"status": "targets_only_not_orders", "weights": weights, "cash": max(0, 1-sum(weights.values()))}


def event_portfolio(active, candidates, decision, next_open):
    """S5 open targets: exits first, no same-open re-entry after a new report.

    Active entries: security_id, industry, weight, scheduled_exit_at.
    Candidates already have validated SUE/percentile/basis upstream; eligibility
    and available_at must refer to the decision snapshot, never today's universe.
    Actual fill cash, delistings and corporate actions remain ledger concerns.
    """
    cutoff, execution = aware(decision), aware(next_open)
    if execution <= cutoff:
        raise ValueError("execution must follow decision")
    required = {"security_id", "industry", "sue", "percentile", "available_at", "eligible"}
    if not required.issubset(candidates) or candidates.security_id.duplicated().any():
        raise ValueError("incomplete/ambiguous event candidates")
    c = candidates.copy()
    c["available_at"] = c.available_at.map(aware)
    if (c.available_at > cutoff).any():
        raise ValueError("future event candidate")
    if not np.isfinite(c[["sue", "percentile"]]).all().all() or not c.percentile.between(0, 1).all():
        raise ValueError("invalid event score")
    if c.industry.isna().any() or not c.eligible.map(lambda x: isinstance(x, (bool, np.bool_))).all():
        raise ValueError("unknown event eligibility/industry")
    weights, sectors, exits = {}, {}, set()
    if len({p["security_id"] for p in active}) != len(active):
        raise ValueError("duplicate active event positions")
    for position in active:
        s, industry, weight = position["security_id"], position["industry"], position["weight"]
        if not np.isfinite(weight) or not 0 <= weight <= 1:
            raise ValueError("invalid active weight")
        if aware(position["scheduled_exit_at"]) <= execution or s in set(c.security_id):
            exits.add(s)
            continue
        weights[s] = min(.02, weight)
        sectors[s] = industry
    for industry in sorted(set(sectors.values())):
        names = [s for s in sorted(weights) if sectors[s] == industry]
        total = sum(weights[s] for s in names)
        if total > .25:
            for s in names:
                weights[s] *= .25/total
    if sum(weights.values()) > 1:
        scale = 1/sum(weights.values())
        weights = {s: w*scale for s, w in weights.items()}
    for event in c.sort_values(["sue", "security_id"], ascending=[False, True]).itertuples():
        if event.security_id in exits or event.security_id in weights or not event.eligible or event.sue <= 0 or event.percentile < .8:
            continue
        industry_used = sum(w for s, w in weights.items() if sectors[s] == event.industry)
        weight = max(0, min(.02, 1-sum(weights.values()), .25-industry_used))
        if weight > 1e-10:
            weights[event.security_id] = weight
            sectors[event.security_id] = event.industry
    return {"status": "targets_only_not_orders", "weights": weights, "exits": sorted(exits),
            "cash": max(0, 1-sum(weights.values())), "execute_not_before": execution.isoformat()}

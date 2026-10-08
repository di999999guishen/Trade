"""S4/S5 signal primitives. Require supplied PIT evidence; never invent it."""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .calendar import calendar, decision_at


def aware(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("explicit timezone required")
    return stamp.tz_convert("UTC")


def asof_versions(records, decision):
    """Keep the latest *available* revision of each security/metric/period/unit."""
    required = {"security_id", "metric", "period_start", "period_end", "unit", "value",
                "published_at", "available_at", "revision_id"}
    if not required.issubset(records.columns):
        raise ValueError(f"missing PIT evidence: {sorted(required-set(records.columns))}")
    rows = records.copy()
    for col in ("published_at", "available_at"):
        rows[col] = rows[col].map(aware)
    if (rows.available_at < rows.published_at).any():
        raise ValueError("availability cannot precede publication")
    rows = rows[rows.available_at <= aware(decision)]
    keys = ["security_id", "metric", "period_start", "period_end", "unit"]
    if rows.duplicated([*keys, "available_at"]).any():
        raise ValueError("ambiguous concurrent fact versions")
    return rows.sort_values("available_at").drop_duplicates(keys, keep="last").reset_index(drop=True)


def quality_scores(panel, decision):
    """S4 ranking only: the caller must supply a historically valid monthly pool.

    TTM construction, historical membership, delistings and sector attribution
    are required upstream. This function does not assert that inputs prove them.
    """
    numeric = ["gross_profit_ttm", "cash_flow_ttm", "net_income_ttm", "assets_start", "assets_end",
               "total_debt", "momentum", "market_cap", "price", "adv20", "observations"]
    evidence = ["financial_available_at", "universe_available_at", "price_available_at"]
    required = {*numeric, *evidence, "security_id", "industry", "is_financial"}
    if not required.issubset(panel.columns) or panel.security_id.duplicated().any():
        raise ValueError("incomplete or duplicate PIT stock panel")
    p = panel.copy()
    for col in evidence:
        p[col] = p[col].map(aware)
        if (p[col] > aware(decision)).any():
            raise ValueError("future input in stock panel")
    if not p.is_financial.map(lambda x: isinstance(x, (bool, np.bool_))).all():
        raise ValueError("explicit financial-sector classification required")
    liquid = (np.isfinite(p[["market_cap", "price", "adv20", "observations"]]).all(axis=1)
              & p.industry.notna() & ~p.is_financial & (p.price > 5) & (p.adv20 > 20_000_000)
              & (p.observations >= 253) & (p.market_cap > 0))
    # Freeze the top-1000 pool before financial coverage exclusions; missing
    # fundamentals must not silently promote the 1001st security into the pool.
    p = p[liquid].sort_values(["market_cap", "security_id"], ascending=[False, True]).head(1000).copy()
    good = (np.isfinite(p[numeric]).all(axis=1) & (p.assets_start > 0) & (p.assets_end > 0) & (p.total_debt >= 0))
    p = p[good].copy()
    if p.empty:
        raise ValueError("no qualified financial observations")
    assets = (p.assets_start+p.assets_end)/2
    p["gross_profitability"] = p.gross_profit_ttm/assets
    p["cash_profitability"] = p.cash_flow_ttm/assets
    p["negative_accruals"] = -(p.net_income_ttm-p.cash_flow_ttm)/assets
    p["negative_leverage"] = -p.total_debt/p.assets_end
    p["global_standardization_fallback"] = p.groupby("industry").industry.transform("size") < 20
    def z(values):
        clipped = values.clip(*values.quantile([.025, .975]).tolist())
        std = clipped.std(ddof=0)
        return (clipped-clipped.mean())/std if std > 1e-12 else clipped*0
    def sector_z(column):
        values = p.groupby("industry")[column].transform(z)
        fallback = p.global_standardization_fallback
        values.loc[fallback] = z(p[column]).loc[fallback]
        return values
    components = ["gross_profitability", "cash_profitability", "negative_accruals", "negative_leverage"]
    p["quality"] = pd.concat([sector_z(c) for c in components], axis=1).mean(axis=1)
    p["score"] = .5*sector_z("quality")+.5*sector_z("momentum")
    return p.sort_values(["score", "security_id"], ascending=[False, True]).reset_index(drop=True)


@dataclass(frozen=True)
class EarningsEvent:
    event_id: str
    security_id: str
    quarter_index: int
    actual_eps: float
    median_estimate: float
    eps_basis: str  # explicit GAAP/non-GAAP definition, not guessed from provider label
    share_basis_id: str  # historical EPS must already be converted to this basis
    published_at: str
    actual_available_at: str
    parsed_at: str
    estimate_snapshot_at: str
    estimate_available_at: str
    source_revision_id: str

    def validate(self):
        if not all((self.event_id, self.security_id, self.eps_basis, self.share_basis_id, self.source_revision_id)):
            raise ValueError("missing event identity/basis/version")
        if type(self.quarter_index) is not int or not np.isfinite([self.actual_eps, self.median_estimate]).all():
            raise ValueError("invalid quarterly EPS")
        published, available, parsed, snapshot, received = [aware(getattr(self, key)) for key in
            ("published_at", "actual_available_at", "parsed_at", "estimate_snapshot_at", "estimate_available_at")]
        if not published <= available <= parsed:
            raise ValueError("invalid actual EPS availability chronology")
        if snapshot > received or max(snapshot, received) > published-pd.Timedelta(minutes=60):
            raise ValueError("consensus was not frozen and available 60 minutes before release")
        return parsed


def event_signal(event, history, decision, eps_floor=.01):
    """Quarterly USD/share SUE. No historical consensus timestamps means refusal."""
    known = event.validate()
    if known > aware(decision) or not np.isfinite(eps_floor) or eps_floor <= 0:
        raise ValueError("event not yet usable or invalid share-adjusted floor")
    prior = []
    for old in history:
        if (old.security_id != event.security_id or not 1 <= event.quarter_index-old.quarter_index <= 8):
            continue
        old_known = old.validate()
        if old_known >= aware(event.published_at):
            continue  # do not use later restatements to normalize the original event
        if old.eps_basis != event.eps_basis or old.share_basis_id != event.share_basis_id:
            raise ValueError("EPS/share basis mismatch; explicit historical conversion required")
        prior.append(old)
    if len({p.quarter_index for p in prior}) != len(prior):
        raise ValueError("duplicate historical quarters; select the as-of version first")
    if len(prior) < 6:
        raise ValueError("fewer than six valid errors in previous eight quarters")
    scale = max(float(np.std([p.actual_eps-p.median_estimate for p in prior], ddof=1)), eps_floor)
    return {"event_id": event.event_id, "error": event.actual_eps-event.median_estimate,
            "sue": (event.actual_eps-event.median_estimate)/scale, "history_count": len(prior),
            "available_at": known.isoformat()}


def event_execution_times(event):
    """18 ET cut, next open entry, exit after twenty held XNYS sessions."""
    known = event.validate()
    cal = calendar()
    session = cal.date_to_session(known.tz_convert("America/New_York").date(), direction="next")
    if decision_at(session) < known:
        session = cal.next_session(session)
    entry = cal.next_session(session)
    exit_session = cal.sessions[cal.sessions.get_loc(entry)+20]
    return {"decision_at": decision_at(session).isoformat(), "entry_at": cal.session_open(entry).isoformat(),
            "scheduled_exit_at": cal.session_open(exit_session).isoformat()}


def event_percentile(event, sue, prior_scores):
    """Rank against already available comparable events in trailing 252 sessions."""
    event.validate()
    if not np.isfinite(sue):
        raise ValueError("finite SUE required")
    required = {"event_id", "published_at", "available_at", "sue", "eps_basis"}
    if not required.issubset(prior_scores.columns) or prior_scores.event_id.duplicated().any():
        raise ValueError("incomplete or duplicate event-score history")
    rows = prior_scores.copy()
    for column in ("published_at", "available_at"):
        rows[column] = rows[column].map(aware)
    if (rows.available_at < rows.published_at).any():
        raise ValueError("invalid prior score chronology")
    published = aware(event.published_at)
    cal = calendar()
    session = cal.date_to_session(published.tz_convert("America/New_York").date(), direction="previous")
    index = cal.sessions.get_loc(session)
    if index < 252:
        raise ValueError("insufficient calendar history")
    earliest = pd.Timestamp(cal.sessions[index-252]).tz_localize("America/New_York").tz_convert("UTC")
    rows = rows[(rows.available_at < published) & (rows.published_at >= earliest)
                & (rows.published_at < published) & (rows.eps_basis == event.eps_basis)
                & (rows.event_id != event.event_id) & np.isfinite(rows.sue)]
    if rows.empty:
        raise ValueError("no comparable prior events; cannot invent percentile")
    percentile = float((rows.sue <= sue).mean())
    return {"percentile": percentile, "reference_events": len(rows),
            "positive_top_quintile": bool(sue > 0 and percentile >= .8)}

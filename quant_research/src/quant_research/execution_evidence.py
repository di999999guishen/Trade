"""Conservative S6 borrow and S7 quote/budget gates; do not manufacture history."""
from dataclasses import dataclass
from math import isfinite

import pandas as pd

from .fundamental_signals import aware
from .option_payoffs import expiry_pnl


@dataclass(frozen=True)
class BorrowEvidence:
    security_id: str
    available_shares: float
    annual_fee: float
    observed_at: str
    available_at: str
    expires_at: str
    recalled: bool
    source_id: str

    def check(self, at, requested_shares):
        if (not self.security_id or not self.source_id or type(self.recalled) is not bool
                or not all(isfinite(x) for x in (self.available_shares, self.annual_fee, requested_shares))
                or min(self.available_shares, self.annual_fee, requested_shares) < 0):
            raise ValueError("invalid borrow evidence")
        if not aware(self.observed_at) <= aware(self.available_at) <= aware(at) <= aware(self.expires_at):
            raise ValueError("stale/future borrow evidence")
        return {"approved_shares": 0.0 if self.recalled else min(requested_shares, self.available_shares),
                "force_cover": self.recalled, "annual_fee": self.annual_fee, "source_id": self.source_id}


class DividendObligations:
    """Freeze signed RAW shares at ex-date; pay receivable/liability at pay-date."""
    def __init__(self):
        self.events = {}

    def accrue(self, event_id, ex_date, pay_date, raw_shares, amount_per_share):
        ex, pay = pd.Timestamp(ex_date), pd.Timestamp(pay_date)
        if event_id in self.events or not event_id or pd.isna(ex) or pd.isna(pay) or pay < ex:
            raise ValueError("duplicate/invalid dividend event")
        if not all(isfinite(x) for x in (raw_shares, amount_per_share)) or amount_per_share < 0:
            raise ValueError("invalid raw dividend inputs")
        self.events[event_id] = {"ex": ex, "pay": pay, "amount": raw_shares*amount_per_share, "paid": False}

    def outstanding(self, at):
        return sum(e["amount"] for e in self.events.values() if not e["paid"] and e["ex"] <= pd.Timestamp(at))

    def settle(self, at):
        cash = 0.0
        for event in self.events.values():
            if not event["paid"] and event["pay"] <= pd.Timestamp(at):
                cash += event["amount"]
                event["paid"] = True
        return cash


def validate_option_entry(legs, quotes, at, nav, cash, underlying_shares, underlying_price,
                          underlying_delta=1.0, variant="collar"):
    """Check supplied vanilla legs at 09:45–10:00 ET; returns proposed cash only.

    Greeks and quotes are external contemporaneous evidence. No theoretical or
    today's Greeks are substituted for historical ones. No orders are sent.
    """
    when = aware(at)
    local = when.tz_convert("America/New_York")
    if not ((9, 45) <= (local.hour, local.minute) < (10, 0)):
        raise ValueError("outside option entry window")
    if (not legs or len(legs) != len(quotes) or not all(isfinite(v) for v in
        (nav, cash, underlying_shares, underlying_price, underlying_delta)) or nav <= 0 or cash < 0
            or underlying_shares < 0 or underlying_price <= 0 or not 0 <= underlying_delta <= 1):
        raise ValueError("invalid option portfolio inputs")
    expiry_pnl(underlying_price, underlying_price, underlying_shares, legs, variant)
    times, premium, fees, delta_shares = [], 0.0, 0.0, underlying_shares*underlying_delta
    for leg, quote in zip(legs, quotes, strict=True):
        stamp, known = aware(quote["quote_at"]), aware(quote["available_at"])
        if not stamp <= known <= when or not 0 <= (when-stamp).total_seconds() <= 30:
            raise ValueError("stale/future option quote")
        if (not quote["contract_id"] or any(quote[key] != getattr(leg, key) for key in
            ("kind", "strike", "expiry", "multiplier", "deliverable_shares"))):
            raise ValueError("contract identity mismatch")
        days = (pd.Timestamp(leg.expiry).date()-local.date()).days
        if not 30 <= days <= 60:
            raise ValueError("new option expiry outside 30-60 days")
        bid, ask, delta = quote["bid"], quote["ask"], quote["delta"]
        if not all(isfinite(v) for v in (bid, ask, delta)) or bid <= 0 or ask < bid or not -1 <= delta <= 1:
            raise ValueError("invalid option bid/ask/Greek")
        if ask-bid > min(.25, .1*(ask+bid)/2)+1e-12:
            raise ValueError("option spread too wide")
        if (bid, ask) != (leg.bid, leg.ask):
            raise ValueError("leg fill price differs from checked quote")
        times.append(stamp)
        premium += leg.contracts*leg.multiplier*(ask if leg.contracts > 0 else bid)
        fees += abs(leg.contracts)*leg.fee_per_contract
        delta_shares += leg.contracts*leg.multiplier*delta
    if (max(times)-min(times)).total_seconds() > 5:
        raise ValueError("option legs not synchronized")
    if len({leg.expiry for leg in legs}) != 1:
        raise ValueError("mismatched option expiry")
    if sum(-leg.contracts*leg.deliverable_shares for leg in legs if leg.kind == "call" and leg.contracts < 0) > underlying_shares:
        raise ValueError("short call not covered")
    if sum(leg.contracts*leg.deliverable_shares for leg in legs if leg.kind == "put" and leg.contracts > 0) > underlying_shares:
        raise ValueError("put protection exceeds shares")
    if max(0, premium) > .005*nav or cash-premium-fees < .05*nav:
        raise ValueError("premium budget or cash reserve exceeded")
    dollar_delta = delta_shares*underlying_price/nav
    if not 0 <= dollar_delta <= 1:
        raise ValueError("portfolio delta outside [0,1]")
    return {"status": "entry_gate_only_not_fill", "premium": premium, "fees": fees,
            "cash_after_if_filled": cash-premium-fees, "dollar_delta_over_nav": dollar_delta}


def assignment_effect(leg, assigned_contracts):
    """Supplied assignment/exercise event, vanilla share deliverable only.

    Does not predict early assignment. Caller must remove the exercised option,
    apply these share/cash changes and reconcile positions before any new order.
    """
    if type(assigned_contracts) is not int or not 0 < assigned_contracts <= abs(leg.contracts):
        raise ValueError("invalid assignment quantity")
    sign = 1 if leg.contracts > 0 else -1
    shares = sign*assigned_contracts*leg.deliverable_shares*(1 if leg.kind == "call" else -1)
    return {"share_change": shares, "strike_cash_change": -shares*leg.strike,
            "remaining_contracts": leg.contracts-sign*assigned_contracts}

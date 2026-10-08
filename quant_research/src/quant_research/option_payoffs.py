"""S7 illustrative expiry economics only; not option-chain historical backtests."""
from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class VanillaLeg:
    kind: str
    strike: float
    contracts: int  # signed; positive buys at ask, negative sells at bid
    multiplier: float  # supplied explicitly; never assume every contract is 100
    deliverable_shares: float
    bid: float
    ask: float
    fee_per_contract: float
    expiry: str

    def __post_init__(self):
        values = (self.strike, self.multiplier, self.deliverable_shares, self.bid, self.ask,
                  self.fee_per_contract)
        if (self.kind not in {"put", "call"} or type(self.contracts) is not int or self.contracts == 0
                or not all(isfinite(x) for x in values) or self.strike <= 0 or self.multiplier <= 0
                or self.bid <= 0 or self.ask < self.bid or self.fee_per_contract < 0 or not self.expiry):
            raise ValueError("invalid vanilla leg")
        # Cash, baskets and nonstandard deliverables require a separate model.
        if self.deliverable_shares != self.multiplier:
            raise ValueError("non-share or adjusted deliverable unsupported; do not assume vanilla payoff")


def expiry_pnl(spot_start, spot_end, shares, legs, variant):
    """Aggregate underlying P&L, signed intrinsic value, entry bid/ask and fees.

    Does not model path, IV changes, dividends, early assignment, exit costs,
    financing, premium-budget acceptance or real quote eligibility.
    """
    if (not all(isfinite(x) for x in (spot_start, spot_end, shares))
            or spot_start <= 0 or spot_end < 0 or shares <= 0):
        raise ValueError("invalid underlying inputs")
    if variant not in {"collar", "put_spread", "covered_call"}:
        raise ValueError("unknown option structure")
    if len({leg.expiry for leg in legs}) != 1:
        raise ValueError("legs must have the same expiry")
    long_puts = [x for x in legs if x.kind == "put" and x.contracts > 0]
    short_puts = [x for x in legs if x.kind == "put" and x.contracts < 0]
    short_calls = [x for x in legs if x.kind == "call" and x.contracts < 0]
    if any(x.kind == "call" and x.contracts > 0 for x in legs):
        raise ValueError("long calls are outside these three structures")
    if variant == "collar" and not (len(legs) == 2 and len(long_puts) == len(short_calls) == 1):
        raise ValueError("collar requires a long put and covered short call")
    if variant == "covered_call" and not (len(legs) == len(short_calls) == 1):
        raise ValueError("covered call requires one short call")
    if variant == "put_spread":
        if not (len(legs) == 2 and len(long_puts) == len(short_puts) == 1):
            raise ValueError("put spread requires long and short puts")
        upper, lower = long_puts[0], short_puts[0]
        if (upper.strike <= lower.strike or upper.multiplier != lower.multiplier
                or upper.contracts != -lower.contracts):
            raise ValueError("put spread must have matched quantity/deliverable and ordered strikes")
    if sum(-x.contracts*x.deliverable_shares for x in short_calls) > shares:
        raise ValueError("uncovered short call")
    if sum(x.contracts*x.deliverable_shares for x in long_puts) > shares:
        raise ValueError("protection exceeds underlying shares")
    underlying = shares*(spot_end-spot_start)
    premium = sum(x.contracts*x.multiplier*(x.ask if x.contracts > 0 else x.bid) for x in legs)
    intrinsic = sum(x.contracts*x.multiplier*max((spot_end-x.strike) if x.kind == "call"
                                               else (x.strike-spot_end), 0) for x in legs)
    fees = sum(abs(x.contracts)*x.fee_per_contract for x in legs)
    return {"underlying_pnl": underlying, "option_intrinsic": intrinsic, "net_premium_paid": premium,
            "entry_fees": fees, "net_pnl": underlying+intrinsic-premium-fees,
            "result_kind": "illustrative_expiry_scenario_not_historical_return"}

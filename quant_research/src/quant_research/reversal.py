"""S6 causal signals and an explicitly hypothetical signed-position TR ledger.

This is not a broker simulator: borrow availability, recall, actual distributions,
margin rules and opening liquidity are unknown. No executable orders are emitted.
"""
import numpy as np
import pandas as pd
from scipy.optimize import linprog

from .portfolio import SECTORS

VARIANTS = ("spy_ief_residual", "spy_residual", "raw_reversal")
POOL = sorted(SECTORS | {"QQQ", "IWM", "MDY"})


def signals(frames):
    """OLS train ends t-5; recent five returns never participate in fitting."""
    dates = frames["SPY"].index
    prices = pd.DataFrame({s: f.close.reindex(dates) for s, f in frames.items()})
    returns = prices.pct_change(fill_method=None)
    rows = []
    for symbol in POOL:
        if symbol not in frames:
            continue
        frame = frames[symbol].reindex(dates)
        values = returns[[symbol, "SPY", "IEF"]].to_numpy()
        for i in range(131, len(dates)):
            if pd.isna(frame.eligible.iloc[i]) or not bool(frame.eligible.iloc[i]):
                continue
            if not frame.adv20.iloc[i] > 20_000_000:
                continue
            train, recent = values[i-130:i-4], values[i-4:i+1]
            if not np.isfinite(train).all() or not np.isfinite(recent).all():
                continue
            for variant in VARIANTS:
                # The raw-price control uses SPY-only OLS beta for risk control,
                # but its ranking is unstandardized negative five-day return.
                n = 2 if variant == "spy_ief_residual" else 1
                x = np.column_stack((np.ones(126), train[:, 1:1+n]))
                coef, _, rank, _ = np.linalg.lstsq(x, train[:, 0], rcond=None)
                if rank != n + 1:
                    continue
                train_residual = train[:, 0] - x @ coef
                residual = recent[:, 0] - np.column_stack((np.ones(5), recent[:, 1:1+n])) @ coef
                score = (-np.sum(residual) / max(np.std(train_residual, ddof=1)*np.sqrt(5), 1e-12)
                         if variant != "raw_reversal" else -np.expm1(np.log1p(recent[:, 0]).sum()))
                rows.append({"session": dates[i], "symbol": symbol, "variant": variant,
                             "score": float(score), "beta": float(coef[1]),
                             "fit_start": dates[i-130], "fit_end": dates[i-5]})
    return pd.DataFrame(rows)


def constrained_weights(scores, excluded=()):
    """Retain maximum gross exposure subject to downward-only linear constraints."""
    if len(scores) < 6:
        return {}
    scores = scores.sort_values(["score", "symbol"], ascending=[False, True])
    longs, shorts = scores.iloc[:3], scores.iloc[-3:]
    selected = pd.concat([longs.assign(side=1.0), shorts.assign(side=-1.0)])
    selected = selected[~selected.symbol.isin(excluded)].sort_values("symbol")
    if selected.empty:
        return {}
    side = selected.side.to_numpy()
    beta = side * selected.beta.to_numpy()
    result = linprog(-np.ones(len(side)), A_ub=np.array([side, -side, beta, -beta]),
                     b_ub=[.02, .02, .05, .05], bounds=[(0, .1)]*len(side), method="highs")
    if not result.success:
        raise RuntimeError(f"risk optimizer failed (zero is feasible): {result.message}")
    weights = side * np.where(result.x < 1e-10, 0, result.x)
    if abs(weights.sum()) > .02000001 or abs(weights @ selected.beta.to_numpy()) > .05000001:
        raise AssertionError("exposure constraint failure")
    return {s: float(w) for s, w in zip(selected.symbol, weights, strict=True) if w != 0}


def target_schedule(panel, dates, variant):
    """Five held sessions force a full one-session reset; never renew in place."""
    groups = {d: group for d, group in panel[panel.variant == variant].groupby("session")}
    previous, ages, pending, rows = {}, {}, {}, []
    for date in dates:
        # Pending targets execute at this open. Count this as a held session.
        ages = {s: ages.get(s, 0)+1 if np.sign(w) == np.sign(previous.get(s, 0)) else 1
                for s, w in pending.items()}
        previous = pending
        scores = groups.get(date)
        excluded = [s for s, age in ages.items() if age >= 5]
        pending = constrained_weights(scores, excluded) if scores is not None else {}
        betas = dict(zip(scores.symbol, scores.beta, strict=True)) if scores is not None else {}
        rows.append({"session": date, "weights": pending,
                     "estimated_beta": sum(w*betas[s] for s, w in pending.items()),
                     "forced_age_exits": excluded})
    return rows


def simulate(frames, schedule, bps, borrow_rate, capital=100_000.0):
    """Next-open exact fractional weights; cash + long value - short liability.

    150% of current short market value is locked as an illustrative collateral
    rule. Interest/rebate is zero. Fees accrue on prior-close short value using
    actual calendar days/365.25. All target changes execute, without a deadband.
    """
    if not (0 <= bps < 10000 and 0 <= borrow_rate <= 1 and capital > 0):
        raise ValueError("invalid costs/capital")
    units, cash, navs, fills, holdings = {}, float(capital), [], [], []
    previous_date, previous_short = None, 0.0
    for i, decision in enumerate(schedule):
        date = pd.Timestamp(decision["session"])
        weights = schedule[i-1]["weights"] if i else {}
        symbols = sorted(set(units) | set(weights))
        opening = {s: float(frames[s].loc[date, "open"]) for s in symbols}
        closing = {s: float(frames[s].loc[date, "close"]) for s in symbols}
        if any(not np.isfinite(p) or p <= 0 for p in [*opening.values(), *closing.values()]):
            raise ValueError("missing/nonpositive execution or valuation price")
        borrow = previous_short * borrow_rate * ((date-previous_date).days/365.25) if i else 0
        cash -= borrow
        pre_nav = cash + sum(units.get(s, 0)*opening[s] for s in symbols)
        rate = bps / 10000
        def costs(post_nav, rate=rate, weights=weights, opening=opening, symbols=symbols):
            return rate*sum(abs(weights.get(s, 0)*post_nav-units.get(s, 0)*opening[s]) for s in symbols)
        low, high = 0.0, pre_nav
        if pre_nav <= 0 or costs(0) >= pre_nav:
            raise ValueError("insolvent scenario")
        for _ in range(55):
            mid = (low+high)/2
            if mid+costs(mid) > pre_nav:
                high = mid
            else:
                low = mid
        post_nav = (low+high)/2
        traded = total_cost = 0.0
        for s in symbols:
            target_units = weights.get(s, 0)*post_nav/opening[s]
            delta = target_units-units.get(s, 0)
            notional, fee = delta*opening[s], abs(delta*opening[s])*rate
            lagged_adv = float(frames[s].loc[previous_date, "adv20"])
            if not np.isfinite(lagged_adv) or lagged_adv <= 0:
                raise ValueError("missing lagged capacity evidence")
            if abs(notional) > .01*lagged_adv:
                raise ValueError("order exceeds 1% of lagged daily ADV; scenario aborted")
            if delta != 0:
                fills.append({"session": str(date.date()), "symbol": s, "units": delta,
                              "price": opening[s], "cost": fee,
                              "decision_session": str(previous_date.date())})
                cash -= notional+fee
                traded += abs(notional)
                total_cost += fee
            if target_units:
                units[s] = target_units
            else:
                units.pop(s, None)
        long_value = sum(max(q, 0)*closing[s] for s, q in units.items())
        short_value = sum(max(-q, 0)*closing[s] for s, q in units.items())
        nav = cash+long_value-short_value
        collateral = 1.5*short_value
        if cash-collateral < -1e-8 or nav <= 0:
            raise ValueError("illustrative collateral requirement violated")
        for s, q in units.items():
            holdings.append({"session": str(date.date()), "symbol": s, "units": q})
        navs.append({"session": str(date.date()), "nav": nav, "cash": cash,
                     "short_liability": short_value, "locked_collateral": collateral,
                     "free_cash": cash-collateral, "cost": total_cost, "borrow_cost": borrow,
                     "cash_weight": cash/nav, "gross_exposure": (long_value+short_value)/nav,
                     "net_exposure": (long_value-short_value)/nav,
                     "one_way_turnover_risky": traded/pre_nav/2})
        previous_date, previous_short = date, short_value
    return {"nav": navs, "fills": fills, "holdings": holdings}


def audit(result, frames, bps, borrow_rate, capital):
    """Replay fills independently; verify fees, short liabilities and holding age."""
    trades, reported = {}, {}
    for fill in result["fills"]:
        trades.setdefault(fill["session"], []).append(fill)
    for row in result["holdings"]:
        reported.setdefault(row["session"], {})[row["symbol"]] = row["units"]
    cash, positions, previous, last_short, errors, ages, max_age = capital, {}, None, 0, [], {}, 0
    for row in result["nav"]:
        date = pd.Timestamp(row["session"])
        old_sign = {s: np.sign(q) for s, q in positions.items()}
        fee = last_short*borrow_rate*(date-previous).days/365.25 if previous is not None else 0
        if abs(fee-row["borrow_cost"]) > 1e-8:
            raise AssertionError("borrow accrual mismatch")
        cash -= fee
        day_cost = 0.0
        for fill in trades.get(row["session"], []):
            s, q = fill["symbol"], fill["units"]
            price = frames[s].loc[date, "open"]
            if pd.Timestamp(fill["decision_session"]) != previous or price != fill["price"]:
                raise AssertionError("trade time/price mismatch")
            fee = abs(q*price)*bps/10000
            if abs(fee-fill["cost"]) > 1e-8:
                raise AssertionError("transaction fee mismatch")
            cash -= q*price+fee
            day_cost += fee
            positions[s] = positions.get(s, 0)+q
        positions = {s: q for s, q in positions.items() if abs(q*frames[s].loc[date, "close"]) > 1e-8}
        expected = reported.get(row["session"], {})
        if positions.keys() != expected.keys() or any(abs(q-expected[s]) > 1e-8 for s, q in positions.items()):
            raise AssertionError("holdings replay mismatch")
        ages = {s: ages.get(s, 0)+1 if old_sign.get(s) == np.sign(q) else 1 for s, q in positions.items()}
        max_age = max([max_age, *ages.values()])
        if max_age > 5:
            raise AssertionError("holding period exceeds five sessions")
        values = [q*frames[s].loc[date, "close"] for s, q in positions.items()]
        last_short = -sum(min(v, 0) for v in values)
        errors.extend([abs(cash-row["cash"]), abs(cash+sum(values)-row["nav"]),
                       abs(last_short-row["short_liability"]), abs(day_cost-row["cost"])])
        previous = date
    error = max(errors, default=0.0)
    if error > 1e-8:
        raise AssertionError(f"independent ledger mismatch: {error}")
    return {"passed": True, "max_abs_usd_error": float(error), "max_held_sessions": max_age}

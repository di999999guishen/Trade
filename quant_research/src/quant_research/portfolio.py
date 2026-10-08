"""Frozen score rules and downward-only constraints."""
import numpy as np
import pandas as pd

SECTORS = {"XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY", "XLRE", "XLC"}


def cap_weights(weights, config):
    p = config.portfolio
    result = {s: min(max(float(w), 0.0), p.single_asset_weight_max) for s, w in weights.items()}
    reasons = []
    for group, cap, reason in ((set(p.technology_proxy_symbols), p.technology_proxy_weight_max, "technology_cap"),
                               (SECTORS, p.sector_etf_total_weight_max, "sector_cap"),
                               (set(result), p.gross_exposure_max, "gross_cap")):
        total = sum(w for s, w in result.items() if s in group)
        if total > cap:
            for s in group & result.keys():
                result[s] *= cap / total
            reasons.append(reason)
    return result, reasons


def turnover(left, right, cash=True):
    value = sum(abs(right.get(s, 0) - left.get(s, 0)) for s in sorted(left.keys() | right.keys()))
    if cash:
        value += abs(sum(right.values()) - sum(left.values()))
    return value / 2


def transition(current, desired, forced, config):
    """Forced exits/cap reduction first; optional changes consume remaining budget."""
    p = config.portfolio
    start = {s: current.get(s, 0.0) for s in desired}
    base = dict(start)
    for symbol in forced:
        base[symbol] = min(base[symbol], desired[symbol])
    base, cap_reasons = cap_weights(base, config)
    used = turnover(start, base)
    optional = {s: desired[s] if abs(desired[s] - base[s]) >= p.weight_change_trade_threshold
                else base[s] for s in desired}
    optional, _ = cap_weights(optional, config)
    demand = turnover(base, optional)
    scale = min(1.0, max(0.0, p.one_way_turnover_soft_cap - used) / demand) if demand else 0.0
    final = {s: base[s] + scale * (optional[s] - base[s]) for s in desired}
    return final, cap_reasons + (["forced_risk_turnover_exemption"] if used > p.one_way_turnover_soft_cap else [])


def desired_weights(strategy, session, frames, factors, current, config, first=False, model_scores=None):
    symbols = list(frames)
    zero = dict.fromkeys(symbols, 0.0)
    valid = {s: bool(frames[s].at[session, "eligible"] == True) if session in frames[s].index else False for s in symbols}
    reasons = []
    scores = {}
    forced = {s for s in symbols if not valid[s]}
    if strategy in ("buy_hold_spy", "buy_hold_qqq"):
        symbol = "SPY" if strategy.endswith("spy") else "QQQ"
        if not first:
            return dict(current), {}, [], set(), False
        if not valid[symbol]:
            raise ValueError(f"benchmark {symbol} not eligible on common starting session")
        zero[symbol] = 1.0
        return zero, {}, [], set(), True
    if strategy == "simple_trend":
        if valid["SPY"] and factors["SPY"].at[session, "trend200"] > 0:
            zero["SPY"] = 1.0
        return zero, {}, [], {"SPY"} if zero["SPY"] == 0 else set(), True
    if strategy == "eligible_pool_equal_weight":
        from .calendar import calendar
        if not first and calendar().next_session(session).month == session.month:
            return dict(current), {}, [], set(), False
        chosen = [s for s in symbols if valid[s]]
        if chosen:
            for s in chosen:
                zero[s] = min(1 / len(chosen), config.portfolio.single_asset_weight_max)
        return zero, {}, [], forced, True
    for s in symbols:
        if not valid[s]:
            continue
        f = factors[s].loc[session]
        if strategy == "s2_inverse_volatility":
            scores[s] = 0.0  # no trend gate, momentum gate, forecast score or top-k
            continue
        positive = f.trend200 > 0 and f["mom6_1" if strategy.startswith("s2_") else "mom126"] > 0
        if positive:
            scores[s] = 0.5 * f.mom126 + 0.5 * f.mom252
            if strategy in {"s3_lightgbm", "s3_ridge", "s8_fixed_ensemble"}:
                if model_scores is None or s not in model_scores or not np.isfinite(model_scores[s]):
                    raise ValueError(f"missing causal model score: {session} {s}")
                scores[s] = float(model_scores[s])
            elif strategy == "single_momentum":
                scores[s] = float(f.mom252)
        else:
            forced.add(s)
    if strategy in {"s1_etf_momentum", "s3_lightgbm", "s3_ridge", "s8_fixed_ensemble", "single_momentum"}:
        chosen = sorted(scores, key=lambda s: (-scores[s], s))[:config.strategy.top_k]
        for s in chosen:
            zero[s] = config.strategy.initial_selected_weight
    elif strategy in {"s2_multi_factor", "s2_equal_allocation", "s2_inverse_volatility"}:
        eligible = list(scores)
        if eligible:
            table = pd.DataFrame({s: factors[s].loc[session] for s in eligible}).T
            required = ["vol63"] if strategy == "s2_inverse_volatility" else ["mom12_1", "mom6_1", "vol63", "down63"]
            if table[required].isna().any().any():
                return zero, {}, ["missing_s2_inputs"], set(symbols), True
            def rank(series):
                return (series.rank(method="average") - 1) / (len(series) - 1) if len(series) > 1 else series * 0 + 0.5
            if strategy == "s2_inverse_volatility":
                chosen = sorted(eligible)
                allocation = {s: 1 / max(table.at[s, "vol63"], .05) for s in chosen}
            else:
                score = .45 * rank(table.mom12_1) + .25 * rank(table.mom6_1) + .15 * rank(-table.vol63) + .15 * rank(-table.down63)
                scores = score.to_dict()
                chosen = sorted(scores, key=lambda s: (-scores[s], s))[:8]
                allocation = {s: (0.5 + scores[s]) / max(table.at[s, "vol63"], .05) for s in chosen}
            if strategy == "s2_equal_allocation":
                allocation = dict.fromkeys(chosen, 1.0)
            total = sum(allocation.values())
            zero.update({s: w / total for s, w in allocation.items()})
            zero, reasons = cap_weights(zero, config)
            returns = pd.DataFrame({s: frames[s].close.loc[:session].pct_change(fill_method=None) for s in chosen}).tail(126)
            if len(returns) < 126 or returns.isna().any().any():
                return dict.fromkeys(symbols, 0.0), scores, ["missing_covariance"], set(symbols), True
            cov = returns.cov().to_numpy()
            cov = .7 * cov + .3 * np.diag(np.diag(cov))
            w = np.array([zero[s] for s in chosen])
            vol = float(np.sqrt(max(0, 252 * w @ cov @ w)))
            scale = min(1, .10 / vol) if vol else 1
            zero = {s: w * scale for s, w in zero.items()}
    else:
        raise ValueError(f"unknown strategy: {strategy}")
    zero, caps = cap_weights(zero, config)
    return zero, scores, reasons + caps, forced, True

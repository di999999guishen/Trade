"""Qlib Exchange/Position adapter with explicit next-session orchestration.

Only the quote-loading hook is replaced. Qlib performs fills, proportional fees,
cash constraints and position updates. No stock signal auto-shift is used.
"""
from importlib import metadata

import numpy as np
import pandas as pd

from .calendar import calendar, decision_at, next_open
from .factors import compute
from .portfolio import desired_weights, transition, turnover


def make_exchange(frames, bps):
    from qlib.backtest.exchange import Exchange
    from qlib.config import C
    from qlib.constant import REG_US
    if metadata.version("pyqlib") != "0.9.7":
        raise RuntimeError("Qlib version differs from validated 0.9.7 adapter")
    C.set(region=REG_US, expression_cache=None, dataset_cache=None, logging_level=40)
    quotes = []
    for symbol, frame in frames.items():
        # Qlib's optional capacity/impact paths must not observe execution-day volume.
        # Exchange's suspension check reads $close. Use the open quote here so
        # execution cannot depend on the as-yet-unknown same-day closing bar.
        # Actual closing valuation is performed explicitly below.
        quote = pd.DataFrame({"$open": frame.open, "$close": frame.open,
                              "$change": 0.0, "$factor": 1.0, "$volume": 1.0}, index=frame.index)
        quote["instrument"] = symbol
        quote["datetime"] = quote.index
        quotes.append(quote.set_index(["instrument", "datetime"]))
    table = pd.concat(quotes).sort_index()

    class SnapshotExchange(Exchange):
        def get_quote_from_qlib(self):
            self.quote_df = table.copy()
            self.trade_w_adj_price = False
            self._update_limit(self.limit_threshold)
            missing = self.quote_df["$open"].isna() | (self.quote_df["$open"] <= 0)
            self.quote_df.loc[missing, ["limit_buy", "limit_sell"]] = True

    return SnapshotExchange(codes=list(frames), deal_price="$open", trade_unit=None,
                            limit_threshold=None, volume_threshold=None, min_cost=0,
                            impact_cost=0, open_cost=bps / 10000, close_cost=bps / 10000)


def run_strategy(frames, config, strategy, bps, start=None, score_panel=None):
    from qlib.backtest.decision import Order
    from qlib.backtest.position import Position
    cal = calendar()
    first_session = min(f.index.min() for f in frames.values())
    last_session = max(f.index.max() for f in frames.values())
    sessions = cal.sessions_in_range(first_session, last_session)
    frames = {s: f.reindex(sessions) for s, f in frames.items()}
    if start is None:
        common = frames["SPY"].eligible.fillna(False) & frames["QQQ"].eligible.fillna(False)
        if not common.any():
            raise ValueError("insufficient_data: no common verified, warmed-up benchmark session")
        start = common[common].index[0]
    if pd.Timestamp(start) >= last_session:
        raise ValueError("insufficient_data: no next-session execution")
    sessions = sessions[sessions >= pd.Timestamp(start)]
    if (strategy in {"s3_lightgbm", "s3_ridge", "s8_fixed_ensemble"}
            and (score_panel is None or score_panel.index.has_duplicates or not sessions.isin(score_panel.index).all())):
        raise ValueError("model score panel must cover every decision session uniquely")
    factors = {s: compute(f) for s, f in frames.items()}
    exchange = make_exchange(frames, bps)
    class OrderedPosition(Position):
        def get_stock_list(self):
            # Qlib 0.9.7 uses a set here. Stable summation is required for exact
            # result hashes across processes with different Python hash seeds.
            return sorted(super().get_stock_list())

    position = OrderedPosition(cash=config.portfolio.initial_capital_usd, position_dict={})
    targets, fills, nav, holdings = [], [], [], []
    pending = None
    total_cost = 0.0
    symbols = list(frames)
    for day_index, session in enumerate(sessions):
        day_cost = 0.0
        notional = 0.0
        risky_turnover = 0.0
        cash_turnover = 0.0
        if pending is not None:
            target, known_adv, signal_session = pending
            opening_prices = {}
            for s in position.get_stock_list():
                price = frames[s].at[session, "open"]
                if not np.isfinite(price):
                    # Existing price is last actual close, used only for sizing;
                    # no fills at that price. Missing close later fails valuation.
                    price = position.get_stock_price(s)
                position.update_stock_price(s, float(price))
            opening_nav = position.calculate_value()
            pre = {s: position.get_stock_amount(s) * position.get_stock_price(s) / opening_nav
                   if position.check_stock(s) else 0.0 for s in symbols}
            deltas = {}
            # Allocate against post-cost NAV, otherwise a 25% target becomes
            # >25% as soon as fees reduce equity. Monotone fixed-point solve.
            lower_nav, upper_nav = 0.0, opening_nav
            target_nav = opening_nav
            # These inputs are constant throughout the fixed-point solve. Cache
            # them once so multi-year runs don't repeat pandas lookups 70 times.
            sizing = []
            for s in symbols:
                op = frames[s].at[session, "open"]
                if np.isfinite(op) and op > 0:
                    cap = known_adv.get(s, np.nan) * config.portfolio.execution_participation_of_lagged_daily_adv_max
                    sizing.append((target[s], pre[s] * opening_nav, cap))
            for _ in range(70):
                target_nav = (lower_nav + upper_nav) / 2
                estimated_trades = 0.0
                for weight, held_value, cap in sizing:
                    proposed = weight * target_nav - held_value
                    if np.isfinite(cap):
                        proposed = np.sign(proposed) * min(abs(proposed), cap)
                    else:
                        proposed = min(proposed, 0.0)
                    estimated_trades += abs(proposed)
                residual = target_nav + estimated_trades * bps / 10000 - opening_nav
                if residual > 0:
                    upper_nav = target_nav
                else:
                    lower_nav = target_nav
            for s in symbols:
                price = frames[s].at[session, "open"]
                if not np.isfinite(price) or price <= 0:
                    if abs(target[s] - pre[s]) > 1e-12:
                        fills.append({"session": str(session.date()), "symbol": s, "status": "missing_open",
                                      "signed_units": 0.0, "notional": 0.0, "cost": 0.0, "price": None})
                    continue
                opening_prices[s] = float(price)
                value = target[s] * target_nav - pre[s] * opening_nav
                # Benchmark capacity is also constrained; insufficient capacity
                # must be reported rather than silently compared as full exposure.
                capacity = known_adv.get(s, np.nan) * config.portfolio.execution_participation_of_lagged_daily_adv_max
                if not np.isfinite(capacity):
                    value = min(value, 0.0)  # unknown ADV: no new risk
                else:
                    value = np.sign(value) * min(abs(value), capacity)
                deltas[s] = float(value)
            # Sell before buy. Pre-scale all buys equally to preserve ranking neutrality.
            sell_proceeds = sum(-v * (1 - bps / 10000) for v in deltas.values() if v < 0)
            buy_need = sum(v * (1 + bps / 10000) for v in deltas.values() if v > 0)
            scale = min(1.0, max(0, position.get_cash() + sell_proceeds) / buy_need) if buy_need else 1.0
            for s in sorted(deltas, key=lambda s: (deltas[s] > 0, s)):
                value = deltas[s] * (scale if deltas[s] > 0 else 1)
                if abs(value) < 1e-5:
                    continue
                order = Order(s, abs(value) / opening_prices[s], Order.BUY if value > 0 else Order.SELL,
                              session, session)
                traded, cost, price = exchange.deal_order(order, position=position, dealt_order_amount={})
                signed = float(order.deal_amount_delta) if traded > 1e-5 else 0.0
                fills.append({"session": str(session.date()), "signal_session": str(signal_session.date()),
                              "execution_at": cal.session_open(session).isoformat(), "symbol": s,
                              "status": "filled" if traded > 1e-5 else "rejected",
                              "signed_units": signed, "notional": float(traded), "cost": float(cost),
                              "price": float(price) if np.isfinite(price) else None,
                              "lagged_adv_usd": float(known_adv[s]) if np.isfinite(known_adv[s]) else None})
                day_cost += cost
                notional += traded
            post_open_nav = position.calculate_value()
            post = {s: position.get_stock_amount(s) * position.get_stock_price(s) / post_open_nav
                    if position.check_stock(s) else 0.0 for s in symbols}
            risky_turnover, cash_turnover = turnover(pre, post, False), turnover(pre, post, True)
        else:
            opening_nav = config.portfolio.initial_capital_usd
        for s in position.get_stock_list():
            close = frames[s].at[session, "close"]
            if not np.isfinite(close) or close <= 0:
                raise ValueError(f"unresolved_held_valuation: {s} {session.date()}")
            position.update_stock_price(s, float(close))
        value = position.calculate_value()
        cash = position.get_cash()
        if cash < -1e-8 or not np.isfinite(value) or value <= 0:
            raise ValueError("cash/NAV conservation failure")
        current = {s: position.get_stock_amount(s) * position.get_stock_price(s) / value
                   if position.check_stock(s) else 0.0 for s in symbols}
        total_cost += day_cost
        nav.append({"session": str(session.date()), "nav": float(value), "cash": float(cash),
                    "cost": float(day_cost), "total_cost": float(total_cost),
                    "one_way_turnover_risky": risky_turnover, "one_way_turnover_with_cash": cash_turnover,
                    "traded_notional_ratio": float(notional / opening_nav), "cash_weight": float(cash / value)})
        for s in symbols:
            holdings.append({"session": str(session.date()), "symbol": s,
                             "normalized_units": position.get_stock_amount(s) if position.check_stock(s) else 0.0,
                             "weight": current[s]})
        desired, scores, reasons, forced, rebalance = desired_weights(
            strategy, session, frames, factors, current, config, first=day_index == 0,
            model_scores=score_panel.loc[session].to_dict() if score_panel is not None else None)
        if strategy in {"s1_etf_momentum", "s2_multi_factor", "s3_lightgbm", "s3_ridge", "s8_fixed_ensemble", "single_momentum", "s2_equal_allocation", "s2_inverse_volatility"}:
            desired, transition_reasons = transition(current, desired, forced, config)
            reasons += transition_reasons
        known_adv = {s: frames[s].at[session, "adv20"] for s in symbols}
        pending = (desired, known_adv, session) if rebalance else None
        for s in symbols:
            score = scores.get(s)
            targets.append({"session": str(session.date()), "symbol": s, "strategy_id": strategy,
                            "decision_at": decision_at(session).tz_convert("UTC").isoformat(),
                            "earliest_execution_at": next_open(session, cal).isoformat(),
                            "target_weight": float(desired[s]), "score": float(score) if score is not None and np.isfinite(score) else None,
                            "reason_codes": reasons + (["not_eligible"] if not bool(frames[s].at[session, "eligible"] == True) else []),
                            "rebalance": rebalance})
    return {"nav": nav, "targets": targets, "fills": fills, "holdings": holdings,
            "engine": "qlib_0.9.7_exchange_position", "price_mode": config.data.price_mode}


def independent_audit(result, frames, initial_capital):
    """Replay only emitted fills, without any Qlib methods or target generation."""
    cash = initial_capital
    units = dict.fromkeys(frames, 0.0)
    max_error = 0.0
    by_date = {}
    for fill in result["fills"]:
        by_date.setdefault(fill["session"], []).append(fill)
    for row in result["nav"]:
        for fill in by_date.get(row["session"], []):
            if fill["status"] != "filled":
                continue
            units[fill["symbol"]] += fill["signed_units"]
            cash -= fill["signed_units"] * fill["price"] + fill["cost"]
        value = cash + sum(amount * frames[s].at[pd.Timestamp(row["session"]), "close"]
                           for s, amount in units.items() if abs(amount) > 1e-12)
        error = max(abs(value - row["nav"]), abs(cash - row["cash"]))
        if error > 1e-8:
            raise ValueError(f"independent ledger mismatch: {row['session']} {error}")
        max_error = max(error, max_error)
    return {"status": "passed", "maximum_absolute_usd_error": max_error,
            "scope": "proxy_fills_cash_units_only_not_LEAN_or_raw_actions"}

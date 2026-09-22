from copy import deepcopy
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant_research.config import DEFAULTS, ResearchConfig
from quant_research.data import DataRequest, normalize, snapshot
from quant_research.engine import independent_audit, make_exchange, run_strategy
from quant_research.factors import compute
from quant_research.fixtures import FixtureProvider
from quant_research.portfolio import cap_weights, transition, turnover
from quant_research.research import metrics


def config():
    data = deepcopy(DEFAULTS)
    data["universe"]["symbols"] = ["SPY", "QQQ"]
    return ResearchConfig.model_validate(data)


def hand_frames():
    table = pd.read_csv(Path(__file__).parent / "fixtures" / "two_asset_hand.csv", parse_dates=["session"])
    frames = {}
    for s, group in table.groupby("symbol"):
        frame = group.set_index("session").drop(columns="symbol")
        frame["adv20"] = 100000000.0
        frame["eligible"] = True
        frames[s] = frame
    return frames


def test_hand_next_open_cost_and_benchmark():
    cfg = config()
    frames = hand_frames()
    result = run_strategy(frames, cfg, "buy_hold_spy", 5)
    units = 100000 / (100 * 1.0005)
    assert len(result["fills"]) == 1
    assert result["fills"][0]["session"] == "2024-03-08"
    assert result["nav"][1]["nav"] == pytest.approx(units * 110, abs=1e-8)
    assert result["nav"][2]["nav"] == pytest.approx(units * 99, abs=1e-8)
    assert result["fills"][0]["cost"] == pytest.approx(units * 100 * .0005, abs=1e-8)
    assert independent_audit(result, frames, 100000)["status"] == "passed"


def test_qlib_explicit_units_and_fee():
    from qlib.backtest.decision import Order
    from qlib.backtest.position import Position
    frames = hand_frames()
    exchange = make_exchange(frames, 20)
    position = Position(cash=1000, position_dict={})
    day = pd.Timestamp("2024-03-08")
    order = Order("SPY", 5.0, Order.BUY, day, day)
    val, cost, price = exchange.deal_order(order, position=position, dealt_order_amount={})
    assert (val, cost, price) == (500, 1, 100)
    position.update_stock_price("SPY", 110)
    assert position.get_cash() == 499
    assert position.calculate_value() == 1049
    assert exchange.trade_unit is None
    assert exchange.limit_threshold is None


def test_missing_open_no_close_fallback():
    frames = hand_frames()
    frames["SPY"].loc["2024-03-08", "open"] = np.nan
    result = run_strategy(frames, config(), "buy_hold_spy", 5)
    assert all(f["status"] != "filled" for f in result["fills"])
    assert result["nav"][-1]["nav"] == 100000
    assert result["fills"][0]["status"] == "missing_open"


def test_missing_held_close_fails():
    frames = hand_frames()
    frames["SPY"].loc["2024-03-11", "close"] = np.nan
    with pytest.raises(ValueError, match="unresolved_held_valuation"):
        run_strategy(frames, config(), "buy_hold_spy", 5)


def test_cost_monotonicity_and_future_invariance():
    frames = hand_frames()
    outcomes = [run_strategy(frames, config(), "buy_hold_spy", cost) for cost in (5, 10, 25)]
    assert outcomes[0]["nav"][-1]["nav"] > outcomes[1]["nav"][-1]["nav"] > outcomes[2]["nav"][-1]["nav"]
    frames["SPY"].loc["2024-03-11", ["open", "high", "low", "close"]] *= 100
    changed = run_strategy(frames, config(), "buy_hold_spy", 5)
    assert changed["fills"][0] == outcomes[0]["fills"][0]
    assert changed["targets"][:4] == outcomes[0]["targets"][:4]


def test_turnover_caps_and_emergency_exit():
    assert turnover({"A": 0}, {"A": 1}, False) == .5
    assert turnover({"A": 0}, {"A": 1}, True) == 1
    assert turnover({"A": 1, "B": 0}, {"A": 0, "B": 1}) == 1
    desired, _ = cap_weights({"QQQ": .25, "XLK": .25, "XLC": .25}, config())
    assert sum(desired.values()) == pytest.approx(.5)
    final, _ = transition({"SPY": 0, "QQQ": 0}, {"SPY": .2, "QQQ": .2}, set(), config())
    assert sum(final.values()) == pytest.approx(.2)
    final, reasons = transition({"SPY": 1, "QQQ": 0}, {"SPY": 0, "QQQ": .2}, {"SPY"}, config())
    assert final == {"SPY": 0, "QQQ": 0}
    assert "forced_risk_turnover_exemption" in reasons


def test_factor_causality_and_warmup(tmp_path):
    request = DataRequest(("SPY", "QQQ"), date(2022, 1, 3), date(2024, 1, 1))
    path = snapshot(request, FixtureProvider(), tmp_path, attempts=1, pause=0)
    frames, _ = normalize(path, config())
    before = compute(frames["SPY"])
    changed = frames["SPY"].copy()
    changed.loc["2023-07-01":, "close"] *= 10
    after = compute(changed)
    pd.testing.assert_frame_equal(before.loc[:"2023-06-30"], after.loc[:"2023-06-30"])
    assert before.mom252.iloc[:252].isna().all()
    assert np.isfinite(before.mom252.iloc[252])
    result = run_strategy(frames, config(), "s1_etf_momentum", 5)
    assert independent_audit(result, frames, 100000)["status"] == "passed"
    assert result["fills"]
    assert max(row["one_way_turnover_with_cash"] for row in result["nav"]) <= .201


def test_metrics_zero_variance_and_loss():
    rows = [{"session": d, "nav": v, "cost": 0, "cash_weight": 1, "one_way_turnover_risky": 0}
            for d, v in [("2024-03-07", 100), ("2024-03-08", 100), ("2024-03-11", 100)]]
    assert metrics(rows)["sharpe_zero_rf"] is None
    rows[-1]["nav"] = 80
    assert metrics(rows)["max_drawdown"] == pytest.approx(.2)

import copy

import numpy as np
import pandas as pd
import pytest

from quant_research.reversal import audit, constrained_weights, signals, simulate, target_schedule


def frames_fixture(n=150):
    rng = np.random.default_rng(18)
    dates = pd.bdate_range("2020-01-01", periods=n)
    frames = {}
    for symbol in ["SPY", "IEF", "QQQ", "IWM", "MDY", "XLF", "XLE", "XLK"]:
        prices = 100*np.cumprod(1+rng.normal(0, .01, n))
        frames[symbol] = pd.DataFrame({"open": prices*.999, "close": prices,
                                       "eligible": True, "adv20": 30_000_000.0}, index=dates)
    return frames


def test_signal_prefix_invariance_and_frozen_fit():
    frames = frames_fixture()
    full = signals(frames)
    cutoff = frames["SPY"].index[140]
    prefix = signals({s: f.loc[:cutoff] for s, f in frames.items()})
    pd.testing.assert_frame_equal(full[full.session <= cutoff].reset_index(drop=True), prefix)
    changed = {s: f.copy() for s, f in frames.items()}
    changed["QQQ"].loc[cutoff, "close"] *= 1.1
    modified = signals(changed)
    base = full[(full.session == cutoff) & (full.symbol == "QQQ")]
    bumped = modified[(modified.session == cutoff) & (modified.symbol == "QQQ")]
    np.testing.assert_array_equal(base.beta, bumped.beta)
    assert (base.score.to_numpy() != bumped.score.to_numpy()).all()
    assert (base.fit_end == frames["SPY"].index[135]).all()


def test_constraints_reduce_only_and_never_overlap_sides():
    scores = pd.DataFrame({"symbol": list("ABCDEF"), "score": [6, 5, 4, 3, 2, 1],
                           "beta": [3., 2., 4., .1, .2, .3]})
    weights = constrained_weights(scores)
    assert sum(abs(w) for w in weights.values()) < .6
    assert abs(sum(weights.values())) <= .02000001
    assert abs(sum(w*float(scores.set_index("symbol").loc[s, "beta"]) for s, w in weights.items())) <= .05000001
    assert all(0 < abs(w) <= .10000001 for w in weights.values())
    assert all(w > 0 if s in "ABC" else w < 0 for s, w in weights.items())
    assert constrained_weights(scores.iloc[:5]) == {}


def test_signed_cash_weekend_borrow_and_independent_audit():
    dates = pd.to_datetime(["2024-01-04", "2024-01-05", "2024-01-08"])
    frames = {"QQQ": pd.DataFrame({"open": [100., 100., 90.], "close": [100., 100., 90.],
                                  "adv20": 1e9}, index=dates)}
    schedule = [{"session": dates[0], "weights": {"QQQ": -.1}},
                {"session": dates[1], "weights": {}}, {"session": dates[2], "weights": {}}]
    result = simulate(frames, schedule, 0, .1, 1000)
    assert result["nav"][1]["cash"] == pytest.approx(1100)
    assert result["nav"][1]["nav"] == pytest.approx(1000)  # proceeds are not profit
    assert result["nav"][1]["locked_collateral"] == pytest.approx(150)
    expected_fee = 100*.1*3/365.25
    assert result["nav"][2]["nav"] == pytest.approx(1010-expected_fee)
    assert audit(result, frames, 0, .1, 1000)["passed"]
    tampered = copy.deepcopy(result)
    tampered["nav"][2]["borrow_cost"] = 0
    with pytest.raises(AssertionError, match="borrow"):
        audit(tampered, frames, 0, .1, 1000)


def test_maximum_five_sessions_and_cost_drag():
    frames = frames_fixture(145)
    panel = signals(frames)
    dates = frames["SPY"].index[131:]
    schedule = target_schedule(panel, dates, "spy_ief_residual")
    gross = simulate(frames, schedule, 0, 0)
    net = simulate(frames, schedule, 25, .1)
    assert audit(net, frames, 25, .1, 100000)["max_held_sessions"] <= 5
    assert net["nav"][-1]["nav"] < gross["nav"][-1]["nav"]
    assert all(pd.Timestamp(f["session"]) > pd.Timestamp(f["decision_session"]) for f in net["fills"])


def test_persistent_signal_forces_flat_session_before_reentry():
    dates = pd.bdate_range("2024-01-01", periods=9)
    panel = pd.DataFrame([{"session": d, "symbol": s, "score": i, "beta": 1.,
                           "variant": "spy_residual"}
                          for d in dates for i, s in enumerate("ABCDEF")])
    schedule = target_schedule(panel, dates, "spy_residual")
    assert len(schedule[0]["weights"]) == 6
    assert schedule[5]["weights"] == {}
    assert len(schedule[5]["forced_age_exits"]) == 6
    assert len(schedule[6]["weights"]) == 6
    frames = {s: pd.DataFrame({"open": 100., "close": 100., "adv20": 1e9}, index=dates) for s in "ABCDEF"}
    result = simulate(frames, schedule, 5, .02)
    assert audit(result, frames, 5, .02, 100000)["max_held_sessions"] == 5
    assert result["nav"][6]["gross_exposure"] == 0

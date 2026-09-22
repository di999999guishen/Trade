import pytest

from quant_research.option_payoffs import VanillaLeg, expiry_pnl


def leg(kind, strike, contracts, multiplier=100):
    return VanillaLeg(kind, strike, contracts, multiplier, multiplier, 1.9, 2.1, .5, "2026-10-16")


def test_collar_caps_and_floor_includes_premiums_and_fees():
    legs = [leg("put", 95, 1), leg("call", 105, -1)]
    low = expiry_pnl(100, 60, 100, legs, "collar")
    high = expiry_pnl(100, 150, 100, legs, "collar")
    assert low["net_pnl"] == pytest.approx(-521)
    assert high["net_pnl"] == pytest.approx(479)
    assert low["net_premium_paid"] == pytest.approx(20)


def test_put_spread_protection_stops_below_lower_strike():
    legs = [leg("put", 95, 1), leg("put", 85, -1)]
    first = expiry_pnl(100, 60, 100, legs, "put_spread")
    second = expiry_pnl(100, 50, 100, legs, "put_spread")
    assert first["option_intrinsic"] == second["option_intrinsic"] == 1000
    assert second["net_pnl"]-first["net_pnl"] == -1000


def test_covered_call_explicit_multiplier_and_coverage():
    legs = [leg("call", 105, -1, 10)]
    assert expiry_pnl(100, 150, 10, legs, "covered_call")["net_pnl"] == pytest.approx(68.5)
    with pytest.raises(ValueError, match="uncovered"):
        expiry_pnl(100, 100, 9, legs, "covered_call")
    with pytest.raises(ValueError, match="deliverable"):
        VanillaLeg("put", 95, 1, 100, 50, 1, 2, .5, "2026-10-16")


def test_reject_mismatched_spread_and_bad_quotes():
    with pytest.raises(ValueError, match="matched"):
        expiry_pnl(100, 100, 200, [leg("put", 95, 1), leg("put", 85, -2)], "put_spread")
    with pytest.raises(ValueError, match="invalid"):
        VanillaLeg("call", 105, -1, 100, 100, 3, 2, .5, "2026-10-16")

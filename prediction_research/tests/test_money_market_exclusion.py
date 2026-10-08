from datetime import date, timedelta

import pytest

from prediction_research.data import Bar
from prediction_research.screening import (atr14_pct, money_market_exclusion,
                                           select_etfs)
from prediction_research.tests.test_cycle_remediation import flow_row
from prediction_research.tests.test_evidence import cfg


def flat_bars(days=20, close=100.0, daily_range=0.001, start=date(2024, 1, 1)):
    """A par-priced money-market series: price moves by hundredths of a percent."""
    return tuple(Bar(trading_date=start + timedelta(days=index), open=close, high=close + daily_range,
                     low=close - daily_range, close=close, volume=1_000_000.0, amount=100_000_000.0)
                 for index in range(days))


def volatile_bars(days=20, close=1.0, daily_range=0.02, start=date(2024, 1, 1)):
    return tuple(Bar(trading_date=start + timedelta(days=index), open=close, high=close + daily_range,
                     low=close - daily_range, close=close, volume=1_000_000.0, amount=100_000_000.0)
                 for index in range(days))


def lookup_for(mapping):
    return lambda symbol, as_of=None: mapping.get(symbol)


def test_flat_price_series_is_excluded_by_atr_even_outside_the_par_band(cfg):
    """511880 sits at 100.83 and 511360 at 113.96 -- both escape a pure price band."""
    rules = cfg["etf_market"]["screen"]
    record = money_market_exclusion({"symbol": "511880", "price": 100.829}, rules,
                                    lookup_for({"511880": 0.0115}))
    assert record is not None
    assert record["reason"] == "money_market_flat_price"
    assert record["basis"] == "atr14_pct"

    drifted = money_market_exclusion({"symbol": "511360", "price": 113.96}, rules,
                                     lookup_for({"511360": 0.0100}))
    assert drifted is not None
    assert drifted["basis"] == "atr14_pct"


def test_par_priced_etf_without_history_is_excluded_by_price_band(cfg):
    rules = cfg["etf_market"]["screen"]
    record = money_market_exclusion({"symbol": "511660", "price": 100.013}, rules, lookup_for({}))
    assert record is not None
    assert record["basis"] == "price_band_no_history"
    assert record["atr14_pct"] is None


def test_normal_volatility_and_unknown_history_are_retained(cfg):
    rules = cfg["etf_market"]["screen"]
    # Known normal volatility: never excluded just for being cheap or quiet.
    assert money_market_exclusion({"symbol": "510210", "price": 1.001}, rules,
                                  lookup_for({"510210": 0.892})) is None
    # Unknown volatility away from the par peg must stay in the universe.
    assert money_market_exclusion({"symbol": "159981", "price": 1.749}, rules, lookup_for({})) is None


def test_rules_are_configurable_and_independently_sufficient(cfg):
    rules = cfg["etf_market"]["screen"]
    atr = lookup_for({"511880": 0.0115})
    assert money_market_exclusion({"symbol": "511880", "price": 100.829}, rules, atr) is not None
    assert money_market_exclusion({"symbol": "511880", "price": 100.829},
                                  {**rules, "exclude_money_market_etfs": False}, atr) is None
    # Loosen the ATR threshold and this instrument falls through both tests:
    # it is 0.83 away from par, outside the default band.
    assert money_market_exclusion({"symbol": "511880", "price": 100.829},
                                  {**rules, "money_market_max_atr_pct": 0.001}, atr) is None
    # Widening the band catches it instead -- the two tests are independent.
    widened = money_market_exclusion({"symbol": "511880", "price": 100.829},
                                     {**rules, "money_market_max_atr_pct": 0.001,
                                      "money_market_price_band": 1.0}, atr)
    assert widened is not None
    assert widened["basis"] == "price_band"


def test_atr14_pct_computes_true_range_and_respects_point_in_time_cutoff():
    # high - low = 0.002 over a flat 100.00 close -> 0.002% of price.
    assert atr14_pct(flat_bars()) == pytest.approx(0.002, rel=1e-6)
    # high - low = 0.04 over a 1.00 close -> 4% of price.
    assert atr14_pct(volatile_bars()) == pytest.approx(4.0, rel=1e-6)
    # Too little history as of the cutoff: unknown, never zero.
    assert atr14_pct(flat_bars(), as_of=date(2024, 1, 1) + timedelta(days=5)) is None
    assert atr14_pct(flat_bars(), as_of=date(2024, 1, 1) + timedelta(days=19)) is not None


def test_select_etfs_drops_and_records_money_market_candidates(cfg):
    rules = cfg["etf_market"]["screen"]
    rows = [flow_row("159981") | {"price": 1.749},
            flow_row("511990") | {"price": 100.004},
            flow_row("511880") | {"price": 100.829}]
    exclusions = []
    eligible, _ = select_etfs(rows, rules, atr_lookup=lookup_for({"159981": 2.84, "511990": 0.0059, "511880": 0.0115}),
                              exclusions=exclusions)
    assert [row["symbol"] for row in eligible] == ["159981"]
    assert sorted(item["symbol"] for item in exclusions) == ["511880", "511990"]
    assert all(item["reason"].startswith("money_market") for item in exclusions)


def test_select_etfs_without_lookup_still_drops_par_priced_rows(cfg):
    rules = cfg["etf_market"]["screen"]
    eligible, _ = select_etfs([flow_row("159981") | {"price": 1.749},
                               flow_row("511990") | {"price": 100.004}], rules)
    assert [row["symbol"] for row in eligible] == ["159981"]

import pandas as pd
import pytest

from quant_research.sina_prepare import parse_adjustments, total_return_proxy


def bars():
    return pd.DataFrame({"Open": [100., 49., 50.], "High": [100., 49., 50.],
                         "Low": [100., 49., 50.], "Close": [100., 49., 50.],
                         "Volume": [1000., 2000., 2000.]},
                        index=pd.to_datetime(["2024-03-07", "2024-03-08", "2024-03-11"]))


def test_split_and_cash_proxy_hand_calculation_and_no_double_counting():
    # On second day, 2-for-1 split plus $2 distribution per OLD share.
    text = 'var SPY_qfq={"data":[{"d":"1900-01-01","f":"0.5","c":"-1"},' \
           '{"d":"2024-03-08","f":"1","c":"0"}]}; /* inert comment */'
    factors = parse_adjustments(text, "SPY")
    result, events = total_return_proxy(bars(), factors)
    assert events.iloc[1].split == 2
    assert events.iloc[1].dividend_old_share == 2
    assert result["Adj Close"].iloc[1] == pytest.approx(100.)
    assert result["Adj Close"].iloc[2] == pytest.approx(100 * 50 / 49)
    pd.testing.assert_frame_equal(result[list(bars().columns)], bars())


def test_scale_never_depends_on_same_day_or_future_close():
    factors = parse_adjustments('var SPY_qfq={"data":[{"d":"1900-01-01","f":"1","c":"-2"},'
                                '{"d":"2024-03-08","f":"1","c":"0"}]}', "SPY")
    raw = bars()
    left, _ = total_return_proxy(raw, factors)
    raw.loc["2024-03-08":, "Close"] *= 1.1
    right, _ = total_return_proxy(raw, factors)
    assert left.loc["2024-03-08", "Adj Close"] / left.loc["2024-03-08", "Close"] == pytest.approx(
        right.loc["2024-03-08", "Adj Close"] / right.loc["2024-03-08", "Close"])
    assert left.iloc[0]["Adj Close"] == right.iloc[0]["Adj Close"]


def test_future_vendor_affine_rebase_does_not_change_past_returns():
    before = parse_adjustments('var SPY_qfq={"data":[{"d":"1900-01-01","f":"1","c":"-2"},'
                               '{"d":"2024-03-08","f":"1","c":"0"}]}', "SPY")
    # A later 2:1 split + dividend rebases all older f,c but cannot change historical actions.
    after = parse_adjustments('var SPY_qfq={"data":[{"d":"1900-01-01","f":"0.5","c":"-4"},'
                              '{"d":"2024-03-08","f":"0.5","c":"-3"},'
                              '{"d":"2025-01-02","f":"1","c":"0"}]}', "SPY")
    left, _ = total_return_proxy(bars(), before)
    right, _ = total_return_proxy(bars(), after)
    pd.testing.assert_frame_equal(left, right)


def test_wrong_symbol_executable_suffix_and_missing_event_day_rejected():
    text = 'var QQQ_qfq={"data":[{"d":"1900-01-01","f":"1","c":"0"}]}'
    with pytest.raises(ValueError, match="symbol"):
        parse_adjustments(text, "SPY")
    with pytest.raises(ValueError, match="suffix"):
        parse_adjustments(text + '; alert(1)', "QQQ")
    factors = parse_adjustments('var SPY_qfq={"data":[{"d":"1900-01-01","f":"1","c":"-1"},'
                                '{"d":"2024-03-09","f":"1","c":"0"}]}', "SPY")
    with pytest.raises(ValueError, match="missing/non-trading"):
        total_return_proxy(bars(), factors)

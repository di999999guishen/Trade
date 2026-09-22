import numpy as np
import pandas as pd
import pytest

from quant_research.calendar import calendar
from quant_research.history_extension import FIELDS, merge_registered, parse_tencent


def bars():
    dates = calendar().sessions_in_range("2018-10-01", "2018-12-14")
    return pd.DataFrame({"Open": 10., "High": 11., "Low": 9., "Close": 10., "Volume": 1000.}, index=dates)


def test_registered_repair_preserves_every_other_bar_and_lineage():
    original = bars()
    original.loc["2018-11-15", "Low"] = 10.5
    alternate = bars()
    alternate.loc["2018-10-02", "Open"] = 10.01
    merged, report = merge_registered(original, alternate, "XLB", original.index)
    unchanged = original.index.difference(pd.DatetimeIndex(["2018-11-15"]))
    pd.testing.assert_frame_equal(merged.loc[unchanged], original.loc[unchanged])
    assert report["patches"][0]["before"]["Low"] == 10.5
    assert report["patches"][0]["after"]["Low"] == 9.
    assert merged.loc["2018-10-02", "Open"] == 10.


def test_no_silent_new_repairs_or_missing_quotes():
    original = bars()
    original.loc["2018-11-14", "Low"] = 10.5
    with pytest.raises(ValueError, match="predeclared"):
        merge_registered(original, bars(), "XLB", original.index)
    original = bars().drop(pd.Timestamp("2018-11-14"))
    with pytest.raises(ValueError, match="missing independently"):
        merge_registered(original, None, "IEF", bars().index)
    merged, report = merge_registered(original, bars(), "IEF", bars().index)
    assert len(merged) == len(bars()) and report["patches"][0]["reason"] == "missing_history"


def test_tencent_rejects_adjusted_wrong_symbol_and_invalid_quotes():
    node = {"day": [["2018-10-01", "10", "10.1", "11", "9", "1000"]]}
    payload = {"code": 0, "data": {"usIEF.OQ": node}}
    frame = parse_tencent(payload, "IEF", "OQ")
    assert list(frame) == FIELDS and frame.Close.iloc[0] == 10.1
    with pytest.raises(ValueError, match="wrong Tencent"):
        parse_tencent(payload, "TLT", "OQ")
    node["qfqday"] = node["day"]
    with pytest.raises(ValueError, match="unadjusted"):
        parse_tencent(payload, "IEF", "OQ")
    del node["qfqday"]
    node["day"][0][1] = np.inf
    with pytest.raises(ValueError, match="invalid Tencent"):
        parse_tencent(payload, "IEF", "OQ")

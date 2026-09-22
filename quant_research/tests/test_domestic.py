from datetime import date

import pytest

from quant_research.data import DataRequest
from quant_research.domestic import parse_bars


def test_sina_exclusive_dates_and_no_invented_adjusted_close():
    rows = [{"d": d, "o": "10", "h": "12", "l": "9", "c": "11", "v": "100"}
            for d in ("2024-03-07", "2024-03-08", "2024-03-11")]
    request = DataRequest(("SPY",), date(2024, 3, 8), date(2024, 3, 11))
    frame = parse_bars(rows, request)
    assert len(frame) == 1
    assert frame.index[0].date() == date(2024, 3, 8)
    assert "Adj Close" not in frame
    with pytest.raises(ValueError, match="duplicate"):
        parse_bars(rows + [rows[-1]], request)
    rows[1]["h"] = "8"
    with pytest.raises(ValueError, match="OHLC"):
        parse_bars(rows, request)


@pytest.mark.parametrize("payload", [None, [], {}, [{"d": "2024-03-08"}]])
def test_sina_rejects_empty_or_incomplete_responses(payload):
    with pytest.raises(ValueError):
        parse_bars(payload, DataRequest(("SPY",), date(2024, 3, 7), date(2024, 3, 12)))

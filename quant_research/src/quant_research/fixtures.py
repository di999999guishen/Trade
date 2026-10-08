"""Synthetic fixtures are labelled as such and never mix with vendor history."""
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .calendar import calendar
from .data import DataRequest, snapshot


class FixtureProvider:
    name = "synthetic_fixture_not_market_data"

    def fetch(self, symbol, request):
        dates = calendar().sessions_in_range(request.start, request.end_exclusive - timedelta(days=1))
        i = np.arange(len(dates))
        close = 100 + i * (0.1 if symbol == "SPY" else 0.2)
        frame = pd.DataFrame({"Open": close - 0.05, "High": close + 0.1,
                              "Low": close - 0.1, "Close": close, "Adj Close": close,
                              "Volume": 1000000, "Dividends": 0.0, "Stock Splits": 0.0}, index=dates)
        return frame, {"price_basis": "raw_verified", "volume_basis": "raw_verified",
                       "inception_verified": True, "inception": "1990-01-01",
                       "verification_source": "synthetic_by_construction", "raw_execution_ready": False}


def create_fixture(root):
    request = DataRequest(("SPY", "QQQ"), date(2022, 1, 3), date(2024, 1, 1))
    path = snapshot(request, FixtureProvider(), root, attempts=1, pause=0)
    return {"status": "synthetic_fixture_only", "snapshot": str(path)}

import numpy as np
import pandas as pd
import pytest

from quant_research.stock_portfolios import event_portfolio, quality_portfolio


def test_quality_sector_caps_and_missing_sector_infeasible():
    ranked = pd.DataFrame({"security_id": [f"S{i:03}" for i in range(100)],
                           "industry": ["A"]*70+["B"]*30, "score": np.arange(100)})
    result = quality_portfolio(ranked, {"A": .7, "B": .3})
    assert result["status"] == "targets_only_not_orders"
    assert max(result["weights"].values()) <= .02
    assert sum(result["weights"].values()) == pytest.approx(1)
    assert quality_portfolio(ranked, {"A": .4, "B": .3, "C": .3})["status"] == "infeasible_cash"


def test_event_exit_priority_and_no_same_open_reentry():
    candidates = pd.DataFrame([{"security_id": f"S{i}", "industry": "A", "sue": 20-i,
                                "percentile": .9, "available_at": "2024-01-02T21:00Z", "eligible": True}
                               for i in range(20)])
    active = [{"security_id": "S0", "industry": "A", "weight": .02,
               "scheduled_exit_at": "2024-02-02T14:30Z"}]
    result = event_portfolio(active, candidates, "2024-01-02T23:00Z", "2024-01-03T14:30Z")
    assert result["exits"] == ["S0"]
    assert "S0" not in result["weights"]
    assert sum(result["weights"].values()) == pytest.approx(.25)
    assert max(result["weights"].values()) <= .02
    assert "S19" not in result["weights"]
    candidates.loc[0, "available_at"] = "2024-01-03T00:00Z"
    with pytest.raises(ValueError, match="future"):
        event_portfolio(active, candidates, "2024-01-02T23:00Z", "2024-01-03T14:30Z")

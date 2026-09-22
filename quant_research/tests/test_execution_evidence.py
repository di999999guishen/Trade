from dataclasses import replace

import pytest

from quant_research.execution_evidence import (
    BorrowEvidence,
    DividendObligations,
    assignment_effect,
    validate_option_entry,
)
from quant_research.option_payoffs import VanillaLeg


def test_borrow_limits_expiry_and_recall():
    evidence = BorrowEvidence("SPY", 50, .03, "2024-01-02T14:29Z", "2024-01-02T14:29:01Z",
                              "2024-01-02T14:31Z", False, "fixture_locate")
    assert evidence.check("2024-01-02T14:30Z", 100)["approved_shares"] == 50
    assert replace(evidence, recalled=True).check("2024-01-02T14:30Z", 100)["force_cover"]
    with pytest.raises(ValueError, match="stale"):
        evidence.check("2024-01-02T14:32Z", 1)


def test_dividend_liability_survives_cover_until_pay_date():
    ledger = DividendObligations()
    ledger.accrue("SPY:ex", "2024-01-02", "2024-01-10", -10, 2)
    assert ledger.outstanding("2024-01-01") == 0
    assert ledger.outstanding("2024-01-03") == -20  # even if shares covered on Jan 3
    assert ledger.settle("2024-01-09") == 0
    assert ledger.settle("2024-01-10") == -20
    assert ledger.outstanding("2024-01-10") == 0
    assert ledger.settle("2024-01-10") == 0
    with pytest.raises(ValueError, match="duplicate"):
        ledger.accrue("SPY:ex", "2024-01-02", "2024-01-10", 10, 2)


def option_inputs():
    leg = VanillaLeg("call", 105, -1, 100, 100, 2, 2.1, .65, "2024-02-16")
    quote = {"contract_id": "fixture_call", "kind": "call", "strike": 105, "expiry": leg.expiry,
             "multiplier": 100, "deliverable_shares": 100, "quote_at": "2024-01-02T14:45:00Z",
             "available_at": "2024-01-02T14:45:01Z", "bid": 2, "ask": 2.1, "delta": .25}
    return leg, quote


def test_option_quote_gate_and_assignment_accounting():
    leg, quote = option_inputs()
    result = validate_option_entry([leg], [quote], "2024-01-02T14:45:02Z", 100000, 90000, 100, 100,
                                   variant="covered_call")
    assert result["premium"] == -200
    assert result["cash_after_if_filled"] == pytest.approx(90199.35)
    effect = assignment_effect(leg, 1)
    assert effect == {"share_change": -100, "strike_cash_change": 10500, "remaining_contracts": 0}
    with pytest.raises(ValueError, match="stale"):
        validate_option_entry([leg], [quote], "2024-01-02T14:46:02Z", 100000, 90000, 100, 100,
                              variant="covered_call")
    with pytest.raises(ValueError, match="identity"):
        validate_option_entry([leg], [dict(quote, strike=110)], "2024-01-02T14:45:02Z", 100000, 90000, 100, 100,
                              variant="covered_call")


def test_option_put_spread_budget_cash_and_leg_synchronization():
    upper = VanillaLeg("put", 95, 1, 100, 100, 8, 8.1, .65, "2024-02-16")
    lower = VanillaLeg("put", 85, -1, 100, 100, 2, 2.1, .65, "2024-02-16")
    _, template = option_inputs()
    quotes = [dict(template, contract_id=str(i), kind="put", strike=leg.strike, bid=leg.bid, ask=leg.ask, delta=-.2)
              for i, leg in enumerate([upper, lower])]
    with pytest.raises(ValueError, match="premium budget"):
        validate_option_entry([upper, lower], quotes, "2024-01-02T14:45:02Z", 100000, 90000, 100, 100,
                              variant="put_spread")
    quotes[1]["quote_at"] = "2024-01-02T14:44:50Z"
    with pytest.raises(ValueError, match="synchronized"):
        validate_option_entry([upper, lower], quotes, "2024-01-02T14:45:02Z", 100000, 90000, 100, 100,
                              variant="put_spread")

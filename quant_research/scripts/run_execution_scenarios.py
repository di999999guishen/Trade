"""Explicitly invented S4-S7 engineering scenarios; never historical performance."""
from pathlib import Path
from uuid import uuid4

import pandas as pd

from quant_research.artifacts import file_hash, provenance, publication, write_json
from quant_research.execution_evidence import (
    BorrowEvidence,
    DividendObligations,
    assignment_effect,
    validate_option_entry,
)
from quant_research.option_payoffs import VanillaLeg
from quant_research.stock_portfolios import event_portfolio, quality_portfolio


def main():
    project = Path(__file__).resolve().parents[1]
    identifier = "execution_scenarios_"+uuid4().hex
    with publication(project / "outputs", identifier) as stage:
        write_json(stage / "contract.json", {"input_kind": "invented_engineering_fixtures",
                   "historical_return": None, "source": provenance(project), "script_hash": file_hash(__file__)})
        ranked = pd.DataFrame({"security_id": [f"fixture_{i:03}" for i in range(100)],
                              "industry": ["A"]*70+["B"]*30, "score": list(range(100))})
        s4 = {"feasible": quality_portfolio(ranked, {"A": .7, "B": .3}),
              "missing_sector": quality_portfolio(ranked, {"A": .4, "B": .3, "C": .3})}
        write_json(stage / "s4_sector_targets.json", s4)
        events = pd.DataFrame([{"security_id": f"fixture_{i}", "industry": "A", "sue": 20-i,
                               "percentile": .9, "available_at": "2024-01-02T21:00Z", "eligible": True}
                              for i in range(20)])
        active = [{"security_id": "fixture_0", "industry": "A", "weight": .02,
                   "scheduled_exit_at": "2024-02-02T14:30Z"}]
        s5 = event_portfolio(active, events, "2024-01-02T23:00Z", "2024-01-03T14:30Z")
        write_json(stage / "s5_event_targets.json", s5)
        borrow = BorrowEvidence("fixture_etf", 50, .03, "2024-01-02T14:29Z", "2024-01-02T14:29:01Z",
                                "2024-01-02T14:31Z", False, "invented_locate")
        dividends = DividendObligations()
        dividends.accrue("fixture_dividend", "2024-01-02", "2024-01-10", -10, 2)
        liability = dividends.outstanding("2024-01-03")
        payment = dividends.settle("2024-01-10")
        repeated = dividends.settle("2024-01-10")
        write_json(stage / "s6_borrow_and_dividend.json", {"locate": borrow.check("2024-01-02T14:30Z", 100),
                   "dividend_liability_after_cover": liability, "payment_cash_change": payment,
                   "repeated_payment_cash_change": repeated, "integrated_historical_short_backtest": False})
        leg = VanillaLeg("call", 105, -1, 100, 100, 2, 2.1, .65, "2024-02-16")
        quote = {"contract_id": "fixture_call", "kind": "call", "strike": 105, "expiry": leg.expiry,
                 "multiplier": 100, "deliverable_shares": 100, "quote_at": "2024-01-02T14:45:00Z",
                 "available_at": "2024-01-02T14:45:01Z", "bid": 2, "ask": 2.1, "delta": .25}
        entry = validate_option_entry([leg], [quote], "2024-01-02T14:45:02Z", 100000, 90000, 100, 100,
                                      variant="covered_call")
        assignment = assignment_effect(leg, 1)
        cash = entry["cash_after_if_filled"]
        before_nav = cash+100*120-100*(120-105)
        after_nav = cash+assignment["strike_cash_change"]+(100+assignment["share_change"])*120
        if abs(before_nav-after_nav) > 1e-8:
            raise AssertionError("assignment accounting mismatch")
        write_json(stage / "s7_entry_and_assignment.json", {"entry_gate": entry, "assignment": assignment,
                   "invented_assignment_spot": 120, "before_nav": before_nav, "after_nav": after_nav,
                   "conservation_error": abs(before_nav-after_nav), "assignment_was_supplied_not_predicted": True})
        write_json(stage / "manifest.json", {"files": {p.name: file_hash(p) for p in sorted(stage.iterdir())}})
    print(project / "outputs" / identifier)


if __name__ == "__main__":
    main()

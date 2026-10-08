from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from quant_research.fundamental_signals import (
    EarningsEvent,
    asof_versions,
    event_execution_times,
    event_percentile,
    event_signal,
    quality_scores,
)
from quant_research.sec_evidence import extract_facts


def event(q=8, year=2024):
    return EarningsEvent(str(q), "CIK1", q, 1+q*.1, 1, "USD/share quarterly GAAP diluted", "split_basis_1",
                         f"{year}-01-05T21:00:00Z", f"{year}-01-05T21:01:00Z", f"{year}-01-05T21:02:00Z",
                         f"{year}-01-05T19:00:00Z", f"{year}-01-05T19:01:00Z", f"revision{q}")


def test_pit_revisions_do_not_rewrite_past():
    base = {"security_id": "A", "metric": "Assets", "period_start": None, "period_end": "2023-12-31",
            "unit": "USD", "value": 100, "published_at": "2024-02-01T10:00Z",
            "available_at": "2024-02-01T10:01Z", "revision_id": "v1"}
    revised = dict(base, value=90, published_at="2024-03-01T10:00Z", available_at="2024-03-01T10:01Z", revision_id="v2")
    rows = pd.DataFrame([base, revised])
    assert asof_versions(rows, "2024-02-10T18:00Z").value.tolist() == [100]
    assert asof_versions(rows, "2024-03-10T18:00Z").value.tolist() == [90]
    with pytest.raises(ValueError, match="timezone"):
        asof_versions(rows, "2024-02-10")


def test_quality_excludes_missing_financials_and_future_data():
    n = 45
    panel = pd.DataFrame({"security_id": [f"A{i:02}" for i in range(n)], "industry": ["tech"]*25+["other"]*20,
                          "is_financial": False, "gross_profit_ttm": np.arange(n, dtype=float), "cash_flow_ttm": 10.,
                          "net_income_ttm": 12., "assets_start": 100., "assets_end": 110., "total_debt": 20.,
                          "momentum": np.arange(n)/100, "market_cap": 1e9, "price": 100., "adv20": 3e7,
                          "observations": 300, "financial_available_at": "2024-01-01T12:00Z",
                          "universe_available_at": "2024-01-01T12:00Z", "price_available_at": "2024-01-01T12:00Z"})
    panel.loc[0, "gross_profit_ttm"] = np.nan
    panel.loc[25, "is_financial"] = True
    ranked = quality_scores(panel, "2024-01-02T12:00Z")
    assert "A00" not in set(ranked.security_id) and "A25" not in set(ranked.security_id)
    assert ranked.loc[ranked.industry == "other", "global_standardization_fallback"].all()
    assert np.isfinite(ranked.score).all()
    panel.loc[1, "financial_available_at"] = "2025-01-01T12:00Z"
    with pytest.raises(ValueError, match="future"):
        quality_scores(panel, "2024-01-02T12:00Z")


def test_pead_requires_pre_event_consensus_and_six_quarters():
    now = event()
    history = [event(q, 2020+q//4) for q in range(8)]
    actual = event_signal(now, history, "2024-01-06T00:00Z")
    assert actual["sue"] == pytest.approx(.8/np.std(np.arange(8)*.1, ddof=1))
    with pytest.raises(ValueError, match="60 minutes"):
        event_signal(replace(now, estimate_available_at="2024-01-05T20:30Z"), history, "2024-01-06T00:00Z")
    with pytest.raises(ValueError, match="six"):
        event_signal(now, history[:5], "2024-01-06T00:00Z")
    with pytest.raises(ValueError, match="basis mismatch"):
        event_signal(now, [replace(history[0], share_basis_id="unadjusted"), *history[1:]], "2024-01-06T00:00Z")


def test_late_event_waits_until_next_decision_then_next_open():
    early = event_execution_times(event())  # Friday 16:02 ET parsed, Monday entry
    late = event_execution_times(replace(event(), parsed_at="2024-01-05T23:01:00Z"))
    assert pd.Timestamp(early["entry_at"]).date().isoformat() == "2024-01-08"
    assert pd.Timestamp(late["entry_at"]).date().isoformat() == "2024-01-09"
    assert pd.Timestamp(late["scheduled_exit_at"]) > pd.Timestamp(late["entry_at"])


def test_event_percentile_excludes_future_and_wrong_basis():
    rows = pd.DataFrame([
        {"event_id": "prior", "published_at": "2023-11-01T21:00Z", "available_at": "2023-11-01T21:01Z",
         "sue": 1., "eps_basis": event().eps_basis},
        {"event_id": "future", "published_at": "2024-02-01T21:00Z", "available_at": "2024-02-01T21:01Z",
         "sue": 100., "eps_basis": event().eps_basis},
        {"event_id": "wrong", "published_at": "2023-11-01T21:00Z", "available_at": "2023-11-01T21:01Z",
         "sue": 100., "eps_basis": "non-GAAP"}])
    result = event_percentile(event(), 2., rows)
    assert result == {"percentile": 1., "reference_events": 1, "positive_top_quintile": True}


def test_sec_preserves_versions_and_does_not_invent_availability():
    facts = {"cik": 1, "facts": {"us-gaap": {"Assets": {"units": {"USD": [
        {"accn": "a", "end": "2023-12-31", "val": 100, "filed": "2024-01-01", "form": "10-K"},
        {"accn": "b", "end": "2023-12-31", "val": 90, "filed": "2024-02-01", "form": "10-K/A"}]}}}}}
    submissions = {"cik": 1, "filings": {"recent": {"accessionNumber": ["b"], "acceptanceDateTime": ["2024-02-01T20:00Z"]}}}
    result = extract_facts(facts, submissions, 1)
    assert result.value.tolist() == [100, 90]
    assert result.available_at.isna().all()
    assert result.acceptance_timestamp_as_returned.notna().sum() == 1
    with pytest.raises(ValueError, match="identity"):
        extract_facts(facts, submissions, 2)

"""Offline regression coverage for compulsory daily ETF market evidence."""

import copy
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import requests

from tradingagents.dataflows import daily_prediction_markets as daily

pytestmark = pytest.mark.unit
NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)


def market(question="Will SPY close above $700?", **updates):
    value = {
        "id": "123", "question": question,
        "endDate": "2026-09-28T20:00:00Z", "closed": False, "active": True,
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.61", "0.39"]',
        "volumeNum": 2000, "volume24hr": 150, "liquidityNum": 400,
        "updatedAt": "2026-09-26T11:00:00Z", "spread": 0.02,
        "description": "Resolves using the specified closing price.",
        "resolutionSource": "Official closing price",
    }
    value.update(updates)
    return value


def response(*markets, title="SPY price", **event_updates):
    event = {"title": title, "slug": "spy-close-september-28", "markets": list(markets)}
    event.update(event_updates)
    return {"events": [event]}


def get(ticker="SPY", data=None, **kwargs):
    with patch.object(daily, "_request", return_value=data if data is not None else response(market())):
        return daily.get_daily_prediction_market_snapshot(ticker, now=NOW, **kwargs)


def test_public_search_parameters_and_required_shape():
    with patch.object(daily, "_request", return_value=response(market())) as request:
        result = daily.get_daily_prediction_market_snapshot(" spy ", now=NOW)
    assert result["ticker"] == "SPY"
    assert result["status"] == "available"
    assert result["schema_version"] == 1
    assert result["fetched_at"] == NOW.isoformat()
    assert len(request.call_args_list) == 2
    for call in request.call_args_list:
        assert call.args[0] == "public-search"
        assert call.args[1]["keep_closed_markets"] == 0
        assert call.args[1]["events_status"] == "active"
        assert call.args[1]["cache"] is False
    assert [entry["query"] for entry in result["queries"]] == ["SPY", "S&P 500"]


@pytest.mark.parametrize("ticker,question,title", [
    ("SPY", "Will S&P 500 close above 6500?", "S&P 500"),
    ("SPY", "Will SPY go up or down on Monday?", "SPY"),
    ("QQQ", "Will QQQ close higher?", "QQQ"),
    ("QQQ", "Will Nasdaq-100 hit 24000?", "Nasdaq"),
    ("QQQ", "Will Nasdaq close above 23000?", "Nasdaq"),
    ("GLD", "Will gold hit $4000 this year?", "Gold price"),
    ("GLD", "Gold (GC) Up or Down on Monday?", "Gold futures"),
    ("GLD", "Will GLD close above $350?", "Gold ETF"),
])
def test_recognizes_asset_specific_event_contracts(ticker, question, title):
    result = get(ticker, response(market(question), title=title))
    assert result["status"] == "available"
    assert result["markets"][0]["outcomes"] == [
        {"label": "Yes", "probability": 0.61}, {"label": "No", "probability": 0.39},
    ]


@pytest.mark.parametrize("ticker,question,title", [
    ("SPY", "Will a spy balloon go up?", "Spy balloon price"),
    ("SPY", "Will Bitcoin close above 100000?", "SPY price"),
    ("QQQ", "Will a new company be listed on Nasdaq?", "Nasdaq"),
    ("QQQ", "Will Nasdaq NDAQ stock close higher?", "Nasdaq"),
    ("GLD", "Will France win a gold medal?", "Gold price"),
    ("GLD", "Will Gold Cup ticket prices increase?", "Gold"),
    ("GLD", "Will Barrick Gold price hit 30?", "Gold price"),
    ("GLD", "Will silver price hit 50?", "Gold and silver prices"),
    ("GLD", "Above $50 for silver?", "Gold and silver prices"),
])
def test_rejects_search_false_positives(ticker, question, title):
    assert get(ticker, response(market(question), title=title))["status"] == "no_matches"


def test_threshold_only_question_can_inherit_relevant_event_title():
    result = get("GLD", response(market("Above $4000?"), title="Gold price this month"))
    assert result["status"] == "available"
    assert "gold commodity" in result["markets"][0]["asset_reference"]


@pytest.mark.parametrize("updates", [
    {"closed": True}, {"closed": "true"}, {"archived": True},
    {"active": False}, {"acceptingOrders": False},
    {"endDate": "2026-09-26T11:59:59Z"},
    {"endDate": "2026-09-26T12:00:00Z"},
    {"endDate": "2026-09-28"}, {"endDate": "not-a-date"},
    {"endDate": None}, {"endDate": 123},
    {"outcomes": '"not a list"'}, {"outcomes": '["Yes"]'},
    {"outcomes": '["Yes", null]'}, {"outcomePrices": '["0.5"]'},
    {"outcomePrices": '["NaN", "0.4"]'},
    {"outcomePrices": '["Infinity", "0.4"]'},
    {"outcomePrices": '["1.1", "-0.1"]'},
    {"outcomePrices": '[true, false]'},
    {"outcomePrices": '["0", "0"]'},
])
def test_rejects_expired_unverifiable_and_invalid_markets(updates):
    assert get(data=response(market(**updates)))["markets"] == []


def test_closed_event_is_rejected_even_with_open_child():
    assert get(data=response(market(), closed=True))["status"] == "no_matches"


def test_all_labeled_outcomes_preserved_instead_of_assuming_first_is_yes():
    result = get(data=response(market(outcomes=["Down", "Up"], outcomePrices=[0.2, 0.8])))
    assert result["markets"][0]["outcomes"][0] == {"label": "Down", "probability": 0.2}
    rendered = daily.render_daily_prediction_market_report(result)
    assert "Down 20.00%" in rendered
    assert "Up 80.00%" in rendered


def test_deduplicates_across_searches_and_prefers_near_term_then_volume():
    far = market("Will SPY be above $700 next year?", id="far", endDate="2027-12-31T00:00:00Z", volumeNum=900000)
    low = market("Will SPY close higher?", id="low", volumeNum=20)
    high = market(id="high", volumeNum=100)
    result = get(data=response(far, low, high), limit=2)
    assert [item["market_id"] for item in result["markets"]] == ["high", "low"]


def test_missing_ids_deduplicate_by_question_and_deadline():
    assert len(get(data=response(market(id="")))["markets"]) == 1


def test_null_events_is_clean_no_matches():
    assert get(data={"events": None})["status"] == "no_matches"


def test_empty_searches_are_not_service_failure():
    assert get(data={"events": []})["status"] == "no_matches"


def test_network_failure_never_raises_or_leaks_exception_credentials():
    with patch.object(daily, "_request", side_effect=requests.RequestException("https://user:secret@proxy")):
        result = daily.get_daily_prediction_market_snapshot("SPY", now=NOW)
    assert result["status"] == "unavailable"
    assert all(entry["status"] == "error" for entry in result["queries"])
    assert "secret" not in str(result)
    assert "不生成或猜测概率" in daily.render_daily_prediction_market_context(result)


def test_partial_failure_preserves_successful_evidence():
    with patch.object(daily, "_request", side_effect=[requests.Timeout(), response(market())]):
        result = daily.get_daily_prediction_market_snapshot("SPY", now=NOW)
    assert result["status"] == "partial"
    assert len(result["markets"]) == 1
    assert "检索失败: SPY" in daily.render_daily_prediction_market_report(result)


def test_partial_without_matches_does_not_claim_definitive_no_matches():
    with patch.object(daily, "_request", side_effect=[requests.Timeout(), {"events": []}]):
        result = daily.get_daily_prediction_market_snapshot("SPY", now=NOW)
    assert result["status"] == "partial"
    assert result["markets"] == []


@pytest.mark.parametrize("data", [[], "error", {"events": "bad"}])
def test_malformed_responses_are_failures(data):
    assert get(data=data)["status"] == "unavailable"


def test_unmapped_ticker_does_not_search():
    with patch.object(daily, "_request") as request:
        result = daily.get_daily_prediction_market_snapshot("AAPL", now=NOW)
    request.assert_not_called()
    assert result["status"] == "unavailable"


@pytest.mark.parametrize("ticker,question,expected_unit", [
    ("SPY", "Will S&P 500 hit 6500?", "index points; not USD per SPY share"),
    ("QQQ", "Will Nasdaq 100 hit 22000?", "index points; not USD per QQQ share"),
    ("GLD", "Will gold price hit $4000?", "not USD per GLD share"),
])
def test_units_horizons_and_source_explain_indirect_event_evidence(ticker, question, expected_unit):
    result = get(ticker, response(market(question), title=question))
    rendered = daily.render_daily_prediction_market_context(result)
    assert expected_unit in rendered
    assert "2026-09-28T20:00:00+00:00" in rendered
    assert "https://polymarket.com/event/spy-close-september-28" in rendered
    assert "不等于 ETF 明日上涨概率" in rendered
    assert "累计成交额不保证" in rendered


def test_snapshot_limit_and_context_budget_are_bounded():
    raw_markets = [market("Will SPY close above $700? " + "x" * 1000, id=str(i), description="z" * 6000) for i in range(30)]
    result = get(data=response(*raw_markets), limit=1000)
    assert len(result["markets"]) == daily.MAX_MARKETS
    for item in result["markets"]:
        assert len(item["rules_excerpt"]) == 2000
    assert len(daily.render_daily_prediction_market_context(result)) <= daily.MAX_CONTEXT_CHARS
    before = copy.deepcopy(result)
    daily.render_daily_prediction_market_report(result)
    assert result == before


def test_missing_volume_renders_unknown_and_nonfinite_volume_not_propagated():
    result = get(data=response(market(volumeNum="NaN")))
    assert result["markets"][0]["volume"] is None
    assert "累计成交额: 未提供" in daily.render_daily_prediction_market_report(result)

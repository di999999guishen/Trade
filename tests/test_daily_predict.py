"""Offline regression for mandatory daily instruments and injected evidence."""
import json
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import Mock

import pandas as pd
import pytest

import daily_predict as daily


def snapshot(ticker):
    return {
        "schema_version": 1, "ticker": ticker, "status": "no_matches",
        "fetched_at": "2026-09-26T03:00:00+00:00", "queries": [], "markets": [],
    }


@pytest.mark.parametrize("configured, expected", [
    (None, ["510050.SS", "515220.SS", "SPY", "QQQ", "GLD"]),
    ("", ["SPY", "QQQ", "GLD"]),
    ("spy, QQQ,spy, gld", ["SPY", "QQQ", "GLD"]),
    ("NVDA, 510050.ss,,", ["NVDA", "510050.SS", "SPY", "QQQ", "GLD"]),
])
def test_holdings_cannot_remove_mandatory_tickers(configured, expected):
    assert daily.build_daily_tickers(configured) == expected


def test_per_ticker_sources_and_evidence_do_not_leak():
    base = daily.build_config()
    base["screen_evidence_context"] = "existing context\n"
    original = deepcopy(base)
    spy = daily.build_ticker_config(base, "SPY", snapshot("SPY"))
    qqq = daily.build_ticker_config(base, "QQQ", snapshot("QQQ"))
    holding = daily.build_ticker_config(base, "510050.SS", None)
    assert spy["data_vendors"]["core_stock_apis"] == "akshare"
    assert spy["data_vendors"]["fundamental_data"] == "yfinance"
    assert spy["tool_vendors"]["get_news"] == "yfinance,eastmoney"
    assert holding["data_vendors"]["fundamental_data"] == "eastmoney"
    assert holding.get("tool_vendors", {}).get("get_news") != "yfinance,eastmoney"
    assert spy["screen_evidence_context"].startswith("existing context\n")
    assert "## SPY Polymarket" in spy["screen_evidence_context"]
    assert "## QQQ Polymarket" in qqq["screen_evidence_context"]
    assert "## SPY Polymarket" not in qqq["screen_evidence_context"]
    assert base == original


def test_runner_attempts_every_ticker_and_freezes_evidence_once(tmp_path, monkeypatch):
    calls, captured = [], []
    loader = Mock(side_effect=snapshot)
    monkeypatch.setattr(daily.time, "sleep", lambda seconds: None)

    class Graph:
        def __init__(self, *, debug, config):
            self.config = config

        def propagate(self, ticker, analysis_date):
            calls.append(ticker)
            captured.append((ticker, deepcopy(self.config)))
            if ticker == "510050.SS":
                raise ValueError("holding failure")
            if ticker == "SPY" and calls.count(ticker) == 1:
                raise ConnectionError("temporary connection failure")
            return {"news_report": "offline result"}, "Hold"

    failed = daily.run_daily_predictions(
        ["510050.SS"], "2026-09-26", datetime(2026, 9, 26, 11), daily.build_config(),
        tmp_path, graph_factory=Graph, snapshot_loader=loader)
    assert failed == ["510050.SS"]
    assert calls == ["510050.SS", "SPY", "SPY", "QQQ", "GLD"]
    assert [call.args[0] for call in loader.call_args_list] == ["SPY", "QQQ", "GLD"]
    spy_configs = [cfg for ticker, cfg in captured if ticker == "SPY"]
    assert spy_configs[0]["screen_evidence_context"] == spy_configs[1]["screen_evidence_context"]
    for ticker in daily.MANDATORY_DAILY_TICKERS:
        evidence = list(tmp_path.glob(f"{ticker}_*_polymarket.json"))
        assert len(evidence) == 1
        assert json.loads(evidence[0].read_text(encoding="utf-8"))["ticker"] == ticker
        summaries = [p for p in tmp_path.glob(f"{ticker}_*.json") if p not in evidence]
        assert len(summaries) == 1
        payload = json.loads(summaries[0].read_text(encoding="utf-8"))
        assert payload["result"]["prediction_market_snapshot"]["ticker"] == ticker
        report = next(tmp_path.glob(f"{ticker}_*.md")).read_text(encoding="utf-8")
        assert "Polymarket" in report
        assert "no_matches" in report


def test_shared_vendor_registry_resets_news_for_holding_after_spy(monkeypatch):
    from tradingagents.dataflows import config as vendor_config

    monkeypatch.setattr(vendor_config, "_config", None)
    base = daily.build_config()
    vendor_config.set_config(daily.build_ticker_config(base, "SPY", snapshot("SPY")))
    assert vendor_config.get_config()["tool_vendors"]["get_news"] == "yfinance,eastmoney"
    vendor_config.set_config(daily.build_ticker_config(base, "510050.SS", None))
    actual = vendor_config.get_config()
    assert actual["tool_vendors"]["get_news"] == "eastmoney"
    assert actual["data_vendors"]["fundamental_data"] == "eastmoney"


def test_polymarket_failure_does_not_skip_required_analysis(tmp_path):
    calls = []

    class Graph:
        def __init__(self, **kwargs):
            pass

        def propagate(self, ticker, analysis_date):
            calls.append(ticker)
            return {}, "Hold"

    loader = Mock(side_effect=RuntimeError("bad response"))
    failed = daily.run_daily_predictions(
        [], "2026-09-26", datetime(2026, 9, 26, 11), daily.build_config(), tmp_path,
        graph_factory=Graph, snapshot_loader=loader)
    assert failed == []
    assert calls == list(daily.MANDATORY_DAILY_TICKERS)
    assert loader.call_count == 3
    for path in tmp_path.glob("*_polymarket.json"):
        assert json.loads(path.read_text(encoding="utf-8"))["status"] == "unavailable"


def test_invalid_holding_and_failed_spy_do_not_block_qqq_or_gold(tmp_path):
    calls = []

    class Graph:
        def __init__(self, **kwargs):
            pass

        def propagate(self, ticker, analysis_date):
            calls.append(ticker)
            if ticker == "SPY":
                raise ValueError("injected permanent error")
            return {}, "Hold"

    failed = daily.run_daily_predictions(
        ["../invalid"], "2026-09-26", datetime(2026, 9, 26, 11), daily.build_config(),
        tmp_path, graph_factory=Graph, snapshot_loader=snapshot)
    assert failed == ["../INVALID", "SPY"]
    assert calls == ["SPY", "QQQ", "GLD"]
    assert len(list(tmp_path.glob("SPY_*_polymarket.json"))) == 1
    assert len(list(tmp_path.glob("error_SPY_*.log"))) == 1


def test_graph_receives_daily_evidence_and_checkpoint_signature_changes():
    from tradingagents.graph.propagation import Propagator
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = object.__new__(TradingAgentsGraph)
    graph.config = daily.build_ticker_config(daily.build_config(), "SPY", snapshot("SPY"))
    graph.selected_analysts = ("market", "news")
    signature = graph._run_signature("stock")
    graph.debug = False
    graph._resuming = False
    graph.memory_log = Mock()
    graph.memory_log.get_past_context.return_value = ""
    graph.resolve_instrument_context = Mock(return_value="SPY is a US equity ETF.\n")
    graph.propagator = Propagator()
    graph.graph = Mock()
    graph.graph.invoke.return_value = {"final_trade_decision": "Hold"}
    graph._log_state = Mock()
    graph.clear_checkpoint_on_success = Mock()
    graph.process_signal = Mock(return_value="Hold")
    graph._run_graph("SPY", "2026-09-26")
    initial_state = graph.graph.invoke.call_args.args[0]
    assert "## SPY Polymarket" in initial_state["instrument_context"]
    assert "Polymarket" in initial_state["instrument_context"]
    changed = snapshot("SPY")
    changed["fetched_at"] = "2026-09-26T05:00:00+00:00"
    graph.config = daily.build_ticker_config(daily.build_config(), "SPY", changed)
    assert graph._run_signature("stock") != signature


def test_sina_us_cache_hit_never_calls_network(tmp_path, monkeypatch):
    from tradingagents.dataflows import akshare_data as vendor

    now = pd.Timestamp.today().normalize()
    start = (pd.Timestamp.today() - pd.DateOffset(years=5)).strftime("%Y-%m-%d")
    end = (pd.Timestamp.today() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    frame = pd.DataFrame({"Date": [now], "Open": [100], "High": [102],
                          "Low": [99], "Close": [101], "Volume": [1000]})
    frame.to_csv(tmp_path / f"SPY-sina-tencent-data-{start}-{end}.csv", index=False)
    monkeypatch.setattr(vendor, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    fetch = Mock(side_effect=AssertionError("cache hit must not fetch US prices"))
    monkeypatch.setattr(vendor, "_fetch_us_sina", fetch)
    result = vendor.load_ohlcv_akshare("SPY", now.strftime("%Y-%m-%d"))
    assert result["Close"].tolist() == [101]
    fetch.assert_not_called()


def test_dry_run_lists_all_instruments_without_analysis(monkeypatch, capsys):
    monkeypatch.setenv("DAILY_TICKER", "NVDA")
    monkeypatch.setattr(daily, "run_daily_predictions", Mock(side_effect=AssertionError("dry run")))
    assert daily.main(["--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tickers"] == ["NVDA", "SPY", "QQQ", "GLD"]


def test_live_market_payload_reaches_analysis_and_saved_report(tmp_path, monkeypatch):
    from tradingagents.dataflows import daily_prediction_markets as markets

    events = [{
        "title": f"{ticker} price", "slug": f"{ticker.lower()}-price",
        "markets": [{
            "id": ticker, "question": f"Will {ticker} close higher?",
            "endDate": "2026-09-28T20:00:00Z", "active": True, "closed": False,
            "outcomes": '["Up", "Down"]', "outcomePrices": '["0.63", "0.37"]',
            "volumeNum": 1200,
        }],
    } for ticker in daily.MANDATORY_DAILY_TICKERS]
    request = Mock(return_value={"events": events})
    monkeypatch.setattr(markets, "_request", request)
    contexts = {}

    class Graph:
        def __init__(self, *, debug, config):
            self.context = config["screen_evidence_context"]

        def propagate(self, ticker, analysis_date):
            contexts[ticker] = self.context
            return {"news_report": "offline market evidence"}, "Hold"

    def loader(ticker):
        return markets.get_daily_prediction_market_snapshot(
            ticker, now=datetime(2026, 9, 26, 12, tzinfo=timezone.utc))

    failed = daily.run_daily_predictions(
        [], "2026-09-26", datetime(2026, 9, 26, 20), daily.build_config(), tmp_path,
        graph_factory=Graph, snapshot_loader=loader)
    assert not failed
    assert request.call_count == 6
    for ticker in daily.MANDATORY_DAILY_TICKERS:
        assert f"Will {ticker} close higher?" in contexts[ticker]
        assert "Up 63.00%; Down 37.00%" in contexts[ticker]
        report = next(tmp_path.glob(f"{ticker}_*.md")).read_text(encoding="utf-8")
        assert "Up 63.00%; Down 37.00%" in report
        evidence = json.loads(next(tmp_path.glob(f"{ticker}_*_polymarket.json")).read_text(encoding="utf-8"))
        assert evidence["status"] == "available"
        assert [market["market_id"] for market in evidence["markets"]] == [ticker]

"""Attribution env vars are set per ticker and restored on every exit path."""

import os
from datetime import datetime

import daily_predict as daily

_ENV_NAMES = ("TRADINGAGENTS_RUN_ID", "TRADINGAGENTS_TICKER")


def _env_snapshot():
    return {name: os.environ.get(name) for name in _ENV_NAMES}


def _run(monkeypatch, tmp_path, tickers, *, fail=()):
    """Run the loop offline; return (failed, seen[env vars per ticker])."""
    monkeypatch.setattr(daily, "MANDATORY_DAILY_TICKERS", ())
    monkeypatch.setattr(daily, "new_run_id", lambda run_time: "RUN-X")
    monkeypatch.setattr(daily, "save_daily_result", lambda *args, **kwargs: ("r", "s"))

    seen = {}

    def fake_prediction(ticker, analysis_date, config, *, graph_factory=None):
        seen[ticker] = {
            "ticker": os.environ.get("TRADINGAGENTS_TICKER"),
            "run_id": os.environ.get("TRADINGAGENTS_RUN_ID"),
        }
        if ticker in fail:
            raise RuntimeError(f"injected failure: {ticker}")
        return {"news_report": "offline"}, "Hold"

    monkeypatch.setattr(daily, "run_prediction_with_retry", fake_prediction)

    failed = daily.run_daily_predictions(
        list(tickers), "2026-10-18", datetime(2026, 10, 18, 9), {}, tmp_path)
    return failed, seen


def test_each_ticker_runs_with_its_own_attribution_env(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_RUN_ID", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_TICKER", raising=False)

    failed, seen = _run(monkeypatch, tmp_path, ["AAA", "BBB"])

    assert failed == []
    assert seen == {
        "AAA": {"ticker": "AAA", "run_id": "RUN-X"},
        "BBB": {"ticker": "BBB", "run_id": "RUN-X"},
    }


def test_previous_env_values_are_restored_after_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_RUN_ID", "PRIOR-RUN")
    monkeypatch.setenv("TRADINGAGENTS_TICKER", "PRIOR-TICKER")
    before = _env_snapshot()

    failed, seen = _run(monkeypatch, tmp_path, ["AAA"])

    assert failed == []
    assert seen["AAA"] == {"ticker": "AAA", "run_id": "RUN-X"}
    assert _env_snapshot() == before


def test_unset_env_vars_stay_unset_after_the_run(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_RUN_ID", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_TICKER", raising=False)
    before = _env_snapshot()

    failed, _ = _run(monkeypatch, tmp_path, ["AAA"])

    assert failed == []
    assert _env_snapshot() == before == {
        "TRADINGAGENTS_RUN_ID": None,
        "TRADINGAGENTS_TICKER": None,
    }


def test_a_failing_ticker_does_not_leak_env_into_the_next(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_RUN_ID", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_TICKER", raising=False)
    before = _env_snapshot()

    failed, seen = _run(monkeypatch, tmp_path, ["AAA", "BBB"], fail={"AAA"})

    assert failed == ["AAA"]
    # The failing ticker saw its own values, and the next one saw its own.
    assert seen == {
        "AAA": {"ticker": "AAA", "run_id": "RUN-X"},
        "BBB": {"ticker": "BBB", "run_id": "RUN-X"},
    }
    assert _env_snapshot() == before


def test_invalid_ticker_continue_path_also_restores_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_RUN_ID", "PRIOR-RUN")
    monkeypatch.setenv("TRADINGAGENTS_TICKER", "PRIOR-TICKER")
    before = _env_snapshot()

    failed, seen = _run(monkeypatch, tmp_path, ["../invalid", "AAA"])

    assert failed == ["../INVALID"]
    assert seen == {"AAA": {"ticker": "AAA", "run_id": "RUN-X"}}
    assert _env_snapshot() == before

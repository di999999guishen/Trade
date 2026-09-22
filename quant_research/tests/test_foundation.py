from copy import deepcopy
from datetime import date

import pandas as pd
import pytest
from pydantic import ValidationError

from quant_research.artifacts import publication, run_id, write_json
from quant_research.calendar import calendar, decision_at, latest_completed, next_open
from quant_research.config import DEFAULTS, ResearchConfig, load_config
from quant_research.data import DataRequest, normalize, read_snapshot, snapshot
from quant_research.fixtures import FixtureProvider


@pytest.mark.parametrize("path,value", [
    (("portfolio", "single_asset_weight_max"), 1.1),
    (("portfolio", "initial_capital_usd"), -1),
    (("portfolio", "long_only"), False),
    (("market", "timezone"), "Asia/Shanghai"),
    (("data", "price_mode"), "raw_with_corporate_actions"),
    (("features", "broker_connection"), True),
    (("costs", "base_one_way_bps"), float("nan")),
    (("validation", "label_horizons_sessions"), [1]),
    (("universe", "symbols"), ["SPY", "SPY"]),
    (("market", "typo"), 1),
])
def test_invalid_configuration(path, value):
    config = deepcopy(DEFAULTS)
    config[path[0]][path[1]] = value
    with pytest.raises((ValidationError, ValueError)):
        ResearchConfig.model_validate(config)


def test_default_and_no_unknown_root():
    assert load_config().market.timezone == "America/New_York"
    with pytest.raises(ValidationError):
        ResearchConfig.model_validate({**DEFAULTS, "ignored": True})


def test_calendar_dst_holiday_half_day():
    assert next_open("2024-03-08").isoformat() == "2024-03-11T13:30:00+00:00"
    assert next_open("2024-03-28").date() == date(2024, 4, 1)
    assert calendar().session_close("2024-11-29").hour == 18
    assert decision_at("2024-03-08").tz_convert("UTC").hour == 23
    assert decision_at("2024-03-11").tz_convert("UTC").hour == 22
    assert latest_completed("2024-03-11T20:00:00Z").date() == date(2024, 3, 8)


def test_atomic_publication_and_identifiers(tmp_path):
    assert len({run_id() for _ in range(100)}) == 100
    with pytest.raises(RuntimeError), publication(tmp_path, "failed") as stage:
        write_json(stage / "partial.json", {"x": 1})
        raise RuntimeError("injected disk/work failure")
    assert not (tmp_path / "failed").exists()
    with publication(tmp_path, "ok") as stage:
        write_json(stage / "a.json", {})
    with pytest.raises(FileExistsError), publication(tmp_path, "ok"):
        pass


def test_snapshot_tampering_partial_failure(tmp_path):
    request = DataRequest(("SPY", "QQQ"), date(2024, 3, 7), date(2024, 3, 12))
    class Broken(FixtureProvider):
        def fetch(self, symbol, request):
            if symbol == "QQQ":
                raise TimeoutError("injected")
            return super().fetch(symbol, request)
    path = snapshot(request, Broken(), tmp_path, attempts=1, pause=0)
    assert read_snapshot(path, False)["status"] == "partial_failure"
    with pytest.raises(ValueError, match="partial"):
        read_snapshot(path)
    with (path / "SPY.parquet").open("ab") as handle:
        handle.write(b"corrupt")
    with pytest.raises(ValueError, match="integrity"):
        read_snapshot(path, False)


def test_future_data_does_not_change_eligible_history(tmp_path):
    request = DataRequest(("SPY", "QQQ"), date(2022, 1, 3), date(2024, 1, 1))
    config = deepcopy(DEFAULTS)
    config["universe"]["symbols"] = ["SPY", "QQQ"]
    config = ResearchConfig.model_validate(config)
    original = snapshot(request, FixtureProvider(), tmp_path, attempts=1, pause=0)
    class Perturbed(FixtureProvider):
        def fetch(self, symbol, request):
            frame, meta = super().fetch(symbol, request)
            frame.loc["2023-07-01":, ["Open", "High", "Low", "Close", "Adj Close"]] *= 2
            return frame, meta
    future = snapshot(request, Perturbed(), tmp_path, attempts=1, pause=0)
    left, _ = normalize(original, config)
    right, _ = normalize(future, config)
    pd.testing.assert_frame_equal(left["SPY"].loc[:"2023-06-30"], right["SPY"].loc[:"2023-06-30"])
    assert not left["SPY"].eligible.iloc[:252].any()
    assert left["SPY"].eligible.iloc[252]


def test_incremental_revisions_preserve_parent_and_exact_request_is_offline(tmp_path):
    from quant_research.artifacts import file_hash
    request = DataRequest(("SPY", "QQQ"), date(2024, 3, 7), date(2024, 3, 12))
    parent = snapshot(request, FixtureProvider(), tmp_path, attempts=1, pause=0)
    before = file_hash(parent / "SPY.parquet")
    class Revised(FixtureProvider):
        def fetch(self, symbol, request):
            frame, meta = super().fetch(symbol, request)
            frame.loc[frame.index[0], ["Open", "High", "Low", "Close", "Adj Close"]] *= 2
            return frame, meta
    future = DataRequest(request.symbols, request.start, date(2024, 3, 15))
    revised = snapshot(future, Revised(), tmp_path, attempts=1, pause=0, parent=parent)
    manifest = read_snapshot(revised)
    assert manifest["revision_report"]["SPY"]["changed_cells"] == 5
    assert manifest["revision_report"]["SPY"]["new_sessions"] == 3
    assert file_hash(parent / "SPY.parquet") == before
    class Offline(FixtureProvider):
        def fetch(self, symbol, request):
            raise AssertionError("network must not run")
    assert snapshot(future, Offline(), tmp_path, parent=revised) == revised


@pytest.mark.parametrize("successful_first", [False, True])
def test_rate_limit_stops_remaining_pool_and_preserves_success(tmp_path, successful_first):
    from yfinance.exceptions import YFRateLimitError
    calls = []
    class Limited(FixtureProvider):
        def fetch(self, symbol, request):
            calls.append(symbol)
            if successful_first and symbol == "SPY":
                return super().fetch(symbol, request)
            raise YFRateLimitError()
    request = DataRequest(("SPY", "QQQ", "IWM"), date(2024, 3, 7), date(2024, 3, 12))
    path = snapshot(request, Limited(), tmp_path, attempts=3, pause=0)
    result = read_snapshot(path, require_complete=False)
    assert calls == (["SPY", "QQQ"] if successful_first else ["SPY"])
    assert result["status"] == ("partial_failure" if successful_first else "failed")
    assert result["stop_reason"] == "provider_rate_limited"
    assert result["errors"]["IWM"]["attempts"] == 0
    assert result["errors"]["IWM"]["type"] == "SkippedAfterRateLimit"
    if successful_first:
        assert result["coverage"]["SPY"]["rows"] == 3
    with pytest.raises(ValueError, match="partial/failed"):
        read_snapshot(path)


def test_transient_failure_still_retries(tmp_path):
    calls = []
    class Transient(FixtureProvider):
        def fetch(self, symbol, request):
            calls.append(symbol)
            if len(calls) == 1:
                raise TimeoutError("transient")
            return super().fetch(symbol, request)
    path = snapshot(DataRequest(("SPY",), date(2024, 3, 7), date(2024, 3, 12)),
                    Transient(), tmp_path, attempts=2, pause=0)
    assert read_snapshot(path)["status"] == "complete"
    assert calls == ["SPY", "SPY"]


def test_validation_requires_both_benchmarks_and_execution_session(tmp_path):
    payload = deepcopy(DEFAULTS)
    payload["universe"]["symbols"] = ["SPY", "QQQ"]
    config = ResearchConfig.model_validate(payload)
    request = DataRequest(("SPY", "QQQ"), date(2022, 1, 3), date(2024, 1, 1))
    class Unverified(FixtureProvider):
        def fetch(self, symbol, request):
            frame, meta = super().fetch(symbol, request)
            if symbol == "QQQ":
                meta["inception_verified"] = False
            return frame, meta
    path = snapshot(request, Unverified(), tmp_path, attempts=1, pause=0)
    _, quality = normalize(path, config)
    assert quality["assets"]["SPY"]["eligible_sessions"] > 0
    assert not quality["backtest_ready"]
    assert quality["blocking_reasons"]
    path = snapshot(request, FixtureProvider(), tmp_path, attempts=1, pause=0)
    _, quality = normalize(path, config)
    assert quality["backtest_ready"]
    sessions = calendar().sessions_in_range("2022-01-03", "2024-01-01")
    only_warmup = DataRequest(request.symbols, request.start, sessions[253].date())
    path = snapshot(only_warmup, FixtureProvider(), tmp_path, attempts=1, pause=0)
    _, quality = normalize(path, config)
    assert quality["assets"]["SPY"]["eligible_sessions"] == 1
    assert not quality["backtest_ready"]

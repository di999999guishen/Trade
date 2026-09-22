from copy import deepcopy
from datetime import UTC, date, datetime

import pytest

from quant_research.calendar import calendar, decision_at
from quant_research.data import DataRequest
from quant_research.evidence import accept_evidence
from quant_research.fixtures import FixtureProvider
from quant_research.validation import TimeSplit, annual_splits, labels


def frame():
    raw, _ = FixtureProvider().fetch("SPY", DataRequest(("SPY",), date(2010, 1, 4), date(2025, 1, 1)))
    raw.columns = [s.lower().replace(" ", "_") for s in raw]
    raw["available_at"] = [decision_at(s).tz_convert("UTC") for s in raw.index]
    return raw


def test_labels_use_next_open_and_horizon_calendar():
    prices = frame()
    sample = labels({"SPY": prices}, 5).iloc[0]
    assert sample.return_value == pytest.approx(prices.close.iloc[5] / prices.open.iloc[1] - 1)
    assert sample.entry_at == calendar().session_open(prices.index[1])
    assert sample.exit_at == calendar().session_close(prices.index[5])
    assert sample.label_available_at == prices.available_at.iloc[5]


def test_purge_date_grouping_and_insufficient_history():
    prices = frame()
    samples = labels({"SPY": prices, "QQQ": prices}, 20)
    result = TimeSplit("2011-01-01", "2016-01-01", "2017-01-01", "2018-01-01").select(samples)
    assert result["status"] == "ready"
    assert result["purged"]
    train = samples[samples.sample_id.isin(result["train"])]
    assert train.label_available_at.max() < decision_at("2016-01-04")
    for ids in (result["train"], result["validation"], result["test"]):
        selected = samples[samples.sample_id.isin(ids)]
        assert selected.groupby("session").symbol.nunique().eq(2).all()
    assert annual_splits(samples)["status"] == "ready"
    assert annual_splits(samples.tail(60))["status"] == "insufficient_data"


def test_missing_session_is_not_compressed_out_of_label():
    prices = frame().iloc[:30].copy()
    prices = prices.drop(prices.index[3])
    sample = labels({"SPY": prices}, 5)
    assert prices.index[0] not in set(sample.session)


def evidence():
    return {"asset_id": "US:SPY", "source": "fixture", "published_at": "2024-03-08T20:00:00Z",
            "available_at": "2024-03-08T20:05:00Z", "ingested_at": "2024-03-08T20:06:00Z",
            "generated_at": "2024-03-08T20:07:00Z", "snapshot_hash": "a" * 64,
            "model_version": "fixture", "prompt_version": "1", "kind": "inference", "rating": "Overweight",
            "parse_status": "validated", "validity_hours": 24}


def test_evidence_semantics_and_late_rejection():
    now = datetime(2024, 3, 8, 23, tzinfo=UTC)
    result = accept_evidence(evidence(), "US:SPY", now)
    assert result["probability"] is None
    assert result["evidence"]["rating"] == "Overweight"
    with pytest.raises(ValueError, match="asset mismatch"):
        accept_evidence(evidence(), "US:QQQ", now)
    late = deepcopy(evidence())
    late["generated_at"] = "2025-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="late"):
        accept_evidence(late, "US:SPY", now, forward=False)
    broken = evidence()
    broken["rating"] = "probably buy"
    with pytest.raises(ValueError):
        accept_evidence(broken, "US:SPY", now)

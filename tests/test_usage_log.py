"""Usage recorder: both token shapes, fail-open behavior, and the kill switch."""

import json
from types import SimpleNamespace

import pytest

from tradingagents.observability import usage_log


def _read_records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_openai_style_token_usage_writes_one_complete_record(tmp_path, monkeypatch):
    log_path = tmp_path / "usage" / "day.jsonl"
    monkeypatch.setenv("TRADINGAGENTS_USAGE_LOG", str(log_path))
    monkeypatch.setenv("TRADINGAGENTS_RUN_ID", "run-42")
    monkeypatch.setenv("TRADINGAGENTS_TICKER", "510050.SS")

    response = SimpleNamespace(
        llm_output={
            "token_usage": {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
            },
            "model_name": "deepseek-v4-flash",
        },
        generations=[],
    )

    usage_log.UsageRecorder().on_llm_end(
        response, run_id="ignored", metadata={"langgraph_node": "market_analyst"}
    )

    records = _read_records(log_path)
    assert len(records) == 1
    record = records[0]
    assert set(record) == {
        "ts", "run_id", "ticker", "agent", "model",
        "prompt_tokens", "completion_tokens", "total_tokens",
    }
    assert record["run_id"] == "run-42"
    assert record["ticker"] == "510050.SS"
    assert record["agent"] == "market_analyst"
    assert record["model"] == "deepseek-v4-flash"
    assert record["prompt_tokens"] == 120
    assert record["completion_tokens"] == 30
    assert record["total_tokens"] == 150
    assert "T" in record["ts"]


def test_usage_metadata_style_token_usage_is_recorded(tmp_path, monkeypatch):
    log_path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("TRADINGAGENTS_USAGE_LOG", str(log_path))
    monkeypatch.delenv("TRADINGAGENTS_RUN_ID", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_TICKER", raising=False)

    message = SimpleNamespace(
        usage_metadata={"input_tokens": 7, "output_tokens": 5, "total_tokens": 12}
    )
    response = SimpleNamespace(
        llm_output={},
        generations=[[SimpleNamespace(message=message)]],
    )

    usage_log.UsageRecorder().on_llm_end(response, run_id="ignored")

    records = _read_records(log_path)
    assert len(records) == 1
    record = records[0]
    assert record["prompt_tokens"] == 7
    assert record["completion_tokens"] == 5
    assert record["total_tokens"] == 12
    assert record["agent"] == ""
    assert record["model"] == ""
    assert record["ticker"] == ""
    # No run id env: the documented fallback is the local date.
    assert len(record["run_id"]) == 10 and record["run_id"].count("-") == 2


def test_exceptions_in_on_llm_end_never_escape(tmp_path, monkeypatch):
    log_path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("TRADINGAGENTS_USAGE_LOG", str(log_path))

    class ExplodingResponse:
        @property
        def llm_output(self):
            raise RuntimeError("provider returned garbage")

    # Must return normally rather than propagate into a prediction run.
    usage_log.UsageRecorder().on_llm_end(ExplodingResponse())
    assert not log_path.exists()


@pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "False"])
def test_disabled_values_write_nothing(tmp_path, monkeypatch, value):
    log_path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("TRADINGAGENTS_USAGE_LOG", value)

    response = SimpleNamespace(
        llm_output={"token_usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        generations=[],
    )
    usage_log.UsageRecorder().on_llm_end(response)

    assert not log_path.exists()
    assert usage_log.get_usage_recorder() is None

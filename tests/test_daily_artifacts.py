import json
from datetime import datetime

import pytest
from pydantic import BaseModel

from daily_artifacts import new_run_id, save_daily_result


def test_full_result_and_same_instant_runs(tmp_path):
    class Message(BaseModel):
        content: str

    now = datetime(2026, 9, 17, 11, 0)
    result = {"messages": [Message(content="证据" * 6000)]}
    first, second = new_run_id(now), new_run_id(now)
    assert first != second
    for run_id in (first, second):
        report, summary = save_daily_result(tmp_path, "510050.SS", run_id, now,
                                           "2026-09-17", result, "Hold")
        with open(summary, encoding="utf-8") as handle:
            assert json.load(handle)["result"]["messages"][0]["content"] == "证据" * 6000
        with open(report, encoding="utf-8") as handle:
            assert "证据" * 6000 in handle.read()
    with pytest.raises(FileExistsError):
        save_daily_result(tmp_path, "510050.SS", first, now, "2026-09-17", {}, "Sell")

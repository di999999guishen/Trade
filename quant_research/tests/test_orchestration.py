import json
from pathlib import Path

import pytest

from quant_research.artifacts import exclusive_lock
from quant_research.config import ResearchConfig
from quant_research.fixtures import create_fixture
from quant_research.orchestration import daily_research, forward_execution_at


def test_delayed_forward_never_backfills_open():
    assert forward_execution_at("2024-03-08", "2024-03-11T13:31:00Z", "2024-03-08T23:00:00Z").isoformat() == "2024-03-12T13:30:00+00:00"
    assert forward_execution_at("2024-03-08", "2024-03-08T23:00:00Z", "2024-03-08T23:00:00Z").isoformat() == "2024-03-11T13:30:00+00:00"


def test_lock_is_exclusive_and_released(tmp_path):
    lock = tmp_path / "running.lock"
    with exclusive_lock(lock), pytest.raises(FileExistsError), exclusive_lock(lock):
        pass
    assert not lock.exists()


def test_daily_staleness_resume_and_idempotency(tmp_path, monkeypatch):
    from quant_research import orchestration
    path = create_fixture(tmp_path / "fixtures")["snapshot"]
    config = ResearchConfig.model_validate_json((Path(__file__).parents[1] / "configs" / "fixture.json").read_text())
    with pytest.raises(ValueError, match="stale_snapshot"):
        daily_research(path, config, tmp_path, now="2024-03-11T23:00:00Z")
    calls = []
    def stage(snapshot, config, project):
        calls.append("factors")
        raise OSError("injected disk failure")
    monkeypatch.setattr(orchestration, "factor_run", stage)
    with pytest.raises(OSError):
        daily_research(path, config, tmp_path, now="2023-12-30T00:00:00Z")
    state = next((tmp_path / "state" / "daily").iterdir())
    assert (state / "01_data_checked.json").exists()
    assert not (state / "running.lock").exists()
    assert list(state.glob("failure*.json"))
    def success(snapshot, config, project):
        calls.append("success")
        folder = tmp_path / f"output_{len(calls)}"
        folder.mkdir()
        (folder / "manifest.json").write_text(json.dumps({"files": {}}))
        return {"directory": str(folder), "run_id": "fixture"}
    monkeypatch.setattr(orchestration, "factor_run", success)
    monkeypatch.setattr(orchestration, "research", success)
    result = daily_research(path, config, tmp_path, now="2023-12-30T00:00:00Z")
    assert result["daily_status"] == "completed_research_only"
    repeated = daily_research(path, config, tmp_path, now="2023-12-30T00:00:00Z")
    assert repeated["daily_status"] == "already_completed"
    assert len(calls) == 3

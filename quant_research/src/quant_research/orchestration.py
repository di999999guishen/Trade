"""Restartable local research stages. No persistent scheduler or broker is installed."""
import json
from pathlib import Path

import pandas as pd

from .artifacts import digest, exclusive_lock, file_hash, provenance, utc_now, verify_files, write_json
from .calendar import calendar, decision_at, latest_completed
from .data import normalize, read_snapshot
from .factor_run import factor_run
from .research import research


def forward_execution_at(signal_session, generated_at, received_at):
    generated, received = pd.Timestamp(generated_at), pd.Timestamp(received_at)
    if generated.tzinfo is None or received.tzinfo is None:
        raise ValueError("forward timestamps must include timezones")
    available = max(generated, received, decision_at(signal_session))
    cal = calendar()
    session = cal.next_session(pd.Timestamp(signal_session))
    while cal.session_open(session) < available:
        session = cal.next_session(session)
    return cal.session_open(session)


def daily_research(snapshot_path, config, project, now=None):
    """Refresh current research from frozen data; this is NOT forward paper trading.

    Completed stage records are immutable. Failures append a unique attempt report;
    the next invocation resumes from the last verified completed stage.
    """
    project = Path(project)
    snapshot_path = Path(snapshot_path).resolve()
    manifest = read_snapshot(snapshot_path)
    expected = latest_completed(now).date().isoformat()
    if any(item["last"] != expected for item in manifest["coverage"].values()):
        raise ValueError("stale_snapshot: daily research requires the latest completed decision session")
    code = provenance(project)
    key = digest({"snapshot": file_hash(snapshot_path / "manifest.json"), "config": config.model_dump(),
                  "session": expected, "source_hash": code["source_hash"], "lock_hash": code["lock_hash"]})
    state = project / "state" / "daily" / key
    state.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(state / "running.lock"):
        try:
            checked = state / "01_data_checked.json"
            if not checked.exists():
                _, quality = normalize(snapshot_path, config)
                if not any(row["eligible_sessions"] for row in quality["assets"].values()):
                    raise ValueError("insufficient_data: no verified eligible universe")
                write_json(checked, {"quality": quality, "completed_at": utc_now()})
            features = state / "02_factors_completed.json"
            if not features.exists():
                output = factor_run(snapshot_path, config, project)
                write_json(features, output)
            else:
                output = json.loads(features.read_text())
                folder = Path(output["directory"])
                verify_files(folder, json.loads((folder / "manifest.json").read_text()))
            done = state / "03_research_completed.json"
            if done.exists():
                result = json.loads(done.read_text())
                folder = Path(result["directory"])
                verify_files(folder, json.loads((folder / "manifest.json").read_text()))
                return {**result, "daily_status": "already_completed", "forward_paper_enabled": False}
            result = research(snapshot_path, config, project)
            write_json(done, result)
            return {**result, "daily_status": "completed_research_only", "forward_paper_enabled": False}
        except Exception as exc:
            from .artifacts import run_id
            write_json(state / f"{run_id('failure')}.json", {"status": "failed", "type": type(exc).__name__,
                                                           "reason": str(exc), "created_at": utc_now()})
            raise

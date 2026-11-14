"""Regression locks for the 2026-11-13 archive MemoryError.

The cycle finished all 22 analysis steps and wrote its research report at
09:56:05, then archive_cycle OOM'd at 09:56:53 while packing a 113 MB
research.db + 2141 input files. The exception handler's own ``persist()``
OOM'd a second time, so the error escaped to the CLI and the whole stage
exited 1 -> manifest=failed, briefing/order_plans never rendered.

These tests lock the three fixes without needing 100+ MB of real data.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from prediction_research import archives


def test_json_stream_is_byte_identical_to_old_writer(tmp_path):
    """request_sha256 / manifest hashes must not change across the fix."""
    value = {"z": 1, "a": {"nested": [1, 2, 3], "text": "中文"}}
    expected = archives._json_bytes(value)
    target = tmp_path / "doc.json"
    archives.write_json_stream(target, value)
    assert target.read_bytes() == expected
    assert b"\r\n" not in target.read_bytes()  # Windows text mode must not emit CRLF
    assert archives._json_sha256(value) == hashlib.sha256(expected).hexdigest()


def test_json_sha256_streams_without_materialising_bytes(monkeypatch):
    """The identity hash must not call json.dumps on the full frozen report."""
    called = []
    original = json.dumps

    def spy(*args, **kwargs):
        called.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(archives.json, "dumps", spy)
    value = {"cycle": {"steps": list(range(100))}, "config": {}}
    digest = archives._json_sha256(value)
    assert len(digest) == 64
    assert not called, "identity hash must use iterencode, not json.dumps"
    assert digest == hashlib.sha256(archives._json_bytes(value)).hexdigest()


def test_collect_references_recurses_into_matched_children():
    """The walker must keep the old nested-reference semantics."""
    doc = {
        "snapshots": {"A": {"path": "cache_A.csv", "sha256": "x"}},
        "price_snapshots": {"B": "y"},
        "history_path": "cache_old.csv",
        "nested": {"source_snapshot": {"path": "etf_snapshot_1.json", "sha256": "z"}},
    }
    refs: list = []
    archives.collect_references(doc, refs)
    kinds = {kind for kind, _ in refs}
    assert "source_snapshot" in kinds
    assert "data_snapshot" in kinds
    paths = [ref.get("path") for _, ref in refs]
    assert "cache_A.csv" in paths
    assert "cache_B.csv" in paths
    assert "cache_old.csv" in paths
    assert "etf_snapshot_1.json" in paths


def test_collect_references_filters_invalid_price_symbols():
    refs: list = []
    archives.collect_references({"price_snapshots": {"GOOD_1": "a", "../bad": "b"}}, refs)
    paths = [ref.get("path") for _, ref in refs]
    assert "cache_GOOD_1.csv" in paths
    assert not any("bad" in str(path) for path in paths)


def test_write_json_stream_accepts_unsorted_cycle_report(tmp_path):
    """cycle.persist() uses sort_keys=False; key order must be preserved."""
    value = {"zeta": 1, "alpha": 2}
    target = tmp_path / "cycle.json"
    archives.write_json_stream(target, value, sort_keys=False)
    assert target.read_text(encoding="utf-8").index("zeta") < target.read_text(encoding="utf-8").index("alpha")
    assert json.loads(target.read_text(encoding="utf-8")) == value


def test_archive_releases_decoded_documents(monkeypatch, tmp_path):
    """A large artifact must be parsed, harvested and released immediately.

    We patch json.loads to return a sentinel and assert archive_cycle never
    keeps a growing list of decoded documents alive (the old ``documents``
    list). Failure here is what produced the >100 MB live-object peak.
    """
    source = Path(archives.__file__).read_text(encoding="utf-8")
    assert "documents.append(" not in source
    assert "collect_references(document, references)" in source
    assert "del document" in source
    assert "gc.collect()" in source


def test_archive_writes_failure_marker_on_memory_error(monkeypatch, tmp_path):
    """A MemoryError during packing must not escape as an uncaught exception."""
    from prediction_research import cycle as cycle_module

    assert "except MemoryError:" in Path(cycle_module.__file__).read_text(encoding="utf-8")
    assert "report_truncated_due_to_memory_pressure" in Path(cycle_module.__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize("rows", [0, 1, 200])
def test_database_snapshot_batches_run_configs(tmp_path, rows):
    """Bounded fetchmany must redact every row without fetchall()."""
    import sqlite3

    source, target = tmp_path / "live.db", tmp_path / "copy.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE runs(run_id TEXT PRIMARY KEY, config_json TEXT)")
        connection.executemany(
            "INSERT INTO runs VALUES (?, ?)",
            [(f"r{i}", json.dumps({"model": {"api_key": "secret"}})) for i in range(rows)],
        )
        connection.commit()
    redacted = archives._database_snapshot(source, target)
    assert redacted == rows
    with sqlite3.connect(target) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        if rows:
            value = connection.execute("SELECT config_json FROM runs LIMIT 1").fetchone()[0]
            assert json.loads(value)["model"]["api_key"] == "[REDACTED]"

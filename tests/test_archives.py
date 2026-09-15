"""Offline archive acceptance tests; runnable with unittest without LLM imports."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from prediction_research.archives import ArchiveError, archive_cycle, redact_config, verify_archive


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for directory in ("runs", "state", "datasets/market", "datasets/etf_market"):
            (self.root / directory).mkdir(parents=True)
        self.cfg = {"_project_dir": str(self.root), "timezone": "Asia/Shanghai", "runs_dir": "runs",
                    "state_db": "state/research.db", "market_data_dir": "datasets/market",
                    "etf_market": {"snapshot_dir": "datasets/etf_market"},
                    "model": {"api_key": "top-secret", "iterations": 3},
                    "news": {"url": "https://name:password@example.test/feed?api_key=secret#fragment"}}
        self.db = self.root / self.cfg["state_db"]
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("CREATE TABLE predictions(prediction_id TEXT PRIMARY KEY, probability_up REAL)")
            connection.execute("INSERT INTO predictions VALUES ('p1', 0.53)")
            connection.execute("CREATE TABLE runs(run_id TEXT PRIMARY KEY, config_json TEXT)")
            connection.execute("INSERT INTO runs VALUES ('r1', ?)", (json.dumps(self.cfg),))
        self.cache = self.root / "datasets/market/cache_510300.csv"
        self.cache.write_text("date,close\n2026-09-14,4.1\n", encoding="utf-8")
        self.snapshot = self.root / "datasets/etf_market/etf_snapshot_20260914_150000.json"
        self.snapshot.write_text('{"records": []}', encoding="utf-8")
        self.screen = self.root / "runs/screen_etf_flow_example.json"
        self.screen.write_text(json.dumps({"source_snapshot": self.reference(self.snapshot)}), encoding="utf-8")
        self.prediction = self.root / "runs/prediction_example.json"
        self.prediction.write_text(json.dumps({"predictions": [{"prediction_id": "p1",
                                                                 "data_snapshot": self.reference(self.cache)}]}), encoding="utf-8")
        self.backtest = self.root / "runs/backtest_example.json"
        self.backtest.write_text(json.dumps({"snapshots": {"510300": self.reference(self.cache)}}), encoding="utf-8")
        self.markdown = self.root / "runs/research_report_example.md"
        self.markdown.write_text("# Daily research\n", encoding="utf-8")
        self.report = {"cycle_id": "test-run", "started_at_utc": "2026-09-14T15:50:00+00:00",
                       "decision_at_utc": "2026-09-14T16:01:00+00:00", "outcome": "complete",
                       "steps": [{"name": "predict_5d", "status": "ok"}],
                       "artifacts": {"screen": str(self.screen), "predict_5d": str(self.prediction),
                                     "backtest_5d": str(self.backtest), "report": str(self.markdown)}}
        self.cycle = self.root / "runs/cycle_example.json"
        self.cycle.write_text(json.dumps(self.report), encoding="utf-8")

    @staticmethod
    def reference(path):
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def create(self):
        return archive_cycle(self.cfg, self.report, self.cycle)

    def test_restore_is_self_contained_and_database_is_queryable(self):
        result = self.create()
        self.assertEqual(result["status"], "complete")
        archive = Path(result["archive_path"])
        self.assertEqual(archive.parent.name, "2026-09-15")
        restored = self.root / "isolated_restore"
        shutil.copytree(archive, restored)
        # Source changes and deletion cannot affect a restored archive.
        self.cache.write_text("changed", encoding="utf-8")
        self.prediction.unlink()
        self.assertTrue(verify_archive(restored)["complete"])
        with closing(sqlite3.connect(restored / "state/research.db")) as connection:
            self.assertEqual(connection.execute("SELECT probability_up FROM predictions WHERE prediction_id='p1'").fetchone(), (0.53,))
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        archived_csv = next((restored / "inputs").rglob("*.csv"))
        self.assertEqual(archived_csv.read_text(encoding="utf-8"), "date,close\n2026-09-14,4.1\n")

    def test_config_and_database_config_are_redacted_without_changing_live_database(self):
        result = self.create()
        archive = Path(result["archive_path"])
        config = json.loads((archive / "config.redacted.json").read_text(encoding="utf-8"))
        self.assertEqual(config["model"]["api_key"], "[REDACTED]")
        self.assertNotIn("_project_dir", config)
        self.assertEqual(config["news"]["url"], "https://example.test/feed")
        self.assertNotIn(b"top-secret", (archive / "state/research.db").read_bytes())
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertIn("top-secret", connection.execute("SELECT config_json FROM runs").fetchone()[0])
        manifest = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
        entry = next(row for row in manifest["files"] if row["kind"] == "database")
        self.assertEqual(entry["redacted_rows"], 1)
        self.assertFalse(entry["source_byte_identity_claimed"])

    def test_retry_reuses_verified_immutable_archive_after_live_data_changes(self):
        first = self.create()
        manifest = Path(first["manifest_path"])
        original = manifest.read_bytes()
        self.cache.write_text("new data", encoding="utf-8")
        self.report["archive"] = {**first, "cycle_outcome_before_archive": "complete"}
        second = self.create()
        self.assertTrue(second["reused"])
        self.assertEqual(original, manifest.read_bytes())

    def test_changed_request_with_same_id_is_rejected(self):
        result = self.create()
        self.report["candidate_symbols"] = ["510500"]
        with self.assertRaisesRegex(ArchiveError, "different cycle"):
            self.create()
        self.assertTrue(verify_archive(result["archive_path"])["ok"])

    def test_missing_input_is_explicit_and_incomplete_retry_is_idempotent(self):
        self.cache.unlink()
        result = self.create()
        self.assertEqual(result["status"], "incomplete")
        verified = verify_archive(result["archive_path"])
        self.assertTrue(verified["ok"])
        self.assertFalse(verified["complete"])
        self.assertTrue(any(row["kind"] == "data_snapshot" for row in verified["missing"]))
        self.report["archive"] = {**result, "cycle_outcome_before_archive": "complete"}
        self.report["outcome"] = "partial_failure"
        self.assertTrue(self.create()["reused"])

    def test_changed_input_hash_cannot_be_reported_as_complete(self):
        self.cache.write_text("revised or updated data", encoding="utf-8")
        result = self.create()
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(any(row["reason"] == "source_hash_mismatch" for row in verify_archive(result["archive_path"])["missing"]))

    def test_corrupted_archive_and_manifest_are_detected_and_never_overwritten(self):
        result = self.create()
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        entry = next(row for row in manifest["files"] if row.get("original_filename") == self.prediction.name)
        target = Path(result["archive_path"]) / entry["archive_path"]
        target.write_text("{}", encoding="utf-8")
        self.assertFalse(verify_archive(result["archive_path"])["ok"])
        with self.assertRaisesRegex(ArchiveError, "failed verification"):
            self.create()
        Path(result["manifest_path"]).write_text("{}", encoding="utf-8")
        self.assertFalse(verify_archive(result["archive_path"])["ok"])

    def test_missing_archived_file_is_detected(self):
        result = self.create()
        (Path(result["archive_path"]) / "state/research.db").unlink()
        self.assertTrue(any(row["reason"] == "missing_file" for row in verify_archive(result["archive_path"])["issues"]))

    def test_copy_failure_never_publishes_final_directory(self):
        with patch("prediction_research.archives.shutil.copyfile", side_effect=OSError("do not disclose token")):
            with self.assertRaises(ArchiveError) as error:
                self.create()
        self.assertNotIn("do not disclose token", str(error.exception))
        parent = self.root / "archives/2026-09-15"
        self.assertFalse((parent / "test-run").exists())
        partial = list(parent.glob("*.partial"))
        self.assertEqual(len(partial), 1)
        self.assertEqual(json.loads((partial[0] / "FAILURE.json").read_text())["status"], "failed")
        self.assertFalse(verify_archive(partial[0])["ok"])
        self.assertEqual(self.create()["status"], "complete")

    def test_corrupt_database_is_failure_and_missing_database_is_incomplete(self):
        self.db.write_bytes(b"not a database")
        with self.assertRaises(ArchiveError):
            self.create()
        self.db.unlink()
        result = self.create()
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(any(row["kind"] == "database" for row in verify_archive(result["archive_path"])["missing"]))

    def test_failed_cycle_is_archived_for_diagnosis(self):
        self.report["outcome"] = "partial_failure"
        self.report["steps"] = [{"name": "predict_20d", "status": "failed", "error_type": "ValueError"}]
        result = self.create()
        archived = json.loads((Path(result["archive_path"]) / "cycle.json").read_text(encoding="utf-8"))
        self.assertEqual(archived["steps"][0]["error_type"], "ValueError")
        self.assertEqual(result["status"], "complete")

    def test_unsafe_id_and_external_artifact_are_rejected(self):
        self.report["cycle_id"] = "../escape"
        with self.assertRaises(ArchiveError):
            self.create()
        self.report["cycle_id"] = "safe-run"
        secret = self.root / "secret.json"
        secret.write_text('{"password": "do not copy"}', encoding="utf-8")
        self.report["artifacts"]["other"] = str(secret)
        result = self.create()
        self.assertEqual(result["status"], "incomplete")
        for path in Path(result["archive_path"]).rglob("*"):
            if path.is_file():
                self.assertNotIn(b"do not copy", path.read_bytes())

    def test_windows_legacy_input_path_migrates_only_to_configured_data_root(self):
        reference = self.reference(self.cache)
        reference["path"] = "C:\\old-checkout\\market\\cache_510300.csv"
        self.prediction.write_text(json.dumps({"predictions": [{"data_snapshot": reference}]}), encoding="utf-8")
        result = self.create()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(list((Path(result["archive_path"]) / "inputs").rglob("*.csv"))), 1)

    def test_long_workspace_and_original_filename_use_short_archive_paths(self):
        workspace = self.root / ("workspace_" + "w" * max(1, 140 - len(str(self.root)) - 11))
        workspace.mkdir()
        for name in ("runs", "state", "datasets"):
            shutil.copytree(self.root / name, workspace / name)
        self.cfg["_project_dir"] = str(workspace)
        self.report["cycle_id"] = "f" * 32
        original = workspace / "runs" / ("prediction_" + "x" * 70 + ".json")
        original.write_bytes(self.prediction.read_bytes())
        self.report["artifacts"]["predict_5d"] = str(original)
        self.cycle = workspace / "runs" / self.cycle.name
        formerly_long_path = workspace / "archives" / "2026-09-15" / self.report["cycle_id"] / "artifacts" / original.name
        self.assertGreater(len(str(formerly_long_path)), 260)
        result = self.create()
        self.assertEqual(result["status"], "complete")
        self.assertTrue(verify_archive(result["archive_path"])["complete"])
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        entry = next(row for row in manifest["files"] if row.get("original_filename") == original.name)
        self.assertLess(len(str(Path(result["archive_path"]) / entry["archive_path"])), 260)

    def test_backup_includes_committed_wal_rows(self):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("INSERT INTO predictions VALUES ('p-wal', 0.61)")
            connection.commit()
            self.assertTrue(Path(str(self.db) + "-wal").is_file())
            result = self.create()
            with closing(sqlite3.connect(Path(result["archive_path"]) / "state/research.db")) as restored:
                self.assertEqual(restored.execute("SELECT probability_up FROM predictions WHERE prediction_id='p-wal'").fetchone(), (0.61,))

    def test_disabled_archive_and_inputs_are_explicit(self):
        self.cfg["archives"] = {"enabled": False}
        self.assertEqual(self.create()["status"], "disabled")
        self.assertFalse((self.root / "archives").exists())
        self.cfg["archives"] = {"include_inputs": False}
        self.assertEqual(self.create()["status"], "incomplete")

    def test_manifest_path_traversal_is_rejected_even_if_manifest_hash_is_recomputed(self):
        result = self.create()
        manifest_path = Path(result["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["archive_path"] = "../../outside.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_path.with_suffix(".sha256").write_text(hashlib.sha256(manifest_path.read_bytes()).hexdigest(), encoding="ascii")
        verified = verify_archive(result["archive_path"])
        self.assertFalse(verified["ok"])
        self.assertTrue(any(row["reason"] == "unsafe_or_duplicate_manifest_path" for row in verified["issues"]))

    def test_nested_credentials_are_redacted(self):
        clean = redact_config({"providers": [{"access_token": "a", "nested": {"password": "b"}}], "value": 42})
        self.assertEqual(clean["providers"][0]["access_token"], "[REDACTED]")
        self.assertEqual(clean["providers"][0]["nested"]["password"], "[REDACTED]")
        self.assertEqual(clean["value"], 42)

    def test_frozen_classification_history_for_non_predicted_symbol_is_archived(self):
        history = self.root / "datasets/market/snapshots/known-hash/cache_510500.csv"
        history.parent.mkdir(parents=True)
        history.write_text("date,close\n2026-09-14,5.3\n", encoding="utf-8")
        payload = json.loads(self.screen.read_text(encoding="utf-8"))
        payload["classified_candidates"] = [{"symbol": "510500", "evidence": {
            "history_snapshot": self.reference(history), "history_path": str(history),
            "history_sha256": self.reference(history)["sha256"]}}]
        self.screen.write_text(json.dumps(payload), encoding="utf-8")
        result = self.create()
        self.assertEqual(result["status"], "complete")
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        found = [row for row in manifest["files"] if row.get("original_filename") == history.name]
        self.assertEqual(len(found), 1)
        archived_history = Path(result["archive_path"]) / found[0]["archive_path"]
        self.assertEqual(archived_history.read_bytes(), history.read_bytes())

    def test_legacy_classification_history_missing_is_incomplete(self):
        payload = json.loads(self.screen.read_text(encoding="utf-8"))
        payload["classified_candidates"] = [{"evidence": {"history_path": str(self.root / "datasets/market/cache_missing.csv"),
                                                          "history_sha256": "a" * 64}}]
        self.screen.write_text(json.dumps(payload), encoding="utf-8")
        result = self.create()
        self.assertEqual(result["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()

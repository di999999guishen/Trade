"""Immutable, verifiable local archives of completed or failed research cycles.

Only explicit generated artifacts and typed market inputs are collected.  This
module never scans the project, copies environment files, or opens the live
database through the application's schema/migration helper.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from .config import resolve_project_path


class ArchiveError(RuntimeError):
    """Archive was not published; caller must record an operational failure."""


_SECRET = re.compile(r"api.?key|secret|password|passwd|token|authorization|credential|private.?key", re.I)
_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_CACHE = re.compile(r"cache_[A-Za-z0-9][A-Za-z0-9_.-]*\.csv\Z")


def redact_config(value):
    """Redact credential fields recursively, including authenticated URLs."""
    if isinstance(value, dict):
        return {str(key): "[REDACTED]" if _SECRET.search(str(key)) else redact_config(item)
                for key, item in value.items() if not str(key).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [redact_config(item) for item in value]
    if isinstance(value, str) and "://" in value:
        try:
            parsed = urlsplit(value)
            return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))
        except ValueError:
            return "[REDACTED_URL]"
    return value


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _basename(value: str) -> str:
    return PureWindowsPath(value).name if "\\" in value else PurePosixPath(value).name


def _resolve_source(value: str, roots: list[Path], *, migrate: bool = False,
                    expected_sha256: str | None = None) -> Path | None:
    """Use an allowed original path, or a same-name input in a configured root.

    Legacy snapshots contain Windows absolute paths from a prior checkout;
    migration never follows those paths outside the configured data roots.
    """
    original = Path(value)
    candidates = []
    if original.is_absolute():
        resolved = original.resolve()
        if any(_inside(resolved, root) for root in roots) and resolved.exists():
            candidates.append(resolved)
    else:
        for root in roots:
            candidate = (root / original).resolve()
            if _inside(candidate, root) and candidate.exists():
                candidates.append(candidate)
    if migrate:
        for root in roots:
            candidate = root / _basename(value)
            if _inside(candidate, root) and candidate.exists():
                candidates.append(candidate.resolve())
            # The known integration directory is the only supported artifact
            # subtree migration, rather than an arbitrary path suffix search.
            parts = PureWindowsPath(value).parts if "\\" in value else PurePosixPath(value).parts
            if len(parts) >= 2 and parts[-2].startswith("etf_integration_"):
                candidate = root / parts[-2] / parts[-1]
                if _inside(candidate, root) and candidate.exists():
                    candidates.append(candidate.resolve())
    if expected_sha256:
        for candidate in candidates:
            if candidate.is_file() and _hash(candidate) == expected_sha256:
                return candidate
    return candidates[0] if candidates else None


def _database_snapshot(source: Path, target: Path) -> int:
    # mode=ro avoids creating a missing database; backup includes committed WAL.
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as reader:
        with closing(sqlite3.connect(target)) as writer:
            deadline = time.monotonic() + 30

            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise ArchiveError("database backup exceeded 30 seconds")

            reader.backup(writer, pages=256, progress=progress, sleep=0.05)
            # Store a standalone database even when the live source uses WAL.
            writer.execute("PRAGMA journal_mode=DELETE")
            redacted_rows = 0
            tables = {row[0] for row in writer.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "runs" in tables:
                columns = {row[1] for row in writer.execute("PRAGMA table_info(runs)")}
                if {"run_id", "config_json"} <= columns:
                    for run_id, raw in writer.execute("SELECT run_id,config_json FROM runs").fetchall():
                        try:
                            clean = json.dumps(redact_config(json.loads(raw)), ensure_ascii=False, sort_keys=True)
                        except (TypeError, ValueError):
                            clean = json.dumps({"status": "unreadable_config_redacted"})
                        try:
                            changed = json.loads(clean) != json.loads(raw)
                        except (TypeError, ValueError):
                            changed = True
                        if changed:
                            writer.execute("UPDATE runs SET config_json=? WHERE run_id=?", (clean, run_id))
                            redacted_rows += 1
                    if redacted_rows:
                        writer.commit()
                        # Remove old credential bytes in free pages of this copy.
                        writer.execute("VACUUM")
            if writer.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ArchiveError("database integrity check failed")
            return redacted_rows


def _frozen_cycle(report: dict) -> dict:
    # Archive outcome is written to the operational cycle only after publication.
    frozen = {key: value for key, value in report.items() if key not in {"archive", "archives"}}
    prior = report.get("archive", {}).get("cycle_outcome_before_archive")
    if prior is not None:
        frozen["outcome"] = prior
    return frozen


def verify_archive(path: str | Path) -> dict:
    """Verify manifest, every declared file, and SQLite integrity without mutation.

    ``ok`` concerns stored-byte integrity.  ``complete`` also requires that all
    requested sources existed and matched their recorded input hashes.
    """
    root = Path(path).resolve()
    issues = []
    try:
        manifest_path = root / "manifest.json"
        expected = (root / "manifest.sha256").read_text(encoding="ascii").strip()
        if _hash(manifest_path) != expected:
            issues.append({"reason": "manifest_hash_mismatch"})
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), list):
            raise ValueError("unsupported manifest")
        seen = set()
        for entry in manifest["files"]:
            name = entry["archive_path"]
            relative = PurePosixPath(name)
            target = root / name
            if (not name or relative.is_absolute() or PureWindowsPath(name).is_absolute()
                    or "\\" in name or ".." in relative.parts or name in seen
                    or not _inside(target, root) or target.is_symlink()):
                issues.append({"file": name, "reason": "unsafe_or_duplicate_manifest_path"})
                continue
            seen.add(name)
            if not target.is_file():
                issues.append({"file": name, "reason": "missing_file"})
                continue
            if target.stat().st_size != entry["size"] or _hash(target) != entry["sha256"]:
                issues.append({"file": name, "reason": "file_hash_mismatch"})
                continue
            if entry.get("kind") == "database":
                try:
                    with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as connection:
                        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            issues.append({"file": name, "reason": "database_integrity_failed"})
                except sqlite3.Error:
                    issues.append({"file": name, "reason": "database_unreadable"})
        actual = {item.relative_to(root).as_posix() for item in root.rglob("*") if item.is_file()}
        unexpected = actual - seen - {"manifest.json", "manifest.sha256"}
        if unexpected:
            issues.append({"reason": "unlisted_files", "files": sorted(unexpected)})
        return {"ok": not issues, "complete": not issues and manifest.get("status") == "complete",
                "status": manifest.get("status"), "archive_path": str(root), "issues": issues,
                "missing": manifest.get("missing", []), "file_count": len(manifest["files"])}
    except (OSError, ValueError, TypeError, KeyError):
        return {"ok": False, "complete": False, "archive_path": str(root),
                "issues": issues + [{"reason": "manifest_unreadable"}]}


def archive_cycle(cfg: dict, report: dict, cycle_path: str | Path) -> dict:
    """Publish ``archives/YYYY-MM-DD/cycle_id`` atomically without overwriting.

    A repeated identical request reuses its verified archive, even after live
    caches/settlements change.  A different cycle/config with the same ID is an
    error.  Missing inputs publish an explicitly incomplete diagnostic archive.
    A failed cycle can still have a complete archive of its available outputs.
    """
    options = cfg.get("archives", {})
    if not options.get("enabled", True):
        return {"status": "disabled", "reason": "disabled_by_configuration"}
    cycle_id = str(report.get("cycle_id", ""))
    if not _COMPONENT.fullmatch(cycle_id) or cycle_id in {".", ".."}:
        raise ArchiveError("cycle_id must be a safe nonempty file component")
    try:
        when = datetime.fromisoformat(str(report.get("decision_at_utc") or report["started_at_utc"]).replace("Z", "+00:00"))
        if when.tzinfo is None:
            raise ValueError("cycle timestamp requires timezone")
        day = when.astimezone(ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))).date().isoformat()
        frozen = _frozen_cycle(report)
        clean_cfg = redact_config(cfg)
        identity = hashlib.sha256(_json_bytes({"cycle": frozen, "config": clean_cfg})).hexdigest()
        root = resolve_project_path(cfg, options.get("root_dir", "archives")).resolve()
        parent = root / day
        final = parent / cycle_id
        if not _inside(final, root):
            raise ArchiveError("archive destination escapes configured root")
        if final.exists():
            verified = verify_archive(final)
            if not verified["ok"]:
                raise ArchiveError("existing archive failed verification; refusing overwrite")
            existing = json.loads((final / "manifest.json").read_text(encoding="utf-8"))
            if existing.get("request_sha256") != identity:
                raise ArchiveError("cycle_id already archived with different cycle or configuration")
            return {"status": existing["status"], "archive_path": str(final),
                    "manifest_path": str(final / "manifest.json"), "reused": True,
                    "missing_count": len(existing["missing"]), "file_count": len(existing["files"])}
        parent.mkdir(parents=True, exist_ok=True)
        staging = parent / f".p-{uuid.uuid4().hex[:12]}.partial"
        staging.mkdir(exist_ok=False)
    except ArchiveError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ArchiveError(f"archive initialization failed ({type(exc).__name__})") from exc

    files, missing, documents = [], [], []
    copied = {}

    def write(relative: str, raw: bytes, kind: str, source: str | None = None):
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        files.append({"archive_path": relative, "kind": kind, "source_path": source,
                      "size": target.stat().st_size, "sha256": _hash(target)})

    def collect(source: Path | None, relative: str, kind: str, recorded: str,
                expected: str | None = None, parse: bool = False):
        if source is None or not source.is_file():
            missing.append({"kind": kind, "source_path": recorded, "reason": "missing_or_disallowed_source"})
            return
        actual = _hash(source)
        if expected and expected != actual:
            missing.append({"kind": kind, "source_path": recorded, "reason": "source_hash_mismatch",
                            "expected_sha256": expected, "actual_sha256": actual})
        key = str(source.resolve())
        if key in copied:
            return
        # Runtime names already carry long timestamps and model names.  Keeping
        # them under another date/UUID tree exceeds legacy Windows MAX_PATH.
        # The manifest provides the original-name/path mapping for restoration.
        relative = (f"artifacts/a{len(files):05d}{source.suffix}" if kind == "artifact"
                    else f"inputs/i{len(files):05d}{source.suffix}")
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if _hash(target) != actual or _hash(source) != actual:
            raise ArchiveError("source changed while archiving; no archive published")
        entry = {"archive_path": relative, "kind": kind, "source_path": str(source.resolve()),
                 "original_filename": source.name,
                 "recorded_source_path": recorded, "sha256": actual, "size": target.stat().st_size}
        if expected:
            entry["expected_source_sha256"] = expected
        files.append(entry)
        copied[key] = relative
        if parse and source.suffix == ".json":
            try:
                documents.append(json.loads(target.read_text(encoding="utf-8")))
            except (ValueError, UnicodeError):
                missing.append({"kind": kind, "source_path": recorded, "reason": "invalid_json"})

    try:
        write("cycle.json", _json_bytes(frozen), "cycle", str(Path(cycle_path).resolve()))
        if not Path(cycle_path).is_file():
            missing.append({"kind": "cycle", "reason": "original_cycle_file_missing"})
        write("config.redacted.json", _json_bytes(clean_cfg), "configuration")
        runs = resolve_project_path(cfg, cfg["runs_dir"]).resolve()
        for role, recorded in sorted(report.get("artifacts", {}).items()):
            if not isinstance(recorded, str):
                missing.append({"kind": "artifact", "role": role, "reason": "invalid_artifact_reference"})
                continue
            source = _resolve_source(recorded, [runs], migrate=True)
            basename = _basename(recorded)
            if (source is None or source.suffix not in {".json", ".md"}
                    or basename.startswith(".") or _SECRET.search(basename)):
                missing.append({"kind": "artifact", "role": role, "reason": "missing_or_disallowed_source"})
                continue
            relative = "artifacts/" + source.relative_to(runs).as_posix()
            collect(source, relative, "artifact", recorded, parse=True)
            if role == "integration" and source.name == "summary.json" and source.parent.name.startswith("etf_integration_"):
                for sibling in sorted(source.parent.iterdir()):
                    if re.fullmatch(r"E\d+\.(json|md)|README\.md", sibling.name):
                        collect(sibling if _inside(sibling, runs) else None,
                                "artifacts/" + sibling.relative_to(runs).as_posix(), "artifact", str(sibling), parse=True)
        if options.get("include_inputs", True):
            cache_roots = [resolve_project_path(cfg, cfg["market_data_dir"]).resolve()]
            for setting in ("legacy_cache_dir", "external_data_dir"):
                if cfg.get(setting):
                    cache_roots.append(resolve_project_path(cfg, cfg[setting]).resolve())
            snapshot_root = resolve_project_path(cfg, cfg.get("etf_market", {}).get("snapshot_dir", "datasets/etf_market")).resolve()
            references = []

            def inspect(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key in {"source_snapshot", "data_snapshot", "history_snapshot"} and isinstance(child, dict):
                            references.append(("data_snapshot" if key == "history_snapshot" else key, child))
                        elif (key == "history_path" and isinstance(child, str)
                              and not isinstance(value.get("history_snapshot"), dict)):
                            references.append(("data_snapshot", {"path": child, "sha256": value.get("history_sha256")}))
                        elif key == "snapshots" and isinstance(child, dict):
                            references.extend(("data_snapshot", item) for item in child.values() if isinstance(item, dict))
                        elif key == "price_snapshots" and isinstance(child, dict):
                            references.extend(("data_snapshot", {"path": f"cache_{symbol}.csv", "sha256": sha})
                                              for symbol, sha in child.items() if _COMPONENT.fullmatch(str(symbol)))
                        inspect(child)
                elif isinstance(value, list):
                    for child in value:
                        inspect(child)

            for document in documents + [frozen]:
                inspect(document)
            for kind, reference in references:
                recorded = reference.get("path")
                if not isinstance(recorded, str):
                    continue
                basename = _basename(recorded)
                roots = [snapshot_root] if kind == "source_snapshot" else cache_roots
                allowed = (re.fullmatch(r"etf_snapshot_[A-Za-z0-9_.-]+\.json", basename)
                           if kind == "source_snapshot" else _CACHE.fullmatch(basename))
                expected = reference.get("sha256")
                source = _resolve_source(recorded, roots, migrate=True, expected_sha256=expected) if allowed else None
                # The directory prefix preserves distinct caches with the same name.
                bucket = str(roots.index(next(root for root in roots if source and _inside(source, root)))) if source else "missing"
                collect(source, f"inputs/{kind}/{bucket}/{basename}" if allowed else "inputs/invalid",
                        kind, recorded, expected)
        else:
            missing.append({"kind": "inputs", "reason": "disabled_by_configuration"})
        if options.get("include_database", True):
            database = resolve_project_path(cfg, cfg["state_db"]).resolve()
            if database.is_file() and database.suffix in {".db", ".sqlite", ".sqlite3"}:
                target = staging / "state" / "research.db"
                target.parent.mkdir(parents=True, exist_ok=True)
                redacted_rows = _database_snapshot(database, target)
                files.append({"archive_path": "state/research.db", "kind": "database",
                              "source_path": str(database), "size": target.stat().st_size, "sha256": _hash(target),
                              "method": "sqlite_online_backup_then_redact_runs_config",
                              "redacted_tables": ["runs.config_json"] if redacted_rows else [],
                              "redacted_rows": redacted_rows, "source_byte_identity_claimed": False})
            else:
                missing.append({"kind": "database", "reason": "missing_or_disallowed_source"})
        else:
            missing.append({"kind": "database", "reason": "disabled_by_configuration"})
        manifest = {"schema_version": 1, "cycle_id": cycle_id, "archive_date": day,
                    "timezone": cfg.get("timezone", "Asia/Shanghai"), "cycle_outcome": report.get("outcome"),
                    "created_at_utc": datetime.now(timezone.utc).isoformat(), "request_sha256": identity,
                    "status": "incomplete" if missing else "complete", "files": files, "missing": missing,
                    "scope": "cycle outputs, referenced market inputs, sanitized configuration and consistent state snapshot",
                    "limitations": ["local archive is not offsite disaster recovery", "SHA256 detects accidental damage, not malicious rewriting",
                                    "raw news and external-agent directories are not recursively copied",
                                    "restore files to an isolated directory; remap recorded paths before rerunning"]}
        (staging / "manifest.json").write_bytes(_json_bytes(manifest))
        (staging / "manifest.sha256").write_text(_hash(staging / "manifest.json") + "\n", encoding="ascii")
        if not verify_archive(staging)["ok"]:
            raise ArchiveError("new archive failed verification; no archive published")
        # rename has no replace semantics for populated target directories.
        staging.rename(final)
        return {"status": manifest["status"], "archive_path": str(final), "manifest_path": str(final / "manifest.json"),
                "reused": False, "missing_count": len(missing), "file_count": len(files)}
    except Exception as exc:
        # Keep only a clearly named partial directory for diagnosis; never claim
        # publication or automatically delete data after a failed write.
        try:
            (staging / "FAILURE.json").write_bytes(_json_bytes({"status": "failed", "error_type": type(exc).__name__,
                                                               "message": "archive not published; retry with a new cycle if inputs changed"}))
        except OSError:
            pass
        if isinstance(exc, ArchiveError):
            raise
        raise ArchiveError(f"archive not published ({type(exc).__name__}); inspect partial directory") from exc

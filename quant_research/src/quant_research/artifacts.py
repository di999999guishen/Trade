"""Content hashes and atomic publication; never overwrite a published run."""
import json
import os
import platform
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from importlib import metadata
from pathlib import Path
from uuid import uuid4


def utc_now():
    return datetime.now(UTC).isoformat()


def run_id(prefix="run"):
    return f"{prefix}_{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}_{uuid4().hex}"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def file_hash(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        handle.write(canonical(value) + "\n")


@contextmanager
def publication(root, identifier):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    final = root / identifier
    stage = root / f".{identifier}.partial-{uuid4().hex}"
    if final.exists():
        raise FileExistsError(final)
    stage.mkdir()
    yield stage
    # Same filesystem rename; failed partials remain inspectable, unpublished.
    if final.exists():
        raise FileExistsError(final)
    stage.rename(final)


def provenance(project):
    project = Path(project).resolve()
    def git(*args):
        result = subprocess.run(["git", "-C", str(project), *args], capture_output=True, check=False)
        return result.stdout if result.returncode == 0 else b""
    # Never archive arbitrary untracked files (which could contain credentials).
    # Hash only this subsystem's source/config plus the tracked repository diff.
    code = {}
    for folder in ("src", "configs", "tests"):
        for path in sorted((project / folder).rglob("*")):
            if path.is_file() and path.suffix in {".py", ".json", ".csv"}:
                code[path.relative_to(project).as_posix()] = file_hash(path)
    lock = project / "uv.lock"
    return {
        "python": sys.version, "executable": sys.executable, "platform": platform.platform(),
        "git_commit": git("rev-parse", "HEAD").decode().strip() or None,
        "dirty_diff_hash": sha256(git("diff", "HEAD", "--", ".")).hexdigest(),
        "source_files": code, "source_hash": digest(code),
        "lock_hash": file_hash(lock) if lock.exists() else None,
        "dependencies": {dist.metadata["Name"]: dist.version for dist in metadata.distributions()},
    }


def verify_files(directory, manifest):
    root = Path(directory).resolve()
    for relative, expected in manifest["files"].items():
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or file_hash(target) != expected:
            raise ValueError(f"artifact integrity failure: {relative}")


@contextmanager
def exclusive_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(canonical({"pid": os.getpid(), "created_at": utc_now()}))
    try:
        yield
    finally:
        path.unlink()

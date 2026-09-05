from __future__ import annotations

import json
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_DIR / "config" / "research.json"


def load_config(path: str | Path | None = None) -> dict:
    config_path = Path(path).resolve() if path else DEFAULT_CONFIG
    with config_path.open(encoding="utf-8") as handle:
        cfg = json.load(handle)
    cfg["_config_path"] = str(config_path)
    cfg["_project_dir"] = str(PROJECT_DIR)
    return cfg


def resolve_project_path(cfg: dict, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (Path(cfg["_project_dir"]) / path).resolve()


def market_data_dir(cfg: dict) -> Path:
    path = Path(cfg["market_data_dir"])
    if path.is_absolute():
        return path
    return (Path(cfg["_project_dir"]) / path).resolve()


def legacy_cache_dir(cfg: dict) -> Path | None:
    value = cfg.get("legacy_cache_dir")
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else (Path(cfg["_project_dir"]) / path).resolve()


def external_data_dir(cfg: dict) -> Path:
    path = Path(cfg["external_data_dir"])
    return path if path.is_absolute() else (Path(cfg["_project_dir"]) / path).resolve()

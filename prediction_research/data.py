from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class Bar:
    trading_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float


@dataclass(frozen=True)
class SeriesSnapshot:
    symbol: str
    bars: tuple[Bar, ...]
    source_path: str
    sha256: str
    provider_metadata: dict | None = None


def _number(row: dict[str, str], field: str) -> float:
    value = row.get(field, "")
    if value is None or not value.strip():
        raise ValueError(f"missing {field}")
    return float(value)


def load_cache(symbol: str, directory: Path) -> SeriesSnapshot:
    path = directory / f"cache_{symbol}.csv"
    raw = path.read_bytes()
    bars: dict[date, Bar] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                day = date.fromisoformat(row.get("date", "").strip()[:10])
                bar = Bar(
                    trading_date=day,
                    open=_number(row, "open"),
                    high=_number(row, "high"),
                    low=_number(row, "low"),
                    close=_number(row, "close"),
                    volume=_number(row, "volume"),
                    amount=_number(row, "amount"),
                )
            except (TypeError, ValueError):
                continue
            if min(bar.open, bar.high, bar.low, bar.close) <= 0 or bar.volume < 0:
                continue
            bars[day] = bar
    ordered = tuple(bars[key] for key in sorted(bars))
    if len(ordered) < 80:
        raise ValueError(f"{symbol}: only {len(ordered)} valid bars")
    provider_metadata = None
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            provider_metadata = manifest.get("assets", {}).get(symbol)
        except (OSError, json.JSONDecodeError):
            provider_metadata = {"status": "manifest_unreadable"}
    return SeriesSnapshot(symbol, ordered, str(path.resolve()), hashlib.sha256(raw).hexdigest(), provider_metadata)


def configured_data_dirs(cfg: dict) -> list[Path]:
    project = Path(cfg["_project_dir"])
    directories = [(project / cfg["market_data_dir"]).resolve()]
    if cfg.get("legacy_cache_dir"):
        directories.append((project / cfg["legacy_cache_dir"]).resolve())
    return directories


def find_cache(symbol: str, directories: list[Path]) -> SeriesSnapshot:
    errors = []
    for directory in directories:
        try:
            return load_cache(symbol, directory)
        except (FileNotFoundError, ValueError) as exc:
            errors.append(str(exc))
    raise FileNotFoundError(" | ".join(errors))


def load_universe(cfg: dict, universe_name: str) -> tuple[dict, dict[str, SeriesSnapshot]]:
    universe = cfg.get("universes", {}).get(universe_name)
    if not universe:
        raise ValueError(f"unknown or empty universe: {universe_name}")
    directories = configured_data_dirs(cfg)
    snapshots: dict[str, SeriesSnapshot] = {}
    missing: dict[str, str] = {}
    for asset in universe:
        symbol = asset["symbol"]
        try:
            snapshots[symbol] = find_cache(symbol, directories)
        except (FileNotFoundError, ValueError) as exc:
            missing[symbol] = str(exc)
    metadata = {item["symbol"]: item for item in universe}
    return {"metadata": metadata, "missing": missing}, snapshots

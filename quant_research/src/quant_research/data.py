"""Immutable vendor snapshots; normalization never accesses the network."""
import json
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from yfinance.exceptions import YFRateLimitError

from .artifacts import digest, file_hash, publication, run_id, utc_now, verify_files, write_json
from .calendar import calendar, decision_at, latest_completed


@dataclass(frozen=True)
class DataRequest:
    symbols: tuple[str, ...]
    start: date
    end_exclusive: date

    def __post_init__(self):
        if self.start >= self.end_exclusive or not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("invalid data request")
        if any(not s.isascii() or not s.isalpha() or not s.isupper() for s in self.symbols):
            raise ValueError("unsafe or unsupported ticker")

    def as_dict(self):
        return {"symbols": list(self.symbols), "start": str(self.start),
                "end_exclusive": str(self.end_exclusive), "interval": "1d",
                "auto_adjust": False, "back_adjust": False, "actions": True, "repair": False}


class YahooProvider:
    name = "yfinance"

    def __init__(self, cache):
        import yfinance as yf
        self.yf = yf
        yf.set_tz_cache_location(str(cache))

    def fetch(self, symbol, request):
        ticker = self.yf.Ticker(symbol)
        frame = ticker.history(start=str(request.start), end=str(request.end_exclusive),
                               interval="1d", auto_adjust=False, back_adjust=False,
                               actions=True, repair=False, keepna=True, timeout=20,
                               raise_errors=True)
        metadata = ticker.history_metadata or {}
        # Vendor metadata is retained as returned, not interpreted as verified raw.
        metadata = json.loads(json.dumps(metadata, default=str))
        return frame, {"vendor_metadata": metadata, "price_basis": "unverified_vendor",
                       "volume_basis": "unverified_vendor", "inception_verified": False,
                       "raw_execution_ready": False, "provider_version": self.yf.__version__}


def snapshot(request, provider, root, attempts=3, pause=0.5, parent=None):
    if type(attempts) is not int or attempts < 1 or not np.isfinite(pause) or pause < 0:
        raise ValueError("attempts must be positive and pause finite/nonnegative")
    parent_manifest = None
    if parent is not None:
        parent = Path(parent)
        parent_manifest = read_snapshot(parent)
        previous = parent_manifest["request"]
        if parent_manifest["provider"] != provider.name or previous["symbols"] != list(request.symbols) or previous["start"] != str(request.start):
            raise ValueError("incremental source, universe and start must match parent")
        if request.end_exclusive < date.fromisoformat(previous["end_exclusive"]):
            raise ValueError("incremental end may not precede parent end")
        if request.as_dict() == previous:
            return parent  # exact request replay is idempotent and offline
    identifier = run_id("snapshot")
    manifest = {"schema_version": 1, "snapshot_id": identifier, "request": request.as_dict(),
                "provider": provider.name, "ingested_at": utc_now(), "files": {}, "coverage": {},
                "errors": {}, "availability_mode": "reconstructed"}
    if parent_manifest:
        manifest["parent_snapshot"] = parent_manifest["snapshot_id"]
        manifest["parent_manifest_hash"] = file_hash(parent / "manifest.json")
        manifest["revision_report"] = {}
    with publication(root, identifier) as stage:
        rate_limited_by = None
        for symbol in request.symbols:
            if rate_limited_by is not None:
                manifest["errors"][symbol] = {"type": "SkippedAfterRateLimit", "attempts": 0,
                                              "trigger_symbol": rate_limited_by}
                continue
            for attempt in range(attempts):
                try:
                    actual_request = request
                    prior = None
                    if parent_manifest:
                        prior = pd.read_parquet(parent / f"{symbol}.parquet")
                        overlap_start = prior.index[max(0, len(prior) - 5)].date()
                        actual_request = DataRequest((symbol,), overlap_start, request.end_exclusive)
                    frame, meta = provider.fetch(symbol, actual_request)
                    if frame.empty or not {"Open", "High", "Low", "Close", "Adj Close", "Volume"}.issubset(frame):
                        raise ValueError("empty or incomplete vendor schema")
                    frame = frame.copy()
                    frame.index = pd.to_datetime([pd.Timestamp(x).date() for x in frame.index])
                    frame.index.name = "session"
                    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
                        raise ValueError("duplicate or unordered sessions")
                    if frame.index.min().date() < actual_request.start or frame.index.max().date() >= actual_request.end_exclusive:
                        raise ValueError("provider violated exclusive date boundaries")
                    if prior is not None:
                        common = frame.index.intersection(prior.index)
                        columns = sorted(set(frame.columns) & set(prior.columns))
                        old, new = prior.loc[common, columns], frame.loc[common, columns]
                        changed = ~(old.eq(new) | (old.isna() & new.isna()))
                        deleted = prior.loc[str(actual_request.start):].index.difference(frame.index)
                        manifest["revision_report"][symbol] = {
                            "overlap_request": actual_request.as_dict(),
                            "changed_cells": int(changed.to_numpy().sum()),
                            "changed_sessions": [str(d.date()) for d in changed.index[changed.any(axis=1)]],
                            "missing_previous_sessions": [str(d.date()) for d in deleted],
                            "new_sessions": len(frame.index.difference(prior.index)),
                        }
                        frame = pd.concat([prior.loc[prior.index < pd.Timestamp(actual_request.start)], frame]).sort_index()
                    frame.to_parquet(stage / f"{symbol}.parquet")
                    write_json(stage / f"{symbol}.metadata.json", meta)
                    manifest["coverage"][symbol] = {"rows": len(frame), "first": str(frame.index.min().date()),
                                                      "last": str(frame.index.max().date())}
                    break
                except Exception as exc:  # noqa: BLE001 -- isolate each provider failure
                    if isinstance(exc, YFRateLimitError):
                        manifest["errors"][symbol] = {"type": type(exc).__name__, "attempts": attempt + 1}
                        manifest["stop_reason"] = "provider_rate_limited"
                        rate_limited_by = symbol
                        break
                    if attempt + 1 == attempts:
                        # Do not serialize arbitrary HTTP exception text, which can contain URLs/credentials.
                        manifest["errors"][symbol] = {"type": type(exc).__name__, "attempts": attempts}
                    else:
                        time.sleep(pause * 2**attempt)
            if pause and rate_limited_by is None:
                time.sleep(pause)
        manifest["status"] = "complete" if not manifest["errors"] else "partial_failure"
        if not manifest["coverage"]:
            manifest["status"] = "failed"
        for path in stage.iterdir():
            manifest["files"][path.name] = file_hash(path)
        manifest["content_hash"] = digest(manifest["files"])
        write_json(stage / "manifest.json", manifest)
    return Path(root) / identifier


def read_snapshot(path, require_complete=True):
    path = Path(path)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    verify_files(path, manifest)
    if digest(manifest["files"]) != manifest["content_hash"]:
        raise ValueError("snapshot manifest content hash mismatch")
    if require_complete and manifest["status"] != "complete":
        raise ValueError("partial/failed snapshot cannot be used for a full-universe backtest")
    return manifest


def normalize(path, config):
    path = Path(path)
    manifest = read_snapshot(path)
    if not manifest["provider"].startswith("synthetic") and manifest["provider"] != config.data.provider:
        raise ValueError("snapshot and configured provider differ")
    if set(config.universe.symbols) != set(manifest["request"]["symbols"]):
        raise ValueError("snapshot and configured universe differ")
    cal = calendar()
    frames, quality = {}, {}
    for symbol in config.universe.symbols:
        raw = pd.read_parquet(path / f"{symbol}.parquet")
        meta = json.loads((path / f"{symbol}.metadata.json").read_text(encoding="utf-8"))
        if not raw.index.isin(cal.sessions).all():
            raise ValueError(f"{symbol}: non-XNYS session")
        ohlc = raw[["Open", "High", "Low", "Close", "Adj Close"]]
        valid_close = np.isfinite(raw["Close"]) & (raw["Close"] > 0)
        known = ohlc.notna()
        if ((ohlc <= 0) & known).any().any() or np.isinf(ohlc).any().any():
            raise ValueError(f"{symbol}: nonpositive or infinite price")
        if (raw["Volume"] < 0).any() or np.isinf(raw["Volume"]).any():
            raise ValueError(f"{symbol}: invalid volume")
        complete = ohlc.notna().all(axis=1)
        if ((raw.High < raw[["Open", "Close", "Low"]].max(axis=1)) & complete).any() or (
                (raw.Low > raw[["Open", "Close", "High"]].min(axis=1)) & complete).any():
            raise ValueError(f"{symbol}: invalid OHLC relationships")
        sessions = cal.sessions_in_range(raw.index.min(), raw.index.max())
        raw = raw.reindex(sessions)
        ratio = raw["Adj Close"] / raw["Close"]
        frame = pd.DataFrame({key.lower(): raw[key] * ratio for key in ("Open", "High", "Low", "Close")})
        frame["volume"] = raw.Volume
        # Until price/volume basis has independent evidence, never claim verified ADV.
        basis_ok = meta.get("price_basis") == "raw_verified" and meta.get("volume_basis") == "raw_verified"
        sampled_basis = (config.data.provider in {"sina_us_daily", "sina_tencent_us_daily"}
                         and meta.get("price_basis") == "raw_sample_checked"
                         and meta.get("volume_basis") == "raw_sample_checked"
                         and meta.get("research_basis_evidence", {}).get("passed") is True)
        frame["adv20"] = (raw.Close * raw.Volume).rolling(20).mean() if basis_ok or sampled_basis else np.nan
        frame["observations"] = frame.close.notna().cumsum()
        frame["available_at"] = [decision_at(s).tz_convert("UTC") for s in sessions]
        inception = pd.Timestamp(meta["inception"]) if meta.get("inception_verified") else None
        frame["eligible"] = (
            (frame.observations >= config.universe.minimum_price_observations)
            & (frame.adv20 >= config.universe.min_adv_usd)
            & frame[["open", "high", "low", "close", "volume"]].notna().all(axis=1)
        )
        if inception is None:
            frame["eligible"] = False
        else:
            frame.loc[frame.index < inception, "eligible"] = False
        if meta.get("termination"):
            frame.loc[frame.index >= pd.Timestamp(meta["termination"]), "eligible"] = False
        quality[symbol] = {"rows": len(raw), "missing_sessions": int(raw.Close.isna().sum()),
                           "eligible_sessions": int(frame.eligible.sum()), "adv_basis_verified": basis_ok,
                           "exploratory_basis_accepted": sampled_basis,
                           "basis_evidence": meta.get("research_basis_evidence"),
                           "inception_verified": inception is not None,
                           "pay_dates_verified": False, "raw_execution_ready": False,
                           "exclusion_reasons": [reason for reason, bad in (
                               ("unverified_price_volume_basis", not (basis_ok or sampled_basis)),
                               ("unverified_inception", inception is None),
                               ("insufficient_warmup", int(valid_close.sum()) < 253)) if bad]}
        frames[symbol] = frame
    common = frames["SPY"].eligible & frames["QQQ"].eligible
    common = common.fillna(False)
    last_session = max(f.index.max() for f in frames.values())
    ready = bool((common & (common.index < last_session)).any())
    return frames, {"status": "research_only", "backtest_ready": ready,
                    "independent_full_history_basis_verified": all(a["adv_basis_verified"] for a in quality.values()),
                    "blocking_reasons": [] if ready else ["no_common_verified_benchmark_signal_with_next_session"],
                    "price_mode": config.data.price_mode,
                    "selection_status": config.universe.selection_status, "assets": quality,
                    "source_snapshot": manifest["snapshot_id"], "source_hash": manifest["content_hash"]}


def request_for_config(config, start=None, end=None):
    stop = date.fromisoformat(end) if end else latest_completed().date() + timedelta(days=1)
    if stop > latest_completed().date() + timedelta(days=1):
        raise ValueError("end includes an unfinished/unavailable session")
    return DataRequest(tuple(config.universe.symbols), date.fromisoformat(start or config.data.requested_start), stop)

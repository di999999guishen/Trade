"""Sina US raw candidates, isolated from verified total-return snapshots."""
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from curl_cffi import requests

from .artifacts import file_hash, publication, run_id, utc_now, write_json

ENDPOINT = "https://stock.finance.sina.com.cn/usstock/api/json_v2.php/US_MinKService.getDailyK"
FACTORS = "https://finance.sina.com.cn/us_stock/company/reinstatement/{}_qfq.js"


def parse_bars(payload, request):
    if not isinstance(payload, list) or not payload:
        raise ValueError("Sina returned no daily bars")
    frame = pd.DataFrame(payload)
    fields = {"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"}
    if not {"d", *fields}.issubset(frame):
        raise ValueError("incomplete Sina schema")
    frame.index = pd.to_datetime(frame["d"], format="%Y-%m-%d", errors="raise")
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError("duplicate or unordered Sina sessions")
    frame = frame.rename(columns=fields)[list(fields.values())].apply(pd.to_numeric, errors="raise")
    frame = frame.loc[(frame.index >= pd.Timestamp(request.start)) &
                      (frame.index < pd.Timestamp(request.end_exclusive))].copy()
    frame.index.name = "session"
    if frame.empty or not np.isfinite(frame.to_numpy()).all():
        raise ValueError("empty or nonfinite Sina history")
    if (frame[["Open", "High", "Low", "Close"]] <= 0).any().any() or (frame.Volume < 0).any():
        raise ValueError("invalid Sina prices or volume")
    if (frame.High < frame[["Open", "Close", "Low"]].max(axis=1)).any() or (
            frame.Low > frame[["Open", "Close", "High"]].min(axis=1)).any():
        raise ValueError("invalid Sina OHLC relationships")
    return frame


def collect(request, root):
    identifier = run_id("sina_candidate")
    manifest = {"schema_version": 1, "provider": "sina_us_daily", "ingested_at": utc_now(),
                "status": "unverified_candidate", "backtest_ready": False,
                "request": {"symbols": list(request.symbols), "start": str(request.start),
                            "end_exclusive": str(request.end_exclusive)},
                "blocking_reasons": ["unverified_price_volume_basis", "unverified_adjustment_semantics",
                                     "unverified_product_metadata"],
                "coverage": {}, "errors": {}, "adjustment_responses": {}}
    with publication(root, identifier) as stage:
        with requests.Session(impersonate="chrome") as session:
            for symbol in request.symbols:
                try:
                    response = session.get(ENDPOINT, params={"symbol": symbol.lower(), "scale": "240",
                                                           "datalen": "10000"}, timeout=20)
                    (stage / f"{symbol}.response.json").write_bytes(response.content)
                    response.raise_for_status()
                    frame = parse_bars(response.json(), request)
                    frame.to_parquet(stage / f"{symbol}.parquet")
                    manifest["coverage"][symbol] = {"rows": len(frame), "first": str(frame.index[0].date()),
                                                    "last": str(frame.index[-1].date()), "url": ENDPOINT}
                except Exception as exc:  # noqa: BLE001 -- retain per-symbol failures
                    manifest["errors"][symbol] = type(exc).__name__
                    print(json.dumps({"symbol": symbol, "error": type(exc).__name__}), flush=True)
                    continue
                try:
                    adjustment = session.get(FACTORS.format(symbol), timeout=20)
                    (stage / f"{symbol}.adjustment.response").write_bytes(adjustment.content)
                    manifest["adjustment_responses"][symbol] = {
                        "http_status": adjustment.status_code, "bytes": len(adjustment.content),
                        "url": FACTORS.format(symbol), "validated": False}
                except Exception as exc:  # noqa: BLE001 -- separate actions from prices
                    manifest["adjustment_responses"][symbol] = {"error": type(exc).__name__, "validated": False}
                print(json.dumps({"symbol": symbol, **manifest["coverage"][symbol]}), flush=True)
                time.sleep(0.25)
        manifest["download_status"] = "complete" if not manifest["errors"] else "partial_failure"
        if not manifest["coverage"]:
            manifest["download_status"] = "failed"
        manifest["files"] = {p.name: file_hash(p) for p in stage.iterdir() if p.is_file()}
        write_json(stage / "manifest.json", manifest)
    return {"directory": str(Path(root) / identifier), **manifest}

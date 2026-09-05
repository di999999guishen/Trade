from __future__ import annotations

import csv
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path


ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


def _secid(symbol: str) -> str:
    return f"{1 if symbol.startswith(('5', '6')) else 0}.{symbol}"


def fetch_daily(symbol: str, output_dir: Path, start: str = "20100101", end: str = "20500101", retries: int = 3) -> dict:
    params = {
        "secid": _secid(symbol),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "beg": start,
        "end": end,
    }
    request = urllib.request.Request(
        ENDPOINT + "?" + urllib.parse.urlencode(params),
        headers={"User-Agent": "Mozilla/5.0 prediction-research/0.1"},
    )
    last_error = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                raise
            time.sleep(1.5 * (2 ** attempt))
    data = payload.get("data") or {}
    lines = data.get("klines") or []
    if not lines:
        raise ValueError(f"{symbol}: provider returned no daily bars")
    rows = []
    for line in lines:
        fields = line.split(",")
        if len(fields) < 7:
            continue
        rows.append({
            "date": fields[0], "open": fields[1], "close": fields[2],
            "high": fields[3], "low": fields[4], "volume": fields[5], "amount": fields[6],
        })
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"cache_{symbol}.csv"
    # A temporary file plus replace prevents a failed refresh from corrupting a good snapshot.
    temporary = output_dir / f".{path.name}.tmp"
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("date", "open", "high", "low", "close", "volume", "amount"))
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)
    return {"symbol": symbol, "rows": len(rows), "start": rows[0]["date"], "end": rows[-1]["date"], "path": str(path.resolve()), "provider_name": data.get("name")}


def fetch_universe(assets: list[dict], output_dir: Path) -> dict:
    successes, failures = [], {}
    for asset in assets:
        symbol = asset["symbol"]
        try:
            successes.append(fetch_daily(symbol, output_dir))
        except Exception as exc:
            eastmoney_error = f"{type(exc).__name__}: {exc}"
            try:
                from .sina import fetch_daily as fetch_sina_daily

                result = fetch_sina_daily(symbol, output_dir)
                result["fallback_from"] = "eastmoney_push2his"
                successes.append(result)
            except Exception as fallback_exc:
                failures[symbol] = {
                    "eastmoney": eastmoney_error,
                    "sina": f"{type(fallback_exc).__name__}: {fallback_exc}",
                }
        time.sleep(1.0)
    return {"provider_chain": ["eastmoney_push2his", "sina_kline"], "successes": successes, "failures": failures}

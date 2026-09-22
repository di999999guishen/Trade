from __future__ import annotations

import csv
import json
import sys
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


def fetch_universe(assets: list[dict], output_dir: Path, trip_after: int = 3,
                   reprobe_every: int = 50, interval: float = 1.0) -> dict:
    """Refresh daily bars for every asset, preferring Eastmoney with a Sina fallback.

    Eastmoney's push2his endpoint can be refused at the WAF level for the local
    egress address. A refused request still costs the whole retry ladder in
    `fetch_daily` (~12s of backoff), so a backlog of a few hundred symbols turns
    into hours. After `trip_after` consecutive primary failures the circuit opens
    and the remaining symbols go straight to Sina, with one re-probe every
    `reprobe_every` symbols so a recovered provider is picked up mid-run instead
    of staying disabled until the process restarts.
    """
    successes, failures = [], {}
    consecutive_failures = 0
    paused = False
    countdown = 0
    skipped_primary = 0
    for asset in assets:
        symbol = asset["symbol"]
        use_primary = True
        if paused:
            if countdown > 0:
                countdown -= 1
                use_primary = False
            else:
                countdown = reprobe_every
        result = None
        primary_error = None
        if use_primary:
            try:
                result = fetch_daily(symbol, output_dir)
            except Exception as exc:
                primary_error = f"{type(exc).__name__}: {exc}"
                consecutive_failures += 1
                if not paused and consecutive_failures >= trip_after:
                    paused = True
                    countdown = reprobe_every
                    print(f"[eastmoney] primary provider paused after {consecutive_failures} consecutive "
                          f"failures; routing the next {reprobe_every} symbols to sina", file=sys.stderr, flush=True)
            else:
                consecutive_failures = 0
                if paused:
                    paused = False
                    countdown = 0
                    print("[eastmoney] primary provider recovered; preferring eastmoney again", file=sys.stderr, flush=True)
        else:
            primary_error = "skipped: primary provider paused after repeated failures"
            skipped_primary += 1
        if result is not None:
            successes.append(result)
        else:
            try:
                from .sina import fetch_daily as fetch_sina_daily

                fallback = fetch_sina_daily(symbol, output_dir)
                fallback["fallback_from"] = "eastmoney_push2his"
                successes.append(fallback)
            except Exception as fallback_exc:
                failures[symbol] = {
                    "eastmoney": primary_error,
                    "sina": f"{type(fallback_exc).__name__}: {fallback_exc}",
                }
        time.sleep(interval)
    return {"provider_chain": ["eastmoney_push2his", "sina_kline"], "successes": successes,
            "failures": failures, "primary_provider_paused": paused,
            "primary_provider_skipped": skipped_primary}

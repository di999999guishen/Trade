from __future__ import annotations

import csv
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


DOMESTIC = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_{symbol}=/InnerFuturesNewService.getDailyKLine?symbol={symbol}"
GLOBAL = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_{symbol}=/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={symbol}"


def _decode_jsonp(raw: str):
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("response did not contain a JSON array")
    return json.loads(raw[start : end + 1])


def _normalized_rows(payload, market: str) -> list[dict]:
    rows = []
    for item in payload:
        if isinstance(item, list):
            if len(item) < 6:
                continue
            day, opening, high, low, close, volume = item[:6]
        elif isinstance(item, dict):
            day = item.get("date") or item.get("day") or item.get("d")
            opening = item.get("open") if item.get("open") is not None else item.get("o")
            high = item.get("high") if item.get("high") is not None else item.get("h")
            low = item.get("low") if item.get("low") is not None else item.get("l")
            close = item.get("close") if item.get("close") is not None else item.get("c")
            volume = item.get("volume") or item.get("v") or 0
        else:
            continue
        try:
            values = [float(opening), float(high), float(low), float(close)]
            float(volume or 0)
        except (TypeError, ValueError):
            continue
        if not day or min(values) <= 0:
            continue
        rows.append({"date": str(day)[:10], "open": opening, "high": high, "low": low, "close": close, "volume": volume or 0, "amount": 0})
    unique = {row["date"]: row for row in rows}
    return [unique[key] for key in sorted(unique)]


def fetch_series(symbol: str, market: str, output_dir: Path) -> dict:
    url = (GLOBAL if market == "global" else DOMESTIC).format(symbol=symbol)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 prediction-research/0.2", "Referer": "https://finance.sina.com.cn/"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = _decode_jsonp(response.read().decode("utf-8", errors="replace"))
    rows = _normalized_rows(payload, market)
    if len(rows) < 80:
        raise ValueError(f"{symbol}: only {len(rows)} usable rows")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"cache_{symbol}.csv"
    temporary = output_dir / f".{path.name}.tmp"
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("date", "open", "high", "low", "close", "volume", "amount"))
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)
    return {"symbol": symbol, "market": market, "rows": len(rows), "start": rows[0]["date"], "end": rows[-1]["date"], "path": str(path.resolve())}


def fetch_all(series: dict, output_dir: Path) -> dict:
    successes, failures = [], {}
    manifest = {"schema_version": 1, "fetched_at_utc": datetime.now(timezone.utc).isoformat(), "assets": {}}
    for symbol, metadata in series.items():
        try:
            result = fetch_series(symbol, metadata["market"], output_dir)
            successes.append(result)
            manifest["assets"][symbol] = {**metadata, "provider": "sina_futures_history", "price_adjustment": "continuous_contract_unadjusted", "amount": "unavailable"}
        except Exception as exc:
            failures[symbol] = f"{type(exc).__name__}: {exc}"
    if successes:
        (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"provider": "sina_futures_history", "successes": successes, "failures": failures}

from __future__ import annotations

import csv
import json
import urllib.parse
import urllib.request
from pathlib import Path


ENDPOINT = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"


def _exchange_symbol(symbol: str) -> str:
    return ("sh" if symbol.startswith(("5", "6")) else "sz") + symbol


def fetch_daily(symbol: str, output_dir: Path) -> dict:
    query = urllib.parse.urlencode({"symbol": _exchange_symbol(symbol), "scale": 240, "ma": "no", "datalen": 1023})
    request = urllib.request.Request(ENDPOINT + "?" + query, headers={"User-Agent": "Mozilla/5.0 prediction-research/0.1", "Referer": "https://finance.sina.com.cn/"})
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"{symbol}: Sina returned no daily bars")
    rows = []
    for item in payload:
        rows.append({
            "date": item["day"][:10], "open": item["open"], "close": item["close"],
            "high": item["high"], "low": item["low"], "volume": item["volume"], "amount": "0",
        })
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"cache_{symbol}.csv"
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as handle:
            existing = list(csv.DictReader(handle))
        if len(existing) > len(rows) and existing[-1].get("date", "") >= rows[-1]["date"]:
            return {
                "symbol": symbol, "rows": len(existing), "start": existing[0]["date"],
                "end": existing[-1]["date"], "path": str(path.resolve()),
                "provider_name": "existing richer snapshot", "fallback_attempted": "Sina K-line",
            }
    temporary = output_dir / f".{path.name}.tmp"
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("date", "open", "high", "low", "close", "volume", "amount"))
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)
    return {"symbol": symbol, "rows": len(rows), "start": rows[0]["date"], "end": rows[-1]["date"], "path": str(path.resolve()), "provider_name": "Sina K-line", "amount_status": "unavailable"}

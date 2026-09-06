from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
import time
from datetime import datetime, timezone
from pathlib import Path

from ..taxonomy import classify_etf, market_scope


URLS = (
    "https://88.push2.eastmoney.com/api/qt/clist/get",
    "https://push2delay.eastmoney.com/api/qt/clist/get",
    "https://push2.eastmoney.com/api/qt/clist/get",
)


def fetch_etf_snapshot(output_dir: Path) -> dict:
    params = {
        "pn": 1, "pz": 100, "po": 1, "np": 1,
        "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": 2, "invt": 2,
        "fid": "f12", "fs": "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827",
        "fields": "f2,f3,f5,f6,f8,f10,f12,f13,f14,f20,f21,f62,f66,f69,f72,f75,f124,f184",
    }
    pages = []
    total = None
    source_url = None
    page_number = 1
    while total is None or len(pages) < total:
        params["pn"] = page_number
        raw = None
        last_error = None
        for attempt in range(6):
            source_url = URLS[attempt % len(URLS)]
            request = urllib.request.Request(
                source_url + "?" + urllib.parse.urlencode(params),
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/", "Accept": "application/json,text/plain,*/*"},
            )
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    raw = response.read()
                break
            except Exception as exc:
                last_error = exc
                time.sleep(min(1 + attempt, 3))
        if raw is None:
            raise RuntimeError(f"ETF quote page {page_number} failed: {type(last_error).__name__}: {last_error}")
        payload = json.loads(raw.decode("utf-8"))
        data = payload.get("data") or {}
        page = data.get("diff") or []
        total = int(data.get("total") or 0)
        if not page:
            break
        pages.extend(page)
        page_number += 1
    diff = list({str(row.get("f12")): row for row in pages if row.get("f12")}.values())
    retrieved = datetime.now(timezone.utc).isoformat()
    records = []
    for row in diff:
        name = str(row.get("f14") or "").strip()
        symbol = str(row.get("f12") or "").strip()
        if not symbol or not name:
            continue
        record = {
            "symbol": symbol, "exchange": "SSE" if int(row.get("f13") or 0) == 1 else "SZSE",
            "name": name, **classify_etf(name), "market_scope": market_scope(name),
            "price": row.get("f2"), "change_pct": row.get("f3"), "volume_lots": row.get("f5"),
            "amount": row.get("f6"), "turnover_pct": row.get("f8"), "volume_ratio": row.get("f10"),
            "market_cap": row.get("f20"), "float_market_cap": row.get("f21"),
            "main_net_inflow": row.get("f62"), "main_net_inflow_pct": row.get("f184"),
            "super_large_net_inflow": row.get("f66"), "super_large_net_inflow_pct": row.get("f69"),
            "large_net_inflow": row.get("f72"), "large_net_inflow_pct": row.get("f75"),
            "quote_epoch": row.get("f124"),
        }
        records.append(record)
    snapshot = {
        "schema_version": 2, "retrieved_at_utc": retrieved,
        "source": source_url, "source_market_total": total,
        "classification_status": "market universe from ETF quote boards; per-record asset/subtype labels are name-inferred and require contract verification",
        "flow_definition": "Eastmoney f62/f184 main flow plus f66/f69 super-large and f72/f75 large-order transaction-size estimates; not ETF creations/redemptions and not disclosed institutional holdings",
        "records": records,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"etf_snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    encoded = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(encoded)
    manifest = {"path": str(path.resolve()), "sha256": hashlib.sha256(encoded).hexdigest(), "records": len(records), "retrieved_at_utc": retrieved}
    (output_dir / "latest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest

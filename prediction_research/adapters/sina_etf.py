from __future__ import annotations

import hashlib
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..taxonomy import classify_etf, market_scope


LIST_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData?page={page}&num=100&sort=symbol&asc=1&node=etf_hq_fund"
)
FLOW_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "MoneyFlow.ssl_qsfx_zjlrqs?page=1&num=2&sort=opendate&asc=0&daima={symbol}"
)
SINA_REFERER = "https://finance.sina.com.cn"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
SHANGHAI = timezone(timedelta(hours=8))

# Sina's MoneyFlow endpoint only exposes the main-flow aggregate and the
# super-large leg. There is no r1/r2/r3 breakdown, so large/medium/small net
# inflow fields are emitted as null and must never be inferred.
DEGRADED_FIELDS = (
    "volume_ratio",
    "large_net_inflow",
    "large_net_inflow_pct",
    "medium_net_inflow",
    "medium_net_inflow_pct",
    "small_net_inflow",
    "small_net_inflow_pct",
)


def _opener():
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )


def _get(url: str, timeout: int = 20, attempts: int = 3) -> bytes:
    last_error: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": SINA_REFERER,
            },
        )
        try:
            with _opener().open(request, timeout=timeout) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - retry any transport failure
            last_error = exc
            time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"sina request failed: {type(last_error).__name__}: {last_error}")


def _number(value):
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def fetch_etf_list() -> list[dict]:
    rows: list[dict] = []
    page = 1
    while page <= 40:
        raw = _get(LIST_URL.format(page=page))
        text = raw.decode("utf-8", "replace").strip()
        if not text or text in ("null", "[]"):
            break
        batch = json.loads(text)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    if not rows:
        raise RuntimeError("sina ETF universe returned no rows")
    return rows


def fetch_money_flow(symbol: str) -> dict | None:
    raw = _get(FLOW_URL.format(symbol=urllib.parse.quote(symbol)), timeout=15)
    text = raw.decode("utf-8", "replace").strip()
    if not text or text in ("null", "[]"):
        return None
    payload = json.loads(text)
    return payload[0] if payload else None


def fetch_money_flows(symbols: list[str], workers: int = 4) -> tuple[dict, list[str]]:
    flows: dict[str, dict] = {}
    failures: list[str] = []

    def work(symbol: str):
        try:
            return symbol, fetch_money_flow(symbol), None
        except Exception as exc:  # noqa: BLE001 - partial coverage is acceptable
            return symbol, None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for symbol, record, error in pool.map(work, symbols):
            if record:
                flows[symbol] = record
            elif error:
                failures.append(f"{symbol}: {error}")
            else:
                failures.append(f"{symbol}: empty payload")
    return flows, failures


def _session_epoch(flows: dict) -> tuple[int, str | None]:
    dates = sorted({str(record.get("opendate") or "") for record in flows.values() if record.get("opendate")})
    if not dates:
        raise RuntimeError("sina money flow returned no trade date; cannot stamp quote_epoch")
    trade_date = dates[-1]
    stamp = datetime.strptime(trade_date, "%Y-%m-%d").replace(hour=15, minute=0, second=0, tzinfo=SHANGHAI)
    return int(stamp.timestamp()), trade_date


def fetch_etf_snapshot(output_dir: Path, fallback_reason: str | None = None) -> dict:
    """Build an ETF snapshot from Sina when the Eastmoney quote API is unreachable.

    Degradation is explicit and machine-readable: only the aggregate main flow
    and the super-large leg are available, so order-size divergence between
    super-large and large orders cannot be computed from this source.
    """
    universe = fetch_etf_list()
    symbols = [str(row.get("symbol") or "").strip() for row in universe]
    symbols = [symbol for symbol in symbols if symbol]
    flows, failures = fetch_money_flows(symbols)
    quote_epoch, trade_date = _session_epoch(flows)

    records = []
    for row in universe:
        symbol = str(row.get("symbol") or "").strip()
        code = str(row.get("code") or "").strip()
        name = str(row.get("name") or "").strip()
        if not symbol or not code or not name:
            continue
        flow = flows.get(symbol) or {}
        market_cap_wan = _number(row.get("mktcap"))
        float_cap_wan = _number(row.get("nmc"))
        volume = _number(row.get("volume"))
        ratio_amount = _number(flow.get("ratioamount"))
        r0_ratio = _number(flow.get("r0_ratio"))
        records.append({
            "symbol": code,
            "exchange": "SSE" if symbol.lower().startswith("sh") else "SZSE",
            "name": name, **classify_etf(name), "market_scope": market_scope(name),
            "price": _number(row.get("trade")), "change_pct": _number(row.get("changepercent")),
            "volume_lots": round(volume / 100.0, 2) if volume is not None else None,
            "amount": _number(row.get("amount")), "turnover_pct": _number(row.get("turnoverratio")),
            "volume_ratio": None,
            "market_cap": market_cap_wan * 10000.0 if market_cap_wan is not None else None,
            "float_market_cap": float_cap_wan * 10000.0 if float_cap_wan is not None else None,
            "main_net_inflow": _number(flow.get("netamount")),
            "main_net_inflow_pct": ratio_amount * 100.0 if ratio_amount is not None else None,
            "super_large_net_inflow": _number(flow.get("r0_net")),
            "super_large_net_inflow_pct": r0_ratio * 100.0 if r0_ratio is not None else None,
            "large_net_inflow": None, "large_net_inflow_pct": None,
            "medium_net_inflow": None, "medium_net_inflow_pct": None,
            "small_net_inflow": None, "small_net_inflow_pct": None,
            "quote_epoch": quote_epoch,
        })

    with_flow = sum(1 for row in records if row["main_net_inflow"] is not None)
    retrieved = datetime.now(timezone.utc).isoformat()
    snapshot = {
        "schema_version": 2, "retrieved_at_utc": retrieved,
        "source": "sina:Market_Center.getHQNodeData(etf_hq_fund) + MoneyFlow.ssl_qsfx_zjlrqs",
        "source_market_total": len(universe),
        "fallback": {
            "used": True,
            "reason": (fallback_reason or "eastmoney quote api unavailable")[:400],
            "primary_source": "eastmoney push2 push2*/api/qt/clist/get",
            "degraded_fields": list(DEGRADED_FIELDS),
            "degradation_note": (
                "Sina exposes only main net flow and the super-large leg. Order-size divergence "
                "between super-large and large orders and any medium/small breakdown are unavailable "
                "and are emitted as null rather than inferred."
            ),
            "unit_notes": "mktcap/nmc converted from 万元 to 元; volume converted from 股 to 手; ratioamount/r0_ratio (ratios) scaled by 100 to percent",
            "quote_epoch_basis": f"session close 15:00 Asia/Shanghai on {trade_date}",
            "flow_coverage": {"records": len(records), "with_flow": with_flow,
                              "missing_flow": len(records) - with_flow, "failures_sample": failures[:10]},
        },
        "classification_status": "market universe from Sina ETF quote board; per-record asset/subtype labels are name-inferred and require contract verification",
        "flow_definition": "Sina MoneyFlow netamount/ratioamount main flow plus r0_net super-large leg; large/medium/small order-size layers are NOT available from this source; not ETF creations/redemptions and not disclosed institutional holdings",
        "records": records,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"etf_snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    encoded = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(encoded)
    manifest = {
        "path": str(path.resolve()), "sha256": hashlib.sha256(encoded).hexdigest(),
        "records": len(records), "retrieved_at_utc": retrieved,
        "source": snapshot["source"], "fallback_used": True, "with_flow": with_flow,
    }
    (output_dir / "latest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[sina_etf] DEGRADED FALLBACK SNAPSHOT: {len(records)} records, "
        f"{with_flow} with main flow, trade date {trade_date}. "
        f"Missing layers: {', '.join(DEGRADED_FIELDS)}",
        file=sys.stderr,
    )
    return manifest

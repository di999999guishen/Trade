"""
机构 13F 持仓变动流 (Institutional 13F Flow)
============================================
解决痛点：桥水/伯克希尔/高盛这类大机构的持仓大变动，应当作为独立新闻流接入 TradingAgents。

做法（无需 API key，仅依赖 SEC EDGAR 公开接口 + 官方要求的 User-Agent）：
  1. 用 `data.sec.gov/submissions/CIK{cik}.json` 取该机构最近两次 13F-HR 申报。
  2. 取各自的 `form13fInfoTable.xml`，解析持仓（发行人 / CUSIP / 市值(千美元) / 股数）。
  3. 对比最新 vs 前次，算出「增持榜 / 减持榜」，并按阈值标记「大变动」。
  4. 用 RELEVANCE_MAP 把变动关联到用户的 ETF（如高盛砍能源ETF→515220；桥水加黄金矿→512400）。

开源参考：SEC EDGAR submissions API + 13F XML schema；本仓库 tradingagents/dataflows/vendors/sec_edgar.py
的 User-Agent / 缓存模式被复用。解析思路与开源 `edgar`(dgzmedia) 的 13F 处理一致。

网络容错： submissions / XML 任一失败均降级（标记 unavailable），不中断整体。
"""

from __future__ import annotations

import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

# 🔧 代理绕过（必须在网络请求前设置）：本机环境所有出站走代理，sec.gov 需直连
_BYPASS_DOMAINS = "sec.gov,data.sec.gov,www.sec.gov"
for _key in ("NO_PROXY", "no_proxy"):
    _existing = os.environ.get(_key, "")
    if _existing and _existing != "localhost,127.0.0.1,::1":
        missing = [d for d in _BYPASS_DOMAINS.split(",") if d not in _existing]
        if missing:
            os.environ[_key] = f"{_existing},{','.join(missing)}"
    else:
        os.environ[_key] = f"localhost,127.0.0.1,::1,{_BYPASS_DOMAINS}"

_BASE = Path(__file__).resolve().parent.parent   # news_aggregator/
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

import requests  # noqa: E402

from flow_config import (  # noqa: E402
    INSTITUTIONS, BIG_CHANGE_VALUE_USD, BIG_CHANGE_PCT_PORTFOLIO, RELEVANCE_MAP,
)

_CACHE_DIR = _BASE / ".cache" / "sec_edgar"
_CACHE_TTL = 24 * 60 * 60
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


def _user_agent() -> str:
    return os.getenv("SEC_EDGAR_USER_AGENT",
                     "TradingAgents-NewsFlow/1.0 (liu.yuwei@example.com)")


def _get(url: str) -> requests.Response:
    return requests.get(url, headers={"User-Agent": _user_agent()}, timeout=30)


def _cached_get(url: str, name: str) -> bytes | None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / name  # noqa
    if path.exists() and time.time() - path.stat().st_mtime < _CACHE_TTL:
        return path.read_bytes()
    try:
        resp = _get(url)
        resp.raise_for_status()
        data = resp.content
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.rename(path)
        return data
    except Exception as e:
        print(f"  [WARN] 取 {url} 失败: {str(e)[:100]}")
        return None


def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _parse_infotable(xml_bytes: bytes) -> dict[str, dict]:
    """解析 form13fInfoTable.xml → {cusip: {issuer, value(USD), shares}}。

    两个 SEC schema 差异必须处理：
    1) 股数标签 2023 年后是小写 ``sshPrnamt``（旧 filing 是 ``sshPrnAmt``），
       按小写比较才能兼容两代；
    2) value 单位不固定：2023-01 后的 filing 直接报美元，更早的报千美元。
       用「每股价格中位数」自动校准，避免整体放大/缩小 1000 倍。
    """
    root = ET.fromstring(xml_bytes)
    out: dict[str, dict] = {}
    for info in root.iter():
        if _local(info.tag).lower() != "infotable":
            continue
        d: dict[str, Any] = {}
        pending_amt: int | None = None
        for child in info.iter():
            ln = _local(child.tag).lower()
            if ln == "nameofissuer":
                d["nameOfIssuer"] = (child.text or "").strip()
            elif ln == "cusip":
                d["cusip"] = (child.text or "").strip()
            elif ln == "value":
                try:
                    d["value"] = float(child.text or 0)
                except Exception:
                    d["value"] = 0.0
            elif ln == "sshprnamt":
                try:
                    pending_amt = int(float(child.text or 0))
                except Exception:
                    pending_amt = None
            elif ln == "sshprnamttype":
                # 只认 SH（股份），忽略 PRN（本金）
                if (child.text or "").strip().upper() == "SH" and pending_amt is not None:
                    d["shares"] = pending_amt
                pending_amt = None
        if d.get("cusip"):
            c = d["cusip"]
            if c in out:
                # 同一 CUSIP 会在多行出现（不同子公司/账户/投票权），必须累加。
                # 用 dict 覆盖会丢掉 60~75% 的持仓（伯克希尔 89 行只剩 29 个 CUSIP）。
                out[c]["value"] = out[c].get("value", 0.0) + d.get("value", 0.0)
                out[c]["shares"] = out[c].get("shares", 0) + d.get("shares", 0)
                if d.get("nameOfIssuer") and not out[c].get("nameOfIssuer"):
                    out[c]["nameOfIssuer"] = d["nameOfIssuer"]
            else:
                out[c] = d

    # ---- 单位自动校准：用 value/shares 的中位数判断是美元还是千美元 ----
    ratios = sorted(
        v["value"] / v["shares"] for v in out.values()
        if v.get("shares") and v.get("value")
    )
    if ratios:
        median_price = ratios[len(ratios) // 2]
        # 正常股价约 $0.5~$20000；中位数远超此区间说明单位是千美元
        if median_price > 20000:
            for v in out.values():
                v["value"] = v.get("value", 0.0) * 1000
    return out


def _filing_dir_url(cik: str, accession: str) -> str:
    cik_int = str(int(cik))                     # 去前导零
    acc_nodash = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/"


def _infotable_url(cik: str, accession: str) -> str | None:
    """从 filing 目录索引里定位 13F 持仓明细 XML（文件名各家不固定）。"""
    base = _filing_dir_url(cik, accession)
    data = _cached_get(base + "index.json", f"idx_{cik}_{accession.replace('-', '')}.json")
    if data is None:
        # 退回到最常见的文件名
        return base + "form13fInfoTable.xml"
    try:
        items = __import__("json").loads(data).get("directory", {}).get("item", [])
    except Exception:
        return base + "form13fInfoTable.xml"
    # 优先挑名字里带 infotable 的 xml，其次任何 xml
    names = [it.get("name", "") for it in items]
    for n in names:
        if n.lower().endswith(".xml") and "infotable" in n.lower():
            return base + n
    for n in names:
        if n.lower().endswith(".xml") and "13f" in n.lower():
            return base + n
    for n in names:
        if n.lower().endswith(".xml"):
            return base + n
    return None


def _fetch_holdings(cik: str, accession: str) -> dict[str, dict] | None:
    url = _infotable_url(cik, accession)
    if not url:
        return None
    name = f"hold_{cik}_{accession.replace('-', '')}.xml"
    data = _cached_get(url, name)
    if data is None:
        return None
    try:
        return _parse_infotable(data)
    except Exception as e:
        print(f"  [WARN] 解析 {url} 失败: {str(e)[:80]}")
        return None


def _latest_13f(cik: str) -> list[dict] | None:
    """返回该机构最近的 13F-HR 申报列表（按申报日倒序，最多取前2条）。"""
    url = _SUBMISSIONS_URL.format(cik=cik)
    name = f"submissions_{cik}.json"
    data = _cached_get(url, name)
    if data is None:
        return None
    try:
        j = __import__("json").loads(data)
    except Exception:
        return None
    recent = j.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accs = recent.get("accessionNumber", [])
    fdates = recent.get("filingDate", [])
    # SEC submissions JSON 的字段名是 reportDate（periodOfReport 不存在，
    # 用它会让 zip 被空列表截断成 0 条，导致静默取不到任何 13F）
    periods = recent.get("reportDate") or recent.get("periodOfReport", [])
    hits = []
    for f, a, fd, pr in zip(forms, accs, fdates, periods):
        if f in ("13F-HR", "13F-HR/A"):
            hits.append({"accession": a, "filingDate": fd, "periodOfReport": pr, "form": f})
    hits.sort(key=lambda x: x["filingDate"], reverse=True)
    # 同一报告期只保留最新一次申报（原始申报被 13F-HR/A 修正时取修正版）
    seen_period: set[str] = set()
    unique: list[dict] = []
    for h in hits:
        p = h.get("periodOfReport")
        if p and p in seen_period:
            continue
        if p:
            seen_period.add(p)
        unique.append(h)
    return unique[:2]


def _relevance(issuer: str) -> list[str]:
    ups = (issuer or "").upper()
    etfs = []
    for m in RELEVANCE_MAP:
        if any(k.upper() in ups for k in m["keywords"]):
            etfs.append(f"{m['etf']}({m['note']})")
    return etfs


def _analyze_institution(inst: dict) -> dict[str, Any]:
    cik = inst["cik"]
    hits = _latest_13f(cik)
    if not hits or len(hits) < 1:
        return {"key": inst["key"], "name": inst["name"], "ok": False,
                "error": "未取到 13F 申报（网络/CIK 问题）", "adds": [], "cuts": [], "top": []}
    latest = hits[0]
    prior = hits[1] if len(hits) > 1 else None

    latest_h = _fetch_holdings(cik, latest["accession"])
    if latest_h is None:
        return {"key": inst["key"], "name": inst["name"], "ok": False,
                "error": "持仓 XML 解析失败", "adds": [], "cuts": [], "top": []}
    prior_h = _fetch_holdings(cik, prior["accession"]) if prior else {}

    total_latest = sum(v.get("value", 0) for v in latest_h.values())
    total_prior = sum(v.get("value", 0) for v in prior_h.values()) if prior_h else 0

    rows = []
    if prior_h:
        all_cusips = set(latest_h) | set(prior_h)
        for c in all_cusips:
            lv = latest_h.get(c, {}).get("value", 0)
            pv = prior_h.get(c, {}).get("value", 0)
            issuer = latest_h.get(c, {}).get("nameOfIssuer") or prior_h.get(c, {}).get("nameOfIssuer") or "?"
            shares = latest_h.get(c, {}).get("shares", 0)
            delta = lv - pv
            if delta == 0:
                continue
            rows.append({
                "issuer": issuer, "cusip": c, "value_latest": lv, "value_prior": pv,
                "delta": delta, "shares": shares,
                "pct_portfolio": (lv / total_latest * 100) if total_latest else 0,
                "relevance": _relevance(issuer),
                "big": (abs(delta) >= BIG_CHANGE_VALUE_USD) or
                       (total_latest and abs(delta) / total_latest * 100 >= BIG_CHANGE_PCT_PORTFOLIO),
            })
    else:
        for c, v in latest_h.items():
            rows.append({
                "issuer": v.get("nameOfIssuer", "?"), "cusip": c,
                "value_latest": v.get("value", 0), "value_prior": 0, "delta": v.get("value", 0),
                "shares": v.get("shares", 0),
                "pct_portfolio": (v.get("value", 0) / total_latest * 100) if total_latest else 0,
                "relevance": _relevance(v.get("nameOfIssuer", "")),
                "big": v.get("value", 0) >= BIG_CHANGE_VALUE_USD,
            })

    adds = sorted([r for r in rows if r["delta"] > 0], key=lambda r: r["delta"], reverse=True)[:8]
    cuts = sorted([r for r in rows if r["delta"] < 0], key=lambda r: r["delta"])[:8]
    top = sorted(latest_h.values(), key=lambda v: v.get("value", 0), reverse=True)[:15]
    top_list = [{"issuer": v.get("nameOfIssuer", "?"), "value": v.get("value", 0),
                 "pct": (v.get("value", 0) / total_latest * 100) if total_latest else 0,
                 "relevance": _relevance(v.get("nameOfIssuer", ""))} for v in top]

    return {
        "key": inst["key"], "name": inst["name"], "ok": True,
        "filing_date": latest["filingDate"], "report_date": latest.get("periodOfReport"),
        "prior_filing_date": prior.get("filingDate") if prior else None,
        "total_value": total_latest, "prior_total_value": total_prior,
        "position_count": len(latest_h),
        "adds": adds, "cuts": cuts, "top": top_list,
        "big_changes": [r for r in (adds + cuts) if r["big"]],
    }


def fetch_institutional_flow() -> dict[str, Any]:
    print("\n[机构13F流] 抓取桥水/伯克希尔/高盛最新持仓变动...")
    results = []
    for inst in INSTITUTIONS:
        if not inst.get("active", True):
            continue
        print(f"  ▶ {inst['name']} (CIK {inst['cik']}) ...")
        r = _analyze_institution(inst)
        if r["ok"]:
            nbig = len(r["big_changes"])
            print(f"    持仓 {r['position_count']} 只，总市值 ${r['total_value']/1e9:.1f}B，"
                  f"大变动 {nbig} 条")
        else:
            print(f"    ⚠️ {r['error']}")
        results.append(r)

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "institutions": results,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(fetch_institutional_flow(), ensure_ascii=False, indent=2, default=str))

"""单独跑 159792.SZ 港股通互联网ETF富国 / 159740.SZ 恒生科技ETF大成 / 159158.SZ 电力ETF景顺。

【数据源与降级声明】
声明1 — 基本面/新闻全国内源：fundamental_data/get_news 一律 eastmoney；insider 注册 cn_unavailable no-op。
声明2 — 结算价格源全国内（Sina A股/US + 腾讯 HK + akshare 恒指），预算 600s。
声明3 — 跨境溢价每日动态注入：159792/159740 为港股跨境ETF，须先算同日溢价再谈方向
   （>5% 否决、≥3% 提示）。口径：境内 price_date 收盘 / NAV(price_date) - 1。
   价格取 price_date(上一交易日) 收盘（sina kline，避免盘中实时价偏差）；NAV 取 akshare fund_etf_fund_daily_em。
   东财 push2his 港股指数代理本机被 WAF 阻断，且 NAV 与 price_date 同日故无需滚动代理（factor=1）。
声明4 — 159158 电力ETF为纯A股ETF，无跨境溢价，按技术/基本面评估，不注入溢价证据。

用法：.venv/Scripts/python.exe run_3.py
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJECT = Path(r"E:/Trade/TradingAgents")
sys.path.insert(0, str(PROJECT))
try:
    import wmi_guard  # noqa: F401
except Exception:
    pass
import pandas as pd  # noqa: E402
import requests  # noqa: E402

import daily_predict as dp  # noqa: E402
from tradingagents.dataflows import router as R  # noqa: E402

SETTLE_BUDGET_SECONDS = float(os.environ.get("SETTLE_BUDGET_SECONDS", "600"))
TICKERS = ["159792.SZ", "159740.SZ", "159158.SZ"]
NAMES = {"159792": "港股通互联网ETF富国", "159740": "恒生科技ETF大成", "159158": "电力ETF景顺"}
CROSS = {"159792.SZ", "159740.SZ"}  # 跨境港股ETF，需算溢价

S = requests.Session()
S.trust_env = False


# ════════════════ 溢价计算数据层（同日口径） ════════════════
def recent_cn_trading_day(now=None):
    now = now or datetime.now()
    d = now.date() if now.hour >= 15 else now.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def fetch_close_prices(price_date):
    """{code: (name, close)}，sina kline 取 <=price_date 最近交易日收盘。"""
    out = {}
    for code in ("159792", "159740", "159158"):
        try:
            r = S.get("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                      "CN_MarketData.getKLineData",
                      params={"symbol": f"sz{code}", "scale": 240, "ma": "no", "datalen": 12},
                      timeout=20)
            arr = json.loads(r.text)
            for x in reversed(arr):
                if x["day"][:10] <= price_date:
                    out[code] = (NAMES[code], float(x["close"]))
                    break
        except Exception as exc:
            print(f"[premium] sina kline {code} failed: {exc}", flush=True)
    return out


def fetch_navs():
    out = {}
    try:
        import akshare as ak
        df = ak.fund_etf_fund_daily_em()
        cols = [c for c in df.columns if "单位净值" in c]
        sub = df[df["基金代码"].astype(str).isin(["159792", "159740", "159158"])]
        for _, r in sub.iterrows():
            code = str(r["基金代码"])
            for c in sorted(cols, reverse=True):
                v = str(r[c]).strip()
                if v and v not in ("---", "nan", "None"):
                    out[code] = (c.replace("-单位净值", ""), float(v))
                    break
    except Exception as exc:
        print(f"[premium] nav_daily failed: {exc}", flush=True)
    return out


def verdict(p):
    return "否决(>5%)" if p > 0.05 else ("提示(3-5%)" if p >= 0.03 else ("观察(0-3%)" if p >= 0 else "折价"))


def build_evidence(rows, price_date):
    lines = [
        "",
        "【跨境港股ETF溢价证据 — 每日动态重算，注入纪律：先算溢价再谈方向】",
        "",
        f"数据时点：境内 price_date={price_date} 收盘；NAV={price_date}（T+1披露，与价格同日故无滚动）。",
        "口径：溢价 = 境内price_date收盘 / NAV(price_date) - 1。",
        "",
    ]
    for r in rows:
        lines.append(
            f"- {r['ticker']} {r['name']}：收盘 {r['price']:.4f}｜NAV {r['nav_date']} {r['nav']:.4f}"
            f"｜**溢价 {r['premium']*100:+.2f}%**｜判定 {verdict(r['premium'])}")
    lines += [
        "",
        "项目纪律（不可违背）：",
        "1. 溢价 >5% 直接否决买入；≥3% 必须在结论中提示。",
        "2. 指数方向与载体分开评估：可以看多港股科技/互联网，但「买入这只场内ETF」是另一决策。",
        "3. 溢价数学含义：+X% 意味着指数原地不动就亏损约 X%；需指数先涨同等幅度持有人才回本。",
        "4. 跨境ETF溢价通常伴随外汇额度紧张/暂停申购；收敛路径多为境内价格补跌。",
        "5. 替代：场外联接按净值申购无溢价，但当前多数日限额仅5-10元或暂停，建仓受限。",
        "",
        "硬性要求：Buy/Overweight 须显式算「指数预期涨幅能否覆盖溢价」；含 premium_pct 与 premium_verdict 字段。",
    ]
    return "\n".join(lines)


def collect_premium_evidence():
    price_date = recent_cn_trading_day()
    prices = fetch_close_prices(price_date)
    navs = fetch_navs()
    rows, summary = [], {}
    for tk in TICKERS:
        code = tk.split(".")[0]
        if tk not in CROSS:
            continue
        if code not in prices or code not in navs:
            print(f"[premium] {tk} 缺失 prices={code in prices} navs={code in navs}", flush=True)
            continue
        name, close = prices[code]
        nav_date, nav = navs[code]
        prem = close / nav - 1
        row = {"ticker": tk, "name": name, "price": close, "nav": nav,
               "nav_date": nav_date, "premium": prem}
        rows.append(row)
        summary[tk] = row
        print(f"[premium] {tk} {name}: 收盘={close:.4f} NAV={nav:.4f} 溢价={prem*100:+.2f}% {verdict(prem)}", flush=True)
    if not rows:
        return ("\n【跨境港股ETF溢价数据不可得】按项目纪律 >5% 否决处理：数据未能获取，"
                "严禁给出买入结论；方向判断须显式声明溢价缺失。", {}), price_date
    ev = build_evidence(rows, price_date)
    ev += ("\n\n【159158.SZ 电力ETF景顺】纯A股行业ETF，无跨境溢价，按技术面/基本面/资金流评估，不受上述溢价纪律约束。"
           if "159158.SZ" in TICKERS else "")
    return ev, summary


# ════════════════ 基础设施（同 run_qdii_daily.py） ════════════════
INSIDER_VENDOR = "cn_unavailable"


def _cn_insider_unavailable(ticker, trade_date=None):
    return (f"DATA_UNAVAILABLE: 「内部人交易」数据在可用国内数据源（东方财富/新浪/akshare）中没有对应接口；"
            f"按本项目数据源约束不启用 yfinance/alpha_vantage。请把该项报告为不可得，不要估算、不要编造、"
            f"不要据此推断方向。(ticker={ticker})")


R.VENDOR_METHODS["get_insider_transactions"][INSIDER_VENDOR] = _cn_insider_unavailable
_orig_build_ticker_config = dp.build_ticker_config
_EVIDENCE, _SUMMARY = collect_premium_evidence()


def _domestic_build_ticker_config(base_config, ticker, snapshot):
    cfg = _orig_build_ticker_config(base_config, ticker, snapshot)
    cfg.setdefault("data_vendors", {})["fundamental_data"] = "eastmoney"
    tv = cfg.setdefault("tool_vendors", {})
    tv["get_news"] = "eastmoney"
    tv["get_insider_transactions"] = INSIDER_VENDOR
    cfg["screen_evidence_context"] = cfg.get("screen_evidence_context", "") + _EVIDENCE
    return cfg


dp.build_ticker_config = _domestic_build_ticker_config
dp.MANDATORY_DAILY_TICKERS = ()

from tradingagents.memory import settlement  # noqa: E402
from tradingagents.dataflows.symbols import normalize_symbol  # noqa: E402

_HK_INDEX_ALIASES = {"^HSI", "HSI"}


def _series_from(df, date_col, close_col, start_date, end_date):
    if df is None or len(df) == 0:
        return pd.Series(dtype=float)
    dates = pd.to_datetime(df[date_col])
    w = df.assign(_d=dates)
    w = w[(w["_d"] >= pd.Timestamp(start_date)) & (w["_d"] < pd.Timestamp(end_date))]
    w = w.sort_values("_d")
    if w.empty:
        return pd.Series(dtype=float)
    return pd.Series(w[close_col].astype(float).to_numpy(), index=pd.DatetimeIndex(w["_d"]))


def _domestic_closes(symbol: str, start_date: str, end_date: str) -> pd.Series:
    sym = normalize_symbol(symbol)
    if sym.upper() in _HK_INDEX_ALIASES:
        try:
            import akshare as ak
            df = ak.stock_hk_index_daily_sina(symbol="HSI")
            return _series_from(df, "date", "close", start_date, end_date)
        except Exception as exc:
            print(f"[settle] HK index unavailable ({sym}): {exc}", flush=True)
            return pd.Series(dtype=float)
    return settlement._cn_closes(symbol, start_date, end_date)


settlement._closes = _domestic_closes
_orig_settle_pending = settlement.settle_pending
_deadline = {"t": None}


def _bounded_settle_pending(ticker, memory_log, reflector, config, wait=True):
    if _deadline["t"] is None:
        _deadline["t"] = time.monotonic() + SETTLE_BUDGET_SECONDS
    if time.monotonic() > _deadline["t"]:
        return settlement.Settlement()
    t0 = time.time()
    res = _orig_settle_pending(ticker, memory_log, reflector, config, wait=wait)
    if res.settled or res.failed:
        print(f"[settle] {ticker}: settled={len(res.settled)} failed={len(res.failed)} ({time.time()-t0:.1f}s)", flush=True)
    return res


settlement.settle_pending = _bounded_settle_pending


def preflight(tickers):
    base = {"output_language": "Chinese",
            "data_vendors": {"core_stock_apis": "akshare", "technical_indicators": "akshare",
                             "fundamental_data": "eastmoney", "news_data": "eastmoney",
                             "macro_data": "akshare_macro", "prediction_markets": "polymarket"},
            "tool_vendors": {}}
    fatal = []
    for tk in tickers:
        cfg = dp.build_ticker_config(base, tk, None)
        dv, tv = cfg.get("data_vendors", {}), cfg.get("tool_vendors", {})
        for cat, info in R.TOOLS_CATEGORIES.items():
            for meth in info["tools"]:
                chain = tv.get(meth, dv.get(cat, "default"))
                explicit = [v.strip() for v in str(chain).split(",") if v.strip() and v.strip() != "default"]
                avail = list(R.VENDOR_METHODS[meth].keys())
                if explicit and not [v for v in explicit if v in avail]:
                    fatal.append((tk, meth, explicit, avail))
    return fatal


if __name__ == "__main__":
    os.environ.setdefault("DAILY_TICKER", ",".join(TICKERS))
    tickers = dp.build_daily_tickers(os.getenv("DAILY_TICKER"))
    print(f"[runner] tickers = {tickers}", flush=True)
    print(f"[runner] premium evidence:\n{_EVIDENCE}", flush=True)
    bad = preflight(tickers)
    if bad:
        for tk, meth, exp, av in bad:
            print(f"[preflight] FATAL {tk} / {meth}: configured={exp} available={av}", flush=True)
        raise SystemExit("preflight failed: vendor chain(s) would raise at call time")
    print("[preflight] OK - all vendor chains resolvable", flush=True)
    raise SystemExit(dp.main())

"""Domestic (China) data vendor built on Eastmoney / akshare.

Replaces the rate-limited / unavailable foreign sources for the daily A-share
ETF pipeline:

  - news        : Eastmoney per-ticker news (``ak.stock_news_em``) instead of
                  Yahoo Finance news (rate-limited).
  - global news : Eastmoney global financial flash news (``ak.stock_info_global_em``)
                  instead of ``yf.Search``.
  - macro       : Chinese macro series (CPI / PPI / PMI / M2 / GDP / LPR) from
                  akshare instead of FRED (which requires an API key).
  - fundamentals: ETF spot snapshot (price, premium/discount, size, capital
                  flows) from ``ak.fund_etf_spot_em`` instead of yfinance, which
                  has no meaningful "fundamentals" for an ETF.

akshare is imported lazily (inside each function) so importing this module does
not pay akshare's heavy startup cost unless a domestic source is actually used.
"""

from __future__ import annotations

import functools
import logging
from datetime import datetime, timedelta

from .config import get_config
from .errors import NoMarketDataError

logger = logging.getLogger(__name__)

# Market-code suffixes (same convention as akshare_data.py).
_SUFFIX_MARKET = {
    ".SZ": "sz",
    ".SS": "sh",
    ".BJ": "bj",
    ".HK": "hk",
}

# Curated friendly aliases -> (akshare function name, date column, display name).
# Only China-oriented series that akshare exposes without a key are included, so
# an A-share analyst never silently gets US-centric numbers it did not ask for.
_MACRO_SERIES = {
    "cpi":             ("macro_china_cpi",          "月份",       "中国 CPI"),
    "consumer_price":  ("macro_china_cpi",          "月份",       "中国 CPI"),
    "ppi":             ("macro_china_ppi",          "月份",       "中国 PPI"),
    "pmi":             ("macro_china_pmi",          "月份",       "中国 PMI"),
    "manufacturing_pmi": ("macro_china_pmi",        "月份",       "中国 PMI"),
    "m2":              ("macro_china_money_supply", "月份",       "中国货币供应量 M2"),
    "money_supply":    ("macro_china_money_supply", "月份",       "中国货币供应量 M2"),
    "gdp":             ("macro_china_gdp",          "季度",       "中国 GDP"),
    "lpr":             ("macro_china_lpr",          "TRADE_DATE", "中国 LPR 贷款市场报价利率"),
    "loan_prime_rate": ("macro_china_lpr",          "TRADE_DATE", "中国 LPR 贷款市场报价利率"),
}

_MAX_MACRO_ROWS = 12


def _parse_symbol(symbol: str) -> tuple[str, str]:
    """Split a symbol like '510050.SS' into ('510050', 'sh')."""
    upper = symbol.strip().upper()
    for suffix, market in _SUFFIX_MARKET.items():
        if upper.endswith(suffix):
            return upper[: -len(suffix)], market
    return upper, "us"


def _news_symbol(code: str, market: str) -> str:
    """Map a code to the symbol ``ak.stock_news_em`` expects.

    A-share / US: bare code. HK: ``hk`` + zero-padded 5-digit code.
    """
    if market == "hk":
        return f"hk{code.zfill(5)}"
    return code


def _parse_pub_time(value) -> datetime | None:
    """Parse Eastmoney's publish-time string into an aware-naive datetime."""
    if value is None:
        return None
    ts = str(value).strip()
    if not ts or ts.lower() in ("nan", "none", ""):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def _in_window(pub: datetime | None, start_dt: datetime, end_dt: datetime) -> bool:
    """Half-open window [start, end+1day); undated rows kept for live runs only."""
    if pub is not None:
        return start_dt <= pub < end_dt + timedelta(days=1)
    # Undated article: keep only when the window reaches the present (live run).
    return end_dt >= datetime.now() - timedelta(days=1)


# ── News ─────────────────────────────────────────────────────────────────────

def get_news_eastmoney(
    ticker: str,
    start_date: str,
    end_date: str,
) -> str:
    """Per-ticker news from Eastmoney (``ak.stock_news_em``).

    Returns a formatted markdown string; raises ``NoMarketDataError`` on a
    genuine failure so the router can fall back to another vendor.
    """
    import akshare as ak

    code, market = _parse_symbol(ticker)
    symbol = _news_symbol(code, market)

    try:
        df = ak.stock_news_em(symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        raise NoMarketDataError(ticker, None, f"Eastmoney news error: {exc}") from exc

    if df is None or df.empty:
        return f"No news found for {ticker} (东方财富)"

    article_limit = get_config()["news_article_limit"]
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    lines: list[str] = []
    kept = 0
    for _, row in df.iterrows():
        title = str(row.get("新闻标题", "")).strip()
        if not title or title.lower() in ("nan", "none", ""):
            continue
        pub = _parse_pub_time(row.get("发布时间"))
        if not _in_window(pub, start_dt, end_dt):
            continue
        source = str(row.get("文章来源", "")).strip()
        summary = str(row.get("新闻内容", "")).strip()
        link = str(row.get("新闻链接", "")).strip()
        lines.append(f"### {title} (source: {source or '东方财富'})")
        if summary:
            lines.append(summary[:400])
        if link:
            lines.append(f"Link: {link}")
        lines.append("")
        kept += 1
        if kept >= article_limit:
            break

    if kept == 0:
        return f"No news found for {ticker} between {start_date} and {end_date} (东方财富)"

    return (
        f"## {ticker} News, from {start_date} to {end_date} (via 东方财富):\n\n"
        + "\n".join(lines)
    )


def get_global_news_eastmoney(
    curr_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    """Global/financial flash news from Eastmoney (``ak.stock_info_global_em``)."""
    import akshare as ak

    config = get_config()
    if look_back_days is None:
        look_back_days = config["global_news_lookback_days"]
    if limit is None:
        limit = config["global_news_article_limit"]

    try:
        df = ak.stock_info_global_em()
    except Exception as exc:  # noqa: BLE001
        raise NoMarketDataError(curr_date, None, f"Eastmoney global news error: {exc}") from exc

    if df is None or df.empty:
        return f"No global news found for {curr_date} (东方财富)"

    end_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=look_back_days)
    start_date = start_dt.strftime("%Y-%m-%d")

    lines: list[str] = []
    kept = 0
    for _, row in df.iterrows():
        title = str(row.get("标题", "")).strip()
        if not title or title.lower() in ("nan", "none", ""):
            continue
        pub = _parse_pub_time(row.get("发布时间"))
        if not _in_window(pub, start_dt, end_dt):
            continue
        summary = str(row.get("摘要", "")).strip()
        link = str(row.get("链接", "")).strip()
        lines.append(f"### {title}")
        if summary:
            lines.append(summary[:300])
        if link:
            lines.append(f"Link: {link}")
        lines.append("")
        kept += 1
        if kept >= limit:
            break

    if kept == 0:
        return f"No global news found between {start_date} and {curr_date} (东方财富)"

    return (
        f"## Global Market News, from {start_date} to {curr_date} (via 东方财富):\n\n"
        + "\n".join(lines)
    )


# ── Macro ────────────────────────────────────────────────────────────────────

def get_macro_data_akshare(
    indicator: str,
    curr_date: str,
    look_back_days: int | None = None,
) -> str:
    """Chinese macro series from akshare (CPI / PPI / PMI / M2 / GDP / LPR).

    Unknown aliases return guidance (matching the FRED vendor's contract) rather
    than raising, so a bad LLM-supplied indicator does not abort the run.
    """
    import akshare as ak

    key = indicator.strip().lower().replace(" ", "_").replace("-", "_")
    meta = _MACRO_SERIES.get(key)
    if meta is None:
        return (
            f"akshare: '{indicator}' 不是已知的中国宏观指标别名。"
            f"可用: cpi, ppi, pmi, m2, gdp, lpr。"
        )

    fn_name, date_col, label = meta
    try:
        df = getattr(ak, fn_name)()
    except Exception as exc:  # noqa: BLE001
        raise NoMarketDataError(indicator, None, f"akshare {fn_name} error: {exc}") from exc

    if df is None or df.empty:
        return f"akshare: {label} 无数据。"

    df = df.copy()
    if date_col in df.columns:
        df = df.sort_values(date_col, ascending=False)
    shown = df.head(_MAX_MACRO_ROWS)

    import pandas as pd

    header = (
        f"## {label} (via 东方财富/akshare)\n"
        f"- 数据截至: {curr_date}\n"
        f"- 最近 {len(shown)} 期:\n"
    )
    table = "| " + " | ".join(str(c) for c in shown.columns) + " |\n"
    table += "| " + " | ".join("---" for _ in shown.columns) + " |\n"
    for _, row in shown.iterrows():
        cells = []
        for v in row:
            try:
                cells.append("" if bool(pd.isna(v)) else str(v))
            except (TypeError, ValueError):
                cells.append(str(v))
        table += "| " + " | ".join(cells) + " |\n"

    return header + "\n" + table


# ── ETF fundamentals ─────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=1)
def _etf_spot_frame():
    """Fetch + cache the full A-share ETF spot snapshot (one call per process)."""
    import akshare as ak
    return ak.fund_etf_spot_em()


def _is_ashare_etf(ticker: str) -> bool:
    code, market = _parse_symbol(ticker)
    if market not in ("sh", "sz", "bj"):
        return False
    try:
        spot = _etf_spot_frame()
    except Exception:  # noqa: BLE001
        return False
    return (spot["代码"] == code).any()


def _etf_fundamentals_report(ticker: str) -> str:
    code, _market = _parse_symbol(ticker)
    spot = _etf_spot_frame()
    row = spot[spot["代码"] == code].iloc[0]

    def g(col, fmt="{:.4f}"):
        v = row.get(col)
        try:
            f = float(v)
            return fmt.format(f)
        except (TypeError, ValueError):
            return "" if v is None else str(v)

    name = str(row.get("名称", "")).strip()
    price = g("最新价")
    iopv = g("IOPV实时估值")
    premium = g("基金折价率")
    chg = g("涨跌幅")
    turnover = g("成交额", fmt="{:.2f}")
    shares = g("最新份额", fmt="{:.2f}")
    mcap = g("总市值", fmt="{:.2f}")
    data_date = str(row.get("数据日期", "")).strip()

    # 资金流流向拆解（按成交单大小五档：主力=超大单+大单）。
    # 每档同时给出净额（元）与净占比（%），正=净流入、负=净流出。
    flow_tiers = [
        ("主力", "主力净流入-净额", "主力净流入-净占比"),
        ("超大单", "超大单净流入-净额", "超大单净流入-净占比"),
        ("大单", "大单净流入-净额", "大单净流入-净占比"),
        ("中单", "中单净流入-净额", "中单净流入-净占比"),
        ("小单", "小单净流入-净额", "小单净流入-净占比"),
    ]
    flow_lines: list[str] = []
    direction_flags: list[str] = []
    for label, amount_col, pct_col in flow_tiers:
        amount = g(amount_col, fmt="{:.2f}")
        pct = g(pct_col, fmt="{:.2f}")
        flow_lines.append(f"| {label} | {amount} | {pct}% |")
        try:
            net = float(row.get(amount_col))
        except (TypeError, ValueError):
            continue
        if net > 0:
            direction_flags.append(f"{label}净流入")
        elif net < 0:
            direction_flags.append(f"{label}净流出")

    # 主力净额方向作为整体资金流向结论（超大单+大单合计口径）。
    try:
        main_net = float(row.get("主力净流入-净额"))
    except (TypeError, ValueError):
        main_net = 0.0
    if main_net > 0:
        overall = f"主力资金净流入（{g('主力净流入-净额', fmt='{:.2f}')} 元），资金面偏多"
    elif main_net < 0:
        overall = f"主力资金净流出（{g('主力净流入-净额', fmt='{:.2f}')} 元），资金面偏空"
    else:
        overall = "主力资金净额接近零，方向未明"

    return (
        f"## {ticker} 基本面（ETF，via 东方财富）\n\n"
        f"- 名称: {name}\n"
        f"- 最新价: {price} | 涨跌幅: {chg}%\n"
        f"- IOPV 实时估值: {iopv} | 折价率: {premium}%\n"
        f"- 成交额: {turnover} 元\n"
        f"- 最新份额: {shares} 份\n"
        f"- 总市值: {mcap} 元\n"
        f"- 数据日期: {data_date}\n\n"
        f"### 资金流流向（按成交单大小拆解）\n\n"
        f"| 档位 | 净额(元) | 净占比(%) |\n"
        f"|---|---:|---:|\n"
        + "\n".join(flow_lines)
        + "\n\n"
        f"资金流方向：{overall}；分档明细：{'、'.join(direction_flags) if direction_flags else '无有效数据'}。\n\n"
        f"说明: ETF 无传统公司财务报表（资产负债表/利润表/现金流）。"
        f"对 ETF 而言，折价率（相对 IOPV 的偏离）、成交额、资金流流向（主力/超大单/大单/中单/小单净额与净占比）和规模"
        f"是更直接的基本面观察指标。资金流按成交单大小估算，不等于 ETF 申购赎回或机构持仓。"
    )


def get_fundamentals_eastmoney(ticker: str, curr_date: str) -> str:
    """Comprehensive fundamentals for an A-share ETF; otherwise no data."""
    if not _is_ashare_etf(ticker):
        raise NoMarketDataError(ticker, None, "not an A-share ETF")
    return _etf_fundamentals_report(ticker)


def _etf_statement_not_applicable(ticker: str, statement: str) -> str:
    if _is_ashare_etf(ticker):
        return f"{ticker} 是 ETF，无 {statement}（不适用于基金产品）。"
    raise NoMarketDataError(ticker, None, "not an A-share ETF")


def get_balance_sheet_eastmoney(ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
    return _etf_statement_not_applicable(ticker, "资产负债表")


def get_cashflow_eastmoney(ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
    return _etf_statement_not_applicable(ticker, "现金流量表")


def get_income_statement_eastmoney(ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
    return _etf_statement_not_applicable(ticker, "利润表")


# ── Instrument identity (no yfinance) ────────────────────────────────────────

@functools.lru_cache(maxsize=256)
def resolve_cn_instrument_identity(ticker: str) -> dict:
    """Resolve name / type / exchange for an A-share or HK ticker via akshare.

    Used by ``resolve_instrument_identity`` to avoid yfinance (which rate-limits
    and returns unreliable identity for A-share ETFs). Best-effort: returns
    ``{}`` on any failure so callers fall back to ticker-only context.
    """
    code, market = _parse_symbol(ticker)
    if market not in ("sh", "sz", "bj", "hk"):
        return {}

    import akshare as ak

    identity: dict[str, str] = {}
    exchange_name = {
        "sh": "SSE (上交所)",
        "sz": "SZSE (深交所)",
        "bj": "BSE (北交所)",
        "hk": "HKEX (港交所)",
    }.get(market, market.upper())

    # 1) ETF (covers 510050.SS / 159063.SZ and the rest of the A-share ETF pool).
    try:
        spot = _etf_spot_frame()
        match = spot[spot["代码"] == code]
        if not match.empty:
            name = str(match.iloc[0].get("名称", "")).strip()
            if name:
                identity["company_name"] = name
                identity["quote_type"] = "ETF"
                identity["exchange"] = exchange_name
                return identity
    except Exception:  # noqa: BLE001
        pass

    # 2) A-share stock (name / industry via Eastmoney).
    if market in ("sh", "sz", "bj"):
        try:
            info = ak.stock_individual_info_em(symbol=code)
            if info is not None and not info.empty:
                d = dict(zip(info["item"].astype(str), info["value"].astype(str)))
                name = str(d.get("股票简称", "")).strip()
                if name:
                    identity["company_name"] = name
                industry = str(d.get("行业", "")).strip()
                if industry:
                    identity["industry"] = industry
                identity["quote_type"] = "STOCK"
                identity["exchange"] = exchange_name
        except Exception:  # noqa: BLE001
            pass

    return identity

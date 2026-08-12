"""Domestic Chinese stock data provider using Sina + Tencent APIs.

Replaces yfinance for A-share (.SZ/.SS) and HK (.HK) stocks using
Sina Finance (A-shares) and Tencent Finance (HK stocks) direct APIs.
US stocks raise NoMarketDataError so the vendor chain falls back to yfinance.

These APIs are chosen because they work reliably from within this network
environment (eastmoney's push2his API is blocked by the corporate firewall).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Annotated

import pandas as pd
import requests
from dateutil.relativedelta import relativedelta
from stockstats import wrap

from .config import get_config
from .errors import NoMarketDataError
from .symbol_utils import normalize_symbol
from .utils import safe_ticker_component

logger = logging.getLogger(__name__)

MAX_OHLCV_STALE_DAYS = 10
OHLCV_CACHE_TTL_SECONDS = 900

# Map yfinance-style suffixes to market codes for API calls
_SUFFIX_MARKET = {
    ".SZ": "sz",
    ".SS": "sh",
    ".HK": "hk",
    ".BJ": "bj",
}

# Common headers for financial APIs
_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn/",
}


def _parse_symbol(symbol: str) -> tuple[str, str]:
    """Split a symbol like '300750.SZ' into ('300750', 'sz')."""
    upper = symbol.strip().upper()
    for suffix, market in _SUFFIX_MARKET.items():
        if upper.endswith(suffix):
            return upper[: -len(suffix)], market
    return upper, "us"


def _ensure_date_column(data: pd.DataFrame) -> pd.DataFrame:
    if "Date" in data.columns:
        return data
    for candidate in ("日期", "date", "index", "day"):
        if candidate in data.columns:
            return data.rename(columns={candidate: "Date"})
    return data


def _clean_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    data = _ensure_date_column(data)
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data = data.dropna(subset=["Date"])

    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    data[price_cols] = data[price_cols].apply(pd.to_numeric, errors="coerce")
    data = data.dropna(subset=["Close"])
    data[price_cols] = data[price_cols].ffill().bfill()
    return data


def _needs_same_day_refresh(data_file, curr_date_dt, today_date) -> bool:
    if curr_date_dt.date() < today_date.date():
        return False
    return time.time() - os.path.getmtime(data_file) > OHLCV_CACHE_TTL_SECONDS


def _coerce_ohlcv_dates(data: pd.DataFrame) -> pd.Series:
    if "Date" in data.columns:
        return pd.to_datetime(data["Date"], errors="coerce").dropna()
    if isinstance(data.index, pd.DatetimeIndex):
        return pd.Series(pd.to_datetime(data.index, errors="coerce")).dropna()
    df = data.reset_index()
    for col in ("Date", "date", "index"):
        if col in df.columns:
            r = pd.to_datetime(df[col], errors="coerce").dropna()
            if not r.empty:
                return r
    return pd.Series(dtype="datetime64[ns]")


def _assert_ohlcv_not_stale(data: pd.DataFrame, curr_date: str, symbol: str,
                            canonical: str | None = None, *,
                            max_stale_days: int = MAX_OHLCV_STALE_DAYS) -> None:
    if data is None or data.empty:
        return
    requested = pd.to_datetime(curr_date, errors="coerce")
    if pd.isna(requested):
        return
    requested = requested.normalize()
    dates = _coerce_ohlcv_dates(data)
    if dates.empty:
        return
    latest = dates.max().normalize()
    stale_days = (requested - latest).days
    if stale_days > max_stale_days:
        raise NoMarketDataError(
            symbol, canonical,
            f"latest row is {latest.date()}, {stale_days} days before "
            f"requested {requested.date()} (stale)"
        )


# ── Sina Finance ────────────────────────────────────────────────────────────

_SINA_KLINE_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData"
)


def _fetch_ashare_sina(code: str, market: str) -> pd.DataFrame:
    """Fetch A-share daily K-line from Sina Finance.

    Sina returns the most recent ~2000 trading days. We request all available
    data and filter locally.
    """
    symbol = f"{market}{code}"  # e.g. 'sz000858', 'sh600519'
    params = {"symbol": symbol, "scale": 240, "ma": "no", "datalen": 2000}
    resp = requests.get(_SINA_KLINE_URL, params=params, headers=_API_HEADERS, timeout=30)
    if resp.status_code != 200:
        raise NoMarketDataError(symbol, None, f"Sina HTTP {resp.status_code}")

    try:
        raw = json.loads(resp.text)
    except json.JSONDecodeError:
        raise NoMarketDataError(symbol, None, "Sina returned invalid JSON")

    if not raw or raw is None or (isinstance(raw, dict) and not raw):
        raise NoMarketDataError(symbol, None, "Sina returned empty data")

    if not isinstance(raw, list):
        raise NoMarketDataError(symbol, None, f"Sina unexpected format: {type(raw)}")

    rows = []
    for item in raw:
        try:
            rows.append({
                "Date": item["day"],
                "Open": float(item["open"]),
                "High": float(item["high"]),
                "Low": float(item["low"]),
                "Close": float(item["close"]),
                "Volume": float(item["volume"]),
            })
        except (KeyError, ValueError, TypeError):
            continue

    if not rows:
        raise NoMarketDataError(symbol, None, "Sina returned no valid rows")

    return pd.DataFrame(rows)


# ── Tencent Finance ──────────────────────────────────────────────────────────

_TENCENT_KLINE_URL = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def _fetch_hk_tencent(code: str) -> pd.DataFrame:
    """Fetch HK stock daily K-line from Tencent Finance (前复权).

    Returns all available historical data.
    """
    # Tencent requires 5-digit HK stock codes (zero-padded)
    code_padded = code.zfill(5)
    param = f"hk{code_padded},day,,,2000,qfq"
    resp = requests.get(_TENCENT_KLINE_URL, params={"param": param}, timeout=30)
    if resp.status_code != 200:
        raise NoMarketDataError(code, None, f"Tencent HTTP {resp.status_code}")

    try:
        raw = json.loads(resp.text)
    except json.JSONDecodeError:
        raise NoMarketDataError(code, None, "Tencent returned invalid JSON")

    if raw.get("code") != 0:
        raise NoMarketDataError(code, None, f"Tencent error: {raw.get('msg', 'unknown')}")

    key = f"hk{code_padded}"
    data_section = raw.get("data", {}).get(key, {})
    days = data_section.get("day") or data_section.get("qfqday") or []

    if not days:
        raise NoMarketDataError(code, None, "Tencent returned no K-line data")

    # Tencent format: [date, open, close, high, low, volume]
    rows = []
    for item in days:
        try:
            rows.append({
                "Date": item[0],
                "Open": float(item[1]),
                "Close": float(item[2]),
                "High": float(item[3]),
                "Low": float(item[4]),
                "Volume": float(item[5]),
            })
        except (IndexError, ValueError, TypeError):
            continue

    if not rows:
        raise NoMarketDataError(code, None, "Tencent returned no valid rows")

    return pd.DataFrame(rows)


# ── US Stocks via Sina ────────────────────────────────────────────────────────

_SINA_US_KLINE_URL = (
    "https://stock.finance.sina.com.cn/usstock/api/json_v2.php/"
    "US_MinKService.getDailyK"
)


def _fetch_us_sina(symbol: str) -> pd.DataFrame:
    """Fetch US stock daily K-line from Sina Finance.

    Sina returns ALL historical daily data from IPO to present.
    Column mapping: d→Date, o→Open, h→High, l→Low, c→Close, v→Volume.
    """
    params = {"symbol": symbol.lower(), "scale": "240", "datalen": "10000"}
    resp = requests.get(_SINA_US_KLINE_URL, params=params, headers=_API_HEADERS, timeout=30)
    if resp.status_code != 200:
        raise NoMarketDataError(symbol, None, f"Sina US HTTP {resp.status_code}")

    try:
        raw = json.loads(resp.text)
    except json.JSONDecodeError:
        raise NoMarketDataError(symbol, None, "Sina US returned invalid JSON")

    if not raw or not isinstance(raw, list):
        raise NoMarketDataError(symbol, None, f"Sina US unexpected format: {type(raw)}")

    rows = []
    for item in raw:
        try:
            rows.append({
                "Date": item["d"],
                "Open": float(item["o"]),
                "High": float(item["h"]),
                "Low": float(item["l"]),
                "Close": float(item["c"]),
                "Volume": float(item["v"]),
            })
        except (KeyError, ValueError, TypeError):
            continue

    if not rows:
        raise NoMarketDataError(symbol, None, "Sina US returned no valid rows")

    return pd.DataFrame(rows)


# ── Unified OHLCV loader ─────────────────────────────────────────────────────

def load_ohlcv_akshare(symbol: str, curr_date: str) -> pd.DataFrame:
    """Fetch OHLCV data using Sina (A-shares) / Tencent (HK+US) APIs with caching.

    Supports: A-shares (.SZ/.SS/.BJ), HK (.HK), US (no suffix like MSFT/AAPL).
    """
    canonical = normalize_symbol(symbol)
    safe_symbol = safe_ticker_component(canonical)
    code, market = _parse_symbol(canonical)

    if market == "us":
        downloaded = _fetch_us_sina(code)

    config = get_config()
    curr_date_dt = pd.to_datetime(curr_date)
    today_date = pd.Timestamp.today()
    start_date = today_date - pd.DateOffset(years=5)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = (today_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    os.makedirs(config["data_cache_dir"], exist_ok=True)
    data_file = os.path.join(
        config["data_cache_dir"],
        f"{safe_symbol}-sina-tencent-data-{start_str}-{end_str}.csv",
    )

    # Check cache
    data = None
    if os.path.exists(data_file):
        cached = pd.read_csv(data_file, on_bad_lines="skip", encoding="utf-8")
        if (
            not cached.empty
            and "Close" in cached.columns
            and not _needs_same_day_refresh(data_file, curr_date_dt, today_date)
        ):
            data = cached

    if data is None:
        if market in ("sz", "sh", "bj"):
            downloaded = _fetch_ashare_sina(code, market)
        elif market == "hk":
            downloaded = _fetch_hk_tencent(code)
        elif market == "us":
            downloaded = _fetch_us_sina(code)
        else:
            raise NoMarketDataError(symbol, canonical, f"unsupported market: {market}")

        if downloaded.empty or "Close" not in downloaded.columns:
            raise NoMarketDataError(
                symbol, canonical,
                f"no data returned for {market.upper()} stock {code}"
            )
        downloaded.to_csv(data_file, index=False, encoding="utf-8")
        data = downloaded

    data = _clean_dataframe(data)
    data = data[data["Date"] <= curr_date_dt]
    _assert_ohlcv_not_stale(data, curr_date, symbol, canonical)

    return data


# ── Vendor interface functions ────────────────────────────────────────────────

def get_akshare_data_online(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Get OHLCV data for a date range, returning a CSV string (same format as yfinance)."""
    canonical = normalize_symbol(symbol)
    code, market = _parse_symbol(canonical)

    if market in ("sz", "sh", "bj"):
        df = _fetch_ashare_sina(code, market)
    elif market == "hk":
        df = _fetch_hk_tencent(code)
    elif market == "us":
        df = _fetch_us_sina(code)
    else:
        raise NoMarketDataError(symbol, canonical, f"unsupported market: {market}")

    if df.empty:
        raise NoMarketDataError(symbol, canonical, "no data returned")

    df = _clean_dataframe(df)
    start_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date)
    df = df[(df["Date"] >= start_dt) & (df["Date"] <= end_dt)]

    if df.empty:
        raise NoMarketDataError(symbol, canonical, "no data in requested range")

    for col in ["Open", "High", "Low", "Close"]:
        if col in df.columns:
            df[col] = df[col].round(2)

    csv_string = df.to_csv(index=False)
    label = canonical if canonical == symbol.upper() else f"{canonical} (from {symbol})"
    api_name = "新浪财经" if market in ("sz", "sh", "bj", "us") else "腾讯财经(HK)"
    header = f"# Stock data for {label} (via {api_name}) from {start_date} to {end_date}\n"
    header += f"# Total records: {len(df)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + csv_string


def get_akshare_indicators_window(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[str, "The current trading date you are trading on, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    """Get technical indicator values using Sina/Tencent OHLCV + stockstats."""

    best_ind_params = {
        "close_50_sma": "50 SMA: A medium-term trend indicator. Usage: Identify trend direction and serve as dynamic support/resistance. Tips: It lags price; combine with faster indicators for timely signals.",
        "close_200_sma": "200 SMA: A long-term trend benchmark. Usage: Confirm overall market trend and identify golden/death cross setups. Tips: It reacts slowly; best for strategic trend confirmation.",
        "close_10_ema": "10 EMA: A responsive short-term average. Usage: Capture quick shifts in momentum and potential entry points. Tips: Prone to noise in choppy markets.",
        "macd": "MACD: Computes momentum via differences of EMAs. Usage: Look for crossovers and divergence as signals of trend changes. Tips: Confirm with other indicators in low-volatility or sideways markets.",
        "macds": "MACD Signal: An EMA smoothing of the MACD line. Usage: Use crossovers with the MACD line to trigger trades.",
        "macdh": "MACD Histogram: Shows the gap between the MACD line and its signal. Usage: Visualize momentum strength and spot divergence early.",
        "rsi": "RSI: Measures momentum to flag overbought/oversold conditions. Usage: Apply 70/30 thresholds and watch for divergence to signal reversals.",
        "boll": "Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. Usage: Acts as a dynamic benchmark for price movement.",
        "boll_ub": "Bollinger Upper Band: Typically 2 standard deviations above the middle line. Usage: Signals potential overbought conditions and breakout zones.",
        "boll_lb": "Bollinger Lower Band: Typically 2 standard deviations below the middle line. Usage: Indicates potential oversold conditions.",
        "atr": "ATR: Averages true range to measure volatility. Usage: Set stop-loss levels and adjust position sizes based on current market volatility.",
        "vwma": "VWMA: A moving average weighted by volume. Usage: Confirm trends by integrating price action with volume data.",
        "mfi": "MFI: The Money Flow Index uses both price and volume to measure buying and selling pressure. Usage: Identify overbought (>80) or oversold (<20) conditions.",
    }

    if indicator not in best_ind_params:
        raise ValueError(f"Indicator {indicator} is not supported. Choose from: {list(best_ind_params.keys())}")

    end_date = curr_date
    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before = curr_date_dt - relativedelta(days=look_back_days)

    try:
        data = load_ohlcv_akshare(symbol, curr_date)
        df = wrap(data)
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
        df[indicator]

        result_dict = {}
        for _, row in df.iterrows():
            val = row[indicator]
            result_dict[row["Date"]] = "N/A" if pd.isna(val) else str(val)

        current_dt = curr_date_dt
        date_values = []
        while current_dt >= before:
            ds = current_dt.strftime("%Y-%m-%d")
            date_values.append((ds, result_dict.get(ds, "N/A: Not a trading day")))
            current_dt -= relativedelta(days=1)

        ind_string = "\n".join(f"{d}: {v}" for d, v in date_values)

    except NoMarketDataError:
        raise
    except Exception as e:
        raise NoMarketDataError(symbol, None, f"indicator error: {e}")

    return (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {end_date} (via Sina/Tencent):\n\n"
        + ind_string + "\n\n" + best_ind_params.get(indicator, "")
    )

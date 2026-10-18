"""Factor pre-screen for the daily ticker list.

Disabled by default: ``daily_predict.build_daily_tickers`` only calls this when
``TRADINGAGENTS_SCREEN_TOP_N`` is set to a positive integer. The single hard
invariant is conservative: a data failure must never remove a candidate.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

# Mirrors run_3.py's CROSS set. Importing run_3 is not an option — it performs
# network work at import time — so the two lists are kept in sync by hand.
CROSS = {"159792.SZ", "159740.SZ"}
CROSS_PREFIXES = ("159", "513")
PREMIUM_VETO = 0.05

MID_WEIGHT = 0.60   # medium-term momentum is the dominant signal
SHORT_WEIGHT = 0.25
LIQUIDITY_WEIGHT = 0.15

# Normal scores live in [0, 1]; the sentinels let select_tickers tell a real
# ranking failure (keep everything) from a deliberate premium veto (never pick).
UNSELECTABLE_SCORE = -1.0
UNRANKED_SCORE = -2.0


class ScreeningDataError(RuntimeError):
    """History needed by the screen is missing or unusable."""


def _unique(tickers: Iterable[str] | None) -> list[str]:
    cleaned = (str(t).strip().upper() for t in (tickers or ()))
    return list(dict.fromkeys(t for t in cleaned if t))


def _code(ticker: str) -> str:
    return ticker.split(".", 1)[0]


def _load_history(ticker: str, config: Any = None) -> tuple[list[float], list[float]]:
    """Return aligned closes and daily turnover, oldest first.

    Sina's K-line (the live OHLCV path here) carries no 成交额 column, so
    turnover is approximated as close × volume. The screen only compares
    candidates against each other, so a consistent proxy is sufficient.
    """
    from tradingagents.dataflows.akshare_data import load_ohlcv_akshare

    curr_date = datetime.now().strftime("%Y-%m-%d")
    frame = load_ohlcv_akshare(ticker, curr_date)
    if frame is None or len(frame) == 0:
        raise ScreeningDataError(f"{ticker}: empty OHLCV")
    if "Close" not in frame.columns or "Volume" not in frame.columns:
        raise ScreeningDataError(f"{ticker}: missing Close/Volume")

    closes: list[float] = []
    amounts: list[float] = []
    for close, volume in zip(frame["Close"].tolist(), frame["Volume"].tolist()):
        try:
            close = float(close)
            volume = float(volume)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(close) and math.isfinite(volume)) or close <= 0:
            continue
        closes.append(close)
        amounts.append(close * max(volume, 0.0))
    if not closes:
        raise ScreeningDataError(f"{ticker}: no valid rows")
    return closes, amounts


_NAV_CACHE: dict[str, float] = {}
_NAV_LOADED = False


def _load_navs() -> None:
    """Best-effort one-shot NAV snapshot (same source as run_3.py)."""
    global _NAV_LOADED
    if _NAV_LOADED:
        return
    _NAV_LOADED = True
    try:
        import akshare as ak

        frame = ak.fund_etf_fund_daily_em()
        nav_columns = [c for c in frame.columns if "单位净值" in str(c)]
        if "基金代码" not in frame.columns or not nav_columns:
            return
        for _, row in frame.iterrows():
            code = str(row["基金代码"]).strip().zfill(6)
            for column in sorted(nav_columns, reverse=True):
                value = str(row[column]).strip()
                if not value or value in ("---", "nan", "None"):
                    continue
                try:
                    _NAV_CACHE[code] = float(value)
                except ValueError:
                    continue
                break
    except Exception as exc:  # noqa: BLE001 - a missing NAV just skips the veto
        logger.debug("NAV snapshot unavailable: %s", exc)


def _cross_premium(ticker: str, close: float) -> float | None:
    """Same-day premium for cross-border ETFs; None when not applicable/unknown."""
    ticker = ticker.upper()
    if not (_code(ticker).startswith(CROSS_PREFIXES) or ticker in CROSS):
        return None
    _load_navs()
    nav = _NAV_CACHE.get(_code(ticker).zfill(6))
    if not nav or nav <= 0:
        return None
    premium = close / nav - 1
    return premium if math.isfinite(premium) else None


def _percentile_ranks(values: Sequence[float]) -> list[float]:
    """Ranks in (0, 1], ties sharing their average rank."""
    count = len(values)
    if count == 0:
        return []
    order = sorted(range(count), key=lambda index: values[index])
    ranks = [0.0] * count
    index = 0
    while index < count:
        end = index
        while end + 1 < count and values[order[end + 1]] == values[order[index]]:
            end += 1
        average_rank = (index + end) / 2 + 1
        for position in range(index, end + 1):
            ranks[order[position]] = average_rank / count
        index = end + 1
    return ranks


def _momentum(closes: Sequence[float], days: int) -> float:
    if len(closes) <= days or closes[-1 - days] <= 0:
        raise ScreeningDataError(f"need {days + 1} closes, got {len(closes)}")
    return closes[-1] / closes[-1 - days] - 1


def rank_candidates(
    candidates: Iterable[str],
    config: Any = None,
    *,
    lookback_days: int = 20,
) -> list[tuple[str, float, str]]:
    """Score ``candidates`` best-first as ``(ticker, score, reason)``.

    A candidate whose history cannot be read is still returned, carrying
    ``UNRANKED_SCORE``; a cross-border ETF above the premium limit carries
    ``UNSELECTABLE_SCORE``. Neither is silently dropped here.
    """
    tickers = _unique(candidates)
    if not tickers:
        return []

    computed: list[dict[str, Any]] = []
    for ticker in tickers:
        try:
            closes, amounts = _load_history(ticker, config)
            if len(closes) < lookback_days + 1:
                raise ScreeningDataError(
                    f"{ticker}: only {len(closes)} rows, need {lookback_days + 1}"
                )
            window = amounts[-min(20, len(amounts)):]
            liquidity = sum(window) / len(window)
            computed.append({
                "ticker": ticker,
                "medium": _momentum(closes, lookback_days),
                "short": _momentum(closes, 5),
                "liquidity": liquidity,
                "premium": _cross_premium(ticker, closes[-1]),
                "error": None,
            })
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the screen
            logger.debug("candidate %s could not be scored: %s", ticker, exc)
            computed.append({"ticker": ticker, "error": str(exc) or type(exc).__name__})

    valid = [item for item in computed if item["error"] is None]
    medium_ranks = dict(zip(
        (item["ticker"] for item in valid),
        _percentile_ranks([item["medium"] for item in valid]),
    ))
    short_ranks = dict(zip(
        (item["ticker"] for item in valid),
        _percentile_ranks([item["short"] for item in valid]),
    ))
    liquidity_ranks = dict(zip(
        (item["ticker"] for item in valid),
        _percentile_ranks([item["liquidity"] for item in valid]),
    ))

    results: list[tuple[str, float, str]] = []
    for item in valid:
        ticker = item["ticker"]
        score = (
            MID_WEIGHT * medium_ranks[ticker]
            + SHORT_WEIGHT * short_ranks[ticker]
            + LIQUIDITY_WEIGHT * liquidity_ranks[ticker]
        )
        reason = (
            f"中期{item['medium']:+.2%} 短期{item['short']:+.2%} "
            f"流动性分位{liquidity_ranks[ticker]:.0%} "
            f"20日均额{item['liquidity'] / 1e8:.2f}亿"
        )
        if item["premium"] is not None:
            reason += f" 跨境溢价{item['premium']:+.2%}"
            if item["premium"] > PREMIUM_VETO:
                score = UNSELECTABLE_SCORE
                reason += " >5%，不可选"
        results.append((ticker, score, reason))

    for item in computed:
        if item["error"] is not None:
            results.append((
                item["ticker"],
                UNRANKED_SCORE,
                f"数据不可得（{item['error']}），保守保留",
            ))

    results.sort(key=lambda entry: (-entry[1], entry[0]))
    return results


def select_tickers(
    holdings: Iterable[str],
    mandatory: Iterable[str],
    candidates: Iterable[str],
    top_n: int,
    config: Any = None,
) -> list[str]:
    """Keep holdings/mandatory unconditionally and add up to ``top_n`` screened names.

    Holdings and mandatory names never consume a slot. When any candidate could
    not be scored, the whole candidate pool is retained rather than risking a
    deletion caused by a data outage.
    """
    base = _unique([*holdings, *mandatory])
    base_set = set(base)
    pool = [ticker for ticker in _unique(candidates) if ticker not in base_set]

    if top_n <= 0 or not pool:
        return base

    try:
        ranked = rank_candidates(pool, config)
    except Exception as exc:  # noqa: BLE001 - conservative fallback
        logger.debug("screening failed, keeping every candidate: %s", exc)
        return _unique([*base, *pool])

    if any(score == UNRANKED_SCORE for _, score, _ in ranked):
        # Never let a fetch failure shrink the universe; only an explicit
        # premium veto is allowed to remove a name.
        kept = [ticker for ticker, score, _ in ranked
                if score != UNSELECTABLE_SCORE]
        return _unique([*base, *kept])

    chosen = [ticker for ticker, score, _ in ranked
              if score > UNSELECTABLE_SCORE][:top_n]
    return _unique([*base, *chosen])

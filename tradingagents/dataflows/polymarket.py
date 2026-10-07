"""Polymarket prediction-market vendor.

Surfaces live, market-implied probabilities for forward-looking events (Fed
decisions, recession, elections, geopolitics, crypto) to the news analyst, as a
complement to news (what happened) and FRED macro data (where things stand):
what the crowd actually prices to happen next.

Uses Polymarket's public Gamma API (https://gamma-api.polymarket.com) — no key,
no auth. Each market's ``outcomePrices`` are the implied probabilities of its
outcomes (a "Yes" at 0.76 means the market prices a 76% chance).
"""
import json
import logging
import os
import urllib.request
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

# Network timeout (seconds), consistent with the other vendors.
REQUEST_TIMEOUT = 30

# Proxy that served the last successful request, reused for the rest of the
# process so a failing proxy is not retried on every call of a daily run.
_working_proxies = None

# Default number of markets to return, ranked by traded volume.
DEFAULT_LIMIT = 6


def _system_proxy() -> dict | None:
    """Proxy advertised by the OS, which may differ from this session's env.

    A host can inject its own proxy into HTTP(S)_PROXY that refuses to tunnel
    overseas domains (observed: CONNECT answered "502 Bad Gateway") while the
    OS-level proxy reaches them fine. Returns None when unavailable/disabled.
    """
    if os.name != "nt":
        return None
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        if winreg.QueryValueEx(key, "ProxyEnable")[0] != 1:
            return None
        server = str(winreg.QueryValueEx(key, "ProxyServer")[0]).strip()
    except Exception:
        return None
    if not server:
        return None
    if server.startswith(("http://", "https://", "socks")):
        return {"http": server, "https": server}
    return {"http": f"http://{server}", "https": f"http://{server}"}


def _proxy_candidates() -> list:
    """Proxy configurations to try, most preferred first."""
    candidates = []
    routes = urllib.request.getproxies()
    env = {scheme: routes[scheme] for scheme in ("http", "https") if routes.get(scheme)}
    if env:
        candidates.append(env)
    system = _system_proxy()
    if system and system != env:
        candidates.append(system)
    candidates.append({})  # last resort: direct connection
    return candidates


def _request(path: str, params: dict) -> dict:
    global _working_proxies
    url = f"{GAMMA_BASE}/{path}"
    # A known-good proxy is retried first; the others stay as fallback.
    ordered = ([_working_proxies] if _working_proxies is not None
               else []) + _proxy_candidates()
    attempts, seen = [], set()
    for proxies in ordered:
        marker = tuple(sorted(proxies.items()))
        if marker not in seen:
            seen.add(marker)
            attempts.append(proxies)
    last_error = None
    for proxies in attempts:
        try:
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT,
                                    proxies=proxies)
            response.raise_for_status()
            _working_proxies = proxies
            logger.debug("Polymarket request succeeded via proxy %s",
                         proxies.get("https") or proxies.get("http") or "direct")
            return response.json()
        except Exception as exc:
            last_error = exc
            if proxies is _working_proxies:
                _working_proxies = None  # cached winner stopped working
            continue
    raise last_error


def _parse_json_list(value) -> list:
    """Gamma encodes ``outcomes``/``outcomePrices`` as JSON-string arrays."""
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


def _is_forward_looking(market: dict, now: datetime) -> bool:
    """Keep only open markets that resolve in the future.

    ``closed`` is the reliable resolved flag (``active`` stays True even for
    settled markets), and a past ``endDate`` means the event already resolved —
    either way it is not a forward-looking signal.
    """
    if market.get("closed"):
        return False
    end_date = market.get("endDate")
    if end_date:
        try:
            if datetime.fromisoformat(end_date.replace("Z", "+00:00")) < now:
                return False
        except ValueError:
            pass
    return bool(_parse_json_list(market.get("outcomePrices"))) and bool(
        _parse_json_list(market.get("outcomes"))
    )


def get_prediction_markets(topic: str, limit: int | None = None) -> str:
    """Return live prediction-market probabilities for an event topic.

    Args:
        topic: Event keyword(s), e.g. "Fed rate cut", "recession 2026",
            "US election", or a sector/company event.
        limit: Max markets to return (ranked by traded volume); ``None`` uses
            DEFAULT_LIMIT.

    Returns:
        A markdown report of the most-traded open markets matching the topic,
        each with its implied probability, traded volume, resolution date, and
        recent (1-week) move.
    """
    if limit is None:
        limit = DEFAULT_LIMIT

    try:
        data = _request("public-search", {"q": topic, "limit_per_type": 20})
    except requests.RequestException as e:
        logger.warning("Polymarket search failed for %r: %s", topic, e)
        return (
            f"Polymarket data is currently unavailable (network error: {e}). "
            f"Proceed without prediction-market signal for '{topic}'."
        )

    now = datetime.now(timezone.utc)
    candidates = [
        m
        for event in data.get("events", [])
        for m in event.get("markets", [])
        if _is_forward_looking(m, now)
    ]
    candidates.sort(key=lambda m: m.get("volumeNum") or 0, reverse=True)

    header = (
        f'## Polymarket prediction markets: "{topic}"\n'
        f"Live, market-implied probabilities (higher traded volume = deeper, "
        f"more reliable). A probability is the crowd's priced odds of the event, "
        f"not a forecast you should take as certain.\n\n"
    )

    if not candidates:
        return header + (
            f"No open prediction markets matched '{topic}'. Polymarket coverage "
            f"is concentrated in macro, political, geopolitical, and crypto "
            f"events; a specific equity may have none."
        )

    lines = []
    for m in candidates[:limit]:
        prices = _parse_json_list(m.get("outcomePrices"))
        outcomes = _parse_json_list(m.get("outcomes"))
        try:
            prob = float(prices[0])
        except (ValueError, IndexError):
            continue
        label = outcomes[0] if outcomes else "Yes"
        volume = m.get("volumeNum") or 0
        end_date = (m.get("endDate") or "")[:10]
        wk = m.get("oneWeekPriceChange")
        wk_str = (
            f", 1-week {wk * 100:+.1f}pp"
            if isinstance(wk, (int, float)) and wk
            else ""
        )
        lines.append(
            f"- **{m.get('question')}** — {label} {prob:.0%} "
            f"(${volume:,.0f} volume, resolves {end_date}{wk_str})"
        )

    return header + "\n".join(lines) + "\n"

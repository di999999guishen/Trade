"""HTTP helpers shared by the vendors."""

import logging
import os
import urllib.request

import requests

logger = logging.getLogger(__name__)


def get_scrubbed(url: str, *, params: dict, timeout: float, secret: str, passthrough=()):
    """``requests.get`` plus ``raise_for_status``, with ``secret`` kept out of errors.

    Vendors that authenticate with a query parameter put the key in the URL, and
    requests quotes the full URL in HTTP, connection and timeout errors, so any
    log or traceback that records one would carry the key (#1324). A requests
    error is re-raised as the same class with the key replaced and nothing
    attached: no request or response (both hold the URL) and no exception chain,
    which is why this raises after the ``except`` block rather than inside it.
    Statuses in ``passthrough`` are returned for the caller to handle.
    """
    try:
        response = requests.get(url, params=params, timeout=timeout)
        if response.status_code not in passthrough:
            response.raise_for_status()
        return response
    except requests.RequestException as exc:
        error = type(exc)(str(exc).replace(secret, "***")) if secret else exc
    raise error


def vendor_reachable(url: str, timeout: float = 5.0) -> bool:
    """Whether the vendor answers at all, for telling silence from an outage.

    A client that returns an empty result instead of raising leaves those two
    cases indistinguishable. Called only when a result is empty.
    """
    try:
        requests.head(url, timeout=timeout, allow_redirects=True)
        return True
    except requests.RequestException:
        return False


# ---------------------------------------------------------------------------
# Proxy fallback
#
# A host can advertise a proxy in HTTP(S)_PROXY that refuses to tunnel the
# destination (observed: CONNECT answered "502 Bad Gateway") while the
# OS-level proxy reaches it fine, and a direct connection can work when
# neither does. A vendor that trusts the environment's single answer therefore
# fails for reasons that have nothing to do with the vendor. Overseas-only
# endpoints (Polymarket's Gamma API, SEC EDGAR) are the ones exposed to this,
# so they route their requests through get_with_proxy_fallback().
# ---------------------------------------------------------------------------

# The proxy that served the last successful request, reused for the rest of
# the process so a failing route is not re-tried on every call of a daily run.
# ``None`` means "no winner known yet"; ``{}`` is a cached win for direct.
_working_proxy: dict | None = None


def system_proxy() -> dict | None:
    """The proxy the OS advertises, which may differ from this session's env.

    Read from the Windows registry because that is where the desktop setting
    lives; another platform, or a disabled setting, returns ``None`` and the
    caller simply omits this candidate.
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


def proxy_candidates() -> list[dict]:
    """Proxy configurations to try, most preferred first, direct last."""
    candidates: list[dict] = []
    routes = urllib.request.getproxies()
    env = {scheme: routes[scheme] for scheme in ("http", "https") if routes.get(scheme)}
    if env:
        candidates.append(env)
    system = system_proxy()
    if system and system != env:
        candidates.append(system)
    candidates.append({})  # last resort: direct connection
    return candidates


def get_with_proxy_fallback(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 30,
    proxies: dict | None = None,
):
    """``requests.get`` trying each proxy candidate until one answers.

    ``proxies`` pins a single route (used by tests); otherwise the candidate
    that last worked is tried first, then the environment's route, the OS
    route, and finally a direct connection, with duplicates collapsed.

    Raises the last ``requests.RequestException`` when every candidate fails,
    so callers keep their existing ``except requests.RequestException``.
    """
    global _working_proxy
    ordered: list[dict] = []
    if proxies is not None:
        ordered.append(proxies)
    elif _working_proxy is not None:
        ordered.append(_working_proxy)
    ordered.extend(proxy_candidates())

    attempts: list[dict] = []
    seen: set[tuple] = set()
    for candidate in ordered:
        marker = tuple(sorted(candidate.items()))
        if marker not in seen:
            seen.add(marker)
            attempts.append(candidate)

    last_error: requests.RequestException | None = None
    for candidate in attempts:
        try:
            response = requests.get(
                url, params=params, headers=headers, timeout=timeout, proxies=candidate
            )
            response.raise_for_status()
            if proxies is None:
                _working_proxy = candidate
                logger.debug(
                    "Reached %s via %s",
                    url,
                    candidate.get("https") or candidate.get("http") or "direct",
                )
            return response
        except requests.RequestException as exc:
            last_error = exc
            if candidate is _working_proxy:
                _working_proxy = None  # the cached winner stopped working
    if last_error is not None:
        raise last_error
    raise requests.RequestException(f"no proxy candidate reached {url}")

"""Post-close history backfill and settlement, decoupled from the research cycle.

The end-to-end cycle refreshes daily bars for every symbol that still has an
unsettled prediction, screen selection or agent analysis. That backlog grows
into the hundreds and each refresh is slow, so running it inside the 19:00 cycle
pushed report generation hours past the intended window.

This module runs the same refresh as a standalone post-close job and, crucially,
also runs the settlements afterwards. Refreshing alone would not help: the
backlog is defined by the *unsettled* rows, so without settling, the next cycle
would simply re-fetch the same symbols. Doing both drains the backlog, which
leaves the cycle with a warm cache and a short step.

Two distinct symbol sets cause waits in the cycle, and both are covered here:

1. `pending_history_assets` -- symbols with rows still awaiting settlement.
2. The historical flow screen's cohort universe -- it rebuilds cohorts from every
   frozen snapshot and needs bars for the whole eligible universe of each cohort.
   That set is much larger (hundreds of symbols) and is *not* implied by the
   pending rows, so fetching only (1) left `historical_flow_screen` waiting.
"""

from __future__ import annotations

import copy
import json
import platform
import sys
from datetime import datetime

from .adapters.eastmoney import fetch_universe
from .config import market_data_dir, resolve_project_path
from .evidence import now_utc
from .pipeline import settle_predictions
from .screening import settle_screen_selections
from .settlement import pending_history_assets
from .store import connect, record_run
from .tradingagents_adapter import settle_agent_analyses


def _without_items(name: str, payload: dict) -> dict:
    """Keep the counters from a settlement result and drop the per-row detail."""
    return {"name": name, **{key: value for key, value in payload.items() if key != "items"}}


def _flow_screen_backlog(cfg: dict) -> list[dict]:
    """Cohort symbols the historical flow screen needs but has no cached bars for.

    Mirrors the lookup that `run_flow_backtest` performs, so anything reported
    here is exactly what that step would count as a missing history. A failure to
    reconstruct the cohorts is not fatal: the pending backlog still gets drained.
    """
    try:
        from .data import configured_data_dirs, find_cache
        from .flow_backtest import reconstruct_screens

        directories = configured_data_dirs(cfg)
        symbols: set[str] = set()
        for cohort in reconstruct_screens(cfg)["cohorts"]:
            symbols.update(*map(set, cohort["groups"].values()))
        missing = []
        for symbol in sorted(symbols):
            try:
                find_cache(symbol, directories)
            except (FileNotFoundError, ValueError):
                missing.append({"symbol": symbol})
        return missing
    except Exception as exc:  # noqa: BLE001 - backfill must not die on this
        print(f"[backfill] could not enumerate flow-screen backlog: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return []


def run_backfill(cfg: dict) -> dict:
    cfg = copy.deepcopy(cfg)
    runs = resolve_project_path(cfg, cfg["runs_dir"])
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / f"backfill_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"

    report = {
        "run_type": "history_backfill_and_settlement",
        "started_at_utc": now_utc(),
        "result_path": str(path.resolve()),
        "runtime": {"python": sys.version, "platform": platform.platform()},
        "outcome": "running",
    }

    def persist():
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    persist()

    before = pending_history_assets(cfg, [])
    report["pending_before"] = len(before)
    flow_backlog = _flow_screen_backlog(cfg)
    report["flow_screen_missing_before"] = len(flow_backlog)

    # De-duplicate: a symbol can be both awaiting settlement and part of a
    # historical cohort, and fetching it twice would double the slowest step.
    targets = {row["symbol"]: row for row in flow_backlog}
    for row in before:
        targets.setdefault(row["symbol"], row)
    targets = list(targets.values())

    if targets:
        refresh = fetch_universe(targets, market_data_dir(cfg))
        report["history_refresh"] = {
            "requested": len(targets),
            "from_pending": len(before),
            "from_flow_screen": len(flow_backlog),
            "refreshed": len(refresh["successes"]),
            "failed": len(refresh["failures"]),
            "failures": refresh["failures"],
            "provider_chain": refresh["provider_chain"],
            "primary_provider_paused": refresh["primary_provider_paused"],
            "primary_provider_skipped": refresh["primary_provider_skipped"],
        }
    else:
        report["history_refresh"] = {"requested": 0, "from_pending": 0, "from_flow_screen": 0,
                                     "refreshed": 0, "failed": 0, "failures": {},
                                     "provider_chain": [], "primary_provider_paused": False,
                                     "primary_provider_skipped": 0}
    persist()

    # Settlement is what actually drains the backlog; the refresh above only makes
    # it possible for horizons that have already elapsed.
    report["settlement"] = [
        _without_items("predictions", settle_predictions(cfg)),
        _without_items("agent_analyses", settle_agent_analyses(cfg)),
        _without_items("screen_selections", settle_screen_selections(cfg)),
    ]
    persist()

    after = pending_history_assets(cfg, [])
    report["pending_after"] = len(after)
    report["pending_after_symbols"] = [row["symbol"] for row in after]
    report["flow_screen_missing_after"] = len(_flow_screen_backlog(cfg))
    report["finished_at_utc"] = now_utc()
    report["outcome"] = "complete" if not report["history_refresh"]["failed"] else "partial_failure"
    persist()

    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        record_run(connection, "backfill", cfg, path)
    return report


def main(argv: list[str] | None = None) -> int:
    from .cli import main as cli_main

    return cli_main(["backfill", *(argv or [])])


if __name__ == "__main__":
    raise SystemExit(main())

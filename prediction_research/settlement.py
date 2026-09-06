from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from .config import resolve_project_path
from .evidence import utc
from .features import build_samples
from .store import connect


def decision_outcome(cfg: dict, snapshot, feature_date: str, horizon: int, decision: str | None = None):
    if decision is None:
        sample = next((row for row in build_samples(snapshot, horizon) if row.feature_date.isoformat() == feature_date), None)
        if sample is None or sample.target_return is None:
            return None
        return {"target_end_date": sample.target_end_date.isoformat(), "actual_return": sample.target_return, "actual_up": sample.target_up}
    stamp = utc(decision)
    zone = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))
    bars = [bar for bar in snapshot.bars if bar.trading_date.isoformat() > feature_date
            and datetime.combine(bar.trading_date, time(9, 30), zone) > stamp]
    if len(bars) < horizon:
        return None
    result = bars[horizon - 1].close / bars[0].open - 1
    return {"entry_date": bars[0].trading_date.isoformat(), "target_end_date": bars[horizon - 1].trading_date.isoformat(),
            "actual_return": result, "actual_up": int(result > 0)}


def pending_history_assets(cfg: dict, candidates: list[dict]) -> list[dict]:
    symbols = {row["symbol"] for row in candidates}
    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        for query in (
            "SELECT DISTINCT symbol FROM predictions WHERE settled_at_utc IS NULL",
            "SELECT DISTINCT symbol FROM screen_selections WHERE settled_at_utc IS NULL",
            "SELECT DISTINCT a.symbol FROM agent_analyses a WHERE a.status='ok' AND "
            "(SELECT count(*) FROM agent_outcomes o WHERE o.analysis_id=a.analysis_id) < ?",
        ):
            params = (len(cfg["horizons"]),) if "?" in query else ()
            symbols.update(row[0] for row in connection.execute(query, params).fetchall())
    return [{"symbol": symbol} for symbol in sorted(symbols)]

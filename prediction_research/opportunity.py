"""Auditable observation categories, independent of selection and trade decisions.

Daily quote/flow observations never establish a multi-day trend. History-derived
categories are descriptive hypotheses with versioned thresholds, not model output.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import market_data_dir
from .data import SeriesSnapshot, configured_data_dirs, find_cache
from .evidence import cutoff_utc, utc
from .flow_context import finite
from .taxonomy import classify_etf, market_scope


CATEGORY_LABELS = {
    "trend_continuation": "趋势延续",
    "pullback_watch": "回调观察",
    "oversold_recovery": "超跌修复",
    "high_level_divergence": "高位分歧",
    "trend_weakening": "趋势走弱",
    "range_no_edge": "震荡无优势",
    "insufficient_data": "数据不足",
}
ASSET_LABELS = {"equity": "股票", "bond": "债券", "commodity": "商品",
                "money": "货币", "multi_asset": "多资产", "unknown": "待核实"}
SNAPSHOT_LABELS = {
    "inflow_price_confirmation": "单日价涨资金流入",
    "outflow_price_weakness": "单日价跌资金流出",
    "price_flow_disagreement": "单日价格资金分歧",
    "neutral_snapshot": "单日信号中性",
    "insufficient_data": "单日数据不足",
}
RULES = {"version": "observation-v1", "minimum_history_bars": 61,
         "flow_watch_threshold_pct": 3.0, "high_return_20": 0.08,
         "high_distance_ma20": 0.04, "oversold_return_20": -0.06,
         "oversold_drawdown_60": -0.08, "recovery_return_5": 0.01,
         "pullback_drawdown_20": -0.02, "pullback_max_return_5": 0.02,
         "trend_min_return_20": 0.01}
FOLLOWUP = {
    "trend_continuation": ("观察价格是否继续位于20/60日均线上方，后续资金是否支持", "跌破20日均线或后续资金转弱时重新分类"),
    "pullback_watch": ("观察回调后是否重新站上20日均线，并出现后续资金支持", "跌破60日均线且中期收益转负时重新分类"),
    "oversold_recovery": ("观察修复能否延续并逐步收复20日均线", "再创新低或后续资金明显流出时重新分类"),
    "high_level_divergence": ("观察高位价格与资金的分歧能否消除", "后续资金重新流入且价格趋势维持时重新分类"),
    "trend_weakening": ("观察能否收复20/60日均线并改善资金流", "价格收复均线且中期走势转强时重新分类"),
    "range_no_edge": ("等待趋势与资金出现可复核的同向条件", "出现其他类别明确条件时重新分类"),
    "insufficient_data": ("补齐截至观察时点可用的日线和时间来源，再重新观察", "数据补齐并通过时效检查后解除数据不足状态"),
}


def _stamp(value):
    try:
        return utc(value) if value else None
    except (ValueError, TypeError, AttributeError):
        return None


def classify_observation(row: dict, decision_at_utc: str, cfg: dict,
                         history: SeriesSnapshot | None = None,
                         history_available_at_utc: str | None = None,
                         history_availability_basis: str = "missing") -> dict:
    """Classify one quote as-of decision without looking at later bars.

The explicit availability argument makes boundary tests and archived replay
possible. Filesystem mtime may support present-day exploration but is never
described as verified point-in-time data for a historical backtest.
"""
    decision = utc(decision_at_utc)
    zone = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))
    max_age = float(cfg.get("workflow", {}).get("max_quote_age_days", 4))
    epoch = finite(row.get("quote_epoch"))
    try:
        observed = datetime.fromtimestamp(epoch, timezone.utc) if epoch is not None else None
    except (OverflowError, OSError, ValueError):
        observed = None
    quote_status = ("missing_timestamp" if observed is None else
                    "future_quote" if observed > decision else
                    "stale_quote" if (decision - observed).total_seconds() > max_age * 86400 else "fresh")
    flow, pct, change = (finite(row.get(key)) for key in ("main_net_inflow", "main_net_inflow_pct", "change_pct"))
    snapshot_category = "insufficient_data"
    if quote_status == "fresh" and None not in (flow, pct, change):
        if flow * pct < 0:
            snapshot_category = "insufficient_data"
        elif flow > 0 and change > 0:
            snapshot_category = "inflow_price_confirmation"
        elif flow < 0 and change < 0:
            snapshot_category = "outflow_price_weakness"
        elif flow * change < 0:
            snapshot_category = "price_flow_disagreement"
        else:
            snapshot_category = "neutral_snapshot"
    category = "insufficient_data"
    quality = {"quote_freshness": quote_status, "history_status": "missing_history",
               "history_availability": history_availability_basis,
               "point_in_time_verified": False, "model_validation": "not_evaluated_by_screen",
               "category_validation": "heuristic_not_backtested",
               "flow_observation_days": 1 if snapshot_category != "insufficient_data" else 0,
               "history_calendar": "observed_bars_not_exchange_calendar_verified"}
    evidence = {"quote_epoch": epoch, "change_pct": change,
                "main_net_inflow": flow, "main_net_inflow_pct": pct,
                "history_bars": 0, "future_or_unclosed_bars_excluded": 0}
    reason_codes = []
    if quote_status != "fresh":
        reason_codes.append(quote_status)
    if snapshot_category == "insufficient_data":
        reason_codes.append("incomplete_or_inconsistent_snapshot")
    available = _stamp(history_available_at_utc)
    if history:
        evidence.update(history_path=history.source_path, history_sha256=history.sha256,
                        history_available_at_utc=history_available_at_utc)
        quality["history_status"] = "unknown_history_availability" if available is None else (
            "history_unavailable_at_decision" if available > decision else "checking")
        if quality["history_status"] == "checking":
            quote_day = observed.astimezone(zone).date() if observed else decision.astimezone(zone).date()
            dated = [bar for bar in history.bars if bar.trading_date <= quote_day
                     and cutoff_utc(cfg, bar.trading_date) <= decision]
            evidence["future_or_unclosed_bars_excluded"] = len(history.bars) - len(dated)
            valid = [bar for bar in dated if all(finite(value) is not None and value > 0
                     for value in (bar.open, bar.high, bar.low, bar.close))
                     and bar.high >= max(bar.open, bar.close, bar.low)
                     and bar.low <= min(bar.open, bar.close)]
            bars = sorted({bar.trading_date: bar for bar in valid}.values(), key=lambda bar: bar.trading_date)
            evidence["history_bars"] = len(bars)
            if len(valid) != len(dated):
                quality["history_status"] = "invalid_history_bars"
            elif len(valid) != len(bars):
                quality["history_status"] = "duplicate_history_dates"
            elif len(bars) < RULES["minimum_history_bars"]:
                quality["history_status"] = "too_few_history_bars"
            elif (decision.astimezone(zone).date() - bars[-1].trading_date).days > max_age:
                quality["history_status"] = "stale_history"
            else:
                quality["history_status"] = "available"
                quality["point_in_time_verified"] = history_availability_basis == "recorded_receipt"
                quality["history_quote_alignment"] = "same_date" if bars[-1].trading_date == quote_day else "lagging"
                closes = [bar.close for bar in bars]
                close = closes[-1]
                ma20, ma60 = sum(closes[-20:]) / 20, sum(closes[-60:]) / 60
                ret5, ret20 = close / closes[-6] - 1, close / closes[-21] - 1
                dd20, dd60 = close / max(closes[-20:]) - 1, close / max(closes[-60:]) - 1
                evidence.update(history_feature_date=bars[-1].trading_date.isoformat(),
                                close=close, ma20=ma20, ma60=ma60, return_5=ret5, return_20=ret20,
                                drawdown_20=dd20, drawdown_60=dd60)
                if snapshot_category != "insufficient_data":
                    bearish = row.get("order_divergence", {}).get("signal") == "bearish_divergence"
                    if (ret20 >= RULES["high_return_20"] or close / ma20 - 1 >= RULES["high_distance_ma20"]) and (flow < 0 or bearish):
                        category = "high_level_divergence"
                    elif ret20 <= RULES["oversold_return_20"] and dd60 <= RULES["oversold_drawdown_60"] and ret5 >= RULES["recovery_return_5"] and flow >= 0:
                        category = "oversold_recovery"
                    elif close < ma20 < ma60 and ret20 < 0:
                        category = "trend_weakening"
                    elif ma20 > ma60 and ret20 > 0 and dd20 <= RULES["pullback_drawdown_20"] and close >= ma60 and ret5 <= RULES["pullback_max_return_5"]:
                        category = "pullback_watch"
                    elif close >= ma20 >= ma60 and ret20 > RULES["trend_min_return_20"] and flow > 0:
                        category = "trend_continuation"
                    else:
                        category = "range_no_edge"
    if quality["history_status"] != "available":
        reason_codes.append(quality["history_status"])
    asset = classify_etf(row.get("name", ""))
    positive = snapshot_category != "insufficient_data" and flow > 0 and pct >= RULES["flow_watch_threshold_pct"] and change >= 0
    negative = snapshot_category != "insufficient_data" and flow < 0 and pct <= -RULES["flow_watch_threshold_pct"]
    watchlists = []
    if positive or category in {"trend_continuation", "pullback_watch", "oversold_recovery"}:
        watchlists.append("opportunity_watch")
    if negative or category in {"high_level_divergence", "trend_weakening"}:
        watchlists.append("risk_watch")
    trigger, invalidation = FOLLOWUP[category]
    return {"symbol": row["symbol"], "name": row.get("name", row["symbol"]), **asset,
            "asset_class_label": ASSET_LABELS[asset["asset_class"]], "market_scope": market_scope(row.get("name", "")),
            "screen_group": row.get("screen_group"), "screen_score": row.get("screen_score"),
            "category": category, "category_label": CATEGORY_LABELS[category],
            "snapshot_category": snapshot_category, "snapshot_label": SNAPSHOT_LABELS[snapshot_category],
            "watchlists": watchlists, "quality": quality, "evidence": evidence,
            "reason_codes": reason_codes or ["matched_" + category],
            "followup_condition": trigger, "invalidation_condition": invalidation,
            "trade_action": None, "holding_context": "not_provided",
            "interpretation": "观察分类；不构成买入、持有或减持指令，单日资金不证明连续流入或流出"}


def _cached_history(cfg: dict, symbol: str):
    try:
        history = find_cache(symbol, configured_data_dirs(cfg))
    except (OSError, ValueError):
        return None, None, "missing"
    metadata = history.provider_metadata or {}
    for key in ("available_at_utc", "retrieved_at_utc", "fetched_at_utc"):
        if metadata.get(key):
            basis = "recorded_receipt" if metadata.get("sha256") == history.sha256 else "recorded_receipt_unbound_to_hash"
            return history, metadata[key], basis
    # Existing ETF CSVs have no receipt manifest. Record this weaker provenance
    # explicitly; these observations cannot be used as historical PIT validation.
    try:
        available = datetime.fromtimestamp(Path(history.source_path).stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return history, None, "missing"
    return history, available, "filesystem_mtime_only"


def _freeze_history(cfg: dict, history: SeriesSnapshot) -> SeriesSnapshot:
    """Preserve bytes used by this observation before live refresh replaces them.

    Receipt/mtime is captured by the caller before this copy. A content-addressed
    local copy is a replay input, never new evidence of historical availability.
    """
    raw = Path(history.source_path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != history.sha256:
        raise ValueError("observation history changed while being read")
    filename = f"cache_{history.symbol}.csv"
    if Path(filename).name != filename:
        raise ValueError("unsafe observation history symbol")
    root = (market_data_dir(cfg) / "snapshots").resolve()
    target = (root / history.sha256 / filename).resolve()
    if not target.is_relative_to(root):
        raise ValueError("observation history snapshot escapes its root")
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != history.sha256:
            raise ValueError("existing observation history snapshot hash mismatch")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{uuid.uuid4().hex}.partial"
        temporary.write_bytes(raw)
        # The target is content-addressed: concurrent creators have identical
        # bytes. Never replace a preexisting inconsistent snapshot.
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != history.sha256:
                raise ValueError("existing observation history snapshot hash mismatch")
            temporary.unlink()
        else:
            try:
                temporary.rename(target)
            except FileExistsError:
                # Windows may observe another identical creator between the
                # existence check and rename. Verify before accepting its file.
                if hashlib.sha256(target.read_bytes()).hexdigest() != history.sha256:
                    raise ValueError("existing observation history snapshot hash mismatch")
                temporary.unlink()
    return replace(history, source_path=str(target))


def build_opportunity_observation(cfg: dict, eligible: list[dict], selected: list[dict],
                                  manifest: dict, decision_at_utc: str) -> dict:
    if utc(manifest["retrieved_at_utc"]) > utc(decision_at_utc):
        raise ValueError("observation snapshot was unavailable at decision time")
    rows = []
    for row in eligible:
        history, available, basis = _cached_history(cfg, row["symbol"])
        source_path = history.source_path if history else None
        if history:
            history = _freeze_history(cfg, history)
        result = classify_observation(row, decision_at_utc, cfg, history, available, basis)
        if history:
            result["evidence"]["history_snapshot"] = {"path": history.source_path, "sha256": history.sha256}
            result["evidence"]["history_original_path"] = source_path
        rows.append(result)
    selected_symbols = {row["symbol"] for row in selected}
    for row in rows:
        row["in_frozen_selection"] = row["symbol"] in selected_symbols
    top = int(cfg.get("opportunity_observation", {}).get("watchlist_top_n", 20))
    if top < 1:
        raise ValueError("observation watchlist_top_n must be positive")
    opportunities = [row for row in rows if "opportunity_watch" in row["watchlists"]]
    risks = [row for row in rows if "risk_watch" in row["watchlists"]]
    opportunities.sort(key=lambda row: (-(row["screen_score"] or 0), row["symbol"]))
    risks.sort(key=lambda row: (row["evidence"]["main_net_inflow_pct"], row["evidence"]["main_net_inflow"], row["symbol"]))
    return {"schema_version": 1, "rule_version": RULES["version"], "rules": dict(RULES),
            "rule_sha256": hashlib.sha256(json.dumps(RULES, sort_keys=True).encode()).hexdigest(),
            "decision_at_utc": utc(decision_at_utc).isoformat(), "source_snapshot": manifest,
            "scope": "all_eligible_candidates_before_group_limit", "selection_effect": "none",
            "category_counts": {key: sum(row["category"] == key for row in rows) for key in CATEGORY_LABELS},
            "snapshot_counts": dict(Counter(row["snapshot_category"] for row in rows)),
            "asset_class_counts": dict(Counter(row["asset_class"] for row in rows)),
            "coverage": {"eligible": len(rows), "classified_with_history": sum(row["category"] != "insufficient_data" for row in rows),
                         "insufficient_data": sum(row["category"] == "insufficient_data" for row in rows),
                         "selected": len(selected), "opportunity_watch": len(opportunities), "risk_watch": len(risks)},
            "watchlist_top_n": top, "opportunity_watch": [row["symbol"] for row in opportunities[:top]],
            "risk_watch": [row["symbol"] for row in risks[:top]], "rows": rows,
            "caveat": "阈值为待回测的描述性规则；机会榜和风险榜允许交叉。风险榜用于复核，未读取持仓，不等于减持指令。旧冻结记录不回填本轮分类。"}

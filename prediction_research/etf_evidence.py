"""Bridge existing immutable ETF snapshots/events into the shared evidence layer."""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone

from .config import resolve_project_path
from .evidence import digest, eligible_records, etf_assets, load_records, now_utc, save_records, utc
from .events import _published_at
from .store import connect
from .tradingagents_adapter import decision_direction
from .order_divergence import order_divergence

FLOW_DEFINITION = "成交单大小分类估计；不是账户披露、机构身份、ETF 申赎或份额变动"


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def sync_etf_evidence(cfg: dict) -> dict:
    assets = etf_assets(cfg)
    records = []
    missing_time = 0
    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        rows = connection.execute(
            "SELECT snapshot_sha256,symbol,quote_epoch,retrieved_at_utc,main_net_inflow,"
            "main_net_inflow_pct,amount,change_pct,super_large_net_inflow,"
            "super_large_net_inflow_pct,large_net_inflow,large_net_inflow_pct FROM etf_flow_snapshots"
        ).fetchall()
    divergence_records = 0
    for sha, symbol, epoch, fetched, net, pct, amount, change, super_net, super_pct, large_net, large_pct in rows:
        if symbol not in assets:
            continue
        if not epoch:
            missing_time += 1
            continue
        observed = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
        records.append({
            "source": "etf_flow", "source_key": sha, "symbol": symbol, "kind": "flow",
            "claim": FLOW_DEFINITION, "epistemic": "fact", "source_ref": "eastmoney:etf_flow_snapshots",
            "snapshot_sha256": sha, "observed_at_utc": observed,
            "available_at_utc": max(utc(observed), utc(fetched)).isoformat(),
            "direction": "unknown", "score": max(-1.0, min(1.0, pct / 100)) if _finite(pct) else None,
            "metrics": {"net_inflow": net if _finite(net) else None,
                        "net_inflow_pct": pct if _finite(pct) else None,
                        "amount": amount if _finite(amount) else None,
                        "change_pct": change if _finite(change) else None},
            "identity_status": "unobservable", "flow_basis": "transaction_size_estimate",
            "missing": [key for key, value in (("net_inflow", net), ("net_inflow_pct", pct),
                                               ("amount", amount), ("change_pct", change)) if not _finite(value)],
        })
        divergence = order_divergence({"super_large_net_inflow": super_net,
                                       "super_large_net_inflow_pct": super_pct,
                                       "large_net_inflow": large_net,
                                       "large_net_inflow_pct": large_pct}, cfg["etf_market"]["screen"])
        if divergence["status"] == "available":
            records.append({
                "source": "etf_order_divergence", "source_key": sha, "symbol": symbol, "kind": "anomaly",
                "claim": "超大单与大单方向背离筛选因子；不代表账户身份或未来价格方向",
                "epistemic": "fact", "source_ref": "eastmoney:etf_flow_snapshots",
                "snapshot_sha256": sha, "observed_at_utc": observed,
                "available_at_utc": max(utc(observed), utc(fetched)).isoformat(),
                "direction": "up" if divergence["signal"] == "bullish_divergence" else
                             "down" if divergence["signal"] == "bearish_divergence" else "neutral",
                "score": divergence["factor"], "metrics": divergence,
                "identity_status": "unobservable", "flow_basis": "transaction_size_estimate",
                "limitations": ["不识别账户", "不是ETF申赎", "信号效果需独立样本外验证"],
            })
            divergence_records += 1
    return {**save_records(cfg, records), "order_divergence_records": divergence_records,
            "skipped_missing_quote_time": missing_time,
            "scope": "configured_etfs_only"}


def sync_news_chains(cfg: dict) -> dict:
    assets = etf_assets(cfg)
    # Freeze the actual mapping, not only its name, and keep first registration time.
    mapping = {symbol: {"exposure": asset["exposure"], "asset_type": asset["asset_type"]}
               for symbol, asset in assets.items()}
    mapping_hash = digest(mapping)
    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        connection.execute("INSERT OR IGNORE INTO evidence_mapping_versions VALUES (?, ?, ?)",
                           (mapping_hash, now_utc(), json.dumps(mapping, sort_keys=True)))
        first_seen = connection.execute("SELECT first_seen_at_utc FROM evidence_mapping_versions WHERE mapping_hash=?",
                                        (mapping_hash,)).fetchone()[0]
        connection.commit()
        sources = connection.execute(
            "SELECT e.event_id,e.canonical_hash,e.title,e.exposures_json,e.evidence_score,"
            "s.article_url,s.source_url,s.published_at,s.fetched_at_utc,s.raw_snapshot_sha256,"
            "s.raw_snapshot_path FROM events e JOIN event_sources s ON e.event_id=s.event_id "
            "ORDER BY s.fetched_at_utc,s.occurrence_id"
        ).fetchall()
    seen = set()
    records = []
    for event_id, canonical, title, exposures_json, score, article, source, published, fetched, sha, path in sources:
        for symbol, asset in assets.items():
            if asset["exposure"] not in json.loads(exposures_json) or (event_id, symbol) in seen:
                continue
            seen.add((event_id, symbol))
            # Older event snapshots contain RFC-2822 dates; preserve the original text.
            normalized_published = _published_at(published)
            available = max(utc(first_seen), utc(fetched), utc(normalized_published) if normalized_published else utc(fetched))
            records.append({
                "source": "news_chain", "source_key": f"{canonical}:{mapping_hash}",
                "symbol": symbol, "kind": "news", "claim": title, "epistemic": "hypothesis",
                "source_ref": article or source, "snapshot_sha256": sha, "raw_snapshot_path": path,
                "observed_at_utc": fetched, "published_at_utc": normalized_published,
                "original_published_at": published,
                "available_at_utc": available.isoformat(), "direction": "unknown", "score": None,
                "source_quality_score": score,
                "independence_key": canonical,
                "mapping_hash": mapping_hash, "mapping_first_seen_at_utc": first_seen,
                "chain": [{"type": "news", "id": event_id, "claim_status": "source_fact"},
                          {"type": "commodity_or_sector", "id": asset["exposure"],
                           "basis": "configured_exposure_keywords", "claim_status": "relevance_hypothesis"},
                          {"type": "etf", "id": symbol, "asset_type": asset["asset_type"],
                           "basis": "configured_fund_exposure", "claim_status": "relevance_hypothesis"}],
                "limitations": ["并非基金实时持仓", "未验证价格影响方向", "映射建立前不可用于历史回测"],
            })
    return {**save_records(cfg, records), "mapping_hash": mapping_hash,
            "mapping_available_at_utc": first_seen, "price_impact": "unverified"}


def sync_existing_agents(cfg: dict) -> dict:
    records = []
    assets = etf_assets(cfg)
    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        rows = connection.execute("SELECT analysis_id,symbol,decision_text,report_dir,created_at_utc "
                                  "FROM agent_analyses WHERE status='ok' AND decision_text IS NOT NULL").fetchall()
    for analysis_id, symbol, decision, report, created in rows:
        if symbol not in assets:
            continue
        direction = decision_direction(decision)
        direction = "unknown" if direction == "unclassified" else direction
        records.append({"source": "tradingagents", "source_key": analysis_id, "symbol": symbol,
                        "kind": "analysis", "claim": decision, "epistemic": "inference",
                        "source_ref": report or f"sqlite:agent_analyses/{analysis_id}",
                        "snapshot_sha256": digest([analysis_id, symbol, decision, report, created]),
                        "snapshot_basis": "frozen_agent_analysis_row", "observed_at_utc": created,
                        "available_at_utc": created, "direction": direction,
                        "score": {"up": 1.0, "down": -1.0, "neutral": 0.0}.get(direction)})
    return save_records(cfg, records)


def daily_flows(rows: list[dict], cfg: dict) -> list[dict]:
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))
    daily = {}
    for row in sorted(rows, key=lambda item: (utc(item["observed_at_utc"]), utc(item["available_at_utc"]), item["evidence_id"])):
        if row["source"] == "etf_flow":
            day = utc(row["observed_at_utc"]).astimezone(zone).date().isoformat()
            daily[day] = row
    return [daily[day] for day in sorted(daily)]


def etf_profiles(cfg: dict, as_of: datetime | None = None) -> dict:
    as_of = as_of or datetime.now(timezone.utc)
    rows = load_records(cfg)
    profiles = []
    threshold = float(cfg.get("evidence", {}).get("flow_anomaly_pct", 20))
    for symbol, asset in sorted(etf_assets(cfg).items()):
        eligible = eligible_records(cfg, rows, symbol, as_of)
        daily = daily_flows(eligible, cfg)
        valid = [row for row in daily if row["metrics"]["net_inflow"] is not None]
        directions = [1 if row["metrics"]["net_inflow"] > 0 else -1 if row["metrics"]["net_inflow"] < 0 else 0
                      for row in valid]
        # A missing daily observation interrupts a run; absent trading days cannot be inferred.
        streak = 0
        if directions and len(valid) == len(daily):
            for direction in reversed(directions):
                if direction != directions[-1] or direction == 0:
                    break
                streak += 1
        latest = daily[-1] if daily else None
        pct = latest["metrics"]["net_inflow_pct"] if latest else None
        actor_evidence = [row for row in eligible if row["kind"] == "actor_profile"]
        profiles.append({
            "symbol": symbol, "name": asset["name"], "observed_days": len(daily),
            "net_inflow_observations": len(valid),
            "net_inflow_sum": sum(row["metrics"]["net_inflow"] for row in valid) if valid else None,
            "same_direction_observation_streak": streak,
            "streak_definition": "连续已观测截面，未证明交易日连续性或账户持仓周期",
            "latest_observed_at_utc": latest["observed_at_utc"] if latest else None,
            "flow_imbalance": "no_data" if pct is None else "threshold_exceeded" if abs(pct) >= threshold else "within_threshold",
            "flow_imbalance_threshold_pct": threshold,
            "flow_definition": FLOW_DEFINITION,
            "actor_identity": "external_claims_require_source_review" if actor_evidence else "unobservable",
            "actor_evidence_ids": [row["evidence_id"] for row in actor_evidence],
            "missing_sources": ["ETF creations/redemptions", "disclosed account identity", "ETF seat-level LHB"],
            "evidence_ids": [row["evidence_id"] for row in daily],
        })
    return {"as_of_utc": as_of.isoformat(), "profiles": profiles, "scope": "etf_only"}


def chain_report(cfg: dict, as_of: datetime | None = None) -> dict:
    as_of = as_of or datetime.now(timezone.utc)
    rows = load_records(cfg)
    chains = []
    for symbol in etf_assets(cfg):
        distinct = {}
        for row in eligible_records(cfg, rows, symbol, as_of):
            if row["source"] == "news_chain":
                distinct[row["independence_key"]] = row
        chains.extend(distinct.values())
    by_exposure = defaultdict(int)
    for row in chains:
        by_exposure[row["chain"][1]["id"]] += 1
    return {"as_of_utc": as_of.isoformat(), "chain_count": len(chains),
            "by_exposure": dict(by_exposure), "chains": chains,
            "direction_policy": "unknown_until_independently_supported"}

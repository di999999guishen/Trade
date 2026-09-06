"""Paired ETF evidence ablations using the existing price-volume walk-forward model."""
from __future__ import annotations

import json
import math
from bisect import bisect_left
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import resolve_project_path
from .data import load_universe
from .etf_evidence import daily_flows
from .evaluation import metrics, serialize_results, walk_forward
from .evidence import FEATURE_GROUPS, cutoff_utc, digest, eligible_records, etf_assets, load_records, now_utc, utc
from .features import FEATURE_NAMES, build_samples
from .store import connect, record_run


def group_features(cfg: dict, records: list[dict], sample, group: str) -> tuple[float, ...]:
    rows = [row for row in eligible_records(cfg, records, sample.symbol,
                                           cutoff_utc(cfg, sample.feature_date)) if row["source"] == group]
    if group == "etf_flow":
        rows = daily_flows(rows, cfg)[-1:]
    else:
        # Revised mappings / repeated agents describing one event are one observation.
        dedup = {}
        for row in rows:
            dedup[row.get("independence_key", row["source_key"])] = row
        rows = list(dedup.values())
    scores = [row["score"] for row in rows if row.get("score") is not None]
    return (sum(scores) / len(scores) if scores else 0.0,
            float(bool(scores)), math.log1p(len(rows)))


def training_observation_days(cfg: dict, records: list[dict], samples: list, fold: dict) -> int:
    cutoffs = {}
    for row in samples:
        if fold["train_start"] <= row.feature_date.isoformat() and row.target_end_date.isoformat() < fold["test_start"]:
            cutoffs.setdefault(row.symbol, []).append(cutoff_utc(cfg, row.feature_date))
    for symbol in cutoffs:
        cutoffs[symbol].sort()
    days = set()
    zone = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))
    for record in records:
        dates = cutoffs.get(record["symbol"], [])
        position = bisect_left(dates, utc(record["available_at_utc"]))
        if position < len(dates) and eligible_records(cfg, [record], record["symbol"], dates[position]):
            days.add(utc(record["observed_at_utc"]).astimezone(zone).date())
    return len(days)


def run_evidence_experiments(cfg: dict, universe: str = "commodity", horizon: int = 5) -> tuple:
    if horizon not in cfg["horizons"]:
        raise ValueError(f"unsupported horizon: {horizon}")
    metadata, snapshots = load_universe(cfg, universe)
    if set(metadata["metadata"]) - set(etf_assets(cfg)):
        raise ValueError("evidence experiments are ETF-only")
    if len(snapshots) < 2:
        raise ValueError("need at least two ETF histories for paired experiments")
    samples = [row for snapshot in snapshots.values() for row in build_samples(snapshot, horizon)]
    records = load_records(cfg)
    config = cfg.get("evidence", {})
    min_days = int(config.get("minimum_observation_days", 60))
    min_rows = int(config.get("minimum_test_rows", 200))
    min_train_days = int(config.get("minimum_train_observation_days", 20))
    runs = resolve_project_path(cfg, cfg["runs_dir"])
    runs.mkdir(parents=True, exist_ok=True)
    input_hash = digest({"version": "etf-evidence-ablation-v3", "universe": metadata,
                         "horizon": horizon, "snapshots": {s: p.sha256 for s, p in snapshots.items()},
                         "evidence": records, "config": config, "model": cfg["model"],
                         "cutoff": cfg.get("data_cutoff"), "timezone": cfg.get("timezone")})
    for prior in sorted(runs.glob(f"evidence_ablation_{universe}_{horizon}d_*.json"), reverse=True):
        cached = json.loads(prior.read_text(encoding="utf-8"))
        if cached.get("input_hash") == input_hash:
            return prior, cached
    baseline, folds = walk_forward(samples, cfg["model"])
    base_metrics = metrics(baseline)

    def keys(row):
        return row.symbol, row.feature_date

    indexed = {group: {symbol: [row for row in records if row["source"] == group and row["symbol"] == symbol]
                       for symbol in snapshots} for group in FEATURE_GROUPS}
    features = {group: {keys(row): group_features(cfg, indexed[group][row.symbol], row, group) for row in samples}
                for group in FEATURE_GROUPS}
    # Count observed evidence days, not repeated sample dates carrying one stale observation.
    zone = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))
    observed = {group: {utc(row["observed_at_utc"]).astimezone(zone).date() for row in records
                       if row["source"] == group and row["symbol"] in snapshots}
                for group in FEATURE_GROUPS}
    readiness = {}
    for group in FEATURE_GROUPS:
        covered = {keys(row) for row in samples if features[group][keys(row)][2] > 0}
        eligible_test_dates = set()
        for fold in folds:
            # Only evaluate evidence gains in folds with actual earlier training evidence.
            group_records = [record for symbol_rows in indexed[group].values() for record in symbol_rows]
            if len(observed[group]) >= min_days and training_observation_days(cfg, group_records, samples, fold) >= min_train_days:
                eligible_test_dates.update(row.feature_date for row in baseline
                                           if fold["test_start"] <= row.feature_date.isoformat() <= fold["test_end"])
        comparison_keys = {keys(row) for row in baseline
                           if keys(row) in covered and row.feature_date in eligible_test_dates}
        reason = ("waiting_for_evidence" if not observed[group] else
                  "waiting_for_history" if len(observed[group]) < min_days or len(comparison_keys) < min_rows
                  else "ready")
        readiness[group] = {"status": reason, "observed_days": len(observed[group]),
                            "covered_labeled_rows": len(covered), "comparable_test_rows": len(comparison_keys),
                            "comparison_keys": comparison_keys}
    variants = [(f"base+{group}", (group,)) for group in FEATURE_GROUPS]
    variants.append(("base+all", FEATURE_GROUPS))
    variants.extend((f"all-minus-{group}", tuple(g for g in FEATURE_GROUPS if g != group)) for group in FEATURE_GROUPS)
    results = []
    for name, groups in variants:
        waiting = {group: readiness[group]["status"] for group in groups if readiness[group]["status"] != "ready"}
        if waiting:
            results.append({"variant": name, "groups": groups, "status": "waiting_for_evidence_or_history", "reasons": waiting})
            continue
        paired_keys = set.intersection(*(readiness[group]["comparison_keys"] for group in groups))
        if len(paired_keys) < min_rows:
            results.append({"variant": name, "groups": groups, "status": "waiting_for_common_coverage",
                            "comparable_test_rows": len(paired_keys)})
            continue
        augmented = [replace(row, features=row.features + tuple(value for group in groups for value in features[group][keys(row)]))
                     for row in samples]
        predicted, variant_folds = walk_forward(augmented, cfg["model"])
        if variant_folds != folds or {keys(row) for row in predicted} != {keys(row) for row in baseline}:
            raise ValueError("paired experiment fold/sample mismatch")
        paired_base = [row for row in baseline if keys(row) in paired_keys]
        paired_variant = [row for row in predicted if keys(row) in paired_keys]
        before, after = metrics(paired_base), metrics(paired_variant)
        improves = after["brier"] < before["brier"] and after["brier"] < after["historical_rate_brier"]
        results.append({"variant": name, "groups": groups, "status": "evaluated_research_only",
                        "baseline_metrics": before, "metrics": after,
                        "delta_brier": round(after["brier"] - before["brier"], 6),
                        "delta_accuracy": round(after["accuracy"] - before["accuracy"], 6),
                        "improved_on_both_baselines": improves,
                        "promotion": "disabled_pending_independent_forward_validation",
                        "predictions": serialize_results(paired_variant),
                        "paired_baseline_predictions": serialize_results(paired_base)})
    payload = {
        "run_type": "etf_evidence_ablation", "created_at_utc": now_utc(), "universe": universe,
        "input_hash": input_hash,
        "horizon": horizon, "label": "next session open to horizon close",
        "baseline_model_version": "price-volume-logistic-v2", "baseline_metrics": base_metrics,
        "baseline_predictions": serialize_results(baseline), "base_feature_names": FEATURE_NAMES,
        "extra_features_per_group": ["mean_score", "score_present", "log_evidence_count"],
        "sample_hash": digest([[row.symbol, row.feature_date.isoformat(), row.target_up, row.features] for row in samples]),
        "evidence_hash": digest(records), "evidence_ids": [row["evidence_id"] for row in records],
        "model_config": cfg["model"], "evidence_config": config,
        "snapshots": {symbol: {"sha256": snapshot.sha256, "path": snapshot.source_path}
                      for symbol, snapshot in snapshots.items()}, "missing_assets": metadata["missing"],
        "folds": folds, "readiness": {group: {key: value for key, value in item.items() if key != "comparison_keys"}
                                     for group, item in readiness.items()},
        "variants": results, "status": "evaluated" if all(row["status"] == "evaluated_research_only" for row in results)
        else "partially_evaluated_with_data_waits" if any(row["status"] == "evaluated_research_only" for row in results)
        else "waiting_for_evidence_or_history",
        "validation_scope": "conditional_on_current_universe_not_historical_etf_screen",
        "minimum_observation_days": min_days, "minimum_comparable_test_rows": min_rows,
        "interpretation": "Paired research diagnostics; no causal proof or trading-cost backtest; no automatic promotion",
    }
    path = runs / f"evidence_ablation_{universe}_{horizon}d_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        record_run(connection, "evidence_ablation", cfg, path)
    return path, payload

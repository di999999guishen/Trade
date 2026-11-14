"""Observation-only diagnostics for the ETF coarse screen.

Mechanism adapted from the screening engine in
``ZhuLinsen/daily_stock_analysis`` (reference revision ``ac9c33df``,
Apache-2.0; see its ``docs/screening-engine.md`` and
``src/services/screening/{filter,scorer,snapshot,source_guard}.py``). Four
ideas are ported, re-implemented here against ETF flow snapshots with the
standard library only:

  1. explicit, ordered source chain plus degraded-field contract;
  2. hard-filter waterfall where a missing field is never a pass;
  3. effective (post-``screen_mode``) factor weights recorded with the run;
  4. last-good observation with an explicit stale marker.

Schema v2 (2026-11-14) adds seven purely observational blocks requested by the
user: ``field_coverage``, ``quote_freshness``, ``score_distribution``,
``factor_availability``, ``guard_counts``, ``group_competition``,
``staleness`` and ``run_contract``. They aggregate the *same* records that had
already been loaded by ``select_etfs``; no predicate is re-executed, no extra
network call, no new dependency, and no candidate list is copied into the
diagnostics payload.

Deliberately NOT ported: pandas / individual-stock factor scoring, PE-PB and
turnover-rate factors, overseas sources (yfinance/Reddit/X/Polymarket),
LLM re-ranking, hotspot/news enrichment and ``variant_seed`` rotation. The
local project forbids those data sources, and importing pandas on this host
triggers the known WMI hang.

Invariants (asserted in ``tests/test_screen_diagnostics.py``):

  * ``observe_only`` — never reorders, adds or removes a selected symbol;
  * never touches ``etf_market.screen``, so ``rule_identity`` / ``rule_hash``
    and the frozen ``screen_batches`` semantics stay byte-identical;
  * last-good is diagnostics only and **NEVER** a source of candidates;
  * a missing field is never treated as a passing observation;
  * stock picking remains disabled while ICIR < 0.30 (``evaluation.ICIR_GATE``);
    this module produces no buy/sell signal.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2
DEFAULT_SOURCE_PRIORITY = ("eastmoney", "sina")
STAGE_ORDER = (
    "money_category_excluded",
    "eligibility",
    "money_market_exclusion",
    "missing_money_flow",
    "group_cap_exceeded",
)
REQUIRED_FIELDS = (
    "symbol", "name", "price", "change_pct", "amount", "market_cap",
    "main_net_inflow", "main_net_inflow_pct", "quote_epoch",
)
RED_LINE_FIELDS = ("main_net_inflow", "main_net_inflow_pct")
DISCLAIMER = (
    "observe_only diagnostics; ICIR < 0.30 gate means stock picking is disabled; "
    "discipline price levels / conditional orders only, not investment advice"
)
OBSERVATION_POLICY = "aggregates only; no candidate list, no predicate re-execution, no decision change"


def _is_missing(value) -> bool:
    """Conservative unknown marker; mirrors the 'missing never passes' rule."""
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip().lower() in {"nan", "none", "null", "-", "--"}
    if isinstance(value, (int, float)):
        return not math.isfinite(float(value))
    return True


def _number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json_safe(value):
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)


def _round(value, digits=6):
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolation percentile; avoids numpy/pandas entirely."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = min(1.0, max(0.0, q)) * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(position)]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _stats(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "min": None, "p10": None, "median": None,
                "p90": None, "max": None, "mean": None}
    return {
        "count": len(values),
        "min": _round(min(values)),
        "p10": _round(_percentile(values, 0.10)),
        "median": _round(_percentile(values, 0.50)),
        "p90": _round(_percentile(values, 0.90)),
        "max": _round(max(values)),
        "mean": _round(sum(values) / len(values)),
    }


def _parse_utc(text) -> datetime | None:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _epoch_utc(epoch) -> str | None:
    number = _number(epoch)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _counter(rows: list, key: str) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        label = str(row.get(key) if row.get(key) is not None else "unknown")
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def build_source_chain(manifest: dict, snapshot: dict, priority=None) -> dict:
    """Describe the *actual* provider chain behind this coarse-screen snapshot.

    The eastmoney adapter falls back to Sina when the quote API is blocked;
    that fallback loses the large/medium/small order-size flow layers. Recording
    the chain makes the degradation machine-readable instead of a log-only fact.
    """
    ordered = tuple(str(item) for item in (priority or DEFAULT_SOURCE_PRIORITY))
    label = str(manifest.get("source") or snapshot.get("source") or "")
    lowered = label.lower()
    if "sina" in lowered:
        actual = "sina"
    elif "eastmoney" in lowered or "push2" in lowered:
        actual = "eastmoney"
    else:
        actual = "unknown"
    fallback = snapshot.get("fallback") if isinstance(snapshot.get("fallback"), dict) else {}
    degraded = list(fallback.get("degraded_fields") or [])
    records = manifest.get("records")
    with_flow = manifest.get("with_flow")
    coverage = None
    if isinstance(records, (int, float)) and records > 0 and isinstance(with_flow, (int, float)):
        coverage = round(float(with_flow) / float(records), 6)
    return {
        "priority": list(ordered),
        "actual_source": actual,
        "source_label": label,
        "fallback_used": bool(manifest.get("fallback_used") or fallback),
        "fallback_reason": fallback.get("reason"),
        "degraded_fields": degraded,
        "degraded_field_count": len(degraded),
        "records": records,
        "with_flow": with_flow,
        "flow_coverage": coverage,
        "sha256": manifest.get("sha256"),
        "retrieved_at_utc": manifest.get("retrieved_at_utc"),
        "flow_definition": snapshot.get("flow_definition"),
        "priority_position": ordered.index(actual) if actual in ordered else None,
    }


def build_effective_weights(rules: dict, eligible: list) -> dict:
    """Record the weights that actually scored this run, including zeroed ones.

    ``screen_mode=liquidity_only`` silently zeroes flow/momentum/flow_to_cap in
    ``select_etfs``; the raw config therefore does not describe the run. Prefer
    the real per-row ``screen_weights`` when any eligible row exists.
    """
    mode = str(rules.get("screen_mode", "composite")).lower()
    if eligible and isinstance(eligible[0].get("screen_weights"), dict):
        weights = {str(key): float(value) for key, value in eligible[0]["screen_weights"].items()}
        basis = "eligible_row_screen_weights"
    else:
        configured = rules.get("score_weights") or {}
        weights = {
            "flow": float(configured.get("main_flow", 0.50)),
            "liquidity": float(configured.get("liquidity", 0.35)),
            "momentum": float(configured.get("momentum", 0.15)),
            "order_divergence": float(configured.get("order_divergence", 0.15)),
            "flow_to_cap": float(configured.get("flow_to_cap", 0.0)),
            "relative_strength": float(configured.get("relative_strength", 0.0)),
            "overheat_penalty": float(configured.get("overheat_penalty", 0.0)),
        }
        if mode == "liquidity_only":
            for key in ("flow", "momentum", "flow_to_cap"):
                weights[key] = 0.0
        basis = "rules_derived_no_eligible_rows"
    return {
        "screen_mode": mode,
        "basis": basis,
        "weights": {key: round(value, 6) for key, value in sorted(weights.items())},
        "zeroed_factors": sorted(key for key, value in weights.items() if value == 0.0),
        "policy": "missing factors excluded from the denominator; heuristic weights, not calibrated probabilities",
    }


def build_waterfall(rejections: list, universe_count: int, eligible_count: int,
                    selected_count: int, max_samples: int = 5) -> dict:
    """Summarise each hard-filter stage without re-running the predicates.

    ``rejections`` is produced by ``select_etfs`` itself, so the diagnosis can
    never drift away from the real eligibility decision.
    """
    stages = {}
    for row in rejections:
        stage = str(row.get("stage") or "unknown")
        bucket = stages.setdefault(stage, {"removed": 0, "reasons": {}, "samples": [],
                                          "missing": 0})
        bucket["removed"] += 1
        reason = str(row.get("reason") or "unspecified")
        bucket["reasons"][reason] = bucket["reasons"].get(reason, 0) + 1
        if row.get("field_status") == "missing" or row.get("missing_fields"):
            bucket["missing"] += 1
        if len(bucket["samples"]) < max(0, int(max_samples)):
            bucket["samples"].append({"symbol": row.get("symbol"), "name": row.get("name"),
                                      "reason": reason})
    ordered = [stage for stage in STAGE_ORDER if stage in stages]
    ordered += sorted(stage for stage in stages if stage not in STAGE_ORDER)
    steps, remaining = [], int(universe_count)
    for stage in ordered:
        bucket = stages[stage]
        remaining -= bucket["removed"]
        steps.append({"stage": stage, "removed": bucket["removed"],
                      "remaining_after": max(0, remaining),
                      "missing_field_records": bucket["missing"],
                      "reasons": dict(sorted(bucket["reasons"].items())),
                      "samples": bucket["samples"]})
    missing_by_field = {}
    for row in rejections:
        for field in row.get("missing_fields") or []:
            missing_by_field[field] = missing_by_field.get(field, 0) + 1
    return {
        "steps": steps,
        "universe_records": universe_count,
        "eligible_records": eligible_count,
        "selected_records": selected_count,
        "total_rejections": len(rejections),
        "missing_by_field": dict(sorted(missing_by_field.items())),
        "missing_policy": "a missing field is never a pass; the red line requires main_net_inflow(_pct) to be present",
        "basis": "rejection records emitted by select_etfs; no predicate re-execution",
    }


# ---------------------------------------------------------------------------
# Schema v2: additional observation-only blocks.
# ---------------------------------------------------------------------------

def build_field_coverage(fresh: list, snapshot: dict, degraded_fields: list) -> dict:
    """Per-field completeness of the *fresh* records, including the red line.

    Mirrors the reference project's source-coverage guard, but it observes
    rather than gates: ``select_etfs`` still enforces
    ``main_net_inflow(_pct)``. Recording the numbers makes a degraded day
    distinguishable from a genuinely broken rule.
    """
    by_field = {}
    for field in REQUIRED_FIELDS:
        present = missing = non_finite = 0
        for row in fresh:
            value = row.get(field)
            if _is_missing(value):
                missing += 1
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    non_finite += 1
            else:
                present += 1
        total = len(fresh)
        by_field[field] = {
            "present": present,
            "missing": missing,
            "non_finite": non_finite,
            "coverage": _round(present / total) if total else None,
        }
    red_line_violations = sum(
        1 for row in fresh
        if any(_is_missing(row.get(field)) for field in RED_LINE_FIELDS)
    )
    sample_keys = set()
    for row in fresh[:50]:
        sample_keys.update(row.keys())
    absent_degraded = sorted(field for field in degraded_fields if field not in sample_keys)
    return {
        "records": len(fresh),
        "required_fields": list(REQUIRED_FIELDS),
        "by_field": by_field,
        "red_line_fields": list(RED_LINE_FIELDS),
        "red_line_violations": red_line_violations,
        "red_line_enforced_by": "select_etfs (missing main_net_inflow or main_net_inflow_pct is skipped)",
        "degraded_fields_declared": list(degraded_fields),
        "degraded_fields_absent_in_records": absent_degraded,
        "snapshot_record_count": len(snapshot.get("records") or []) if isinstance(snapshot.get("records"), list) else None,
        "policy": OBSERVATION_POLICY + "; coverage is reported, never used to relax a filter",
    }


def build_quote_freshness(fresh: list, snapshot: dict, decision_at_utc: str,
                          max_age_days: float | None) -> dict:
    """Age distribution of the quote epochs and how many records were dropped."""
    epochs = [number for row in fresh if (number := _number(row.get("quote_epoch"))) is not None and number > 0]
    decision = _parse_utc(decision_at_utc)
    latest = max(epochs) if epochs else None
    retrieved = _parse_utc(snapshot.get("retrieved_at_utc"))
    all_records = snapshot.get("records") if isinstance(snapshot.get("records"), list) else []
    all_epochs = [number for row in all_records
                  if isinstance(row, dict) and (number := _number(row.get("quote_epoch"))) is not None and number > 0]
    stale = 0
    if decision is not None and max_age_days is not None:
        limit = float(max_age_days) * 86400
        for number in all_epochs:
            delta = decision.timestamp() - number
            if delta < 0 or delta > limit:
                stale += 1
    days = set()
    for number in epochs:
        text = _epoch_utc(number)
        if text:
            days.add(text[:10])
    return {
        "records_with_epoch": len(epochs),
        "records_without_epoch": len(fresh) - len(epochs),
        "universe_records_with_epoch": len(all_epochs),
        "dropped_stale_or_future_records": stale,
        "max_age_days_policy": max_age_days,
        "min_quote_utc": _epoch_utc(min(epochs)) if epochs else None,
        "max_quote_utc": _epoch_utc(max(epochs)) if epochs else None,
        "median_quote_utc": _epoch_utc(_percentile(epochs, 0.5)) if epochs else None,
        "span_hours": _round((max(epochs) - min(epochs)) / 3600, 3) if epochs else None,
        "decision_lag_hours": _round((decision.timestamp() - latest) / 3600, 3)
        if decision is not None and latest is not None else None,
        "retrieved_at_utc": snapshot.get("retrieved_at_utc"),
        "retrieved_before_decision": bool(retrieved and decision and retrieved <= decision),
        "distinct_quote_days_utc": len(days),
        "policy": "quote days are UTC calendar dates; the production screen uses Asia/Shanghai local dates",
    }


def build_score_distribution(eligible: list, selected: list, top_groups: int = 5) -> dict:
    """Aggregate score/ cohort shape without copying any candidate row."""
    eligible_scores = [_number(row.get("screen_score")) for row in eligible]
    selected_scores = [_number(row.get("screen_score")) for row in selected]
    eligible_scores = [value for value in eligible_scores if value is not None]
    selected_scores = [value for value in selected_scores if value is not None]
    group_counts = _counter(selected, "screen_group")
    cutoff = min(selected_scores) if selected_scores else None
    above = sum(1 for value in eligible_scores if cutoff is not None and value > cutoff)
    return {
        "eligible": _stats(eligible_scores),
        "selected": _stats(selected_scores),
        "selection_cutoff_score": _round(cutoff),
        "eligible_above_cutoff": above,
        "cutoff_gap_to_next": _round(
            max((value for value in eligible_scores if cutoff is not None and value > cutoff), default=None)
            - cutoff) if cutoff is not None and above else None,
        "groups_selected": len(group_counts),
        "top_groups": dict(list(group_counts.items())[:max(0, int(top_groups))]),
        "by_asset_class": _counter(selected, "asset_class"),
        "by_market_scope": _counter(selected, "market_scope"),
        "policy": OBSERVATION_POLICY,
    }


def build_factor_availability(eligible: list, weights: dict) -> dict:
    """How often each factor was actually usable, plus denominator drift.

    ``select_etfs`` excludes missing factors from the score denominator, so two
    rows can be compared with different effective weight sums. That drift is a
    real (heuristic) comparability caveat; recording it makes it auditable.
    """
    factor_names = sorted({name for row in eligible
                           if isinstance(row.get("screen_components"), dict)
                           for name in row["screen_components"]})
    by_factor, denominators = {}, []
    for name in factor_names:
        available = missing = 0
        for row in eligible:
            components = row.get("screen_components") or {}
            if name in components and components[name] is not None:
                available += 1
            else:
                missing += 1
        weight = _number((weights or {}).get(name))
        by_factor[name] = {
            "available": available,
            "missing": missing,
            "availability": _round(available / len(eligible)) if eligible else None,
            "effective_weight": weight,
            "zero_weight": bool(weight == 0.0),
        }
    for row in eligible:
        components = row.get("screen_components") or {}
        row_weights = row.get("screen_weights") or {}
        denominator = sum(float(row_weights.get(name, 0.0) or 0.0)
                          for name, value in components.items() if value is not None)
        denominators.append(denominator)
    return {
        "eligible_records": len(eligible),
        "by_factor": by_factor,
        "denominator": {**_stats(denominators), "distinct": len(set(round(value, 6) for value in denominators))},
        "order_divergence_status": _counter(
            [row.get("order_divergence") or {} for row in eligible], "status"),
        "normalization_policy": "missing factors excluded from denominator; rows may therefore have different effective weight sums",
        "policy": OBSERVATION_POLICY,
    }


def build_guard_counts(rejections: list, rules: dict, eligible: list,
                       selection_cap: int | None, max_per_group: int | None = None) -> dict:
    """Counts behind every hard filter, plus an explicit red-line assertion."""
    by_stage = _counter(rejections, "stage")
    by_reason = _counter(rejections, "reason")
    missing_flow_rows = sum(1 for row in rejections if row.get("stage") == "missing_money_flow")
    red_line_ok = not any(
        _is_missing(row.get("main_net_inflow")) or _is_missing(row.get("main_net_inflow_pct"))
        for row in eligible
    )
    return {
        "by_stage": by_stage,
        "by_reason": by_reason,
        "missing_money_flow_intercepts": missing_flow_rows,
        "group_cap_intercepts": by_stage.get("group_cap_exceeded", 0),
        "eligibility_intercepts": by_stage.get("eligibility", 0),
        "money_market_intercepts": by_stage.get("money_market_exclusion", 0)
        + by_stage.get("money_category_excluded", 0),
        "missing_field_status_records": sum(1 for row in rejections if row.get("field_status") == "missing"),
        "red_line_enforced": red_line_ok,
        "red_line_statement": "no eligible row may lack main_net_inflow or main_net_inflow_pct",
        "max_per_group": max_per_group if max_per_group is not None else rules.get("max_per_group"),
        "selection_limit": selection_cap,
        "allowed_asset_classes": list(rules.get("asset_classes") or []),
        "policy": "counts describe the real decision; they never change it",
    }


def build_group_competition(eligible: list, selected: list, rejections: list) -> dict:
    """Cost of ``max_per_group``: higher-scoring rows rejected by the group cap.

    This is the single most interpretable number for the user's coarse screen:
    a group cap can discard a higher-scored ETF in favour of a lower-scored one
    from another group. It is an observation, not a recommendation to change
    the cap (that would alter ``rule_hash`` and the frozen screen semantics).
    """
    selected_scores = [_number(row.get("screen_score")) for row in selected]
    selected_scores = [value for value in selected_scores if value is not None]
    cutoff = min(selected_scores) if selected_scores else None
    near_miss = []
    for row in rejections:
        if row.get("stage") != "group_cap_exceeded":
            continue
        score = _number(row.get("screen_score"))
        if score is not None and cutoff is not None and score > cutoff:
            near_miss.append(score)
    groups = _counter(rejections, "screen_group") if any(
        row.get("stage") == "group_cap_exceeded" for row in rejections) else {}
    eligible_groups = _counter(eligible, "screen_group")
    return {
        "near_miss_rejections": len(near_miss),
        "near_miss_score_advantage_max": _round(max(near_miss) - cutoff) if near_miss and cutoff is not None else None,
        "near_miss_score_advantage_mean": _round(sum(near_miss) / len(near_miss) - cutoff)
        if near_miss and cutoff is not None else None,
        "group_cap_rejected": sum(1 for row in rejections if row.get("stage") == "group_cap_exceeded"),
        "contested_groups": len(groups),
        "largest_group_candidates": max(eligible_groups.values()) if eligible_groups else 0,
        "eligible_groups": len(eligible_groups),
        "policy": "diagnoses the cost of the frozen max_per_group rule; never reorders or promotes a candidate",
    }


def build_staleness(previous: dict | None, decision_at_utc: str) -> dict:
    """Age of the last good observation and consecutive-failure streak."""
    decision = _parse_utc(decision_at_utc)
    written = _parse_utc((previous or {}).get("written_at_utc"))
    seconds = (decision - written).total_seconds() if decision and written else None
    return {
        "previous_record_present": previous is not None,
        "previous_status": (previous or {}).get("status"),
        "seconds_since_previous": _round(seconds, 3) if seconds is not None else None,
        "hours_since_previous": _round(seconds / 3600, 3) if seconds is not None else None,
        "previous_consecutive_failures": int((previous or {}).get("consecutive_failures") or 0),
        "currently_stale": bool(previous and previous.get("status") != "ok"),
        "policy": "a stale last-good record is never used as a candidate source",
    }


def build_run_contract(rules: dict, diagnostics_config: dict,
                       selection_cap: int | None, algorithm_version: int = 6) -> dict:
    """Fingerprint the inputs of THIS observation without touching rule_hash."""
    canonical = json.dumps(rules, ensure_ascii=False, sort_keys=True, default=str)
    exposed = {key: value for key, value in (diagnostics_config or {}).items()
               if key not in {"note", "last_good_path"}}
    return {
        "algorithm_version": algorithm_version,
        "rules_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "rules_sha256_note": "fingerprint of the screening rules only; NOT the frozen screen_batches rule_hash",
        "screen_mode": str(rules.get("screen_mode", "composite")).lower(),
        "selection_limit": selection_cap,
        "max_per_group": rules.get("max_per_group"),
        "asset_classes": list(rules.get("asset_classes") or []),
        "diagnostics_config": _json_safe(exposed),
        "policy": "metadata for reproducibility; no decision input",
    }


def read_last_good(path) -> dict | None:
    if path is None:
        return None
    target = Path(path)
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def commit_last_good(path, diagnostics: dict, *, status: str,
                     failure_reason: str | None = None) -> dict | None:
    """Persist one observation atomically. Never read back as a candidate source."""
    if path is None:
        return None
    target = Path(path)
    previous = read_last_good(target)
    previous_ok = previous if (previous or {}).get("status") == "ok" else None
    chain = diagnostics.get("source_chain") or {}
    waterfall = diagnostics.get("filter_waterfall") or {}
    coverage = diagnostics.get("field_coverage") or {}
    record = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "written_at_utc": datetime.now(timezone.utc).isoformat(),
        "decision_at_utc": diagnostics.get("decision_at_utc"),
        "source_chain": chain,
        "effective_factor_weights": diagnostics.get("effective_factor_weights"),
        "universe_records": waterfall.get("universe_records"),
        "eligible_records": waterfall.get("eligible_records"),
        "selected_records": waterfall.get("selected_records"),
        "red_line_violations": coverage.get("red_line_violations"),
        "consecutive_failures": (int(previous.get("consecutive_failures") or 0) + 1)
        if status != "ok" else 0,
        "staleness": diagnostics.get("staleness"),
        "failure_reason": failure_reason,
        "previous_ok": ({"decision_at_utc": previous_ok.get("decision_at_utc"),
                         "source_chain": previous_ok.get("source_chain")}
                        if previous_ok else None),
        "stale": status != "ok",
        "usage": "diagnostics_only",
        "not_a_candidate_source": True,
        "note": "Observation record. Frozen screen selection is never re-derived from this file.",
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(_json_safe(record), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        return None
    return record


def build_diagnostics(*, manifest: dict, snapshot: dict, fresh: list, rules: dict,
                      eligible: list, selected: list, rejections: list,
                      decision_at_utc: str, last_good_path=None,
                      source_priority=None, max_samples: int = 5,
                      max_age_days: float | None = None,
                      selection_cap: int | None = None,
                      diagnostics_config: dict | None = None,
                      top_groups: int = 5) -> dict:
    """Assemble the observation payload. Pure read except for last-good lookup."""
    chain = build_source_chain(manifest, snapshot, source_priority)
    weights = build_effective_weights(rules, eligible)
    previous = read_last_good(last_good_path)
    previous_chain = (previous or {}).get("source_chain") or {}
    priority_match = previous_chain.get("priority") == chain["priority"] if previous_chain else None
    source_match = previous_chain.get("actual_source") == chain["actual_source"] if previous_chain else None
    snapshot_with_retrieved = {**snapshot, "retrieved_at_utc": manifest.get("retrieved_at_utc")}
    degradation = list(chain.get("degraded_fields") or [])
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "observe_only",
        "decision_at_utc": decision_at_utc,
        "reference": {
            "project": "ZhuLinsen/daily_stock_analysis",
            "revision": "ac9c33dfcf4cb84974d7bfee9c8661d88b8cfbff",
            "license": "Apache-2.0",
            "ported": ["source_chain_contract", "hard_filter_waterfall",
                       "effective_factor_weights", "last_good_observation"],
            "excluded": ["pandas_stock_scoring", "pe_pb_turnover_factors",
                         "overseas_data_sources", "llm_rerank", "variant_rotation"],
        },
        "source_chain": chain,
        "last_good_binding": {
            "previous_record_present": previous is not None,
            "previous_status": (previous or {}).get("status"),
            "priority_match": priority_match,
            "actual_source_match": source_match,
            "policy": "mismatch is recorded only; a frozen screen is never re-selected because the provider chain changed",
        },
        "effective_factor_weights": weights,
        "filter_waterfall": build_waterfall(rejections, len(fresh), len(eligible),
                                            len(selected), max_samples),
        # --- schema v2 additions (all observation-only aggregates) ---
        "field_coverage": build_field_coverage(fresh, snapshot, degradation),
        "quote_freshness": build_quote_freshness(fresh, snapshot_with_retrieved,
                                                 decision_at_utc, max_age_days),
        "score_distribution": build_score_distribution(eligible, selected, top_groups),
        "factor_availability": build_factor_availability(eligible, weights.get("weights") or {}),
        "guard_counts": build_guard_counts(rejections, rules, eligible, selection_cap),
        "group_competition": build_group_competition(eligible, selected, rejections),
        "staleness": build_staleness(previous, decision_at_utc),
        "run_contract": build_run_contract(rules, diagnostics_config or {}, selection_cap),
        "observation_policy": OBSERVATION_POLICY,
        "not_a_candidate_source": True,
        "stock_picking": "disabled_while_icir_below_0.30",
        "disclaimer": DISCLAIMER,
    }

"""Isolated old/new calibration comparison; writes research artifacts only.

Run ``python -m prediction_research.calibration_audit --help``. The default
reference is the last five-candidate backtest from 2026-09-06. Candidate order,
market CSV hashes, features and out-of-sample outcome keys are checked before
comparison. This audit neither records predictions nor promotes a model.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import random
import statistics
import sys
import types
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import data, evaluation, features
from .config import load_config


PROJECT = Path(__file__).resolve().parent
OUTPUT_ROOT = PROJECT.parent / "reports" / "remediation_20260915" / "ablation"
BASELINE_CODE = OUTPUT_ROOT.parent / "baseline_code"
LIMITATIONS = [
    "Fixed later-selected candidates: conditional diagnostic, not an unbiased historical screening backtest.",
    "Exploratory comparison on previously inspected data; confidence intervals do not remove selection or survivorship bias.",
    "Overlapping horizon outcomes and cross-sectional dependence are grouped by date; moving blocks only approximate remaining dependence.",
    "Long cohorts overlap; returns are observations, not a funded portfolio NAV, annualized return or Sharpe ratio.",
    "Flat round-trip cost assumptions omit execution constraints, price impact and time-varying spreads.",
    "No automatic model promotion or change to production predictions, runs or state database.",
]


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


@contextmanager
def legacy_engine(directory: Path):
    """Load the saved model/evaluation in a private package, never the live model."""
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for filename in ("features.py", "model.py", "evaluation.py", "pipeline.py"):
        if manifest.get(filename) != file_hash(directory / filename):
            raise ValueError(f"baseline code hash mismatch: {filename}")
    name = "_calibration_audit_legacy_" + uuid.uuid4().hex
    package = types.ModuleType(name)
    package.__path__ = [str(directory)]
    sys.modules[name] = package
    # Both versions consume identical immutable CSV snapshots through this reader.
    sys.modules[name + ".data"] = data
    try:
        modules = {}
        for part in ("features", "model", "evaluation"):
            spec = importlib.util.spec_from_file_location(name + "." + part, directory / (part + ".py"))
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            # Execute the verified source without writing __pycache__ beside it.
            exec(compile((directory / (part + ".py")).read_bytes(), str(directory / (part + ".py")), "exec"), module.__dict__)
            modules[part] = module
        yield modules
    finally:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                del sys.modules[key]


def row_key(row: dict) -> tuple:
    return row["symbol"], row["feature_date"], row["target_end_date"]


def paired_rows(old_rows: list[dict], new_rows: list[dict]) -> list[tuple[dict, dict]]:
    indexes = []
    for label, rows in (("old", old_rows), ("new", new_rows)):
        indexed = {row_key(row): row for row in rows}
        if len(indexed) != len(rows):
            raise ValueError(f"duplicate {label} outcome keys")
        indexes.append(indexed)
    old, new = indexes
    if old.keys() != new.keys():
        raise ValueError(f"outcome key mismatch: old-only={len(old.keys() - new.keys())}, new-only={len(new.keys() - old.keys())}")
    pairs = []
    for key in sorted(old, key=lambda item: (item[1], item[0], item[2])):
        left, right = old[key], new[key]
        if left["actual_up"] != right["actual_up"] or not math.isclose(
                left["actual_return"], right["actual_return"], rel_tol=0, abs_tol=1e-12):
            raise ValueError(f"outcome label mismatch: {key}")
        pairs.append((left, right))
    return pairs


def paired_date_brier(old_rows: list[dict], new_rows: list[dict]) -> list[dict]:
    grouped = {}
    for old, new in paired_rows(old_rows, new_rows):
        day = grouped.setdefault(old["feature_date"], [])
        day.append(((old["probability_up"] - old["actual_up"]) ** 2,
                    (new["probability_up"] - new["actual_up"]) ** 2))
    return [{"feature_date": day, "rows": len(values),
             "old_brier": statistics.fmean(v[0] for v in values),
             "new_brier": statistics.fmean(v[1] for v in values),
             "new_minus_old": statistics.fmean(v[1] - v[0] for v in values)}
            for day, values in sorted(grouped.items())]


def moving_block_bootstrap(daily: list[dict], horizon: int, *, block_dates: int | None = None,
                           replicates: int = 2000, seed: int = 20260915) -> dict:
    if horizon < 1 or replicates < 1:
        raise ValueError("horizon and replicates must be positive")
    block = horizon if block_dates is None else block_dates
    if block < horizon:
        raise ValueError("block length must be at least the overlapping return horizon")
    ordered = sorted(daily, key=lambda row: row["feature_date"])
    if len({row["feature_date"] for row in ordered}) != len(ordered):
        raise ValueError("bootstrap expects one paired observation per date")
    values = [float(row["new_minus_old"]) for row in ordered]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("bootstrap differences must be finite")
    n = len(values)
    result = {"method": "noncircular_moving_date_blocks", "estimand": "equal_date_mean_new_minus_old_brier",
              "improvement_direction": "negative", "dates": n, "block_dates": block,
              "replicates": replicates, "seed": seed,
              "point_estimate": statistics.fmean(values) if values else None,
              "ci95": None, "status": "insufficient_dates_for_two_blocks",
              "interpretation": "Exploratory percentile interval; does not correct candidate selection or survivorship bias."}
    if n < 2 * block:
        return result
    rng = random.Random(seed)
    draws = []
    for _ in range(replicates):
        sampled = []
        while len(sampled) < n:
            start = rng.randrange(n - block + 1)
            sampled.extend(values[start:start + block])
        draws.append(statistics.fmean(sampled[:n]))
    draws.sort()

    def percentile(fraction):
        position = (len(draws) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(draws) - 1)
        return draws[lower] + (draws[upper] - draws[lower]) * (position - lower)

    return {**result, "status": "exploratory_interval", "ci95": [percentile(0.025), percentile(0.975)]}


def probability_metrics(rows: list[dict], field: str = "probability_up") -> dict:
    if not rows:
        return {"rows": 0}
    probabilities = [float(row[field]) for row in rows]
    if any(not math.isfinite(p) or not 0 <= p <= 1 for p in probabilities):
        raise ValueError(f"invalid probability in {field}")
    clipped = [max(1e-6, min(1 - 1e-6, p)) for p in probabilities]
    return {"rows": len(rows), "unique_dates": len({row["feature_date"] for row in rows}),
            "brier": statistics.fmean((p - row["actual_up"]) ** 2 for p, row in zip(probabilities, rows)),
            "log_loss": -statistics.fmean(row["actual_up"] * math.log(p) + (1 - row["actual_up"]) * math.log(1 - p)
                                            for p, row in zip(clipped, rows)),
            "log_loss_clip": 1e-6,
            "accuracy": statistics.fmean(int((p >= 0.5) == bool(row["actual_up"])) for p, row in zip(probabilities, rows)),
            "probability_min": min(probabilities), "probability_max": max(probabilities)}


def long_cohort_diagnostics(rows: list[dict], *, threshold: float = 0.55,
                            costs_bps: tuple[float, ...] = (0, 10, 30, 50), differentiated_only: bool = False) -> dict:
    if not 0 <= threshold <= 1 or any(not math.isfinite(cost) or cost < 0 for cost in costs_bps):
        raise ValueError("invalid fixed threshold or round-trip cost")
    chosen = [row for row in rows if row["probability_up"] >= threshold
              and (not differentiated_only or row.get("discrimination_status") == "model_differentiated")]
    grouped = {}
    for row in chosen:
        grouped.setdefault(row["feature_date"], []).append(row)
    cohorts = [{"feature_date": day, "rows": len(values), "symbols": sorted(row["symbol"] for row in values),
                "gross_return": statistics.fmean(row["actual_return"] for row in values)}
               for day, values in sorted(grouped.items())]
    costs = []
    for cost in costs_bps:
        net = [{**cohort, "net_return": cohort["gross_return"] - cost / 10000} for cohort in cohorts]
        costs.append({"round_trip_cost_bps": cost, "cohorts": len(net),
                      "mean_net_cohort_return": statistics.fmean(row["net_return"] for row in net) if net else None,
                      "positive_net_cohort_fraction": statistics.fmean(row["net_return"] > 0 for row in net) if net else None,
                      "cohort_rows": net})
    return {"selection": "probability_up >= fixed threshold", "threshold": threshold,
            "differentiated_only": differentiated_only,
            "eligible_rows": len(rows), "selected_rows": len(chosen),
            "observation_coverage": len(chosen) / len(rows) if rows else None,
            "selected_baseline_only_rows": sum(row.get("discrimination_status") == "baseline_only" for row in chosen),
            "selected_dates": len(cohorts),
            "mean_gross_cohort_return": statistics.fmean(row["gross_return"] for row in cohorts) if cohorts else None,
            "costs": costs,
            "interpretation": "Equal-weight selected symbols within each date, then equal-weight dates; overlapping observation cohorts, not portfolio returns."}


def run_engine(engine, samples: list, model_config: dict) -> dict:
    """Observe fitted models without altering either walk-forward algorithm."""
    original_fit = engine._fit_model
    fits = []

    def observed_fit(training, cfg):
        model = original_fit(training, cfg)
        fits.append((model, {"rows": len(training),
                            "first_feature_date": min(s.feature_date for s in training).isoformat(),
                            "last_feature_date": max(s.feature_date for s in training).isoformat(),
                            "latest_target": max(s.target_end_date for s in training).isoformat(),
                            "input_order_sha256": _json_hash([(s.symbol, s.feature_date.isoformat()) for s in training])}))
        return model

    engine._fit_model = observed_fit
    try:
        results, folds = engine.walk_forward(samples, model_config)
    finally:
        engine._fit_model = original_fit
    if len(fits) != len(folds):
        raise ValueError("fitted model count differs from walk-forward folds")
    rows = engine.serialize_results(results)
    sample_index = {(s.symbol, s.feature_date.isoformat()): s for s in samples}
    for fold, (model, training) in zip(folds, fits):
        if training["latest_target"] >= fold["test_start"]:
            raise ValueError("training target overlaps outer test window")
        state = "baseline_only" if model.calibration_blend == 0 else "model_differentiated" if model.calibrated else "uncalibrated"
        fold["audit_training"] = training
        fold["fitted_parameters"] = asdict(model)
        fold.setdefault("model_diagnostics", {
            "base_rate": model.base_rate, "calibration_a": model.calibration_a,
            "calibration_b": model.calibration_b, "calibration_blend": model.calibration_blend,
            "discrimination_status": state, "split_policy": "legacy_symbol_order_rows",
        })
        for row in rows:
            if fold["test_start"] <= row["feature_date"] <= fold["test_end"]:
                score = max(-35, min(35, model.raw_score(sample_index[row["symbol"], row["feature_date"]].features)))
                row["raw_probability_up"] = 1 / (1 + math.exp(-score))
                row["discrimination_status"] = state
                row["fold_test_start"] = fold["test_start"]
                row["model_core_base_rate"] = model.base_rate
    count = sum(row.get("discrimination_status") == "baseline_only" for row in rows)
    metrics = {**probability_metrics(rows), "baseline_only_rows": count,
               "baseline_only_coverage": count / len(rows) if rows else None}
    daily_baselines = []
    for day in sorted({row["feature_date"] for row in rows}):
        day_rows = [row for row in rows if row["feature_date"] == day]
        daily_baselines.append({"feature_date": day, "rows": len(day_rows),
                                "historical_training_rates": sorted({row["baseline_probability"] for row in day_rows}),
                                "historical_rate": probability_metrics(day_rows, "baseline_probability"),
                                "momentum_20d_hard_signal": probability_metrics(day_rows, "momentum_prediction")})
    return {"model_config": model_config, "metrics": metrics, "raw_metrics": probability_metrics(rows, "raw_probability_up"),
            "baselines": {"historical_training_rate": probability_metrics(rows, "baseline_probability"),
                          "momentum_20d_hard_signal": probability_metrics(rows, "momentum_prediction"),
                          "momentum_note": "20-day return > 0 is a hard direction signal; its clipped log loss is not calibrated probability quality.",
                          "by_date": daily_baselines},
            "long_cohort_diagnostics": {
                "all_probability_threshold": long_cohort_diagnostics(rows),
                "model_differentiated_only": long_cohort_diagnostics(rows, differentiated_only=True),
            }, "folds": folds, "predictions": rows}


def load_reference(path: Path) -> tuple[dict, dict]:
    reference = json.loads(path.read_text(encoding="utf-8"))
    if reference.get("feature_set") != "base" or reference.get("model_scope") != "pooled" or reference.get("horizon") not in (5, 20):
        raise ValueError("reference must be a pooled base-feature 5/20-day backtest")
    snapshots = {}
    for symbol, recorded in reference["snapshots"].items():
        cached = Path(recorded["path"])
        if cached.name != f"cache_{symbol}.csv":
            raise ValueError(f"unexpected cache filename: {cached}")
        snapshot = data.load_cache(symbol, cached.parent)
        if snapshot.sha256 != recorded["sha256"]:
            raise ValueError(f"market snapshot hash mismatch: {symbol}")
        snapshots[symbol] = snapshot
    return reference, snapshots


def _assert_same_samples(old: list, new: list):
    left = [(s.symbol, s.feature_date, s.target_end_date, s.features, s.target_return, s.target_up) for s in old]
    right = [(s.symbol, s.feature_date, s.target_end_date, s.features, s.target_return, s.target_up) for s in new]
    if left != right:
        raise ValueError("sample order/features/labels differ: comparison would not isolate calibration")


def _write_json(path: Path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def run_audit(baseline_code: Path, output_dir: Path, references: list[Path], cfg: dict,
              *, replicates: int = 2000, seed: int = 20260915) -> Path:
    output_dir = Path(output_dir).resolve()
    if not output_dir.is_relative_to(OUTPUT_ROOT.resolve()):
        raise ValueError(f"audit outputs must stay within {OUTPUT_ROOT}")
    loaded = [(Path(path).resolve(), *load_reference(Path(path))) for path in references]
    if sorted(item[1]["horizon"] for item in loaded) != [5, 20]:
        raise ValueError("provide exactly one reference for each of 5 and 20 days")
    signatures = [[(symbol, snapshot.sha256) for symbol, snapshot in item[2].items()] for item in loaded]
    if signatures[0] != signatures[1]:
        raise ValueError("horizons must use identical fixed candidates, order and market hashes")
    current_hashes = {filename: file_hash(PROJECT / filename) for filename in ("model.py", "evaluation.py", "features.py", "data.py")}
    baseline_hashes = json.loads((Path(baseline_code) / "manifest.json").read_text(encoding="utf-8"))
    stamp = datetime.now(timezone.utc).strftime("audit_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    destination = output_dir / stamp
    summary = {"run_type": "calibration_implementation_audit", "created_at_utc": datetime.now(timezone.utc).isoformat(),
               "promotion_status": "comparison_only_no_promotion", "fixed_candidates_in_order": [s for s, _ in signatures[0]],
               "model_config": cfg["model"], "model_config_sha256": _json_hash(cfg["model"]),
               "code_hashes": {"old": baseline_hashes, "new": current_hashes},
               "feature_policy": "identical features and symbol-major sample ordering before each engine",
               "bootstrap": {"replicates": replicates, "seed": seed, "block_policy": "block_dates=horizon"},
               "limitations": LIMITATIONS, "horizons": {}}
    with legacy_engine(baseline_code) as legacy:
        destination.mkdir(parents=True, exist_ok=False)
        _write_json(destination / "config.json", cfg)
        _write_json(destination / "manifest.json", summary)
        for ref_path, reference, snapshots in sorted(loaded, key=lambda item: item[1]["horizon"]):
            horizon = reference["horizon"]
            old_samples = [s for snapshot in snapshots.values() for s in legacy["features"].build_samples(snapshot, horizon)]
            new_samples = [s for snapshot in snapshots.values() for s in features.build_samples(snapshot, horizon)]
            _assert_same_samples(old_samples, new_samples)
            common = {"horizon": horizon, "reference_path": str(ref_path), "reference_sha256": file_hash(ref_path),
                      "fixed_candidates_in_order": list(snapshots), "snapshots": reference["snapshots"],
                      "limitations": LIMITATIONS}
            variants = {}
            for label, engine, samples in (("old", legacy["evaluation"], old_samples), ("new", evaluation, new_samples)):
                print(f"{horizon}d {label}: fitting walk-forward on {len(samples)} labeled rows", flush=True)
                result = run_engine(engine, samples, cfg["model"])
                variants[label] = {**common, "variant": label, "code_hashes": summary["code_hashes"][label], **result}
                _write_json(destination / f"{label}_{horizon}d.json", variants[label])
                print(f"{horizon}d {label}: Brier={result['metrics'].get('brier')}, baseline_only={result['metrics'].get('baseline_only_coverage')}", flush=True)
            old, new = variants["old"], variants["new"]
            boundary_fields = ("train_start", "latest_observed_target", "test_start", "test_end", "train_rows", "test_rows")
            if [tuple(f[k] for k in boundary_fields) for f in old["folds"]] != [tuple(f[k] for k in boundary_fields) for f in new["folds"]]:
                raise ValueError("old/new outer walk-forward boundaries differ")
            reference_pairs = paired_rows(reference["predictions"], old["predictions"])
            max_difference = max((abs(a["probability_up"] - b["probability_up"]) for a, b in reference_pairs), default=0)
            daily = paired_date_brier(old["predictions"], new["predictions"])
            comparison = {**common, "old_metrics": old["metrics"], "new_metrics": new["metrics"],
                          "paired_daily_brier": daily,
                          "bootstrap": moving_block_bootstrap(daily, horizon, replicates=replicates, seed=seed),
                          "row_weighted_brier_difference": new["metrics"]["brier"] - old["metrics"]["brier"],
                          "reference_reproduction": {"outcome_keys_and_labels_match": True, "max_probability_difference": max_difference,
                                                     "probabilities_reproduced_within_1e_12": max_difference <= 1e-12},
                          "promotion_status": "comparison_only_no_promotion"}
            _write_json(destination / f"comparison_{horizon}d.json", comparison)
            summary["horizons"][str(horizon)] = {k: comparison[k] for k in (
                "old_metrics", "new_metrics", "bootstrap", "row_weighted_brier_difference", "reference_reproduction")}
            _write_json(destination / "summary.json", summary)
    lines = ["# 校准实现对照审计", "", "固定历史候选、相同行情快照与样本；仅诊断，不自动晋级。", "",
             "| 周期 | 旧 Brier | 新 Brier | 旧仅基率占比 | 新仅基率占比 | 日期等权 Brier 差 95% 区间 |",
             "|---|---:|---:|---:|---:|---|"]
    for horizon, result in summary["horizons"].items():
        old, new = result["old_metrics"], result["new_metrics"]
        lines.append(f"| {horizon} 日 | {old['brier']:.6f} | {new['brier']:.6f} | {old['baseline_only_coverage']:.2%} | {new['baseline_only_coverage']:.2%} | {result['bootstrap']['ci95']} |")
    lines.extend(["", "差值为新减旧，负值表示 Brier 降低。移动日期块长度分别为 5/20 日，区间仅作探索。",
                  "固定 p≥0.55、0/10/30/50 bp 往返成本观察组及逐日期基线见各版本 JSON。", "",
                  "## 局限", "", *[f"- {item}" for item in LIMITATIONS]])
    (destination / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-code", type=Path, default=BASELINE_CODE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--reference", type=Path, action="append", help="Repeat once per horizon; defaults to final 2026-09-06 5/20d reports")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--bootstrap-replicates", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20260915)
    args = parser.parse_args()
    references = args.reference or [sorted((PROJECT / "runs").glob(f"backtest_screened_current_base_pooled_{h}d_20260906_235*.json"))[-1]
                                    for h in (5, 20)]
    destination = run_audit(args.baseline_code, args.output_dir, references, load_config(args.config),
                            replicates=args.bootstrap_replicates, seed=args.seed)
    print(json.dumps({"audit_directory": str(destination), "promotion_status": "comparison_only_no_promotion"}), flush=True)


if __name__ == "__main__":
    main()

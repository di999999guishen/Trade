from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import external_data_dir, market_data_dir, resolve_project_path
from .data import SeriesSnapshot, configured_data_dirs, find_cache, load_cache, load_universe
from .evaluation import metrics, serialize_results, walk_forward
from .features import EXTERNAL_FEATURE_NAMES, FEATURE_NAMES, Sample, augment_with_external, build_samples
from .model import LogisticModel
from .store import connect, insert_prediction, record_run


BASE_MODEL_VERSION = "price-volume-logistic-v2"
EXTERNAL_MODEL_VERSION = "commodity-external-logistic-v1"
EXPOSURE_MODEL_VERSION = "commodity-exposure-logistic-v1"
SCREENED_MODEL_VERSION = "screened-flow-price-logistic-v1"


def _run_path(cfg: dict, prefix: str) -> Path:
    root = resolve_project_path(cfg, cfg["runs_dir"])
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return root / f"{prefix}_{stamp}.json"


def _resolve_feature_set(universe: str, feature_set: str) -> str:
    if feature_set == "auto":
        return "external" if universe == "commodity" else "base"
    if feature_set not in {"base", "external"}:
        raise ValueError(f"unsupported feature set: {feature_set}")
    return feature_set


def _model_version(universe: str, feature_set: str, model_scope: str) -> str:
    if universe == "screened_current":
        return SCREENED_MODEL_VERSION
    if model_scope == "exposure":
        return EXPOSURE_MODEL_VERSION
    return EXTERNAL_MODEL_VERSION if feature_set == "external" else BASE_MODEL_VERSION


def _external_snapshots(cfg: dict) -> dict[str, SeriesSnapshot]:
    directory = external_data_dir(cfg)
    snapshots = {}
    for symbol in cfg.get("external_series", {}):
        try:
            snapshots[symbol] = load_cache(symbol, directory)
        except (FileNotFoundError, ValueError):
            continue
    return snapshots


def _samples_for_asset(cfg: dict, snapshot: SeriesSnapshot, asset: dict, horizon: int, feature_set: str, include_unlabeled: bool = False) -> list[Sample]:
    samples = build_samples(snapshot, horizon, include_unlabeled=include_unlabeled)
    if feature_set == "external":
        mapped = cfg.get("exposure_series", {}).get(asset.get("exposure"), [])
        samples = augment_with_external(samples, _external_snapshots(cfg), cfg.get("external_series", {}), mapped)
    return samples


def _all_samples(cfg: dict, snapshots: dict[str, SeriesSnapshot], metadata: dict, horizon: int, feature_set: str) -> list[Sample]:
    return [
        sample
        for symbol, snapshot in snapshots.items()
        for sample in _samples_for_asset(cfg, snapshot, metadata[symbol], horizon, feature_set)
    ]


def doctor(cfg: dict) -> dict:
    report = {"config": cfg["_config_path"], "universes": {}}
    for name in cfg["universes"]:
        metadata, snapshots = load_universe(cfg, name)
        report["universes"][name] = {
            "configured": len(metadata["metadata"]),
            "available": len(snapshots),
            "missing": metadata["missing"],
            "series": {
                symbol: {
                    "rows": len(snapshot.bars),
                    "start": snapshot.bars[0].trading_date.isoformat(),
                    "end": snapshot.bars[-1].trading_date.isoformat(),
                    "sha256": snapshot.sha256,
                }
                for symbol, snapshot in snapshots.items()
            },
        }
    return report


def run_backtest(cfg: dict, universe: str, horizon: int, feature_set: str = "auto", model_scope: str = "pooled") -> tuple[Path, dict]:
    metadata, snapshots = load_universe(cfg, universe)
    if len(snapshots) < 2:
        raise ValueError(f"{universe}: need at least two available series; missing={metadata['missing']}")
    feature_set = _resolve_feature_set(universe, feature_set)
    if model_scope not in {"pooled", "exposure"}:
        raise ValueError(f"unsupported model scope: {model_scope}")
    model_version = _model_version(universe, feature_set, model_scope)
    feature_names = FEATURE_NAMES + EXTERNAL_FEATURE_NAMES if feature_set == "external" else FEATURE_NAMES
    all_samples = _all_samples(cfg, snapshots, metadata["metadata"], horizon, feature_set)
    if model_scope == "pooled":
        results, folds = walk_forward(all_samples, cfg["model"])
    else:
        results, folds = [], []
        exposures = sorted({asset["exposure"] for asset in metadata["metadata"].values()})
        for exposure in exposures:
            symbols = {symbol for symbol, asset in metadata["metadata"].items() if asset["exposure"] == exposure}
            group_results, group_folds = walk_forward([sample for sample in all_samples if sample.symbol in symbols], cfg["model"])
            results.extend(group_results)
            folds.extend([{**fold, "exposure": exposure} for fold in group_folds])
    payload = {
        "run_type": "walk_forward_backtest",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "universe": universe,
        "horizon": horizon,
        "label": "next session open to horizon close",
        "model_version": model_version,
        "feature_set": feature_set,
        "model_scope": model_scope,
        "feature_names": feature_names,
        "validation_scope": "conditional_model_only_not_historical_money_flow_screen" if universe == "screened_current" else "full_configured_universe_model",
        "missing_assets": metadata["missing"],
        "snapshots": {symbol: {"path": item.source_path, "sha256": item.sha256, "provider": item.provider_metadata} for symbol, item in snapshots.items()},
        "metrics": metrics(results),
        "metrics_by_symbol": {
            symbol: metrics([row for row in results if row.symbol == symbol])
            for symbol in sorted(snapshots)
        },
        "metrics_by_exposure": {
            exposure: metrics([
                row for row in results
                if metadata["metadata"][row.symbol]["exposure"] == exposure
            ])
            for exposure in sorted({asset["exposure"] for asset in metadata["metadata"].values()})
        },
        "folds": folds,
        "predictions": serialize_results(results),
    }
    path = _run_path(cfg, f"backtest_{universe}_{feature_set}_{model_scope}_{horizon}d")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    db_path = resolve_project_path(cfg, cfg["state_db"])
    with connect(db_path) as connection:
        record_run(connection, "backtest", cfg, path)
    return path, payload


def _latest_backtest(cfg: dict, universe: str, horizon: int, feature_set: str, model_scope: str, model_version: str) -> dict | None:
    root = resolve_project_path(cfg, cfg["runs_dir"])
    paths = sorted(root.glob(f"backtest_{universe}_*{horizon}d_*.json"), reverse=True)
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (payload.get("model_version") == model_version and payload.get("feature_set", "base") == feature_set
                and payload.get("model_scope", "pooled") == model_scope):
            return {"path": str(path.resolve()), "metrics": payload.get("metrics", {})}
    return None


def _validation_status(cfg: dict, universe: str, horizon: int, feature_set: str, model_scope: str, model_version: str) -> dict:
    report = _latest_backtest(cfg, universe, horizon, feature_set, model_scope, model_version)
    if not report:
        return {"passed": False, "reason": "no matching walk-forward backtest", "report": None}
    metric = report["metrics"]
    gate = cfg["model"].get("validation_gate", {})
    if int(metric.get("rows", 0)) < int(gate.get("minimum_rows", 0)):
        return {"passed": False, "reason": "insufficient out-of-sample rows", "report": report}
    if gate.get("require_brier_below_historical_rate", True) and metric.get("brier", 1) >= metric.get("historical_rate_brier", 0):
        return {"passed": False, "reason": "Brier score did not beat historical-rate baseline", "report": report}
    return {"passed": True, "reason": "validation gate passed", "report": report}


def _latest_observed_training(samples: list[Sample], feature_date) -> list[Sample]:
    return [sample for sample in samples if sample.target_end_date and sample.target_end_date <= feature_date]


def run_prediction(cfg: dict, universe: str, horizon: int, feature_set: str = "auto", model_scope: str = "pooled") -> tuple[Path, dict]:
    metadata, snapshots = load_universe(cfg, universe)
    if len(snapshots) < 2:
        raise ValueError(f"{universe}: need at least two available series; missing={metadata['missing']}")
    feature_set = _resolve_feature_set(universe, feature_set)
    if model_scope not in {"pooled", "exposure"}:
        raise ValueError(f"unsupported model scope: {model_scope}")
    model_version = _model_version(universe, feature_set, model_scope)
    feature_names = FEATURE_NAMES + EXTERNAL_FEATURE_NAMES if feature_set == "external" else FEATURE_NAMES
    labeled = _all_samples(cfg, snapshots, metadata["metadata"], horizon, feature_set)
    latest = [_samples_for_asset(cfg, snapshot, metadata["metadata"][symbol], horizon, feature_set, include_unlabeled=True)[-1] for symbol, snapshot in snapshots.items()]
    latest_date = max(sample.feature_date for sample in latest)
    current = [sample for sample in latest if sample.feature_date == latest_date and sample.target_up is None]
    stale_assets = {sample.symbol: sample.feature_date.isoformat() for sample in latest if sample.feature_date != latest_date}
    eligible_training = _latest_observed_training(labeled, latest_date)
    validation = _validation_status(cfg, universe, horizon, feature_set, model_scope, model_version)
    db_path = resolve_project_path(cfg, cfg["state_db"])
    predictions = []
    from .evidence import cutoff_utc, eligible_records, load_records

    standard_evidence = load_records(cfg) if cfg.get("evidence", {}).get("enabled", False) else []
    training_rows = 0
    latest_observed_targets = []
    fitted_models: dict[str, LogisticModel] = {}
    screen_path = None
    screened_rows = {}
    if universe == "screened_current":
        from .screening import latest_screen

        screen_path, screen_payload = latest_screen(cfg)
        screened_rows = {row["symbol"]: row for row in screen_payload["selected"]}
    with connect(db_path) as connection:
        for sample in current:
            exposure = metadata["metadata"][sample.symbol]["exposure"]
            model_key = exposure if model_scope == "exposure" else "pooled"
            if model_key not in fitted_models:
                symbols = ({symbol for symbol, asset in metadata["metadata"].items() if asset["exposure"] == exposure}
                           if model_scope == "exposure" else set(snapshots))
                training = [row for row in eligible_training if row.symbol in symbols]
                allowed_dates = sorted({row.feature_date for row in training})[-int(cfg["model"]["max_train_dates"]):]
                training = [row for row in training if row.feature_date in set(allowed_dates)]
                if len(training) < 80:
                    raise ValueError(f"{exposure}: only {len(training)} eligible training rows")
                date_split = sorted({row.feature_date for row in training})
                calibration_count = max(1, int(len(date_split) * float(cfg["model"]["calibration_fraction"])))
                cutoff = date_split[-calibration_count]
                core = [row for row in training if row.feature_date < cutoff]
                calibration = [row for row in training if row.feature_date >= cutoff]
                cap = int(cfg["model"].get("max_fit_rows", 2500))
                if len(core) > cap:
                    step = len(core) / cap
                    core = [core[int(index * step)] for index in range(cap)]
                model = LogisticModel.fit(core, float(cfg["model"]["learning_rate"]), int(cfg["model"]["iterations"]), float(cfg["model"]["l2"]))
                if len(calibration) >= int(cfg["model"]["min_calibration_rows"]):
                    model.calibrate(calibration)
                fitted_models[model_key] = model
                training_rows += len(training)
                latest_observed_targets.append(max(row.target_end_date for row in training))
            model = fitted_models[model_key]
            snapshot = snapshots[sample.symbol]
            probability = model.predict_proba(sample.features)
            evidence = [{"type": feature_set + "_features", "feature_names": feature_names, "model_scope": model_scope, "exposure": exposure}]
            available_evidence = eligible_records(cfg, standard_evidence, sample.symbol,
                                                   cutoff_utc(cfg, sample.feature_date))
            if available_evidence:
                evidence.append({"type": "standard_etf_evidence", "schema_version": 1,
                                 "as_of_utc": cutoff_utc(cfg, sample.feature_date).isoformat(),
                                 "evidence_ids": [row["evidence_id"] for row in available_evidence],
                                 "probability_effect": "none_pending_independent_validation"})
            if sample.symbol in screened_rows:
                screen_row = screened_rows[sample.symbol]
                evidence.append({
                    "type": "money_flow_coarse_screen",
                    "screen_report": str(screen_path.resolve()),
                    "screen_score": screen_row["screen_score"],
                    "screen_group": screen_row["screen_group"],
                    "main_net_inflow": screen_row.get("main_net_inflow"),
                    "main_net_inflow_pct": screen_row.get("main_net_inflow_pct"),
                    "definition": "transaction-size classification estimate; not ETF creations/redemptions or disclosed institutional holdings",
                })
            item = {
                "symbol": sample.symbol,
                "name": metadata["metadata"][sample.symbol]["name"],
                "asset_type": metadata["metadata"][sample.symbol]["asset_type"],
                "feature_date": sample.feature_date.isoformat(),
                "horizon": horizon,
                "expected_target_end": None,
                "probability_up": round(probability, 6),
                "probability_status": (
                    "validated_calibrated" if validation["passed"] and model.calibrated
                    else "validated_uncalibrated" if validation["passed"]
                    else "research_only_failed_validation"
                ),
                "model_version": model_version,
                "data_snapshot": {"path": snapshot.source_path, "sha256": snapshot.sha256, "last_date": sample.feature_date.isoformat(), "provider": snapshot.provider_metadata},
                "evidence": evidence,
            }
            prediction_id, inserted = insert_prediction(connection, item)
            item["prediction_id"] = prediction_id
            item["inserted"] = inserted
            predictions.append(item)
    payload = {
        "run_type": "frozen_prediction",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "universe": universe,
        "horizon": horizon,
        "model_version": model_version,
        "feature_set": feature_set,
        "model_scope": model_scope,
        "training_rows": training_rows,
        "latest_observed_target": max(latest_observed_targets).isoformat(),
        "missing_assets": metadata["missing"],
        "stale_assets": stale_assets,
        "validation": validation,
        "predictions": predictions,
    }
    path = _run_path(cfg, f"prediction_{universe}_{feature_set}_{model_scope}_{horizon}d")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with connect(db_path) as connection:
        record_run(connection, "prediction", cfg, path)
    return path, payload


def settle_predictions(cfg: dict) -> dict:
    db_path = resolve_project_path(cfg, cfg["state_db"])
    all_assets = {item["symbol"]: item for values in cfg["universes"].values() for item in values}
    directories = configured_data_dirs(cfg)
    settled = []
    with connect(db_path) as connection:
        rows = connection.execute(
            "SELECT prediction_id, symbol, feature_date, horizon FROM predictions WHERE settled_at_utc IS NULL"
        ).fetchall()
        for prediction_id, symbol, feature_date, horizon in rows:
            if symbol not in all_assets:
                continue
            try:
                snapshot = find_cache(symbol, directories)
            except (FileNotFoundError, ValueError):
                continue
            sample = next((item for item in build_samples(snapshot, int(horizon)) if item.feature_date.isoformat() == feature_date), None)
            if not sample or sample.target_return is None:
                continue
            connection.execute(
                """UPDATE predictions SET settled_at_utc=?, target_end_date=?, actual_return=?, actual_up=?
                WHERE prediction_id=? AND settled_at_utc IS NULL""",
                (datetime.now(timezone.utc).isoformat(), sample.target_end_date.isoformat(), sample.target_return, sample.target_up, prediction_id),
            )
            settled.append({"prediction_id": prediction_id, "symbol": symbol, "actual_return": sample.target_return, "actual_up": sample.target_up})
        connection.commit()
    return {"eligible_open_predictions": len(rows), "settled": len(settled), "items": settled}

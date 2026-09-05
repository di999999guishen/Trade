from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date

from .features import Sample
from .model import LogisticModel


@dataclass(frozen=True)
class PredictionResult:
    symbol: str
    feature_date: date
    target_end_date: date
    probability_up: float
    actual_up: int
    actual_return: float
    calibrated: bool
    baseline_probability: float
    momentum_prediction: int


def _fit_model(training: list[Sample], model_cfg: dict) -> LogisticModel:
    dates = sorted({sample.feature_date for sample in training})
    calibration_dates = max(1, int(len(dates) * float(model_cfg["calibration_fraction"])))
    cutoff = dates[-calibration_dates]
    core = [sample for sample in training if sample.feature_date < cutoff]
    calibration = [sample for sample in training if sample.feature_date >= cutoff]
    if len(core) < 40 or len({sample.target_up for sample in core}) < 2:
        core, calibration = training, []
    cap = int(model_cfg.get("max_fit_rows", 2500))
    if len(core) > cap:
        step = len(core) / cap
        core = [core[int(index * step)] for index in range(cap)]
    model = LogisticModel.fit(
        core,
        learning_rate=float(model_cfg["learning_rate"]),
        iterations=int(model_cfg["iterations"]),
        l2=float(model_cfg["l2"]),
    )
    if len(calibration) >= int(model_cfg["min_calibration_rows"]):
        model.calibrate(calibration)
    return model


def walk_forward(samples: list[Sample], model_cfg: dict) -> tuple[list[PredictionResult], list[dict]]:
    samples = [sample for sample in samples if sample.target_end_date and sample.target_up is not None]
    dates = sorted({sample.feature_date for sample in samples})
    min_dates = int(model_cfg["min_train_dates"])
    max_dates = int(model_cfg["max_train_dates"])
    window = int(model_cfg["test_window_dates"])
    results: list[PredictionResult] = []
    folds: list[dict] = []
    for start in range(min_dates, len(dates), window):
        test_dates = dates[start : start + window]
        if not test_dates:
            break
        test_start = test_dates[0]
        eligible = [sample for sample in samples if sample.target_end_date < test_start]
        eligible_dates = sorted({sample.feature_date for sample in eligible})[-max_dates:]
        eligible_set = set(eligible_dates)
        training = [sample for sample in eligible if sample.feature_date in eligible_set]
        testing = [sample for sample in samples if sample.feature_date in set(test_dates)]
        if len(training) < 80 or len({sample.target_up for sample in training}) < 2:
            continue
        model = _fit_model(training, model_cfg)
        base_rate = sum(int(sample.target_up) for sample in training) / len(training)
        for sample in testing:
            results.append(PredictionResult(
                symbol=sample.symbol,
                feature_date=sample.feature_date,
                target_end_date=sample.target_end_date,
                probability_up=model.predict_proba(sample.features),
                actual_up=int(sample.target_up),
                actual_return=float(sample.target_return),
                calibrated=model.calibrated,
                baseline_probability=base_rate,
                momentum_prediction=int(sample.features[1] > 0),
            ))
        folds.append({
            "train_rows": len(training),
            "train_start": min(sample.feature_date for sample in training).isoformat(),
            "latest_observed_target": max(sample.target_end_date for sample in training).isoformat(),
            "test_start": test_dates[0].isoformat(),
            "test_end": test_dates[-1].isoformat(),
            "test_rows": len(testing),
            "calibrated": model.calibrated,
        })
    return results, folds


def metrics(results: list[PredictionResult]) -> dict:
    if not results:
        return {"rows": 0}
    n = len(results)
    brier = sum((row.probability_up - row.actual_up) ** 2 for row in results) / n
    base_brier = sum((row.baseline_probability - row.actual_up) ** 2 for row in results) / n
    log_loss = -sum(row.actual_up * math.log(row.probability_up) + (1 - row.actual_up) * math.log(1 - row.probability_up) for row in results) / n
    accuracy = sum(int((row.probability_up >= 0.5) == bool(row.actual_up)) for row in results) / n
    momentum_accuracy = sum(int(row.momentum_prediction == row.actual_up) for row in results) / n
    decisive = [row for row in results if row.probability_up >= 0.55 or row.probability_up <= 0.45]
    decisive_accuracy = (
        sum(int((row.probability_up >= 0.5) == bool(row.actual_up)) for row in decisive) / len(decisive)
        if decisive else None
    )
    return {
        "rows": n,
        "start": min(row.feature_date for row in results).isoformat(),
        "end": max(row.feature_date for row in results).isoformat(),
        "brier": round(brier, 6),
        "historical_rate_brier": round(base_brier, 6),
        "log_loss": round(log_loss, 6),
        "accuracy": round(accuracy, 6),
        "momentum_accuracy": round(momentum_accuracy, 6),
        "decisive_coverage": round(len(decisive) / n, 6),
        "decisive_accuracy": round(decisive_accuracy, 6) if decisive_accuracy is not None else None,
        "calibrated_rows": sum(int(row.calibrated) for row in results),
    }


def serialize_results(results: list[PredictionResult]) -> list[dict]:
    serialized = []
    for result in results:
        row = asdict(result)
        row["feature_date"] = result.feature_date.isoformat()
        row["target_end_date"] = result.target_end_date.isoformat()
        serialized.append(row)
    return serialized

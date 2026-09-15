from __future__ import annotations

import math
from dataclasses import dataclass, field

from .features import Sample


def _sigmoid(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("nonfinite model score")
    value = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-value))


@dataclass
class LogisticModel:
    means: list[float]
    scales: list[float]
    weights: list[float]
    bias: float
    base_rate: float
    calibration_a: float | None = None
    calibration_b: float | None = None
    calibration_blend: float | None = None
    calibration_details: dict = field(default_factory=dict)
    training_details: dict = field(default_factory=dict)

    @classmethod
    def fit(cls, samples: list[Sample], learning_rate: float, iterations: int, l2: float) -> "LogisticModel":
        x = [list(sample.features) for sample in samples if sample.target_up is not None]
        y = [float(sample.target_up) for sample in samples if sample.target_up is not None]
        if len(x) < 2 or len(set(y)) < 2:
            raise ValueError("training data needs both classes")
        width = len(x[0])
        if not width or any(len(row) != width or not all(math.isfinite(value) for value in row) for row in x):
            raise ValueError("nonfinite or inconsistent training features")
        means = [sum(row[col] for row in x) / len(x) for col in range(width)]
        scales = []
        for col in range(width):
            variance = sum((row[col] - means[col]) ** 2 for row in x) / len(x)
            scales.append(math.sqrt(variance) or 1.0)
        z = [[(row[col] - means[col]) / scales[col] for col in range(width)] for row in x]
        weights = [0.0] * width
        positive_rate = min(0.999, max(0.001, sum(y) / len(y)))
        bias = math.log(positive_rate / (1 - positive_rate))
        for _ in range(iterations):
            grad_w = [0.0] * width
            grad_b = 0.0
            for row, target in zip(z, y):
                error = _sigmoid(bias + sum(w * value for w, value in zip(weights, row))) - target
                grad_b += error
                for col, value in enumerate(row):
                    grad_w[col] += error * value
            n = len(z)
            bias -= learning_rate * grad_b / n
            for col in range(width):
                weights[col] -= learning_rate * (grad_w[col] / n + l2 * weights[col])
        return cls(means, scales, weights, bias, positive_rate)

    def raw_score(self, features: tuple[float, ...]) -> float:
        if len(features) != len(self.weights) or not all(math.isfinite(value) for value in features):
            raise ValueError("nonfinite or inconsistent prediction features")
        z = [(value - mean) / scale for value, mean, scale in zip(features, self.means, self.scales)]
        return self.bias + sum(weight * value for weight, value in zip(self.weights, z))

    def predict_proba(self, features: tuple[float, ...]) -> float:
        score = self.raw_score(features)
        if self.calibration_a is not None and self.calibration_b is not None:
            score = self.calibration_a * score + self.calibration_b
        probability = _sigmoid(score)
        if self.calibration_blend is not None:
            probability = self.base_rate + self.calibration_blend * (probability - self.base_rate)
        return min(0.999, max(0.001, probability))

    def calibrate(self, samples: list[Sample], learning_rate: float = 0.05, iterations: int = 400) -> None:
        # Keep a date's symbols together and purge labels crossing the boundary.
        self.calibration_a = self.calibration_b = self.calibration_blend = None
        ordered = sorted((sample for sample in samples if sample.target_up is not None
                          and sample.target_end_date is not None), key=lambda s: (s.feature_date, s.symbol))
        dates = sorted({sample.feature_date for sample in ordered})
        self.calibration_details = {"status": "insufficient_calibration_dates", "input_rows": len(ordered)}
        if len(dates) < 2:
            return
        cutoff = dates[len(dates) // 2]
        fit_rows = [s for s in ordered if s.feature_date < cutoff and s.target_end_date < cutoff]
        blend_rows = [s for s in ordered if s.feature_date >= cutoff]
        self.calibration_details.update(
            status="insufficient_calibration_classes", blend_start=cutoff.isoformat(),
            fit_rows=len(fit_rows), blend_rows=len(blend_rows),
            purged_rows=sum(s.feature_date < cutoff and s.target_end_date >= cutoff for s in ordered),
            fit_latest_target=max((s.target_end_date.isoformat() for s in fit_rows), default=None),
        )
        if len({s.target_up for s in fit_rows}) < 2 or len({s.target_up for s in blend_rows}) < 2:
            return
        fit_pairs = [(self.raw_score(s.features), float(s.target_up)) for s in fit_rows]
        blend_pairs = [(self.raw_score(s.features), float(s.target_up)) for s in blend_rows]
        a, b = 1.0, 0.0
        for _ in range(iterations):
            grad_a = grad_b = 0.0
            for score, target in fit_pairs:
                error = _sigmoid(a * score + b) - target
                grad_a += error * score
                grad_b += error
            a -= learning_rate * grad_a / len(fit_pairs)
            b -= learning_rate * grad_b / len(fit_pairs)
        best_blend, best_loss = 0.0, float("inf")
        for index in range(21):
            blend = index / 20
            loss = 0.0
            for score, target in blend_pairs:
                calibrated = _sigmoid(a * score + b)
                probability = self.base_rate + blend * (calibrated - self.base_rate)
                loss += (probability - target) ** 2
            if loss < best_loss:
                best_blend, best_loss = blend, loss
        self.calibration_a, self.calibration_b, self.calibration_blend = a, b, best_blend
        self.calibration_details.update(status="baseline_only" if best_blend == 0 else "calibrated",
                                        blend=best_blend, blend_brier=best_loss / len(blend_pairs))

    @property
    def discrimination_status(self) -> str:
        if self.calibration_blend == 0:
            return "baseline_only"
        return "model_differentiated" if self.calibrated else "uncalibrated"

    def diagnostics(self) -> dict:
        return {"discrimination_status": self.discrimination_status, "base_rate": self.base_rate,
                "calibration_a": self.calibration_a, "calibration_b": self.calibration_b,
                "calibration_blend": self.calibration_blend,
                "training": self.training_details, "calibration": self.calibration_details}

    @property
    def calibrated(self) -> bool:
        return self.calibration_a is not None and self.calibration_blend is not None

from __future__ import annotations

import math
from dataclasses import dataclass

from .features import Sample


def _sigmoid(value: float) -> float:
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

    @classmethod
    def fit(cls, samples: list[Sample], learning_rate: float, iterations: int, l2: float) -> "LogisticModel":
        x = [list(sample.features) for sample in samples if sample.target_up is not None]
        y = [float(sample.target_up) for sample in samples if sample.target_up is not None]
        if len(x) < 2 or len(set(y)) < 2:
            raise ValueError("training data needs both classes")
        width = len(x[0])
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
        pairs = [(self.raw_score(sample.features), float(sample.target_up)) for sample in samples if sample.target_up is not None]
        if len(pairs) < 2 or len({target for _, target in pairs}) < 2:
            return
        split = max(1, len(pairs) // 2)
        fit_pairs = pairs[:split]
        blend_pairs = pairs[split:]
        if len({target for _, target in fit_pairs}) < 2 or not blend_pairs:
            return
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

    @property
    def calibrated(self) -> bool:
        return self.calibration_a is not None and self.calibration_blend is not None

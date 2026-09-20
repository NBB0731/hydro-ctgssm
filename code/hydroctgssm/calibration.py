"""Distribution-free group conformal intervals for held-out basins."""

from __future__ import annotations

import numpy as np


class GroupConformalCalibrator:
    def __init__(self, alpha: float = 0.10, min_group_size: int = 50):
        self.alpha = alpha
        self.min_group_size = min_group_size
        self.global_q: np.ndarray | None = None
        self.group_q: dict[object, np.ndarray] = {}

    def _quantile(self, scores: np.ndarray) -> np.ndarray:
        n = scores.shape[0]
        q_level = min(1.0, np.ceil((n + 1) * (1 - self.alpha)) / n)
        return np.quantile(scores, q_level, axis=0, method="higher")

    def fit(self, y: np.ndarray, location: np.ndarray, scale: np.ndarray, group: np.ndarray) -> "GroupConformalCalibrator":
        scores = np.abs(y - location) / np.maximum(scale, 1e-8)
        self.global_q = self._quantile(scores)
        for label in np.unique(group):
            subset = scores[group == label]
            if len(subset) >= self.min_group_size:
                self.group_q[label] = self._quantile(subset)
        return self

    def interval(self, location: np.ndarray, scale: np.ndarray, group: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.global_q is None:
            raise RuntimeError("fit must be called before interval")
        q = np.stack([self.group_q.get(label, self.global_q) for label in group])
        radius = q * np.maximum(scale, 1e-8)
        return location - radius, location + radius


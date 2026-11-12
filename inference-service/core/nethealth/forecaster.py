"""A small learned failure forecaster.

A logistic regression over scale-free features of one metric's recent history (how close to the
failure threshold, how fast it is rising, whether the rise is accelerating, how anomalous, how clean
the trend is). It estimates P(this metric crosses its critical level within the next 30 minutes) and
is shared across metrics because the features are normalised by each metric's own thresholds.

Weights are trained offline by ``scripts/train_nethealth.py`` on simulated fault runs and shipped as
JSON (``forecaster_weights.json`` next to this file). Without that file the monitor uses the rule-based
predictor alone. Pure Python on purpose: inference is a dot product per metric.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from .metrics import METRICS

HORIZON_MIN = 30.0
FEATURE_NAMES = (
    "bias", "headroom", "level", "slope", "accel", "z", "r2", "eta_soon", "eta_inv",
    "slope_x_r2", "level_x_slope", "accel_x_r2", "headroom_x_slope", "slope_x_accel",
)
LEARNED_METRICS = tuple(k for k in METRICS if k != "threat_pressure")
DEFAULT_PATH = Path(__file__).with_name("forecaster_weights.json")  # ships with the service (models/*.json is not tracked)


def _c(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def metric_features(key: str, analysis: dict[str, Any]) -> list[float]:
    spec = METRICS[key]
    span = max(spec.target - spec.warn, 1e-6)
    latest = analysis["value"]
    headroom = _c((spec.target - latest) / span, -0.5, 2.0)
    level = _c((latest - spec.warn) / max(spec.crit - spec.warn, 1e-6), -1.0, 2.0)
    slope = _c(analysis["slope_per_min"] * 15 / span, -1.0, 2.0)
    accel = _c(analysis.get("accel_per_min", 0.0) * 15 / span, -1.0, 1.5)
    z = _c(analysis["z"] / 10.0, -1.0, 2.0)
    r2 = analysis["r2"]
    eta = analysis["eta_min"]
    eta_soon = 1.0 if eta is not None and eta <= HORIZON_MIN else 0.0
    eta_inv = 0.0 if eta is None else _c(HORIZON_MIN / (eta + 5.0), 0.0, 3.0) / 3.0
    return [1.0, headroom, level, slope, accel, z, r2, eta_soon, eta_inv,
            slope * r2, level * slope, accel * r2, headroom * slope, slope * accel]


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


class Forecaster:
    def __init__(self, weights: list[float], version: str = "unknown", meta: dict[str, Any] | None = None) -> None:
        if len(weights) != len(FEATURE_NAMES):
            raise ValueError("weight vector does not match the feature set")
        self.weights = weights
        self.version = version
        self.meta = meta or {}

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Forecaster | None":
        candidates = [Path(path)] if path else [
            *( [Path(os.environ["NETHEALTH_MODEL_PATH"])] if os.environ.get("NETHEALTH_MODEL_PATH") else [] ),
            DEFAULT_PATH,
        ]
        for candidate in candidates:
            try:
                data = json.loads(candidate.read_text())
                if tuple(data["features"]) != FEATURE_NAMES:
                    continue
                return cls(list(map(float, data["weights"])), data.get("version", "unknown"), data.get("meta"))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return None

    def predict_metric(self, key: str, analysis: dict[str, Any]) -> float:
        feats = metric_features(key, analysis)
        return sigmoid(sum(w * f for w, f in zip(self.weights, feats)))

    def predict_device(self, signals: list[dict[str, Any]]) -> float:
        """P(any metric fails within the horizon), treating metrics as independent."""
        survive = 1.0
        for sig in signals:
            if sig["metric"] in LEARNED_METRICS and sig["value"] > METRICS[sig["metric"]].warn * 0.4:
                survive *= 1.0 - sig["p_fail"]
        return 1.0 - survive

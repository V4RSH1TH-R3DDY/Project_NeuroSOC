"""Failure prediction from metric history.

Three signals per metric, all computed from a sliding window and all explainable:

* level      how close the latest value is to its warn/crit thresholds
* trend      a least-squares slope, extrapolated to the failure threshold (an ETA in minutes)
* anomaly    a robust z-score against the window's own baseline (median and MAD)

They are combined into a 0-1 risk per device. The ETA is only reported when the fit is good
(r² above ``MIN_R2``) and the metric is moving toward the threshold, so noise does not produce
confident-sounding predictions.
"""
from __future__ import annotations

import statistics
from collections import deque
from typing import Any

from .metrics import METRICS

WINDOW = 90            # samples kept per metric
TREND_WINDOW = 18      # samples used for the slope
MIN_TREND_POINTS = 8
MIN_R2 = 0.65
HORIZON_MIN = 90.0     # an ETA beyond this contributes no risk
Z_FLOOR = 3.5          # |z| below this is ordinary variation
EVENT_BOOST = 0.06     # risk added per unit of decayed event weight, capped below
EVENT_BOOST_CAP = 0.15


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def linear_fit(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Least squares over (x, y). Returns (slope, r²); (0, 0) when degenerate."""
    n = len(points)
    if n < 2:
        return 0.0, 0.0
    mean_x = sum(p[0] for p in points) / n
    mean_y = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mean_x) ** 2 for p in points)
    if sxx == 0:
        return 0.0, 0.0
    sxy = sum((p[0] - mean_x) * (p[1] - mean_y) for p in points)
    slope = sxy / sxx
    syy = sum((p[1] - mean_y) ** 2 for p in points)
    if syy == 0:
        return slope, 0.0
    r2 = (sxy * sxy) / (sxx * syy)
    return slope, r2


class MetricHistory:
    def __init__(self) -> None:
        self.samples: deque[tuple[float, float]] = deque(maxlen=WINDOW)  # (minutes, value)

    def add(self, minutes: float, value: float) -> None:
        self.samples.append((minutes, float(value)))

    def latest(self) -> float | None:
        return self.samples[-1][1] if self.samples else None

    def analyze(self, key: str, until: float | None = None) -> dict[str, Any] | None:
        """Analyse the history, or only the part up to ``until`` (minutes) to see what we would have said then."""
        spec = METRICS[key]
        samples = [p for p in self.samples if until is None or p[0] <= until]
        if not samples:
            return None
        values = [v for _, v in samples]
        latest = values[-1]

        # Anomaly: compare the latest few samples with the older baseline.
        z = 0.0
        if len(values) >= 20:
            reference = values[:-6]
            median = statistics.median(reference)
            mad = statistics.median(abs(v - median) for v in reference)
            scale = max(1.4826 * mad, spec.scale_floor)
            recent = statistics.median(values[-3:])
            z = (recent - median) / scale

        # Trend: slope over the recent window, extrapolated to the failure threshold.
        slope = 0.0
        r2 = 0.0
        eta: float | None = None
        recent_points = samples[-TREND_WINDOW:]
        if len(recent_points) >= MIN_TREND_POINTS:
            slope, r2 = linear_fit(recent_points)
            if slope > 0 and r2 >= MIN_R2 and latest < spec.target:
                eta = (spec.target - latest) / slope
            elif latest >= spec.target:
                eta = 0.0

        # Acceleration: is the slope itself growing? (a quadratic ramp that a straight line under-predicts)
        accel = 0.0
        pts = samples
        if len(pts) >= 18:
            s_recent, _ = linear_fit(pts[-9:])
            s_prior, _ = linear_fit(pts[-18:-9])
            accel = s_recent - s_prior

        # Risk components, each 0-1.
        level = _clamp((latest - spec.warn) / (spec.crit - spec.warn)) * 0.6
        if latest >= spec.crit:
            level = 0.75 if spec.fatal is None else 0.85
        trend = 0.0
        if eta is not None:
            trend = _clamp(1.0 - eta / HORIZON_MIN)
            trend *= 0.55 + 0.45 * r2
            # Rising but still far below the warn level is just drift, not a threat.
            if latest < spec.warn * 0.6:
                trend *= 0.4
        anomaly = _clamp((z - Z_FLOOR) / 8.0) * 0.8 if latest > spec.warn * 0.5 else 0.0
        risk = max(level, trend, anomaly) * spec.risk_weight

        severity = "ok"
        if latest >= spec.crit:
            severity = "critical"
        elif latest >= spec.warn or risk >= 0.5:
            severity = "warning"
        elif risk >= 0.25:
            severity = "watch"

        return {
            "metric": key,
            "label": spec.label,
            "unit": spec.unit,
            "value": round(latest, 2),
            "warn": spec.warn,
            "crit": spec.crit,
            "fail_at": spec.target,
            "z": round(z, 2),
            "slope_per_min": round(slope, 4),
            "accel_per_min": round(accel, 4),
            "r2": round(r2, 2),
            "eta_min": None if eta is None else round(eta, 1),
            "risk": round(risk, 3),
            "severity": severity,
            "components": {"level": round(level, 3), "trend": round(trend, 3), "anomaly": round(anomaly, 3)},
        }


def describe_signal(sig: dict[str, Any]) -> str:
    unit = sig["unit"]
    parts = [f"{sig['label']} is {sig['value']}{unit}"]
    if sig["eta_min"] is not None and sig["eta_min"] > 0 and sig["components"]["trend"] > 0.1:
        parts.append(f"rising {sig['slope_per_min']:+.2f}{unit}/min, reaching {sig['fail_at']}{unit} in ~{sig['eta_min']:.0f} min")
    elif sig["eta_min"] == 0.0:
        parts.append(f"at or past the {sig['fail_at']}{unit} failure threshold")
    if sig["components"]["anomaly"] > 0.1:
        parts.append(f"{sig['z']:.1f}σ above its own baseline")
    elif sig["value"] >= sig["warn"]:
        parts.append(f"above the {sig['warn']}{unit} warning level")
    return "; ".join(parts)


def combine(signals: list[dict[str, Any]], event_risk: float) -> dict[str, Any]:
    """Merge per-metric analyses into a device-level prediction.

    ``event_risk`` is the decayed weight of recent device events (see events.py), 0 or more.
    """
    ranked = sorted(signals, key=lambda s: s["risk"], reverse=True)
    risk = 0.0
    if ranked:
        risk = ranked[0]["risk"]
        if len(ranked) > 1:
            risk += 0.2 * ranked[1]["risk"] * (1 - risk)  # corroborating signals raise confidence
    risk += min(EVENT_BOOST_CAP, EVENT_BOOST * event_risk) * (1 if risk >= 0.2 else 0.25)
    risk = _clamp(risk)

    etas = [s["eta_min"] for s in signals if s["eta_min"] is not None and s["components"]["trend"] > 0.1]
    eta = min(etas) if etas else None
    return {"risk": round(risk, 3), "eta_min": eta}


def status_for_risk(risk: float) -> str:
    if risk >= 0.75:
        return "critical"
    if risk >= 0.5:
        return "warning"
    if risk >= 0.25:
        return "watch"
    return "healthy"

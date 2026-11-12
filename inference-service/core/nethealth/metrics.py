"""Metric definitions shared by the simulator, the predictor and the recommender.

``warn`` and ``crit`` mark degraded service. ``fatal`` (when set) is the level at which a device is
considered down. The predictor extrapolates toward the failure threshold: ``fatal`` if there is one,
otherwise ``crit``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricSpec:
    key: str
    label: str
    unit: str
    warn: float
    crit: float
    fatal: float | None
    scale_floor: float  # smallest deviation scale used for z-scores, so a flat metric does not explode
    risk_weight: float = 1.0  # how much of this metric's risk counts toward the device (attack pressure is indirect)

    @property
    def target(self) -> float:
        return self.fatal if self.fatal is not None else self.crit


METRICS: dict[str, MetricSpec] = {
    spec.key: spec
    for spec in (
        MetricSpec("cpu_pct", "CPU", "%", 80, 95, 99, 3.0),
        MetricSpec("mem_pct", "Memory", "%", 80, 92, 98, 2.0),
        MetricSpec("temp_c", "Temperature", "°C", 70, 82, 92, 1.5),
        MetricSpec("link_util_pct", "Link utilization", "%", 75, 90, None, 4.0),
        MetricSpec("error_rate_pct", "Interface errors", "%", 0.5, 2.0, 8.0, 0.1),
        MetricSpec("packet_loss_pct", "Packet loss", "%", 1.0, 5.0, 25.0, 0.2),
        MetricSpec("latency_ms", "Latency", "ms", 80, 200, None, 5.0),
        # Fed by the traffic models' verdicts, not by the device: see NetHealthMonitor.ingest_verdict.
        MetricSpec("threat_pressure", "Attack pressure", "", 35, 70, None, 8.0, 0.6),
    )
}

DEVICE_METRICS: dict[str, tuple[str, ...]] = {
    "router": ("cpu_pct", "mem_pct", "temp_c", "link_util_pct", "error_rate_pct", "packet_loss_pct", "latency_ms", "threat_pressure"),
    "switch": ("cpu_pct", "mem_pct", "temp_c", "link_util_pct", "error_rate_pct"),
    "firewall": ("cpu_pct", "mem_pct", "temp_c", "link_util_pct", "packet_loss_pct", "latency_ms", "threat_pressure"),
    "server": ("cpu_pct", "mem_pct", "temp_c"),
    "wan_link": ("link_util_pct", "error_rate_pct", "packet_loss_pct", "latency_ms", "threat_pressure"),
    "host": ("cpu_pct", "mem_pct", "temp_c", "link_util_pct", "error_rate_pct"),
}

# Devices that face the outside world, and so feel attack traffic first.
EDGE_TYPES = ("firewall", "wan_link", "router")

STATUS_ORDER = ("healthy", "watch", "warning", "critical", "down")

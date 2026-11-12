"""Deterministic network telemetry simulator.

Generates device metrics and syslog-style events for a small fleet, and lets a demo inject failure
scenarios (memory leak, fan failure, link degradation, ...) that ramp up the way real incidents do.
A device whose fatal threshold is crossed goes down until the fix is applied.

One tick is one simulated minute.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any

from .metrics import DEVICE_METRICS, METRICS

TICK_SECONDS = 60.0

FLEET: tuple[dict[str, str], ...] = (
    {"id": "core-rtr-01", "name": "Core router 1", "type": "router", "site": "HQ"},
    {"id": "core-rtr-02", "name": "Core router 2", "type": "router", "site": "HQ"},
    {"id": "edge-fw-01", "name": "Edge firewall", "type": "firewall", "site": "HQ"},
    {"id": "dist-sw-01", "name": "Distribution switch A", "type": "switch", "site": "HQ"},
    {"id": "dist-sw-02", "name": "Distribution switch B", "type": "switch", "site": "HQ"},
    {"id": "wan-link-01", "name": "WAN uplink (ISP 1)", "type": "wan_link", "site": "HQ"},
    {"id": "wan-link-02", "name": "WAN uplink (ISP 2)", "type": "wan_link", "site": "HQ"},
    {"id": "app-srv-01", "name": "App server 1", "type": "server", "site": "DC-East"},
    {"id": "app-srv-02", "name": "App server 2", "type": "server", "site": "DC-East"},
    {"id": "db-srv-01", "name": "Database server", "type": "server", "site": "DC-East"},
    {"id": "br-rtr-01", "name": "Branch router", "type": "router", "site": "Branch-West"},
    {"id": "br-sw-01", "name": "Branch switch", "type": "switch", "site": "Branch-West"},
)

# metric -> function of dt (ticks since the scenario began) returning the amount added to the baseline.
SCENARIOS: dict[str, dict[str, Any]] = {
    "memory_leak": {
        "label": "Memory leak",
        "effects": {"mem_pct": lambda dt: 0.9 * dt, "cpu_pct": lambda dt: 0.12 * dt},
        "event": ("warning", "kern", "oom-killer: low free memory, process restarts increasing"),
        "types": ("router", "switch", "firewall", "server"),
    },
    "thermal_runaway": {
        "label": "Fan failure / overheating",
        "effects": {"temp_c": lambda dt: 0.85 * dt, "cpu_pct": lambda dt: 0.1 * dt},
        "event": ("warning", "env", "fan tray 2 speed below minimum RPM"),
        "types": ("router", "switch", "firewall", "server"),
    },
    "cpu_runaway": {
        "label": "CPU runaway process",
        "effects": {"cpu_pct": lambda dt: 1.15 * dt, "temp_c": lambda dt: 0.12 * dt},
        "event": ("warning", "sched", "process 'routed' consuming sustained high CPU"),
        "types": ("router", "switch", "firewall", "server"),
    },
    "link_degradation": {
        "label": "Link degradation (optics / cable)",
        "effects": {
            "packet_loss_pct": lambda dt: 0.005 * dt * dt,
            "error_rate_pct": lambda dt: 0.003 * dt * dt,
            "latency_ms": lambda dt: 2.2 * dt,
        },
        "event": ("warning", "link", "CRC errors rising on uplink interface, rx power low"),
        "types": ("router", "switch", "wan_link"),
    },
    "traffic_surge": {
        "label": "Traffic surge (possible DDoS)",
        "effects": {
            "link_util_pct": lambda dt: 1.5 * dt,
            "latency_ms": lambda dt: 1.9 * dt,
            "cpu_pct": lambda dt: 0.45 * dt,
            "packet_loss_pct": lambda dt: 0.004 * max(0.0, dt - 25) ** 2,
        },
        "event": ("warning", "traffic", "inbound traffic 4x above baseline, SYN rate abnormal"),
        "types": ("router", "firewall", "wan_link"),
    },
    "flapping": {
        "label": "Interface flapping",
        "effects": {"error_rate_pct": lambda dt: 0.06 * dt},
        "event": ("warning", "link", "interface Gi0/1 changed state to down/up repeatedly"),
        "types": ("router", "switch", "wan_link"),
    },
}

# Used for training and evaluation only; not offered in the demo.
HIDDEN_SCENARIOS: dict[str, dict[str, Any]] = {
    "blip": {  # a short benign spike that should NOT raise an alarm
        "label": "Transient spike",
        "effects": {
            "cpu_pct": lambda dt: 38.0 * max(0.0, 1 - abs(dt - 6) / 6),
            "latency_ms": lambda dt: 60.0 * max(0.0, 1 - abs(dt - 6) / 6),
            "link_util_pct": lambda dt: 30.0 * max(0.0, 1 - abs(dt - 6) / 6),
            "temp_c": lambda dt: 6.0 * max(0.0, 1 - abs(dt - 6) / 6),
        },
        "event": ("info", "sys", "scheduled job started"),
        "types": ("router", "switch", "firewall", "server", "wan_link"),
    },
}
ALL_SCENARIOS = {**SCENARIOS, **HIDDEN_SCENARIOS}

_NOISE = {
    "cpu_pct": 1.2, "mem_pct": 0.25, "temp_c": 0.35, "link_util_pct": 2.0,
    "error_rate_pct": 0.03, "packet_loss_pct": 0.06, "latency_ms": 1.5,
}


@dataclass
class _DeviceState:
    spec: dict[str, str]
    base: dict[str, float]
    phase: float
    up: bool = True
    scenario: str | None = None
    started: int = 0
    scale: float = 1.0
    intensity: float = 1.0
    recovering: bool = False
    over_ticks: int = 0
    down_reason: str = ""


@dataclass
class TickResult:
    ts: float
    samples: dict[str, dict[str, float]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)


def _baseline(rng: random.Random, device_type: str) -> dict[str, float]:
    heavy = device_type in {"server", "firewall"}
    base = {
        "cpu_pct": rng.uniform(22, 48) if heavy else rng.uniform(12, 32),
        "mem_pct": rng.uniform(42, 62),
        "temp_c": rng.uniform(42, 56),
        "link_util_pct": rng.uniform(25, 52),
        "error_rate_pct": rng.uniform(0.01, 0.08),
        "packet_loss_pct": rng.uniform(0.02, 0.15),
        "latency_ms": rng.uniform(8, 30),
    }
    return {k: v for k, v in base.items() if k in DEVICE_METRICS[device_type]}


class NetworkSimulator:
    def __init__(self, seed: int = 7, start_ts: float = 0.0) -> None:
        self._seed = seed
        self._start_ts = start_ts
        self.reset()

    def reset(self) -> None:
        self.rng = random.Random(self._seed)
        self.tick_no = 0
        self.devices: dict[str, _DeviceState] = {}
        for spec in FLEET:
            self.devices[spec["id"]] = _DeviceState(
                spec=spec, base=_baseline(self.rng, spec["type"]), phase=self.rng.uniform(0, math.tau))

    # ── control ────────────────────────────────────────────────────────────
    def inject(self, scenario: str, device_id: str, intensity: float = 1.0) -> dict[str, Any]:
        if scenario not in ALL_SCENARIOS:
            raise KeyError(f"Unknown scenario: {scenario}")
        state = self.devices.get(device_id)
        if state is None:
            raise KeyError(f"Unknown device: {device_id}")
        if state.spec["type"] not in ALL_SCENARIOS[scenario]["types"]:
            raise ValueError(f"{ALL_SCENARIOS[scenario]['label']} does not apply to a {state.spec['type'].replace('_', ' ')}.")
        state.scenario, state.started, state.scale, state.recovering = scenario, self.tick_no, 1.0, False
        state.intensity = max(0.2, float(intensity))
        return {"device_id": device_id, "scenario": scenario}

    def resolve(self, device_id: str) -> bool:
        """The corrective action was applied: the fault clears, metrics recover over a few ticks."""
        state = self.devices.get(device_id)
        if state is None:
            raise KeyError(f"Unknown device: {device_id}")
        had_fault = state.scenario is not None or not state.up
        state.recovering = True
        state.up = True
        state.over_ticks = 0
        state.down_reason = ""
        return had_fault

    def active_scenarios(self) -> dict[str, str]:
        return {d: s.scenario for d, s in self.devices.items() if s.scenario and not s.recovering}

    # ── stepping ───────────────────────────────────────────────────────────
    def step(self) -> TickResult:
        self.tick_no += 1
        ts = self._start_ts + self.tick_no * TICK_SECONDS
        result = TickResult(ts=ts)
        for device_id, state in self.devices.items():
            samples, events = self._step_device(state, ts)
            if samples is not None:
                result.samples[device_id] = samples
            result.events.extend(events)
        return result

    def _step_device(self, state: _DeviceState, ts: float) -> tuple[dict[str, float] | None, list[dict[str, Any]]]:
        device_id = state.spec["id"]
        events: list[dict[str, Any]] = []

        def event(severity: str, facility: str, message: str) -> None:
            events.append({"ts": ts, "device_id": device_id, "severity": severity, "facility": facility, "message": message})

        if not state.up:
            return None, events  # a down device reports nothing

        dt = max(0, self.tick_no - state.started)
        if state.recovering:
            state.scale *= 0.62
            if state.scale < 0.02:
                if state.scenario:
                    event("info", "ops", "metrics back to baseline after corrective action")
                state.scenario, state.recovering, state.scale = None, False, 1.0
        scenario = ALL_SCENARIOS.get(state.scenario) if state.scenario else None
        if scenario is not None and dt == 1 and not state.recovering:
            event(*scenario["event"])

        diurnal = math.sin(self.tick_no / 60.0 * math.tau / 24 * 4 + state.phase)  # slow wave, visible in minutes
        samples: dict[str, float] = {}
        for key, base in state.base.items():
            value = base + _NOISE[key] * self.rng.gauss(0, 1)
            if key in {"cpu_pct", "link_util_pct"}:
                value += 3.0 * diurnal
            if scenario is not None and key in scenario["effects"]:
                value += scenario["effects"][key](dt * state.intensity) * state.scale
            if state.scenario == "flapping" and key == "packet_loss_pct" and self.tick_no % 4 == 0 and not state.recovering:
                value += self.rng.uniform(6, 14)
                event("error", "link", "interface Gi0/1 link down")
            lo = 0.0
            hi = 100.0 if key.endswith("_pct") else 1000.0
            samples[key] = min(hi, max(lo, value))

        # Threshold events that a real device would log on its own.
        for key, value in samples.items():
            spec = METRICS[key]
            if value >= spec.crit and self.tick_no % 5 == 0:
                event("error", "monitor", f"{spec.label} {value:.1f}{spec.unit} exceeded critical threshold {spec.crit}{spec.unit}")

        # Fatal condition: crossing the fatal threshold for 3 consecutive ticks takes the device down.
        fatal_hit = next((k for k, v in samples.items() if METRICS[k].fatal is not None and v >= METRICS[k].fatal * 0.995), None)
        if fatal_hit is not None:
            state.over_ticks += 1
            if state.over_ticks >= 3:
                state.up = False
                state.down_reason = fatal_hit
                spec = METRICS[fatal_hit]
                event("critical", "monitor", f"{state.spec['name']} is DOWN: {spec.label} reached {samples[fatal_hit]:.1f}{spec.unit}")
        else:
            state.over_ticks = 0

        if self.rng.random() < 0.02:
            event("info", "sys", "config saved by scheduled backup")
        return samples, events


def warmup(sim: NetworkSimulator, ticks: int) -> list[TickResult]:
    return [sim.step() for _ in range(ticks)]

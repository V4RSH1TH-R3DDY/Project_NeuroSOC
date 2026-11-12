"""NetHealthMonitor: holds telemetry history, runs the predictors, serves the API.

Inputs, any mix of:
  * the built-in simulator (demo fleet; one tick = one simulated minute)
  * ``ingest`` / ``ingest_event``: real telemetry (host collector, Prometheus, syslog, the HTTP API)
  * ``ingest_verdict``: verdicts from NeuroSOC's traffic models, which become "attack pressure" on edge devices

Failure risk combines the rule-based predictor (trend, anomaly, level) with the learned forecaster, and is
corroborated by what device events say. Timestamps are epoch seconds. Simulated devices run on the simulated
clock, live devices on the wall clock, so each device is analysed against its own clock.
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque
from typing import Any

from .events import decayed, event_weight
from .forecaster import Forecaster
from .metrics import DEVICE_METRICS, EDGE_TYPES, METRICS, STATUS_ORDER
from .predictor import MetricHistory, combine, describe_signal, status_for_risk
from .recommender import recommend
from .simulator import FLEET, SCENARIOS, TICK_SECONDS, NetworkSimulator, TickResult, warmup
from .traffic import TrafficProbe, flow_features, heuristic_probe

SERIES_POINTS = 60
MAX_DEVICES = 500
STALE_AFTER_S = 120.0
THREAT_HALF_LIFE_S = 180.0
THREAT_CLASS_WEIGHT = {"DDOS": 28.0, "BRUTE_FORCE": 8.0, "RECONNAISSANCE": 8.0}
PROBE_EVERY_TICKS = {"ddos": 2, "benign": 10}
PERSIST_S = 240.0
GUARD_P = 0.85  # learned probability above which it overrides the rules in the 'guarded' blend  # risk must stay high this long before a device is called warning/critical (unless already past critical)
BLENDS = ("rules", "learned", "max", "mean", "guarded")


class NetHealthMonitor:
    def __init__(self, simulator: NetworkSimulator | None = None, warmup_ticks: int = 60, forecaster: Forecaster | None = None,
                 blend: str = "rules", traffic_probe: TrafficProbe | None = None) -> None:
        if blend not in BLENDS:
            raise ValueError(f"blend must be one of {BLENDS}")
        self._lock = threading.RLock()
        self.simulator = simulator
        self.forecaster = forecaster
        self.blend = blend if forecaster is not None else "rules"
        self.traffic_probe = traffic_probe
        self._probe_rng = random.Random(99)
        self.devices: dict[str, dict[str, Any]] = {}
        self.history: dict[str, dict[str, MetricHistory]] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=500)
        self.down: dict[str, dict[str, Any]] = {}
        self.last_seen: dict[str, float] = {}
        self.resolved: list[dict[str, Any]] = []
        self._threat: deque[dict[str, Any]] = deque(maxlen=400)
        self._sim_now = 0.0
        self._event_seq = 0
        self.sources: dict[str, Any] = {}
        self.warmup_ticks = warmup_ticks
        if simulator is not None:
            for spec in FLEET:
                self.devices[spec["id"]] = {**spec, "source": "simulated"}
            self._warm_up()

    def _warm_up(self) -> None:
        assert self.simulator is not None
        self.simulator._start_ts = time.time() - (self.warmup_ticks + 1) * TICK_SECONDS
        for tick in warmup(self.simulator, self.warmup_ticks):
            self._apply(tick, probe=False)
        self.events.clear()  # warm-up chatter is not news
        self._threat.clear()

    def _is_sim(self, device_id: str) -> bool:
        return self.simulator is not None and device_id in self.simulator.devices

    def _clock(self, device_id: str) -> float:
        return self._sim_now if self._is_sim(device_id) else time.time()

    # ── ingest ─────────────────────────────────────────────────────────────
    def register_device(self, device_id: str, name: str | None = None, device_type: str = "router", site: str = "default") -> None:
        with self._lock:
            if device_type not in DEVICE_METRICS:
                raise ValueError(f"device_type must be one of {sorted(DEVICE_METRICS)}")
            if device_id not in self.devices:
                if len(self.devices) >= MAX_DEVICES:
                    raise ValueError(f"At most {MAX_DEVICES} devices are tracked.")
                self.devices[device_id] = {"id": device_id, "name": name or device_id, "type": device_type, "site": site, "source": "live"}

    def ingest(self, device_id: str, ts: float, metrics: dict[str, float]) -> int:
        """Record one telemetry sample. Unknown metric names are ignored; returns how many were accepted."""
        with self._lock:
            if device_id not in self.devices:
                self.register_device(device_id)
            accepted = 0
            hist = self.history.setdefault(device_id, {})
            for key, value in metrics.items():
                if key not in METRICS or isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
                    continue
                hist.setdefault(key, MetricHistory()).add(ts / 60.0, float(value))
                accepted += 1
            if accepted:
                self.last_seen[device_id] = ts
                if device_id in self.down and not self._is_sim(device_id):
                    self.down.pop(device_id, None)  # a live device that reports again is back up
            return accepted

    def ingest_event(self, device_id: str, ts: float, severity: str, facility: str, message: str) -> dict[str, Any]:
        with self._lock:
            if device_id not in self.devices:
                self.register_device(device_id)
            weight, pattern = event_weight(severity, message)
            self._event_seq += 1
            event = {"id": self._event_seq, "ts": ts, "device_id": device_id, "device_name": self.devices[device_id]["name"],
                     "severity": severity.lower(), "facility": facility, "message": message,
                     "category": pattern.category if pattern else None, "label": pattern.label if pattern else None,
                     "weight": round(weight, 3), "metric": pattern.metric if pattern else None}
            self.events.append(event)
            if pattern is not None and pattern.category == "device_down" and event["severity"] in {"critical", "alert", "emergency", "error"}:
                self.down[device_id] = {"since": ts, "message": message}
            return event

    def ingest_verdict(self, verdict: dict[str, Any]) -> bool:
        """A NeuroSOC traffic verdict. Attacks add decaying pressure to every edge device; benign traffic adds none."""
        cls = str(verdict.get("xgb_class") or "").upper()
        if cls in {"", "BENIGN", "INCONCLUSIVE"} or str(verdict.get("verdict", "")).upper() in {"LEGITIMATE", "INCONCLUSIVE"}:
            return False
        weight = THREAT_CLASS_WEIGHT.get(cls, 12.0) * max(0.0, min(1.0, float(verdict.get("confidence") or 0.5)))
        with self._lock:
            self._threat.append({"wall": time.time(), "sim": self._sim_now, "weight": weight, "class": cls,
                                 "confidence": float(verdict.get("confidence") or 0), "device_id": verdict.get("_device_id")})
        return True

    def _threat_state(self, device_id: str) -> dict[str, Any]:
        clock, key = (self._sim_now, "sim") if self._is_sim(device_id) else (time.time(), "wall")
        pressure, by_class, last = 0.0, {}, None
        for t in self._threat:
            if t["device_id"] not in (None, device_id):
                continue
            w = t["weight"] * 0.5 ** (max(0.0, clock - t[key]) / THREAT_HALF_LIFE_S)
            pressure += w
            by_class[t["class"]] = by_class.get(t["class"], 0.0) + w
            last = t
        top = max(by_class, key=by_class.get) if by_class else None
        return {"pressure": min(100.0, pressure), "attack_class": top, "confidence": last["confidence"] if last and top else 0.0}

    def _apply(self, tick: TickResult, probe: bool = True) -> None:
        self._sim_now = tick.ts
        for device_id, samples in tick.samples.items():
            self.ingest(device_id, tick.ts, samples)
        for ev in tick.events:
            self.ingest_event(ev["device_id"], ev["ts"], ev["severity"], ev["facility"], ev["message"])
        if self.simulator is not None:
            for device_id, state in self.simulator.devices.items():
                if state.up:
                    self.down.pop(device_id, None)
            if probe:
                self._sample_edge_traffic(tick)

    def _sample_edge_traffic(self, tick: TickResult) -> None:
        """Describe the flows an edge device sees, have the traffic models classify them, record attack pressure."""
        assert self.simulator is not None
        probe = self.traffic_probe or heuristic_probe
        for device_id, state in self.simulator.devices.items():
            if state.spec["type"] not in EDGE_TYPES or device_id not in tick.samples:
                continue
            attacking = state.scenario == "traffic_surge" and not state.recovering and self.simulator.tick_no - state.started >= 4
            kind = "ddos" if attacking else "benign"
            if self.simulator.tick_no % PROBE_EVERY_TICKS[kind] == 0:
                try:
                    verdict = probe(flow_features(kind, self._probe_rng))
                except Exception:  # noqa: BLE001 - a model failure must not stop the health feed
                    verdict = heuristic_probe(flow_features(kind, self._probe_rng))
                if verdict:
                    verdict["_device_id"] = device_id
                    self.ingest_verdict(verdict)
            if state.spec["type"] in EDGE_TYPES and "threat_pressure" in DEVICE_METRICS[state.spec["type"]]:
                self.ingest(device_id, tick.ts, {"threat_pressure": self._threat_state(device_id)["pressure"]})

    def tick(self) -> None:
        """Advance the simulator one minute and ingest what it produced."""
        if self.simulator is None:
            return
        with self._lock:
            self._apply(self.simulator.step())

    # ── demo control ───────────────────────────────────────────────────────
    def inject(self, scenario: str, device_id: str, intensity: float = 1.0) -> dict[str, Any]:
        if self.simulator is None:
            raise RuntimeError("The simulator is not enabled.")
        if scenario not in SCENARIOS:
            raise KeyError(f"Unknown scenario: {scenario}")
        with self._lock:
            return self.simulator.inject(scenario, device_id, intensity)

    def apply_fix(self, device_id: str, fix: str | None = None) -> dict[str, Any]:
        with self._lock:
            device = self.devices.get(device_id)
            if device is None:
                raise KeyError(device_id)
            if self.simulator is None or not self._is_sim(device_id):
                raise RuntimeError("Corrective actions can only be applied to simulated devices.")
            had_fault = self.simulator.resolve(device_id)
            ts = self._sim_now
            self.down.pop(device_id, None)
            self.resolved.append({"device_id": device_id, "fix": fix, "ts": ts})
            self._threat = deque((t for t in self._threat if t["device_id"] != device_id), maxlen=400)
            self.ingest_event(device_id, ts, "info", "ops", f"Corrective action applied: {fix or 'manual fix'}")
            return {"device_id": device_id, "had_fault": had_fault}

    def reset(self) -> None:
        if self.simulator is None:
            return
        with self._lock:
            live = {d: v for d, v in self.devices.items() if v["source"] == "live"}
            for d in [d for d in self.devices if d not in live]:
                self.history.pop(d, None)
                self.last_seen.pop(d, None)
                self.down.pop(d, None)
            self.simulator.reset()
            self.events.clear()
            self.resolved.clear()
            self._threat.clear()
            self._warm_up()

    # ── analysis ───────────────────────────────────────────────────────────
    def _event_context(self, device_id: str, now: float) -> tuple[float, list[dict[str, Any]]]:
        """Decayed total event weight for a device, and the per-category breakdown."""
        total, cats = 0.0, {}
        for e in self.events:
            if e["device_id"] != device_id or e["weight"] <= 0:
                continue
            w = decayed(e["weight"], now - e["ts"])
            if w < 0.02:
                continue
            total += w
            c = cats.setdefault(e["category"] or "other", {"category": e["category"] or "other", "label": e["label"] or "Other",
                                                          "metric": e["metric"], "count": 0, "weight": 0.0})
            c["count"] += 1
            c["weight"] += w
        out = sorted(cats.values(), key=lambda c: c["weight"], reverse=True)
        for c in out:
            c["weight"] = round(c["weight"], 3)
        return total, out

    def _device_view(self, device_id: str, with_series: bool = False) -> dict[str, Any]:
        device = self.devices[device_id]
        now = self.last_seen.get(device_id, self._clock(device_id)) if self._is_sim(device_id) else time.time()
        hist = self.history.get(device_id, {})
        signals = [a for key in DEVICE_METRICS.get(device["type"], ()) if key in hist and (a := hist[key].analyze(key)) is not None]

        learned = None
        if self.forecaster is not None:
            for sig in signals:
                spec = METRICS[sig["metric"]]
                sig["p_fail"] = 0.0 if sig["metric"] == "threat_pressure" or sig["value"] >= spec.crit else self.forecaster.predict_metric(sig["metric"], sig)
            learned = self.forecaster.predict_device([s for s in signals if s["metric"] != "threat_pressure"])

        event_weight_sum, event_cats = self._event_context(device_id, now)
        pred = combine(signals, event_weight_sum)
        rules_risk = pred["risk"]
        # An event that points at a metric already trending badly corroborates it.
        hinted = {c["metric"] for c in event_cats if c["metric"] and c["weight"] >= 0.1}
        corroborated = [s["metric"] for s in signals if s["metric"] in hinted and s["risk"] >= 0.25]
        if corroborated:
            rules_risk = min(1.0, rules_risk + 0.08)

        if learned is None or self.blend == "rules":
            risk = rules_risk
        elif self.blend == "learned":
            risk = max(learned, rules_risk if any(s["value"] >= s["crit"] for s in signals) else 0.0)
        elif self.blend == "guarded":  # the learned model only overrides the rules when it is very sure
            risk = max(rules_risk, learned) if learned >= GUARD_P else rules_risk
        elif self.blend == "mean":
            risk = 0.5 * rules_risk + 0.5 * learned if learned >= 0.1 else rules_risk * 0.5 + learned * 0.5
        else:
            risk = max(rules_risk, learned)
        risk = max(0.0, min(1.0, risk))

        down = self.down.get(device_id)
        stale = device["source"] == "live" and device_id in self.last_seen and time.time() - self.last_seen[device_id] > STALE_AFTER_S
        status = "down" if down else status_for_risk(risk)
        # Persistence: a spike that rises for a minute or two and falls back should not page anyone. A device is
        # only called warning/critical if the rules would already have said so PERSIST_S ago (or it is past critical).
        if risk >= 0.5 and not down and hist:
            past_crit = any(s["value"] >= s["crit"] for s in signals if s["metric"] != "threat_pressure")
            then = [a for key in DEVICE_METRICS.get(device["type"], ()) if key in hist and hist[key].samples
                    and (a := hist[key].analyze(key, until=hist[key].samples[-1][0] - PERSIST_S / 60.0)) is not None]
            if not past_crit and combine(then, 0.0)["risk"] < 0.4:
                status = "watch"
        eta = pred["eta_min"]
        if down:
            risk, eta = 1.0, 0.0
        for sig in signals:
            sig["message"] = describe_signal(sig)
            if sig["metric"] == "threat_pressure":
                t = self._threat_state(device_id)
                sig["message"] = (f"Attack pressure is {sig['value']:.0f}/100: the traffic models classify recent flows as "
                                  f"{str(t['attack_class'] or 'hostile').replace('_', ' ').lower()}")
        top = sorted(signals, key=lambda s: s["risk"], reverse=True)
        threat = self._threat_state(device_id) if DEVICE_METRICS.get(device["type"]) and "threat_pressure" in DEVICE_METRICS[device["type"]] else None
        view: dict[str, Any] = {
            **device,
            "status": status,
            "risk": round(risk * 100),
            "risk_rules": round(rules_risk * 100),
            "failure_probability_30m": None if learned is None else round(learned, 3),
            "predicted_failure_in_min": eta,
            "stale": bool(stale),
            "event_context": event_cats[:4],
            "corroborated_by_events": corroborated,
            "metrics": {s["metric"]: s["value"] for s in signals},
            "signals": [s for s in top if s["risk"] >= 0.15][:4],
            "recommendations": recommend(device, signals, risk, eta, threat) if not down else [{
                "id": f"{device_id}:down", "device_id": device_id, "device_name": device["name"], "priority": 1,
                "title": "Restore the device and verify service", "metric": None, "fix": "restore",
                "detail": f"{device['name']} is down ({down['message']}). Apply the fix, then confirm traffic has recovered."}],
            "threat": threat,
            "down": down,
            "scenario": self.simulator.devices[device_id].scenario if self._is_sim(device_id) else None,
        }
        if with_series:
            view["series"] = {key: [{"t": round(t * 60), "v": round(v, 2)} for t, v in list(h.samples)[-SERIES_POINTS:]]
                              for key, h in hist.items()}
            view["thresholds"] = {key: {"warn": METRICS[key].warn, "crit": METRICS[key].crit, "fail_at": METRICS[key].target,
                                        "label": METRICS[key].label, "unit": METRICS[key].unit} for key in hist}
        return view

    def overview(self) -> dict[str, Any]:
        with self._lock:
            views = [self._device_view(d) for d in self.devices]
            counts = {s: 0 for s in STATUS_ORDER}
            for v in views:
                counts[v["status"]] += 1
            at_risk = sorted((v for v in views if v["status"] != "healthy"), key=lambda v: v["risk"], reverse=True)
            predictions = [v for v in at_risk if v["predicted_failure_in_min"] is not None and v["status"] != "down"]
            predictions.sort(key=lambda v: v["predicted_failure_in_min"])
            actions = sorted((r for v in views for r in v["recommendations"]), key=lambda r: r["priority"])
            fleet_risk = round(max((v["risk"] for v in views), default=0) * 0.6 + (sum(v["risk"] for v in views) / max(1, len(views))) * 0.4)
            return {
                "generated_at": time.time(),
                "simulated": self.simulator is not None,
                "sources": self.sources,
                "learned_model": None if self.forecaster is None else {"version": self.forecaster.version, "blend": self.blend,
                                                                      "auroc": self.forecaster.meta.get("test", {}).get("auroc_learned")},
                "counts": counts,
                "total": len(views),
                "fleet_risk": fleet_risk,
                "devices": sorted(views, key=lambda v: (-v["risk"], v["name"])),
                "predictions": [{"device_id": v["id"], "device_name": v["name"], "eta_min": v["predicted_failure_in_min"],
                                 "risk": v["risk"], "driver": v["signals"][0]["message"] if v["signals"] else ""} for v in predictions],
                "recommendations": actions[:12],
                "scenarios": [{"key": k, "label": s["label"], "types": list(s["types"])} for k, s in SCENARIOS.items()],
            }

    def device_detail(self, device_id: str) -> dict[str, Any]:
        with self._lock:
            if device_id not in self.devices:
                raise KeyError(device_id)
            view = self._device_view(device_id, with_series=True)
            view["events"] = [e for e in reversed(self.events) if e["device_id"] == device_id][:25]
            view["as_of"] = self.last_seen.get(device_id, time.time())  # the device's own clock: simulated devices run ahead of wall time
            return view

    def recent_events(self, limit: int = 100, device_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in reversed(self.events) if device_id is None or e["device_id"] == device_id][:limit]

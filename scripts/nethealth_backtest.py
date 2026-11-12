#!/usr/bin/env python3
"""Backtest the network health predictor on the simulator, comparing the rule-based and learned predictors.

For each fault scenario the fault is injected on every applicable device at an intensity the forecaster
was not trained on. Reported per predictor ("rules", "learned", "max", "mean"):

  caught        runs where the device reached "warning" no later than its first critical-level reading
  median lead   minutes between that warning and the first critical-level reading (higher is earlier)
  false alarms  healthy device-hours (with benign transient spikes mixed in) that reached "warning" or worse

  python scripts/nethealth_backtest.py [--seeds 3] [--json]
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "inference-service"))

from core.nethealth import NetHealthMonitor, NetworkSimulator  # noqa: E402
from core.nethealth.forecaster import Forecaster  # noqa: E402
from core.nethealth.simulator import FLEET, SCENARIOS  # noqa: E402

MAX_TICKS = 140
INTENSITY = 1.2  # not in the training set
WARN = {"warning", "critical", "down"}


def make(seed: int, blend: str, forecaster) -> NetHealthMonitor:
    return NetHealthMonitor(NetworkSimulator(seed=seed), forecaster=forecaster, blend=blend)


def run_fault(seed, scenario, device_id, blend, forecaster) -> dict:
    mon = make(seed, blend, forecaster)
    mon.simulator.inject(scenario, device_id, INTENSITY)
    first_warning = failed = None
    for t in range(1, MAX_TICKS + 1):
        mon.tick()
        view = mon.device_detail(device_id)
        if first_warning is None and view["status"] in WARN:
            first_warning = t
        if failed is None and (view["status"] == "down" or any(s["value"] >= s["crit"] for s in view["signals"])):
            failed = t
        if view["status"] == "down":
            break
    caught = first_warning is not None and (failed is None or first_warning <= failed)
    lead = failed - first_warning if (caught and failed is not None) else None
    return {"scenario": scenario, "caught": caught, "lead": lead}


def run_healthy(seed, blend, forecaster, blips: bool, ticks=360) -> tuple[int, int]:
    """Returns (device-hours that raised an alarm, device-hours observed). Benign blips hit random devices."""
    mon = make(seed, blend, forecaster)
    rng = random.Random(seed)
    flagged = set()
    for t in range(ticks):
        if blips and t % 25 == 5:
            dev = rng.choice([d for d in FLEET])
            try:
                mon.simulator.inject("blip", dev["id"], rng.choice([0.8, 1.0, 1.3]))
            except ValueError:
                pass
        mon.tick()
        for d in mon.overview()["devices"]:
            if d["status"] in WARN:
                flagged.add(d["id"])
    return len(flagged), len(FLEET)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    forecaster = Forecaster.load()
    blends = ["rules"] + (["learned", "max", "mean", "guarded"] if forecaster else [])
    report: dict = {"forecaster": forecaster.version if forecaster else None, "blends": {}}
    for blend in blends:
        rows = [run_fault(s, sc, d["id"], blend, forecaster) for s in range(1, args.seeds + 1)
                for sc, spec in SCENARIOS.items() for d in FLEET if d["type"] in spec["types"]]
        per = {}
        for sc in SCENARIOS:
            sub = [r for r in rows if r["scenario"] == sc]
            leads = [r["lead"] for r in sub if r["lead"] is not None]
            per[sc] = {"runs": len(sub), "caught": sum(r["caught"] for r in sub), "median_lead_min": statistics.median(leads) if leads else None}
        fa = [run_healthy(s, blend, forecaster, False) for s in range(1, args.seeds + 1)]
        fb = [run_healthy(s, blend, forecaster, True) for s in range(1, args.seeds + 1)]
        report["blends"][blend] = {"scenarios": per, "caught": sum(r["caught"] for r in rows), "runs": len(rows),
                                   "false_alarm_devices": sum(a for a, _ in fa), "false_alarm_devices_with_spikes": sum(a for a, _ in fb),
                                   "healthy_devices_observed": sum(b for _, b in fa),
                                   "healthy_hours_each": 6}
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    names = list(SCENARIOS)
    print(f"{'predictor':10} " + " ".join(f"{n[:12]:>13}" for n in names) + "   caught  false alarms (quiet / with spikes)")
    for blend, r in report["blends"].items():
        cells = " ".join(f"{str(r['scenarios'][n]['median_lead_min']):>8} min " for n in names)
        print(f"{blend:10} {cells}  {r['caught']:>3}/{r['runs']:<3} {r['false_alarm_devices']} / {r['false_alarm_devices_with_spikes']} of {r['healthy_devices_observed']}")
    print("\ncells are median minutes of warning before the first critical-level reading (higher is better)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

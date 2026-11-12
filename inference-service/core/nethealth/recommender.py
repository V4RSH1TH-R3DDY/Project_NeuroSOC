"""Turns a prediction into corrective actions an operator can act on.

Rules are keyed by the metric driving the risk, written as an operator would: what to do before the
failure, and why. ``fix`` is set when the action is the one the demo can apply to simulated devices.
"""
from __future__ import annotations

from typing import Any

# metric -> list of (title, detail template, priority 1=urgent..3=routine)
_RULES: dict[str, list[tuple[str, str, int]]] = {
    "mem_pct": [
        ("Restart the leaking process in a maintenance window",
         "Memory is climbing steadily. A controlled restart now avoids an out-of-memory crash {eta}.", 1),
        ("Fail traffic over to the peer device",
         "Drain {device} before it runs out of memory so the restart does not drop sessions.", 2),
        ("Open a vendor case with a memory snapshot", "A leak that trends this cleanly is usually a software defect.", 3),
    ],
    "temp_c": [
        ("Dispatch a technician to check fans and airflow",
         "Temperature is rising faster than load explains, which points to a cooling fault {eta}.", 1),
        ("Shift load away from the device and enable thermal throttling", "Lower load slows the climb and buys time for the repair.", 2),
    ],
    "cpu_pct": [
        ("Identify and restart the runaway process",
         "CPU is climbing without a matching rise in traffic. Capture top processes, then restart the offender {eta}.", 1),
        ("Rate-limit the control plane (CoPP)", "Protect routing protocols from starvation while the cause is found.", 2),
    ],
    "packet_loss_pct": [
        ("Reroute traffic over the backup path",
         "Loss is accelerating on this path. Move critical flows now, before the link fails {eta}.", 1),
        ("Inspect optics, cable and interface counters", "Rising errors with low receive power usually mean a failing transceiver or fibre.", 2),
    ],
    "error_rate_pct": [
        ("Inspect and replace the suspect transceiver or cable",
         "Interface errors are rising, which usually precedes loss and a link failure {eta}.", 1),
        ("Clear counters and watch for 10 minutes", "Confirms whether errors keep accumulating or were a one-off.", 3),
    ],
    "latency_ms": [
        ("Check for congestion or a routing change on the path",
         "Latency is rising. Compare against the previous path and check queue drops.", 2),
    ],
    "link_util_pct": [
        ("Apply QoS or rate limiting to non-critical traffic",
         "Utilization is near capacity. Protect business-critical classes first {eta}.", 1),
        ("Check the NeuroSOC security view for a matching attack",
         "A sudden surge can be a DDoS. Review the Intel Feed for the same window before adding capacity.", 2),
        ("Plan a capacity upgrade or a second path", "If the surge is legitimate, the link needs more headroom.", 3),
    ],
}

_THREAT_RULES = [
    ("Engage DDoS mitigation at the edge",
     "NeuroSOC's traffic models classify recent flows on this device as {attack} ({confidence:.0%} confidence). Rate-limit or scrub the offending sources before the link saturates {eta}.", 1),
    ("Review the attacking sources in the Intel Feed", "The same verdicts are in the security alert queue with evidence and source addresses.", 2),
]

_FIXES = {"mem_pct": "restart_process", "temp_c": "repair_cooling", "cpu_pct": "restart_process",
          "packet_loss_pct": "reroute_traffic", "error_rate_pct": "replace_optics",
          "latency_ms": "reroute_traffic", "link_util_pct": "apply_qos", "threat_pressure": "engage_mitigation"}


def recommend(device: dict[str, Any], signals: list[dict[str, Any]], risk: float, eta_min: float | None,
              threat: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """``threat`` is the traffic models' view of this device ({"pressure", "attack_class", "confidence"}) or None."""
    if risk < 0.25:
        return []
    driving = [s for s in sorted(signals, key=lambda s: s["risk"], reverse=True) if s["risk"] >= 0.25]
    if not driving:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    attack = threat and threat.get("pressure", 0) >= 35 and threat.get("attack_class")
    traffic_hit = next((s for s in driving if s["metric"] in {"link_util_pct", "latency_ms", "packet_loss_pct", "cpu_pct", "threat_pressure"}), None)
    if attack and traffic_hit is not None:
        eta_text = f"in about {traffic_hit['eta_min']:.0f} minutes" if traffic_hit["eta_min"] and traffic_hit["eta_min"] > 0 else "soon"
        for idx, (title, detail, priority) in enumerate(_THREAT_RULES):
            seen.add(title)
            out.append({
                "id": f"{device['id']}:threat:{idx}", "device_id": device["id"], "device_name": device["name"], "title": title,
                "detail": detail.format(attack=str(attack).replace("_", " ").lower(), confidence=float(threat.get("confidence", 0)), eta=eta_text),
                "priority": priority, "metric": "threat_pressure", "fix": "engage_mitigation" if idx == 0 else None,
            })
    for sig in driving[:2]:
        if sig["metric"] == "threat_pressure":
            continue
        eta_text = f"in about {sig['eta_min']:.0f} minutes" if sig["eta_min"] and sig["eta_min"] > 0 else "soon"
        for idx, (title, detail, priority) in enumerate(_RULES.get(sig["metric"], [])):
            if title in seen:
                continue
            seen.add(title)
            if sig["metric"] == "link_util_pct" and not attack and title.startswith("Check the NeuroSOC"):
                detail = "The traffic models see no attack in this window, so the surge is probably legitimate demand. Plan capacity rather than mitigation."
            if sig["metric"] == "link_util_pct" and attack and title.startswith("Check the NeuroSOC"):
                continue  # already covered by the mitigation action
            urgency = max(1, priority - (1 if risk >= 0.75 and priority > 1 else 0))
            out.append({
                "id": f"{device['id']}:{sig['metric']}:{idx}",
                "device_id": device["id"],
                "device_name": device["name"],
                "title": title,
                "detail": detail.format(eta=eta_text, device=device["name"]),
                "priority": urgency,
                "metric": sig["metric"],
                "fix": _FIXES.get(sig["metric"]) if idx == 0 else None,
            })
    out.sort(key=lambda r: r["priority"])
    return out

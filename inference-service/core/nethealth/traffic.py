"""Traffic sampling for the health monitor: flow-feature profiles and a rule-based fallback.

NeuroSOC's trained traffic models (SNN, LNN, XGBoost) classify flows described by CIC-IDS style
features. The simulated fleet has no packets, so when a device sees a traffic surge the simulator
describes the flows it would be seeing as that feature set, and the *same models* that protect the
SOC classify them. If the models are not loaded (tests, a bare install) a small rule-based classifier
stands in, so the link between traffic and health works either way.
"""
from __future__ import annotations

import random
from typing import Any, Callable

# probe(features) -> {"xgb_class", "verdict", "confidence"} or None when no model can answer.
TrafficProbe = Callable[[dict[str, float]], "dict[str, Any] | None"]


def flow_features(kind: str, rng: random.Random) -> dict[str, float]:
    """Flow features (the names in data/feature_columns.txt, which the running pipeline uses) for a kind of traffic: "benign", "ddos" or "scan"."""
    if kind == "ddos":  # a flood of tiny SYN-only flows with no replies
        pkts = rng.randint(2, 6)
        rate = rng.uniform(15_000, 90_000)
        return _flow(pkts, 0, pkts / rate, bytes_per_pkt=rng.uniform(0, 6), syn=pkts, ack=0, psh=0, rst=rng.randint(0, 1),
                     win_fwd=rng.choice([256, 512, 8192]), win_bwd=-1)
    if kind == "scan":  # one probe packet per port, reset back
        return _flow(1, 1, rng.uniform(1e-4, 2e-3), bytes_per_pkt=0.0, syn=1, ack=0, psh=0, rst=1, win_fwd=1024, win_bwd=0)
    fwd, bwd = rng.randint(6, 24), rng.randint(5, 30)  # an ordinary request/response exchange
    return _flow(fwd, bwd, rng.uniform(0.2, 6.0), bytes_per_pkt=rng.uniform(60, 400), syn=1, ack=fwd + bwd - 1, psh=rng.randint(1, 6), rst=0,
                 win_fwd=rng.choice([8192, 29200, 65535]), win_bwd=rng.choice([229, 28960, 65535]))


def _flow(fwd: int, bwd: int, duration_s: float, *, bytes_per_pkt: float, syn: int, ack: int, psh: int, rst: int,
          win_fwd: int, win_bwd: int) -> dict[str, float]:
    total = fwd + bwd
    dur_us = duration_s * 1e6
    fwd_bytes, bwd_bytes = fwd * bytes_per_pkt, bwd * bytes_per_pkt
    return {
        "flow_duration": dur_us, "flow_bytes_per_s": (fwd_bytes + bwd_bytes) / duration_s, "flow_packets_per_s": total / duration_s,
        "flow_iat_mean": dur_us / max(1, total - 1), "flow_iat_total": dur_us, "flow_iat_max": dur_us,
        "fwd_packets_total": float(fwd), "fwd_bytes_total": fwd_bytes, "bwd_packets_total": float(bwd), "bwd_bytes_total": bwd_bytes,
        "fwd_pkt_len_mean": bytes_per_pkt, "fwd_pkt_len_max": bytes_per_pkt * 1.5, "bwd_pkt_len_mean": bytes_per_pkt if bwd else 0.0,
        "fwd_packets_per_s": fwd / duration_s, "bwd_packets_per_s": bwd / duration_s,
        "syn_flag_count": float(syn), "ack_flag_count": float(ack), "psh_flag_count": float(psh), "rst_flag_count": float(rst),
        "pkt_len_mean": bytes_per_pkt, "pkt_len_max": bytes_per_pkt * 2, "avg_packet_size": bytes_per_pkt, "bytes_per_packet": bytes_per_pkt,
        "down_up_ratio": float(bwd // max(1, fwd)), "init_win_bytes_fwd": float(win_fwd), "init_win_bytes_bwd": float(win_bwd),
        "subflow_fwd_packets": float(fwd), "subflow_bwd_packets": float(bwd), "act_data_pkt_fwd": float(max(0, fwd - 1)),
        "min_seg_size_fwd": 20.0, "syn_ratio": syn / total, "ack_ratio": ack / total,
    }


def heuristic_probe(features: dict[str, float]) -> dict[str, Any]:
    """Rule-based stand-in for the trained models: very fast, SYN-only flows with no replies mean a flood."""
    rate, syn_ratio = features.get("flow_packets_per_s", 0.0), features.get("syn_ratio", 0.0)
    if rate > 10_000 and syn_ratio > 0.95:
        return {"xgb_class": "DDOS", "verdict": "HACKER", "confidence": 0.9, "source": "rules"}
    if syn_ratio > 0.95 and features.get("rst_flag_count", 0) >= 1 and features.get("fwd_packets_total", 0) <= 1:
        return {"xgb_class": "RECONNAISSANCE", "verdict": "HACKER", "confidence": 0.7, "source": "rules"}
    return {"xgb_class": "BENIGN", "verdict": "LEGITIMATE", "confidence": 0.9, "source": "rules"}

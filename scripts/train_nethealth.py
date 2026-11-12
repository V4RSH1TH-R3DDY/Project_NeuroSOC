#!/usr/bin/env python3
"""Train and evaluate the network health failure forecaster on simulated fault runs.

  python scripts/train_nethealth.py                 # train, evaluate on held-out seeds, write the model
  python scripts/train_nethealth.py --loo           # also report leave-one-fault-type-out generalisation
  python scripts/train_nethealth.py --no-write      # evaluate only

Label: "this metric reaches its critical level within the next 30 minutes". Samples already past the
critical level are skipped (the forecaster is for early warning). Training and test runs use different
seeds, and the test runs use fault intensities that are not in the training set.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "inference-service"))

from core.nethealth.forecaster import FEATURE_NAMES, HORIZON_MIN, LEARNED_METRICS, metric_features, sigmoid  # noqa: E402
from core.nethealth.metrics import DEVICE_METRICS, METRICS  # noqa: E402
from core.nethealth.predictor import MetricHistory  # noqa: E402
from core.nethealth.simulator import ALL_SCENARIOS, FLEET, NetworkSimulator  # noqa: E402

WARM = 60
TRAIN_INTENSITIES = (0.5, 0.75, 1.0, 1.4, 2.0)
TEST_INTENSITIES = (0.6, 0.9, 1.2, 1.7)
OUT = ROOT / "inference-service" / "core" / "nethealth" / "forecaster_weights.json"


def collect_run(seed: int, scenario: str | None, device_id: str, intensity: float, max_ticks: int = 140):
    """Simulate one run; return [(metric, features, label, heuristic_risk, scenario)] for the target device."""
    sim = NetworkSimulator(seed=seed)
    dtype = next(d["type"] for d in FLEET if d["id"] == device_id)
    keys = [k for k in DEVICE_METRICS[dtype] if k in LEARNED_METRICS]
    hist = {k: MetricHistory() for k in keys}
    traj: dict[str, list[tuple[float, float]]] = {k: [] for k in keys}

    def feed() -> bool:
        tick = sim.step()
        sample = tick.samples.get(device_id)
        if sample is None:
            return False
        for k in keys:
            if k in sample:
                hist[k].add(tick.ts / 60.0, sample[k])
                traj[k].append((tick.ts / 60.0, sample[k]))
        return True

    for _ in range(WARM):
        feed()
    if scenario:
        sim.inject(scenario, device_id, intensity)
    rows_at: list[tuple[int, str, dict]] = []
    for t in range(1, max_ticks + 1):
        if not feed():
            break  # the device went down and stopped reporting
        for k in keys:
            a = hist[k].analyze(k)
            if a is not None:
                rows_at.append((len(traj[k]) - 1, k, a))
    out = []
    for idx, k, a in rows_at:
        spec = METRICS[k]
        if a["value"] >= spec.crit:
            continue
        t_now = traj[k][idx][0]
        future = [v for tt, v in traj[k][idx + 1:] if tt <= t_now + HORIZON_MIN]
        failed = any(v >= spec.crit for v in future)
        out.append((k, metric_features(k, a), 1 if failed else 0, a["risk"], scenario or "healthy"))
    return out


def build(seeds: range, intensities, rng: random.Random):
    data = []
    scenarios = list(ALL_SCENARIOS) + [None, None]
    for seed in seeds:
        scenario = rng.choice(scenarios)
        eligible = [d["id"] for d in FLEET if scenario is None or d["type"] in ALL_SCENARIOS[scenario]["types"]]
        device = rng.choice(eligible)
        data.extend(collect_run(seed, scenario, device, rng.choice(intensities)))
    return data


def fit(rows, epochs: int = 14, lr: float = 0.05, l2: float = 1e-4, seed: int = 0):
    rng = random.Random(seed)
    n_feat = len(FEATURE_NAMES)
    pos = sum(r[2] for r in rows) or 1
    neg = (len(rows) - pos) or 1
    w_pos, w_neg = 0.5 * len(rows) / pos, 0.5 * len(rows) / neg  # balance the classes
    # Weights are balanced for fitting, then the bias is shifted back so probabilities mean what they say.
    w = [0.0] * n_feat
    m, v = [0.0] * n_feat, [0.0] * n_feat
    step = 0
    idx = list(range(len(rows)))
    for _ in range(epochs):
        rng.shuffle(idx)
        for start in range(0, len(idx), 256):
            grad = [0.0] * n_feat
            batch = idx[start:start + 256]
            for i in batch:
                feats, y = rows[i][1], rows[i][2]
                p = sigmoid(sum(a * b for a, b in zip(w, feats)))
                err = (p - y) * (w_pos if y else w_neg)
                for j in range(n_feat):
                    grad[j] += err * feats[j]
            step += 1
            for j in range(n_feat):
                g = grad[j] / len(batch) + l2 * w[j]
                m[j] = 0.9 * m[j] + 0.1 * g
                v[j] = 0.999 * v[j] + 0.001 * g * g
                w[j] -= lr * (m[j] / (1 - 0.9 ** step)) / (math.sqrt(v[j] / (1 - 0.999 ** step)) + 1e-8)
    w[0] += math.log((pos / len(rows)) / (neg / len(rows)))  # undo the class balancing
    return w


def auroc(scores, labels) -> float:
    pairs = sorted(zip(scores, labels))
    rank_sum, n_pos, n_neg, i = 0.0, sum(labels), len(labels) - sum(labels), 0
    if not n_pos or not n_neg:
        return float("nan")
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j + 1) / 2
        rank_sum += avg_rank * sum(l for _, l in pairs[i:j])
        i = j
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def evaluate(w, rows) -> dict:
    scores = [sigmoid(sum(a * b for a, b in zip(w, r[1]))) for r in rows]
    labels = [r[2] for r in rows]
    heur = [r[3] for r in rows]

    def pr(sc, thr):
        tp = sum(1 for s, l in zip(sc, labels) if s >= thr and l)
        fp = sum(1 for s, l in zip(sc, labels) if s >= thr and not l)
        fn = sum(1 for s, l in zip(sc, labels) if s < thr and l)
        return (tp / (tp + fp) if tp + fp else None), (tp / (tp + fn) if tp + fn else None), fp
    lp, lr_, lfp = pr(scores, 0.5)
    hp, hr, hfp = pr(heur, 0.5)
    return {"n": len(rows), "positives": sum(labels), "auroc_learned": round(auroc(scores, labels), 4),
            "auroc_rules": round(auroc(heur, labels), 4),
            "learned@0.5": {"precision": lp and round(lp, 3), "recall": lr_ and round(lr_, 3), "false_positives": lfp},
            "rules@0.5": {"precision": hp and round(hp, 3), "recall": hr and round(hr, 3), "false_positives": hfp}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-runs", type=int, default=420)
    ap.add_argument("--test-runs", type=int, default=160)
    ap.add_argument("--loo", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    rng = random.Random(11)
    train = build(range(1000, 1000 + args.train_runs), TRAIN_INTENSITIES, rng)
    test = build(range(9000, 9000 + args.test_runs), TEST_INTENSITIES, rng)
    print(f"data: {len(train)} train / {len(test)} test samples in {time.time() - t0:.0f}s")
    w = fit(train)
    report = evaluate(w, test)
    print(json.dumps(report, indent=1))

    loo = {}
    if args.loo:
        for held in list(ALL_SCENARIOS):
            tr = [r for r in train if r[4] != held]
            te = [r for r in test if r[4] == held]
            if sum(r[2] for r in te) and sum(1 - r[2] for r in te):
                loo[held] = evaluate(fit(tr), te)
                print(held, "AUROC learned/rules:", loo[held]["auroc_learned"], loo[held]["auroc_rules"])
    if not args.no_write:
        OUT.parent.mkdir(exist_ok=True)
        OUT.write_text(json.dumps({"version": time.strftime("%Y%m%d"), "features": list(FEATURE_NAMES), "weights": [round(x, 5) for x in w],
                                   "horizon_min": HORIZON_MIN, "meta": {"trained_on": "simulated fault runs", "train_samples": len(train),
                                   "test": report, "leave_one_fault_out": loo}}, indent=1))
        print("wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

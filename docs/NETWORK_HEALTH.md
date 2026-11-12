# Network health monitoring and outage prediction

NeuroSOC's network health module watches device telemetry, predicts failures before they happen, and
recommends corrective actions. It reuses the SOC's traffic models, so a traffic surge is judged as an attack or as
ordinary demand by the same detection engine that protects the rest of the platform.

Code: `inference-service/core/nethealth/`, `inference-service/api/routes/nethealth.py`, dashboard page `dashboard/src/pages/NetworkHealth.jsx`.
Enable with `ENABLE_NETHEALTH=true` (API) and `VITE_NETHEALTH_ENABLED=true` (dashboard build).

## How a prediction is made

1. **Inputs** (any mix): resource metrics (CPU, memory, temperature, link utilization, interface errors, packet loss, latency),
   device events (syslog), and traffic verdicts from the SNN/LNN/XGBoost engine.
2. **Rule-based predictor** (`predictor.py`), per metric: closeness to its warn/critical thresholds, a least-squares trend extrapolated
   to the failure threshold (an ETA, only when the fit is good), and a robust z-score against the metric's own baseline.
3. **Learned forecaster** (`forecaster.py`): a logistic regression over scale-free features of each metric's recent history,
   estimating P(the metric reaches its critical level within 30 minutes). Shown on the page as the AI forecast.
4. **Events** (`events.py`): syslog lines are parsed (RFC 5424 and 3164) and matched to failure patterns (fan fault, OOM, BGP down, CRC errors, ...).
   Each carries a weight that decays over 10 minutes; an event that points at a metric already trending badly corroborates it.
5. **Traffic** (`traffic.py`, `ingest_verdict`): attack verdicts become decaying "attack pressure" on edge devices. A surge with attack
   verdicts is reported as an attack with a mitigation action; a surge without them is reported as demand with a capacity action.
6. **Persistence**: a device is called warning or critical only if the rules would already have said so 4 minutes earlier (or it is past critical).
7. **Recommendations** (`recommender.py`): prioritized actions per driving signal, each with the reason and the predicted time to failure.

## Data sources

| Source | How | Enable |
|---|---|---|
| Simulated fleet (12 devices, 6 fault types) | built in | `NETHEALTH_SIMULATOR=true` (default) |
| This host's real CPU, memory, temperature, network | reads `/proc` and `/sys`; shows as a **Live** device | `NETHEALTH_HOST_COLLECTOR=true` (default) |
| Prometheus (node_exporter, snmp_exporter for SNMP devices) | instant queries every 15 s | `NETHEALTH_PROMETHEUS_URL=http://prometheus:9090` |
| Device syslog | UDP listener; arrival time is used as the event time | `NETHEALTH_SYSLOG_PORT=5142` (compose publishes it on `127.0.0.1`) |
| Anything else | `POST /api/v1/nethealth/ingest` with samples and events | operator or admin role |

Real attack traffic seen by the main pipeline also feeds attack pressure: every verdict goes through `ingest_verdict`.

In the simulation, a traffic surge is described as flow features and classified by the running detection engine. With the
default local setting (`MODEL_VERSION_FILE` pointing at a placeholder) the engine uses its built-in rules; set
`MODEL_VERSION_FILE=/models/model_version.json` to use the trained SNN, LNN and XGBoost models instead (XGBoost labelled
4 of 4 simulated flood flows DDOS and 4 of 4 ordinary flows BENIGN when checked).

## Evaluation

All results are on the built-in simulator, so they show the predictor works on those fault shapes, not that it will on a real network.
Training and test use different seeds, and the backtest uses a fault intensity (1.2x) that is not in the training set.

**Learned forecaster** (`scripts/train_nethealth.py`, 79,272 held-out samples; label: reaches critical within 30 min):

| | AUROC |
|---|---|
| Learned forecaster | 0.967 |
| Rule-based risk | 0.855 |

Trained with one fault type held out, on that unseen fault type: link degradation 0.944 (rules 0.814), flapping 0.976 (0.672),
traffic surge 0.925 (0.887), memory leak 0.962 (0.953), thermal 0.970 (0.973), CPU 0.984 (0.983).

**Backtest** (`scripts/nethealth_backtest.py`, 3 seeds, 156 fault runs): the rule-based predictor warned before the first critical-level
reading in 156 of 156 runs. Median warning: memory leak 22 min, fan failure 16, CPU runaway 31, traffic surge 17.5,
link degradation 4 (its loss curve accelerates, which a straight-line fit lags). Flapping is visible immediately, so its lead is 0.
A healthy fleet over 6 simulated hours (36 device runs) raised 0 false alarms.

**Known weakness.** With benign transient spikes mixed in (a 12-minute rise of about 35 points of CPU and similar for
latency, link and temperature, 14 per run), 17 of 36 device runs still reached warning: a spike that lasts longer than the persistence window
looks like the start of a failure until it turns around. The learned forecaster is better at ranking but, at a 0.5 threshold, alarms on more spikes
(26 of 36), so the status comes from the rules by default (`NETHEALTH_BLEND=rules`) and the learned probability is shown beside it.
`NETHEALTH_BLEND=guarded|max|mean|learned` lets you trade false alarms for earlier warning.

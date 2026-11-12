# NeuroSOC: Complete Project Documentation

A reference for anyone joining the project. It explains what NeuroSOC is, how the pieces fit together, what each component does, how to run and test it, and where its limits are.

**Contents**

1. [What NeuroSOC is](#1-what-neurosoc-is)
2. [The three product areas](#2-the-three-product-areas)
3. [Architecture and data flow](#3-architecture-and-data-flow)
4. [Repository map](#4-repository-map)
5. [Components in detail](#5-components-in-detail)
6. [The detection engine (models and decisions)](#6-the-detection-engine-models-and-decisions)
7. [The SDK and Agent Guard (universal engine)](#7-the-sdk-and-agent-guard-universal-engine)
8. [The NovaTrust demo](#8-the-novatrust-demo)
9. [Network health monitoring and outage prediction](#9-network-health-monitoring-and-outage-prediction)
10. [Frontends](#10-frontends)
11. [API reference](#11-api-reference)
12. [Configuration reference](#12-configuration-reference)
13. [Security model](#13-security-model)
14. [Running the project](#14-running-the-project)
15. [Testing and CI](#15-testing-and-ci)
16. [Deployment](#16-deployment)
17. [Known limitations](#17-known-limitations)
18. [Glossary](#18-glossary)
19. [Further reading](#19-further-reading)

---

## 1. What NeuroSOC is

NeuroSOC is an AI-driven Security Operations Center (SOC), built for the CyreneAI Hackathon. It works like a tireless junior analyst:

- It scores every event against that entity's own normal behavior instead of against a global threshold.
- It acts on the result. Suspicious sessions are moved into a decoy sandbox, evidence is collected, and a short, ranked queue is left for a human.
- It learns from analyst decisions and sandbox outcomes through a gated retraining loop.
- It ships an SDK so other apps and AI agents can be protected by the same engine.
- It also predicts network device failures before they cause outages (the network health module).

The detection core combines three models:

| Model | What it catches |
|---|---|
| **SNN** (spiking neural network) | Sudden bursts, such as floods and brute-force spikes |
| **LNN** (liquid neural network) | How behavior evolves over time for each entity |
| **XGBoost** | The final threat class (DDoS, brute force, reconnaissance, and so on) |

The system is **human-in-the-loop by design**: the AI handles detection, sandboxing and first-level triage, and a person makes the final block or restore call.

---

## 2. The three product areas

| Area | Purpose | Main code |
|---|---|---|
| **Network and session security** | Ingest traffic, extract flow features, classify threats, divert attackers to a honeypot, learn from the outcome | `ingestion-service`, `feature-service`, `inference-service`, `sandbox-service`, `feedback-service`, `retraining-service` |
| **SDK and Agent Guard** | Let any web app or AI agent send events or tool calls to NeuroSOC and get an allow, block or step-up decision | `sdk/`, `inference-service/core/universal`, `dashboard` Protection page |
| **Network health and outage prediction** | Watch device metrics, events and attack traffic, predict failures, recommend fixes | `inference-service/core/nethealth`, dashboard Network Health page |

The NovaTrust demo (a fictional bank with an AI assistant) exercises the second area end to end.

---

## 3. Architecture and data flow

```
 Sources                    Streaming pipeline                  Detection core (inference :8000)
 ───────                    ──────────────────                  ────────────────────────────────
 PCAP / NetFlow  ─┐
 Syslog (UDP)    ─┼─► ingestion-service ─► Kafka ─► feature-service ─► Kafka ─► SNN ─► LNN ─► XGBoost
 Bank portal     ─┘      :8080                      80 flow features             │
                                                                                  ▼
                                                                          Decision engine
                                                                 (fuse scores, apply policy.yaml)
                                                                    │                    │
                                              risk ≥ 0.50           │                    │ normal
                                                                    ▼                    ▼
                                                           sandbox-service        Analyst dashboard
                                                           (decoy data, :8001)    (:3000, WebSocket feed)
                                                                    │
                                                                    ▼
                                                  feedback-service ─► retraining-service
                                                  (labels)            (candidate models, human promotes)

 PostgreSQL: verdicts, alerts, decisions, audit chain, sandbox interactions, behavioral profiles
 Redis:      cross-replica rate limiting, SDK state
 Keycloak:   OIDC login and roles (optional locally)
 Prometheus + Grafana: metrics and dashboards (optional)
```

**Kafka topics** you will see in the code: `raw-packets`, `extracted-features`, `verdicts`, `alerts`, `behavior-events`, `feedback-trigger`.

**One request, step by step (network path)**

1. A sensor (PCAP file, NetFlow on UDP 2055, syslog on UDP 5140, or the bank portal) sends events to `ingestion-service`, which wraps them in a versioned, tenant-scoped schema and publishes to Kafka.
2. `feature-service` assembles bidirectional flows and computes 80 statistical features, scaled with a fixed MinMax scaler.
3. `inference-service` consumes the features. The SNN gives an anomaly score, the LNN a class and confidence from the session's recent sequence, XGBoost the threat class, and the behavioral profiler a drift value against the user's baseline.
4. The decision engine fuses these into a confidence and a verdict (`HACKER`, `FORGETFUL_USER`, `LEGITIMATE`, `INCONCLUSIVE`). A tree-logic layer can override the models for unambiguous cases (for example a very high packet rate with an almost pure SYN ratio is DDoS).
5. At confidence 0.50 or more, the session is transparently diverted to the sandbox. Otherwise it continues normally.
6. The verdict is stored, pushed over WebSocket to the dashboard, and, for attacks, becomes an alert.
7. Sandbox outcomes and analyst decisions become labeled training data. Retraining produces candidate models that only an administrator can promote.

---

## 4. Repository map

```
Project_NeuroSOC/
├── ingestion-service/     PCAP, NetFlow, syslog, bank-portal intake -> Kafka
├── feature-service/       Flow assembly and 80-feature extraction
├── inference-service/     FastAPI core (the biggest part of the codebase)
│   ├── main.py            App, wiring, most endpoints (~4.4k lines)
│   ├── api/routes/        dashboard, universal (SDK), demo_agent, nethealth
│   ├── core/engine.py     DecisionEngine: runs the models, fuses, derives the verdict
│   ├── core/snn|lnn|xgboost|behavioral/   The models and the behavioral profiler
│   ├── core/universal/    SDK engine: sites, agent guard, humanity, bot farm, scorer
│   ├── core/demo/         NovaTrust demo backend (account, planner, tools)
│   ├── core/nethealth/    Network health module (stdlib only)
│   ├── core/audit_chain.py, auth.py, kafka_security.py, novatrust_repository.py
│   └── policy.yaml        Confidence-to-action rules
├── sandbox-service/       Honeypot serving decoy data (:8001)
├── feedback-service/      Turns sandbox and analyst outcomes into labels
├── retraining-service/    Trains SNN, LNN, XGBoost and stages candidates
├── dashboard/             React analyst UI (Vite + Tailwind) incl. /demo and /protection
├── simulation_portal/     NovaTrust bank portal (React + TypeScript)
├── sdk/                   Python SDK (PyPI: neurosoc), browser SDK (TypeScript), extension, presets
├── data_pipeline/ datasets/   Synthetic generator, preprocessing, anti-poisoning layers
├── models/ artifacts/     Manifests, scalers, trained weights, staged candidates
├── schemas/               Versioned security-event JSON schemas and the action taxonomy
├── attack_patterns/       Brute force, DDoS, SQLi, bot-farm and agent-hijack simulators
├── identity/              Keycloak realm export
├── monitoring/            Prometheus config, Grafana dashboards
├── deployment/            Preflight, deploy, backup, nginx edge config, Cloudflare guide
├── scripts/               Builds guards, SDK sync, e2e, training and backtest helpers
├── tests/                 pytest suites (unit, integration, e2e, plus top-level suites)
└── docker-compose.yml     The whole stack, selected with profiles
```

---

## 5. Components in detail

### 5.1 Infrastructure

| Service | Role | Host port |
|---|---|---|
| ZooKeeper, Kafka | Event bus | 2181, 9092, 29092 |
| PostgreSQL 15 | Verdicts, alerts, decisions, audit chain, sandbox interactions, profiles | 55432 |
| Redis | Atomic rate-limit counters, SDK key-value state | 6379 |
| Keycloak (profile `local-identity`) | OIDC login, roles `analyst`, `operator`, `admin`, `auditor`, plus `platform-admin` for shared model changes | 8081 |
| Prometheus, Grafana (profile `ops`) | Metrics and dashboards | 9090, 3002 |

All ports bind to `127.0.0.1` by default. Set `HOST_BIND=0.0.0.0` to expose them.

### 5.2 ingestion-service (:8080, host :8082)

Accepts traffic in several modes, selected with `INGESTION_MODE` (`pcap`, `netflow`, `syslog`, `bank_portal`, `all`). It supports PCAP file streams, NetFlow on UDP 2055, syslog on UDP 5140 (it extracts SSH auth-failure lines, RFC 3164 and 5424), and browser or bank-portal behavioral events over HTTP. Every message is normalized into a versioned schema (`schemas/security-event-v1*.json`) and published to Kafka. In staging and production each sensor must carry its own `INGESTION_TENANT_ID`.

### 5.3 feature-service

Assembles bidirectional TCP and UDP flows (an LRU table of up to 100,000 flows), times flows out, and computes 80 statistical features: inter-arrival times, packet length moments, flag counts and ratios, and so on. It applies deterministic MinMax scaling and publishes to `extracted-features`.

### 5.4 inference-service (:8000)

The core. It hosts:

- **The decision engine** (`core/engine.py`): loads the models, runs them, fuses scores, derives the verdict.
- **The REST and WebSocket API** (section 11).
- **The runtime** (`InferenceRuntime` in `main.py`): consumes Kafka, persists verdicts, creates alerts, publishes to the dashboard, calls the sandbox, and keeps an in-memory cache of recent verdicts.
- **Policy** (`policy.yaml`): rules such as `admin_high_risk` (require approval at 0.8 for admins), `critical_attack` (auto-contain web attacks at 0.9), `medium_risk` (observe at 0.6), `low_risk` (log at 0.3).
- **Audit chain**: tenant-scoped, append-only SHA-256 hash chain of sensitive events.
- **Auth**: OIDC bearer tokens with role checks, tenant claim enforcement in shared mode.
- **Universal engine**, **demo backend** and **network health** routes (opt-in via env flags).

### 5.5 sandbox-service (:8001)

The honeypot. When the engine decides a session is hostile, the gateway moves it here without telling the user. The sandbox:

- Serves fake accounts (over $1.2M of decoy balances), a mock transfer flow, and a honey vault with canary files.
- Adds 80 to 200 ms of latency so it is harder to fingerprint.
- Records every request, payload and header in PostgreSQL (`sandbox_interactions`) and enriches the source IP with ASN, ISP and location (IPinfo).
- Watches for the behavioral fork: a confused user stops after an error, a real attacker keeps probing and escalates. Confirmed attacker actions publish to `feedback-trigger`.

### 5.6 feedback-service and retraining-service

- **feedback-service** collects labels from sandbox outcomes and analyst decisions.
- **retraining-service** trains the SNN, LNN and XGBoost (`train_snn.py`, `train_lnn.py`, `train_xgboost.py`), evaluates against a fixed held-out benchmark, and writes **candidates** to `models/candidates/`. It never changes the active model. An administrator promotes, rejects or rolls back through the dashboard or API, and each action is audited before and after it takes effect. Data-poisoning defenses live in `data_pipeline/anti_poisoning.py` and `trust_layer.py`.

### 5.7 Behavioral profiler

`inference-service/core/behavioral` keeps a per-entity baseline (keystroke cadence, mouse entropy, active hours, request speed) and computes a drift value Δ for each session. This is what distinguishes "a user acting oddly" from "someone else".

### 5.8 Attack simulators and data

`attack_patterns/` contains scripted attacks (brute force, DDoS, SQL injection, bot farm, agent hijack) used for demos and testing. `data_pipeline/` has a synthetic generator, risk scoring and the trust and anti-poisoning layers. `datasets/preprocess.py` folds raw CIC dataset labels into NeuroSOC's classes.

### 5.9 Monitoring

Every service exposes Prometheus metrics at `/metrics`. Grafana ships with a pre-provisioned overview dashboard. Optional alerting webhooks and a daily 06:00 SMTP digest exist.

---

## 6. The detection engine (models and decisions)

### 6.1 The models

| Model | Implementation | Training |
|---|---|---|
| SNN | Norse leaky-integrate-and-fire network over spike-encoded flow features (`core/snn`) | Offline, mostly static at runtime |
| LNN | Liquid reservoir plus classifier over a sequence of recent flow vectors (`core/lnn`) | Offline, adaptive state at runtime |
| XGBoost | Tree ensemble with deterministic tree-logic overrides (`core/xgboost`) | Offline, retrained from feedback |

Classes: `BENIGN`, `DDOS`, `BRUTE_FORCE`, `RECONNAISSANCE`, `WEB_ATTACK`, `BOT`, `OTHER` (six have training data).

### 6.2 Fusion and verdicts

In simplified form:

```
confidence = 0.4 * snn_score + 0.4 * lnn_confidence + 0.2 * behavioral_delta   (see engine.py for the exact rule)
verdict    = classify(confidence, behavioral_delta)
```

Verdicts: `LEGITIMATE`, `FORGETFUL_USER` (a confused or locked-out user), `HACKER` (hostile, triggers sandbox and alert), `INCONCLUSIVE` (not enough signal). The tree-logic layer can force `DDOS` when the packet rate is above 10,000 per second and the SYN ratio above 0.95.

### 6.3 Measured accuracy (public data)

From the README, manifest 1.0.4, on CIC-IDS2017 and CIC-DDoS2019 test data:

| | SNN | LNN | XGBoost |
|---|---|---|---|
| Accuracy | 0.903 | 0.920 | 0.996 |
| Macro F1 | 0.739 | 0.749 | 0.961 |

These are public-dataset results, not live traffic results, and the three models were scored on different rows.

### 6.4 Two things worth knowing

- **Local runs use the rule-based path by default.** `.env.example` points `MODEL_VERSION_FILE` at a placeholder file to keep large artifacts off the Docker host, so the engine reports model version `0.0.0` and uses its built-in heuristics (including the DDoS tree-logic override). Set `MODEL_VERSION_FILE=/models/model_version.json` to load the trained SNN, LNN and XGBoost models (manifest 1.0.4).
- **Two feature-name contracts exist in the repo.** The live pipeline reads `data/feature_columns.txt` (snake_case names such as `flow_packets_per_s`, 80 columns), while `models/feature_columns.txt` and `artifacts/feature_columns.txt` use CICFlowMeter-style names. The trained XGBoost candidate's metadata uses the snake_case names. Anything that builds feature dicts for the engine should use the `data/` contract.

---

## 7. The SDK and Agent Guard (universal engine)

This part lets other applications use NeuroSOC without running the whole pipeline.

### 7.1 Pieces

- **Python SDK** (`sdk/python/neurosoc`, PyPI package `neurosoc`, standard library only): check a browser session's verdict from your backend, and guard AI agent tool calls.
- **Browser SDK** (`sdk/js`, TypeScript, bundled to `neurosoc.min.js`): optional telemetry from the page. Sends nothing until the user consents and never blocks the page. Not yet on npm; the dashboard serves the bundle at `/neurosoc.min.js`.
- **Browser extension** (`sdk/extension`) and **presets** (`sdk/presets/*.json`) for common site types.
- **Server side** (`inference-service/core/universal`): site registry, scorer, baselines, humanity and bot-farm detection, the agent guard, explanations, and SDK CORS handling.

### 7.2 Onboarding model

An operator adds an **application** (name, URL, type web / agent / both, mode **Monitor** or **Protect**). The wizard issues a publishable key (`pk_`, safe in a page) and a secret key (`sk_`, shown once, server only). For agents it also records the agent id, its allowed tools, sensitive actions, and authorized resources.

### 7.3 Agent Guard

Every guarded tool call is checked **outside the language model** before it runs. It blocks:

- an unregistered agent,
- a tool not on the agent's list,
- a resource outside the agent's `authorized_resources`,
- more than `max_sensitive_per_minute` (default 10) sensitive actions,
- patterns like prompt injection (a transfer to a new destination, far above the usual amount, right after an instruction that came from outside content).

In **Monitor** mode the decision is recorded and the call runs. In **Protect** mode it is blocked. If NeuroSOC is unreachable, guarded tools **fail closed** (money moves nothing) unless the caller passes `fail_open=True`.

### 7.4 Example

```python
from neurosoc import NeuroSOC, ActionBlocked

soc = NeuroSOC(endpoint="https://your-neurosoc-host", secret_key="sk_live_...")

@soc.guard_tool(agent_id="flows-agent-1", action="token.transfer", resource="treasury", sensitivity="critical")
def transfer(to: str, amount: float, instruction_source: str = "owner"):
    ...

verdict = soc.session_verdict(request.form["neurosoc_session"])   # server-side check of a browser session
```

---

## 8. The NovaTrust demo

NovaTrust is a fictional bank with an AI assistant, **Nova AI**. Everything except the bank data is real: the browser runs the real JS SDK, Nova AI's backend uses the real Python SDK over HTTP, and decisions come from the real engine.

Demo pages (`/demo`): Dashboard, Transfer, Nova AI chat, Account, Security (with a testing panel in demo mode). The panel can launch four labeled, simulated attacks: prompt injection, unauthorized resource, excessive actions, and a suspicious scripted signup. **Reset demo** restores the account.

Gating: `NEUROSOC_DEMO_MODE` (API) and `VITE_DEMO_MODE` (dashboard) are off by default. The API refuses to start in demo mode when `APP_ENV` is staging or production. Nova AI uses Claude if `ANTHROPIC_API_KEY` is set; otherwise a deterministic scripted planner answers.

Test evidence is in `docs/DEMO_TEST_REPORT.md`; re-run the browser flow with `node scripts/e2e_novatrust.mjs` against a freshly started API.

---

## 9. Network health monitoring and outage prediction

The newest module. It answers: *which devices will fail soon, why, and what should we do?* Code lives in `inference-service/core/nethealth/`, with a longer write-up in `docs/NETWORK_HEALTH.md`.

### 9.1 Enabling it

API: `ENABLE_NETHEALTH=true`. Dashboard (build-time): `VITE_NETHEALTH_ENABLED=true`, which adds the **Network Health** tab.

### 9.2 Inputs

| Source | How | Notes |
|---|---|---|
| Simulated fleet | Built in, 12 devices (routers, switches, firewall, servers, WAN links), one tick = one simulated minute | `NETHEALTH_SIMULATOR=true` by default |
| This host | `HostCollector` reads `/proc` and `/sys` for CPU, memory, temperature, link utilization, interface errors | Shows as a **Live** device |
| Prometheus | Instant queries (node_exporter, and snmp_exporter for SNMP devices) | `NETHEALTH_PROMETHEUS_URL` |
| Syslog | UDP listener, RFC 5424 and 3164 | `NETHEALTH_SYSLOG_PORT` (compose publishes 5142/udp) |
| HTTP | `POST /api/v1/nethealth/ingest` with samples and events | Operator or admin role |
| Traffic verdicts | Attack verdicts from the detection engine | Become "attack pressure" on edge devices |

Metrics: `cpu_pct`, `mem_pct`, `temp_c`, `link_util_pct`, `error_rate_pct`, `packet_loss_pct`, `latency_ms`, plus `threat_pressure` (from the traffic models).

### 9.3 How a prediction is made

1. **Rule-based predictor** (`predictor.py`), per metric:
   - *Level*: how close the value is to its warn and critical thresholds.
   - *Trend*: a least-squares slope over the recent window, extrapolated to the failure threshold to give an ETA in minutes. An ETA is shown only when the fit is good (r² above 0.65).
   - *Anomaly*: a robust z-score against the metric's own baseline (median and MAD).
   Combined into a 0 to 1 risk per device, with the second-strongest signal adding corroboration.
2. **Learned forecaster** (`forecaster.py`): a logistic regression over scale-free features (headroom, level, slope, acceleration, z-score, trend quality, ETA) estimating *P(this metric reaches its critical level within 30 minutes)*. Weights are in `forecaster_weights.json` and retrained with `scripts/train_nethealth.py`. The page shows it as "AI forecast".
3. **Device events** (`events.py`): syslog lines are matched to failure patterns (fan fault, out-of-memory, BGP down, CRC errors, power fault, and others). Each has a weight scaled by severity that halves every 10 minutes. An event that points at a metric already trending badly adds corroboration.
4. **Attack traffic** (`traffic.py`, `ingest_verdict`): attack verdicts add decaying pressure to firewalls, routers and WAN links. In the demo, a traffic surge is converted to flow features and classified by the running engine, and DDoS verdicts also appear in the Intel Feed. A surge with attack verdicts gets a mitigation action; a surge without them is treated as demand and gets a capacity action.
5. **Persistence**: a device is called warning or critical only if the rules would have said so 4 minutes earlier, or it is already past critical. This stops short spikes from paging anyone.
6. **Recommendations** (`recommender.py`): prioritized actions (Urgent, Soon, Routine) for the strongest signals, each with the reason and the predicted time to failure. In the demo some actions have an **Apply fix** button.

Device status: `healthy`, `watch`, `warning`, `critical`, `down`. The status comes from the rule-based risk by default; the learned probability is shown beside it (`NETHEALTH_BLEND` can mix them: `rules`, `learned`, `max`, `mean`, `guarded`).

### 9.4 Fault scenarios in the demo

Memory leak, fan failure (overheating), CPU runaway, link degradation, traffic surge (possible DDoS), interface flapping. The Network Health page has an "Inject fault" control; "Apply fix" clears the fault and metrics recover over a few minutes.

### 9.5 Evaluation

All numbers are from the built-in simulator, with training and test on different seeds, so they show the predictor works on those fault shapes, not that it will on a real network.

- Learned forecaster, held-out AUROC: **0.967** (rule-based risk: 0.855). With one fault type left out of training, it scored 0.93 to 0.98 on the unseen type.
- Backtest (`python scripts/nethealth_backtest.py`, 156 runs): the rules warned before the first critical reading in 156 of 156. Median warning: memory leak 22 min, fan failure 16, CPU runaway 31, traffic surge 17.5, link degradation 4.
- A healthy fleet over 6 simulated hours: 0 false alarms.
- Weakness: with benign 12-minute spikes mixed in, 17 of 36 device runs still reached warning. A spike longer than the persistence window looks like the start of a failure until it turns around.

### 9.6 Network health API

| Method and path | Purpose |
|---|---|
| `GET /api/v1/nethealth/overview` | Fleet status, predicted outages, recommended actions, sources, model info |
| `GET /api/v1/nethealth/devices/{id}` | One device: metric series, thresholds, signals, events, forecast |
| `GET /api/v1/nethealth/events` | Recent device events (`limit`, `device_id`) |
| `POST /api/v1/nethealth/ingest` | Push samples and events (operator or admin) |
| `POST /api/v1/nethealth/simulate` | Inject a fault (simulator only; `scenario`, `device_id`, `intensity`) |
| `POST /api/v1/nethealth/devices/{id}/fix` | Apply a corrective action (simulator only) |
| `POST /api/v1/nethealth/reset` | Restart the simulated fleet |

### 9.7 Demo script (about 3 minutes)

1. Open **Network Health**. Point out the 12 simulated devices and the one **Live** device (the real host).
2. Choose *Traffic surge* on the **Edge firewall**, click **Inject fault**. Within about 30 seconds the page shows "Attack traffic: classified as ddos", an AI forecast percentage, and the action "Engage DDoS mitigation". Open **Intel Feed** to show the same verdicts as security alerts.
3. Click **Apply fix**. The attack note disappears and the device recovers.
4. Repeat with *Memory leak* on **App server 1** to show a trend-based prediction with a time to failure.

---

## 10. Frontends

### 10.1 Analyst dashboard (:3000)

React 18, Vite, Tailwind, Recharts, Leaflet, Zustand. Routes:

| Route | Page |
|---|---|
| `/` | Overview: activity, threat map, model health |
| `/intel-feed` | Alert triage queue, severity trend, event log |
| `/response-ops` | Response operations, candidate model review and promotion |
| `/protection` | SDK protection: campaign integrity, agent watch, live verdicts, sites (needs `VITE_UNIVERSAL_ENABLED`) |
| `/network-health` | Outage prediction (needs `VITE_NETHEALTH_ENABLED`) |
| `/demo` | The NovaTrust customer demo (needs demo mode) |
| `/callback` | OIDC redirect |

Live data arrives over WebSocket. Mock data (MSW) is available with `VITE_USE_MOCKS=true` (never in production builds; a build guard enforces it).

**Generated copy warning:** `dashboard/src/sdk/*.ts` and `dashboard/public/neurosoc.min.js` are generated from `sdk/js` by `scripts/sync_dashboard_sdk.py`. Never edit them by hand.

### 10.2 NovaTrust bank portal (:3001)

A React and TypeScript simulation of a bank, used to generate realistic behavioral telemetry and credential-stuffing scenarios. Its simulation APIs are off unless `ENABLE_SIMULATION_API=true` and are local-demo only.

---

## 11. API reference

Interactive docs: `http://localhost:8000/docs` (local and test environments).

**Core (inference-service)**

| Path | Purpose |
|---|---|
| `GET /`, `/health`, `/metrics` | Status, health, Prometheus metrics |
| `GET /api/v1/verdicts/latest`, `/verdicts/current`, `/verdicts/{user_id}` | Recent and per-user verdicts |
| `GET /api/v1/alerts/latest`, `GET /api/v1/alerts` | Alert feeds |
| `POST /api/v1/alerts/{session_id}/decision` | Analyst decision (block, restore, and so on) |
| `WS /api/v1/ws/alerts`, `/ws/alerts` | Live alert stream |
| `POST /api/v1/analyze` | Analyze a session on demand |
| `POST /api/v1/behavioral`, `/behavioral/vectorize` | Behavioral events and vectors |
| `GET /api/v1/profiles/{user_id}` | Behavioral profile |
| `GET /api/v1/stats` | Headline counters |
| `GET /api/v1/audit/events?after_sequence=0` | Verified audit chain export (admins, auditors) |
| `GET /api/v1/sandbox/{session_id}/replay` | Replay a sandbox session |
| `GET /api/v1/model/version`, `POST /api/v1/models/reload` | Active model info and reload (admin) |
| `GET /api/v1/models/candidates`, `POST .../{id}/promote`, `.../{id}/reject`, `POST /api/v1/models/rollback` | Candidate review (platform-admin for shared models) |
| `POST /api/v1/bank/login`, `/transfer`, `/honeypot-hit`, `/web-attack-detected` | Bank-portal simulation (local only) |

**SDK (universal engine; keyed by `X-NeuroSOC-Key`)**

| Path | Purpose |
|---|---|
| `POST /api/v1/sdk/events` | Browser SDK telemetry |
| `POST /api/v1/sdk/guard` | Guard an agent tool call |
| `GET /api/v1/sdk/sessions/{id}/verdict` | Server-side session verdict |
| `GET /api/v1/sdk/config` | SDK configuration |
| `GET /api/v1/universal/verdicts/latest`, `/entities/{type}/{id}`, `/resources/{id}/integrity` | Analyst reads |
| `POST /api/v1/universal/verdicts/{id}/override` | Restore or confirm a flagged verdict |
| `GET/POST/PUT /api/v1/universal/sites`, `POST .../sites/{id}/webhooks` | Application registry |
| `WS /api/v1/universal/ws` | Live verdict stream |

**Demo (only in demo mode)**: `/api/v1/demo/config`, `/connect`, `/account`, `/account/reset`, `/agent/chat`, `/transfer`, `/transfer/{id}/confirm|cancel`, `/simulate`.

**Sandbox service**: `/sessions`, `/sessions/{token}/terminate`, `/sessions/{token}/replay`, and the decoy bank pages (`/login`, `/dashboard`, `/transfer`, `/security-alert`).

**Network health**: see section 9.6.

---

## 12. Configuration reference

Copy `.env.example` to `.env`. Important variables (see the file for all of them):

| Variable | Meaning |
|---|---|
| `APP_ENV` | `local`, `test`, `staging`, `production`. Staging and production enforce strict checks and refuse demo flags. |
| `DATABASE_URL`, `POSTGRES_PASSWORD` | PostgreSQL connection (required) |
| `REDIS_URL` | Redis (rate limits) |
| `KAFKA_BOOTSTRAP`, `KAFKA_SECURITY_PROTOCOL`, per-service SASL settings | Kafka access |
| `OIDC_REQUIRED`, `OIDC_ISSUER`, `OIDC_AUDIENCE` | Require Keycloak bearer tokens |
| `MODEL_PATH`, `MODEL_VERSION_FILE` | Where models live; the version file selects the active manifest (see 6.4) |
| `HOST_BIND` | Interface for published ports (default `127.0.0.1`) |
| `ENABLE_UNIVERSAL_ENGINE`, `UNIVERSAL_SITES_FILE`, `UNIVERSAL_HASH_SECRET` | SDK engine |
| `NEUROSOC_DEMO_MODE`, `VITE_DEMO_MODE`, `ENABLE_SIMULATION_API`, `NOVATRUST_DEMO_*` | NovaTrust demo (local only) |
| `VITE_UNIVERSAL_ENABLED` | Show the Protection page |
| `ENABLE_NETHEALTH`, `VITE_NETHEALTH_ENABLED` | Network health API and page |
| `NETHEALTH_SIMULATOR`, `NETHEALTH_TICK_SECONDS` | Simulated fleet and its speed (real seconds per simulated minute) |
| `NETHEALTH_BLEND` | `rules` (default), `learned`, `max`, `mean`, `guarded` |
| `NETHEALTH_HOST_COLLECTOR`, `NETHEALTH_HOST_INTERVAL`, `NETHEALTH_HOST_ID` | Real host as a live device |
| `NETHEALTH_PROMETHEUS_URL`, `NETHEALTH_PROMETHEUS_INTERVAL` | Pull metrics from Prometheus |
| `NETHEALTH_SYSLOG_PORT` | UDP syslog port (0 = off) |
| `ANTHROPIC_API_KEY` | Optional: Nova AI reasons with Claude |

Tip: in `.env` files, do not leave an empty value followed by an inline comment (`KEY=  # note`); compose reads the comment as the value. Put the comment on its own line.

---

## 13. Security model

- **Identity**: Keycloak OIDC with role-based access (`analyst`, `operator`, `admin`, `auditor`, `platform-admin`). In shared mode every token must carry a signed `tenant_id`; caller-supplied tenant headers are ignored.
- **Tenancy**: verdicts, alerts, decisions, audit events, labels and profiles are tenant-scoped in the application and with PostgreSQL row-level security.
- **Audit**: append-only SHA-256 hash chain, exportable and verified per page. External anchoring and retention are still needed against a database administrator rewriting rows and chain.
- **Rate limiting**: atomic Redis counters shared across replicas; if Redis is lost, requests fail with 503 rather than falling back to per-process limits.
- **Production checks**: staging and production refuse to start without HTTPS for OIDC and CORS, OIDC required, simulation APIs off, a non-demo database, Kafka SASL_SSL with separate service principals, Redis over TLS with a shared hash secret, and PostgreSQL `sslmode=verify-full`.
- **Model changes**: promotion, rejection and rollback write an audited attempt first and need a success audit event; files are restored if the final write fails. Shared models need `platform-admin`.
- **Agent Guard**: policy is enforced outside the model, fails closed for money, and the source of an instruction (owner or content) is decided by the harness, not the model.
- **Webhooks**: an SSRF guard blocks private targets unless explicitly allowed.
- **Network health**: mutating routes require operator or admin roles when roles are known; the syslog listener caps its rate; device count is capped at 500; attack probes use TEST-NET-3 documentation addresses.

---

## 14. Running the project

### 14.1 Prerequisites

Docker with Compose, Node.js 20+, Python 3.11+ (only for tests and training), roughly 16 GB of RAM.

### 14.2 Full demo stack

```bash
cp .env.example .env     # then edit passwords; set ENABLE_NETHEALTH / VITE_NETHEALTH_ENABLED etc. as needed
docker compose --profile phase8plus --profile phase10plus --profile phase11plus --profile demo up -d --build
```

Compose profiles:

| Profile | Adds |
|---|---|
| none | ZooKeeper, Kafka, PostgreSQL, Redis, ingestion, feature, inference |
| `phase8plus` | feedback and retraining |
| `phase10plus` | sandbox |
| `phase11plus` | analyst dashboard |
| `demo` | dashboard and the NovaTrust bank portal |
| `local-identity` | Keycloak |
| `ops` | Prometheus and Grafana |

### 14.3 Addresses

| What | Address |
|---|---|
| Analyst dashboard | http://localhost:3000 |
| Network Health page | http://localhost:3000/network-health |
| Protection page | http://localhost:3000/protection |
| NovaTrust bank portal | http://localhost:3001 |
| Inference API and docs | http://localhost:8000 and /docs |
| Sandbox | http://localhost:8001 |
| Keycloak, Prometheus, Grafana | :8081, :9090, :3002 (with their profiles) |

### 14.4 Without Docker (demo only)

```bash
# API, from inference-service/
ENABLE_UNIVERSAL_ENGINE=true NEUROSOC_DEMO_MODE=true REDIS_URL='' DATABASE_URL='' KAFKA_BOOTSTRAP=127.0.0.1:1 \
  python -m uvicorn main:app --port 8010
# Dashboard, from dashboard/
VITE_DEMO_MODE=true VITE_UNIVERSAL_ENABLED=true VITE_API_URL=/ VITE_PROXY_TARGET=http://127.0.0.1:8010 npx vite --port 5173
```

### 14.5 Training

```bash
python datasets/preprocess.py
python retraining-service/train_snn.py --epochs 50
python retraining-service/train_lnn.py --epochs 30
python retraining-service/train_xgboost.py
python scripts/train_nethealth.py        # the network health forecaster (a few minutes)
```

Trained models are written as candidates and are never promoted automatically.

### 14.6 Operating notes

- The inference container mounts `./inference-service` at `/app`, so Python changes take effect after `docker compose restart inference`. Dashboard changes need a rebuild: `docker compose --profile phase11plus up -d --build dashboard`.
- `VITE_*` values are baked in at dashboard build time.
- Demo state (applications, bot flags) is in memory; restart the API for a clean e2e run.

---

## 15. Testing and CI

```bash
pip install -r inference-service/requirements.txt pytest jsonschema httpx
pytest tests -q                                  # everything; no Docker, Kafka, Redis or database needed
pytest tests/test_nethealth.py -v                # network health (stdlib + FastAPI only)
ruff check .                                     # correctness-only lint (F, E9)
cd sdk/js && npm run build && npm test           # browser SDK
cd dashboard && npx vite build                   # dashboard builds
cd simulation_portal && npm run typecheck        # portal type-check
node --test tests/02_integration/test_production_build_guard.mjs
python scripts/check_production_credential_patterns.py
node scripts/e2e_novatrust.mjs                   # browser e2e of the demo (needs Chrome and a fresh API)
python scripts/nethealth_backtest.py             # network health predictor comparison
```

Test layout: `tests/01_unit`, `02_integration`, `03_e2e`, plus top-level suites for the SDK, agent guard, demo and deployment. Tests inside the numbered folders find the repo root with `parents[2]`; top-level files use `parents[1]`. Keep that in mind when moving a test.

CI (`.github/workflows/product-safety.yml`) runs the build guards, the credential scan, ruff, the portal type-check and the backend suite; other workflows deploy and publish the SDKs.

At the time of writing, the full suite passed inside the project's Python 3.11 image: 417 passed, 7 skipped.

---

## 16. Deployment

The demo can run on a spare PC published through a Cloudflare Tunnel: no router ports, nothing published on the PC's network, the analyst console behind a password, and a self-hosted GitHub runner redeploying approved release tags.

```bash
python3 deployment/preflight.py init --hostname demo.yourdomain.com
python3 deployment/preflight.py check
deployment/deploy.sh
```

See `deployment/README.md`. This is a hardened public demo, not production: it refuses to run with `APP_ENV=production`.

---

## 17. Known limitations

- Model accuracy figures are on public datasets, not live traffic; the live feature service computes some statistics differently from the training data.
- Retraining data from human feedback is still small (137 rows in the current manifest), so continuous improvement is a design, not a measured result.
- Local runs default to the engine's rule-based path (section 6.4), and the repo carries two feature-name contracts.
- Network health results come from a simulator. Only the host collector is real data. The Prometheus collector has been tested against a fake server, not a real exporter.
- Network health weak spots: benign multi-minute spikes can still raise warnings; link degradation (an accelerating curve) gets only a few minutes of warning.
- The browser SDK is not published to npm yet.
- Shared-mode multi-tenancy still needs a production migration rehearsal, IdP verification and source-network isolation; local tests are not a production certification.
- Peer-reviewed references for the SNN + LNN + XGBoost combination are not listed yet.

---

## 18. Glossary

| Term | Meaning |
|---|---|
| **SOC** | Security Operations Center |
| **SNN** | Spiking neural network: event-driven, good at bursts |
| **LNN** | Liquid neural network: a continuous-time state that follows behavior |
| **Verdict** | The engine's judgement of a session (`HACKER`, `FORGETFUL_USER`, `LEGITIMATE`, `INCONCLUSIVE`) |
| **Sandbox / honeypot** | A decoy environment that serves fake data to capture an attacker's actions |
| **Candidate model** | A retrained model staged for human approval; never active until promoted |
| **Agent Guard** | The check that runs before an AI agent's tool call executes |
| **Monitor / Protect** | SDK modes: record only, or block |
| **pk_ / sk_ keys** | Publishable (browser-safe) and secret (server-only) SDK keys |
| **Edge device** | A firewall, router or WAN link facing the outside, which feels attack traffic first |
| **Attack pressure** | A 0 to 100 score from recent attack verdicts, decaying with a 3-minute half-life |
| **ETA / time to failure** | Minutes until a metric is predicted to reach its failure threshold |
| **AUROC** | How well a score ranks failing cases above healthy ones (1.0 perfect, 0.5 chance) |
| **Persistence** | Requiring risk to have been high 4 minutes ago before calling a warning |
| **TEST-NET-3** | The 203.0.113.0/24 documentation address range, used for simulated attackers |

---

## 19. Further reading

In the repository: `README.md`, `TESTING.md`, `docs/NETWORK_HEALTH.md`, `docs/DEMO_TEST_REPORT.md`, `sdk/README.md`, `sdk/python/README.md`, `sdk/js/README.md`, `deployment/README.md`, `schemas/taxonomy.json`.

Elsewhere: the Technical Specification Series (four Word volumes in the project's Google Drive folder), the design document on Google Docs, and the demo video on YouTube (links in `README.md`).

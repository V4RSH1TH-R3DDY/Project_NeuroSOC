"""Network health monitoring: predictor, simulator, recommender and API. No Docker or external services."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "inference-service"))

from core.nethealth import NetHealthMonitor, NetworkSimulator  # noqa: E402
from core.nethealth.predictor import MetricHistory, linear_fit  # noqa: E402


def fresh(seed: int = 7) -> NetHealthMonitor:
    return NetHealthMonitor(NetworkSimulator(seed=seed))


def test_linear_fit_recovers_slope_and_r2():
    slope, r2 = linear_fit([(x, 2.0 * x + 1) for x in range(10)])
    assert slope == pytest.approx(2.0) and r2 == pytest.approx(1.0)
    assert linear_fit([(1, 5), (1, 6)]) == (0.0, 0.0)  # degenerate x


def test_healthy_fleet_has_no_alarms():
    mon = fresh()
    for _ in range(240):
        mon.tick()
        assert all(d["status"] in {"healthy", "watch"} for d in mon.overview()["devices"])
    assert mon.overview()["predictions"] == []


def test_noisy_flat_metric_gives_no_eta():
    hist = MetricHistory()
    import random
    rng = random.Random(1)
    for i in range(60):
        hist.add(i, 50 + rng.gauss(0, 1))
    sig = hist.analyze("mem_pct")
    assert sig["eta_min"] is None and sig["risk"] < 0.25


def test_memory_leak_is_predicted_before_the_device_goes_down():
    mon = fresh()
    mon.inject("memory_leak", "app-srv-01")
    warned = down = None
    for t in range(1, 120):
        mon.tick()
        view = mon.device_detail("app-srv-01")
        if warned is None and view["status"] in {"warning", "critical"}:
            warned = t
            assert view["predicted_failure_in_min"] is not None
            assert view["signals"][0]["metric"] == "mem_pct"
        if view["status"] == "down":
            down = t
            break
    assert warned is not None and down is not None
    assert down - warned >= 15, "operators need at least 15 minutes of warning"


def test_prediction_eta_is_roughly_right():
    mon = fresh()
    mon.inject("thermal_runaway", "core-rtr-01")
    for _ in range(25):
        mon.tick()
    view = mon.device_detail("core-rtr-01")
    temp = next(s for s in view["signals"] if s["metric"] == "temp_c")
    # 0.85 °C/min toward 92 °C: the true time to failure follows from the current reading.
    actual = (92 - temp["value"]) / 0.85
    assert temp["eta_min"] == pytest.approx(actual, rel=0.3)


@pytest.mark.parametrize("scenario,device,metric", [
    ("cpu_runaway", "edge-fw-01", "cpu_pct"),
    ("link_degradation", "wan-link-01", "packet_loss_pct"),
    ("traffic_surge", "wan-link-02", "link_util_pct"),
])
def test_scenarios_are_detected_with_the_right_driver(scenario, device, metric):
    mon = fresh()
    mon.inject(scenario, device)
    for _ in range(35):
        mon.tick()
    view = mon.device_detail(device)
    assert view["status"] in {"warning", "critical", "down"}
    assert metric in {s["metric"] for s in view["signals"]}
    assert view["recommendations"], "a risky device must come with corrective actions"


def test_down_device_reports_down_and_recovers_after_fix():
    mon = fresh()
    mon.inject("memory_leak", "db-srv-01")
    for _ in range(80):
        mon.tick()
    assert mon.device_detail("db-srv-01")["status"] == "down"
    assert mon.overview()["counts"]["down"] == 1
    mon.apply_fix("db-srv-01", "restart_process")
    for _ in range(30):
        mon.tick()
    view = mon.device_detail("db-srv-01")
    assert view["status"] in {"healthy", "watch"}, view["status"]
    assert view["scenario"] is None


def test_fix_before_failure_prevents_the_outage():
    mon = fresh()
    mon.inject("cpu_runaway", "app-srv-02")
    for _ in range(25):
        mon.tick()
    mon.apply_fix("app-srv-02", "restart_process")
    for _ in range(80):
        mon.tick()
    assert mon.device_detail("app-srv-02")["status"] != "down"


def test_scenario_must_fit_device_type():
    mon = fresh()
    with pytest.raises(ValueError):
        mon.inject("link_degradation", "app-srv-01")  # servers have no link metrics here
    with pytest.raises(KeyError):
        mon.inject("memory_leak", "nope")
    with pytest.raises(KeyError):
        mon.inject("nope", "app-srv-01")


def test_external_telemetry_without_simulator():
    mon = NetHealthMonitor(None)
    mon.register_device("sw-ext", "External switch", "switch", "Lab")
    t0 = time.time() - 3600
    for i in range(60):
        mon.ingest("sw-ext", t0 + i * 60, {"cpu_pct": 20, "mem_pct": 40 + i * 0.8, "bogus": 1, "temp_c": float("nan")})
    detail = mon.device_detail("sw-ext")
    assert "bogus" not in detail["metrics"] and "temp_c" not in detail["metrics"]
    assert detail["status"] in {"warning", "critical"}
    assert detail["predicted_failure_in_min"] is not None
    with pytest.raises(RuntimeError):
        mon.inject("memory_leak", "sw-ext")


def test_reset_restores_a_clean_fleet():
    mon = fresh()
    mon.inject("memory_leak", "app-srv-01")
    for _ in range(80):
        mon.tick()
    mon.reset()
    assert mon.overview()["counts"]["down"] == 0
    assert all(d["scenario"] is None for d in mon.overview()["devices"])


# ── API ────────────────────────────────────────────────────────────────────

@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes.nethealth import build_router

    app = FastAPI()
    app.state.roles = None
    mon = fresh()
    app.include_router(build_router(mon, lambda request: app.state.roles))
    c = TestClient(app)
    c.monitor, c.app_ref = mon, app
    return c


def test_api_overview_and_device(client):
    data = client.get("/api/v1/nethealth/overview").json()
    assert data["total"] == 12 and data["simulated"] is True
    assert {s["key"] for s in data["scenarios"]} >= {"memory_leak", "link_degradation"}
    dev = client.get("/api/v1/nethealth/devices/core-rtr-01").json()
    assert dev["series"]["cpu_pct"] and dev["thresholds"]["cpu_pct"]["crit"] == 95
    assert client.get("/api/v1/nethealth/devices/missing").status_code == 404


def test_api_simulate_fix_reset_flow(client):
    r = client.post("/api/v1/nethealth/simulate", json={"scenario": "memory_leak", "device_id": "app-srv-01"})
    assert r.status_code == 200
    for _ in range(30):
        client.monitor.tick()
    assert client.get("/api/v1/nethealth/overview").json()["recommendations"]
    assert client.post("/api/v1/nethealth/devices/app-srv-01/fix", json={"fix": "restart_process"}).status_code == 200
    assert client.post("/api/v1/nethealth/reset").json() == {"status": "reset"}
    assert client.post("/api/v1/nethealth/simulate", json={"scenario": "nope", "device_id": "app-srv-01"}).status_code == 404
    assert client.post("/api/v1/nethealth/simulate", json={"scenario": "link_degradation", "device_id": "app-srv-01"}).status_code == 422


def test_api_ingest_registers_devices_and_validates(client):
    now = time.time()
    body = {"samples": [{"device_id": "ext-1", "device_type": "router", "ts": now, "metrics": {"cpu_pct": 12.5, "nope": 1}}],
            "events": [{"device_id": "ext-1", "severity": "error", "message": "BGP neighbor down"}]}
    out = client.post("/api/v1/nethealth/ingest", json=body).json()
    assert out == {"accepted_metrics": 1, "accepted_events": 1}
    assert client.get("/api/v1/nethealth/devices/ext-1").status_code == 200
    bad = {"samples": [{"device_id": "x", "device_type": "toaster", "metrics": {}}]}
    assert client.post("/api/v1/nethealth/ingest", json=bad).status_code == 422
    assert any(e["message"] == "BGP neighbor down" for e in client.get("/api/v1/nethealth/events").json()["events"])


def test_api_mutations_need_operator_role_when_roles_are_known(client):
    client.app_ref.state.roles = {"viewer"}
    assert client.get("/api/v1/nethealth/overview").status_code == 200
    for path, body in [("/simulate", {"scenario": "memory_leak", "device_id": "app-srv-01"}),
                       ("/reset", None), ("/devices/app-srv-01/fix", {}), ("/ingest", {"samples": []})]:
        assert client.post("/api/v1/nethealth" + path, json=body).status_code == 403, path
    client.app_ref.state.roles = {"operator"}
    assert client.post("/api/v1/nethealth/reset").status_code == 200


# ── traffic models, events, learned forecaster, live collectors ───────────

def test_traffic_surge_is_classified_as_an_attack_and_raises_pressure():
    mon = fresh()
    mon.inject("traffic_surge", "wan-link-01")
    for _ in range(30):
        mon.tick()
    view = mon.device_detail("wan-link-01")
    assert view["threat"]["attack_class"] == "DDOS" and view["threat"]["pressure"] > 35
    assert "threat_pressure" in {s["metric"] for s in view["signals"]}
    titles = [r["title"] for r in view["recommendations"]]
    assert titles and titles[0].startswith("Engage DDoS mitigation")
    assert mon.device_detail("app-srv-01")["threat"] is None  # servers are not edge devices


def test_a_busy_link_without_attack_verdicts_is_not_called_an_attack():
    mon = fresh()
    mon.ingest_verdict({"xgb_class": "BENIGN", "verdict": "LEGITIMATE", "confidence": 0.99})
    assert mon.device_detail("wan-link-01")["threat"]["pressure"] == 0


def test_real_verdicts_feed_attack_pressure_and_decay():
    mon = fresh()
    for _ in range(6):
        assert mon.ingest_verdict({"xgb_class": "DDOS", "verdict": "HACKER", "confidence": 0.95})
    assert mon.ingest_verdict({"xgb_class": "BENIGN", "verdict": "LEGITIMATE", "confidence": 0.9}) is False
    high = mon._threat_state("edge-fw-01")["pressure"]
    assert high > 70
    mon._sim_now += 20 * 60
    assert mon._threat_state("edge-fw-01")["pressure"] < high / 20


def test_fix_clears_attack_on_that_device():
    mon = fresh()
    mon.inject("traffic_surge", "edge-fw-01")
    for _ in range(25):
        mon.tick()
    mon.apply_fix("edge-fw-01", "engage_mitigation")
    for _ in range(40):
        mon.tick()
    view = mon.device_detail("edge-fw-01")
    assert view["threat"]["pressure"] < 10 and view["status"] in {"healthy", "watch"}


def test_syslog_parsing_rfc5424_and_rfc3164():
    from core.nethealth.events import event_weight, parse_syslog

    a = parse_syslog("<131>1 2026-11-12T10:00:00Z core-rtr-01 kernel - - - fan tray 2 speed below minimum RPM")
    assert a["device_id"] == "core-rtr-01" and a["severity"] == "error" and "fan tray" in a["message"]
    b = parse_syslog("<28>Nov 12 10:01:02 br-sw-01 env[123]: temperature high on slot 1")
    assert b["device_id"] == "br-sw-01" and b["severity"] == "warning" and b["facility"] == "env"
    assert parse_syslog("not syslog") is None and parse_syslog("<999>x") is None
    w_fan, pat = event_weight("error", a["message"])
    assert pat.category == "fan_failure" and pat.metric == "temp_c" and w_fan > 0.4
    assert event_weight("info", "config saved")[0] == 0


def test_event_semantics_corroborate_a_trend():
    mon = fresh()
    mon.inject("thermal_runaway", "core-rtr-02")
    for _ in range(14):
        mon.tick()
    before = mon.device_detail("core-rtr-02")
    assert any(c["category"] == "fan_failure" for c in before["event_context"])  # the scenario logs a fan event
    assert "temp_c" in before["corroborated_by_events"] or before["risk_rules"] < 25


def test_down_event_marks_a_live_device_down_until_it_reports_again():
    mon = NetHealthMonitor(None)
    mon.register_device("sw-x", device_type="switch")
    mon.ingest("sw-x", time.time(), {"cpu_pct": 10})
    mon.ingest_event("sw-x", time.time(), "critical", "syslog", "sw-x is DOWN: power supply failure")
    assert mon.device_detail("sw-x")["status"] == "down"
    mon.ingest("sw-x", time.time() + 5, {"cpu_pct": 11})
    assert mon.device_detail("sw-x")["status"] != "down"


def test_persistence_ignores_a_short_spike_but_not_a_real_leak():
    mon = fresh()
    mon.simulator.inject("blip", "app-srv-01", 1.0)
    statuses = set()
    for _ in range(40):
        mon.tick()
        statuses.add(mon.device_detail("app-srv-01")["status"])
    assert not statuses & {"warning", "critical", "down"}, statuses


def test_learned_forecaster_loads_and_ranks_a_failing_metric_above_a_healthy_one():
    from core.nethealth.forecaster import Forecaster

    fc = Forecaster.load()
    assert fc is not None, "core/nethealth/forecaster_weights.json is missing; run scripts/train_nethealth.py"
    assert fc.meta["test"]["auroc_learned"] > 0.9
    mon = NetHealthMonitor(NetworkSimulator(seed=3), forecaster=fc)
    quiet = mon.device_detail("app-srv-02")
    assert quiet["failure_probability_30m"] < 0.25
    mon.inject("memory_leak", "app-srv-02")
    for _ in range(35):
        mon.tick()
    sick = mon.device_detail("app-srv-02")
    assert sick["failure_probability_30m"] > 0.6 and sick["failure_probability_30m"] > quiet["failure_probability_30m"]


def test_forecaster_rejects_a_mismatched_model_file(tmp_path):
    from core.nethealth.forecaster import Forecaster

    bad = tmp_path / "m.json"
    bad.write_text('{"features": ["x"], "weights": [1.0]}')
    assert Forecaster.load(bad) is None and Forecaster.load(tmp_path / "missing.json") is None


def test_host_collector_reads_proc_style_files(tmp_path):
    from core.nethealth.collectors import HostCollector

    proc, sysd = tmp_path / "proc", tmp_path / "sys"
    (proc / "net").mkdir(parents=True)
    (sysd / "class" / "thermal" / "thermal_zone0").mkdir(parents=True)
    (sysd / "class" / "thermal" / "thermal_zone0" / "temp").write_text("61500\n")
    (proc / "meminfo").write_text("MemTotal:       1000 kB\nMemAvailable:    250 kB\n")

    def write(idle, busy, rx, tx, pkts=100, errs=0):
        (proc / "stat").write_text(f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0\n")
        (proc / "net" / "dev").write_text(
            "Inter-|   Receive |  Transmit\n face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\n"
            f"  lo: 5 5 0 0 0 0 0 0 5 5 0 0 0 0 0 0\n eth0: {rx} {pkts} {errs} 0 0 0 0 0 {tx} {pkts} 0 0 0 0 0 0\n")

    col = HostCollector(str(proc), str(sysd))
    write(1000, 1000, 0, 0)
    first = col.sample(now=100.0)
    assert first["mem_pct"] == pytest.approx(75.0) and first["temp_c"] == pytest.approx(61.5)
    write(1100, 1300, 12_500_000, 0, pkts=300, errs=4)  # 300 busy vs 100 idle -> 75% CPU; 100 Mbit over 10 s = 10 Mbit/s on 1 Gbit; 4 errors in 400 packets
    second = col.sample(now=110.0)
    assert second["cpu_pct"] == pytest.approx(75.0)
    assert second["link_util_pct"] == pytest.approx(1.0, abs=0.05)
    assert second["error_rate_pct"] == pytest.approx(1.0)


def test_prometheus_collector_maps_series_to_devices():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from core.nethealth.collectors import PrometheusCollector

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"status": "success", "data": {"result": [
                {"metric": {"instance": "10.0.0.5:9100"}, "value": [0, "42.5"]},
                {"metric": {"instance": "sw1"}, "value": [0, "NaN"]}]}}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        out = PrometheusCollector(f"http://127.0.0.1:{srv.server_port}", {"cpu_pct": "up"}).collect()
    finally:
        srv.shutdown()
    assert out == {"10.0.0.5": {"cpu_pct": 42.5}}
    with pytest.raises(ValueError):
        PrometheusCollector("file:///etc/passwd")


def test_syslog_udp_listener_records_events():
    import asyncio

    from core.nethealth.collectors import start_syslog_listener

    async def go():
        mon = NetHealthMonitor(None)
        transport = await start_syslog_listener(mon, "127.0.0.1", 0)
        port = transport.get_extra_info("sockname")[1]
        loop = asyncio.get_running_loop()
        tx, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, remote_addr=("127.0.0.1", port))
        tx.sendto(b"<131>Nov 12 10:00:00 lab-rtr bgpd[9]: BGP neighbor 10.1.1.1 down")
        tx.sendto(b"garbage")
        await asyncio.sleep(0.2)
        tx.close()
        transport.close()
        return mon

    mon = asyncio.run(go())
    events = mon.recent_events()
    assert len(events) == 1 and events[0]["device_id"] == "lab-rtr" and events[0]["category"] == "routing"


def test_live_and_simulated_devices_coexist():
    mon = fresh()
    mon.register_device("host-1", "Host 1", "host", "Local")
    now = time.time()
    for i in range(30):
        mon.ingest("host-1", now - (30 - i) * 10, {"cpu_pct": 20 + i % 3, "mem_pct": 40})
    mon.tick()
    data = mon.overview()
    assert data["total"] == 13
    assert {d["source"] for d in data["devices"]} == {"simulated", "live"}
    mon.reset()
    assert "host-1" in mon.devices and mon.overview()["total"] == 13
    with pytest.raises(RuntimeError):
        mon.apply_fix("host-1")


def test_device_limit_is_enforced():
    mon = NetHealthMonitor(None)
    for i in range(500):
        mon.register_device(f"d{i}")
    with pytest.raises(ValueError):
        mon.register_device("one-too-many")

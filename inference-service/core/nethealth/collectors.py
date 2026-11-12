"""Real telemetry sources: this host's /proc, a Prometheus server, and a UDP syslog listener.

Each feeds a NetHealthMonitor through the same ``ingest`` / ``ingest_event`` calls the API uses, so a
live device is analysed exactly like a simulated one. SNMP devices are reached through
``snmp_exporter`` -> Prometheus (set NETHEALTH_PROMETHEUS_URL), which avoids a second SNMP stack here.
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .events import parse_syslog

log = logging.getLogger("neurosoc.nethealth")


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


class HostCollector:
    """Samples the machine this service runs on from /proc and /sys (Linux). Stateful: rates need two readings."""

    def __init__(self, proc: str = "/proc", sys_root: str = "/sys") -> None:
        self.proc, self.sys_root = proc, sys_root
        self._cpu: tuple[float, float] | None = None
        self._net: dict[str, tuple[float, int, int, int, int, int, int]] | None = None
        self.device_id = os.getenv("NETHEALTH_HOST_ID") or socket.gethostname()

    def available(self) -> bool:
        return _read(f"{self.proc}/stat") is not None and _read(f"{self.proc}/meminfo") is not None

    def sample(self, now: float | None = None) -> dict[str, float]:
        now = time.time() if now is None else now
        out: dict[str, float] = {}
        cpu = self._cpu_pct()
        if cpu is not None:
            out["cpu_pct"] = cpu
        mem = self._mem_pct()
        if mem is not None:
            out["mem_pct"] = mem
        temp = self._temp_c()
        if temp is not None:
            out["temp_c"] = temp
        out.update(self._net_metrics(now))
        return out

    def _cpu_pct(self) -> float | None:
        text = _read(f"{self.proc}/stat")
        if not text:
            return None
        parts = [float(x) for x in text.splitlines()[0].split()[1:]]
        idle, total = parts[3] + (parts[4] if len(parts) > 4 else 0.0), sum(parts[:8])
        prev, self._cpu = self._cpu, (idle, total)
        if prev is None or total <= prev[1]:
            return None
        return max(0.0, min(100.0, 100.0 * (1 - (idle - prev[0]) / (total - prev[1]))))

    def _mem_pct(self) -> float | None:
        text = _read(f"{self.proc}/meminfo")
        if not text:
            return None
        info = {m.group(1): float(m.group(2)) for m in re.finditer(r"^(\w+):\s+(\d+)", text, re.M)}
        total, avail = info.get("MemTotal"), info.get("MemAvailable")
        return None if not total or avail is None else 100.0 * (1 - avail / total)

    def _temp_c(self) -> float | None:
        temps = []
        for pattern in (f"{self.sys_root}/class/thermal/thermal_zone*/temp", f"{self.sys_root}/class/hwmon/hwmon*/temp*_input"):
            for path in glob.glob(pattern):
                raw = _read(path)
                try:
                    value = float(raw) / 1000.0 if raw else None
                except ValueError:
                    value = None
                if value is not None and 0 < value < 150:
                    temps.append(value)
        return max(temps) if temps else None

    def _net_metrics(self, now: float) -> dict[str, float]:
        text = _read(f"{self.proc}/net/dev")
        if not text:
            return {}
        current: dict[str, tuple[float, int, int, int, int, int, int]] = {}
        for line in text.splitlines()[2:]:
            name, _, rest = line.partition(":")
            name, f = name.strip(), rest.split()
            if len(f) < 16 or name == "lo" or name.startswith(("veth", "docker", "br-")):
                continue
            current[name] = (now, int(f[0]), int(f[1]), int(f[2]), int(f[8]), int(f[9]), int(f[10]))  # drops are normal on hosts; errors are not
        prev, self._net = self._net, current
        if not prev:
            return {}
        util, errs, pkts = 0.0, 0, 0
        for name, cur in current.items():
            old = prev.get(name)
            if old is None or cur[0] <= old[0]:
                continue
            dt = cur[0] - old[0]
            speed = _read(f"{self.sys_root}/class/net/{name}/speed")
            mbit = float(speed) if speed and speed.strip().lstrip("-").isdigit() and float(speed) > 0 else 1000.0
            rx_bps, tx_bps = (cur[1] - old[1]) * 8 / dt, (cur[4] - old[4]) * 8 / dt
            util = max(util, 100.0 * max(rx_bps, tx_bps) / (mbit * 1e6))
            errs += (cur[3] - old[3]) + (cur[6] - old[6])
            pkts += (cur[2] - old[2]) + (cur[5] - old[5])
        out: dict[str, float] = {"link_util_pct": min(100.0, util)}
        if current:
            out["error_rate_pct"] = min(100.0, 100.0 * errs / pkts) if pkts >= 100 else 0.0  # too few packets to be a rate
        return out


DEFAULT_PROM_QUERIES = {
    "cpu_pct": '100 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[2m])) * 100',
    "mem_pct": "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)",
    "temp_c": "max by (instance) (node_hwmon_temp_celsius)",
    # Assumes 1 Gbit/s links; override with NETHEALTH_PROM_QUERIES (JSON) for other speeds.
    "link_util_pct": 'max by (instance) (rate(node_network_receive_bytes_total{device!~"lo|veth.*|docker.*|br-.*"}[2m]) * 8 / 1e9 * 100)',
    "error_rate_pct": 'sum by (instance) (rate(node_network_receive_errs_total[2m])) / clamp_min(sum by (instance) (rate(node_network_receive_packets_total[2m])), 1) * 100',
}


class PrometheusCollector:
    def __init__(self, base_url: str, queries: dict[str, str] | None = None, timeout: float = 5.0) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("NETHEALTH_PROMETHEUS_URL must be an http(s) URL.")
        self.base_url = base_url.rstrip("/")
        self.queries = queries or DEFAULT_PROM_QUERIES
        self.timeout = timeout

    def _query(self, expr: str) -> list[dict[str, Any]]:
        url = f"{self.base_url}/api/v1/query?{urllib.parse.urlencode({'query': expr})}"
        with urllib.request.urlopen(url, timeout=self.timeout) as resp:  # noqa: S310 - scheme checked in __init__
            body = json.loads(resp.read(4_000_000))
        if body.get("status") != "success":
            raise RuntimeError(f"Prometheus query failed: {body.get('error', 'unknown')}")
        return body["data"]["result"]

    def collect(self) -> dict[str, dict[str, float]]:
        """{device_id: {metric: value}} from one round of instant queries. A failing query is skipped, not fatal."""
        out: dict[str, dict[str, float]] = {}
        for metric, expr in self.queries.items():
            try:
                for series in self._query(expr):
                    instance = series["metric"].get("instance") or series["metric"].get("job") or "unknown"
                    device = instance.rsplit(":", 1)[0] if re.search(r":\d+$", instance) else instance
                    value = float(series["value"][1])
                    if value == value and abs(value) != float("inf"):
                        out.setdefault(device, {})[metric] = value
            except Exception as exc:  # noqa: BLE001
                log.warning("Prometheus query for %s failed: %s", metric, type(exc).__name__)
        return out


class SyslogProtocol(asyncio.DatagramProtocol):
    """Receives UDP syslog and records each line as a device event, stamped with its arrival time."""

    MAX_PER_SECOND = 500

    def __init__(self, monitor) -> None:
        self.monitor = monitor
        self._window_start = 0.0
        self._count = 0
        self.dropped = 0

    def datagram_received(self, data: bytes, addr) -> None:
        now = time.time()
        if now - self._window_start >= 1.0:
            self._window_start, self._count = now, 0
        self._count += 1
        if self._count > self.MAX_PER_SECOND or len(data) > 4096:
            self.dropped += 1
            return
        event = parse_syslog(data.decode("utf-8", "replace"), now)
        if event is None:
            return
        # Header clocks are unreliable (no timezone in RFC 3164), so arrival time is what counts.
        self.monitor.ingest_event(event["device_id"], now, event["severity"], event["facility"], event["message"])


async def start_syslog_listener(monitor, host: str, port: int):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: SyslogProtocol(monitor), local_addr=(host, port))
    log.info("Network health syslog listener on udp://%s:%d", host, port)
    return transport

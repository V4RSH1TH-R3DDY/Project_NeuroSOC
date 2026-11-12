"""Device events: syslog parsing and what each message means for failure risk.

A message is matched against known failure patterns. Each pattern has a weight (how strongly it
precedes an outage) and, where it applies, the metric it corroborates, so "fan failure" supports a
rising temperature trend. Severity scales the weight; weights decay with a 10 minute half-life.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

SEVERITIES = ("emergency", "alert", "critical", "error", "warning", "notice", "info", "debug")  # syslog 0..7
_SEVERITY_SCALE = {"emergency": 1.0, "alert": 1.0, "critical": 1.0, "error": 0.8, "warning": 0.5, "notice": 0.2, "info": 0.1, "debug": 0.0}
HALF_LIFE_S = 600.0


@dataclass(frozen=True)
class EventPattern:
    category: str
    label: str
    regex: re.Pattern[str]
    weight: float          # 0-1: how strongly this precedes an outage
    metric: str | None     # the metric this event corroborates


def _p(category: str, label: str, pattern: str, weight: float, metric: str | None = None) -> EventPattern:
    return EventPattern(category, label, re.compile(pattern, re.I), weight, metric)


PATTERNS: tuple[EventPattern, ...] = (
    _p("device_down", "Device down", r"\bis DOWN\b|\bhost down\b|\bunreachable\b|\bheartbeat lost\b", 1.0),
    _p("link_down", "Link down", r"link (is )?down|changed state to down|link down|carrier lost|loss of signal", 0.6, "packet_loss_pct"),
    _p("link_flap", "Interface flapping", r"flap|down/up repeatedly|state to up.*state to down", 0.6, "error_rate_pct"),
    _p("crc_errors", "Interface errors", r"\bCRC\b|input errors|rx power low|fcs error|frame error|optic", 0.5, "error_rate_pct"),
    _p("routing", "Routing instability", r"bgp.*(down|reset|flap|notification)|ospf.*(down|dead)|neighbor .*down|adjacency.*(lost|down)", 0.6, "packet_loss_pct"),
    _p("fan_failure", "Cooling fault", r"fan.*(fail|fault|below|rpm|stopped)|cooling|airflow", 0.7, "temp_c"),
    _p("overtemp", "Overtemperature", r"over.?temp|temperature .*(high|critical|threshold)|thermal (shutdown|warning)", 0.7, "temp_c"),
    _p("memory", "Memory exhaustion", r"oom|out of memory|low (free )?memory|memory .*(leak|exhaust)|malloc fail", 0.7, "mem_pct"),
    _p("cpu", "CPU saturation", r"cpu (hog|high|usage)|high cpu|sustained high cpu|runaway|watchdog", 0.5, "cpu_pct"),
    _p("power", "Power fault", r"power (supply|fail|loss)|psu|brown.?out|ups on battery", 0.7),
    _p("storage", "Storage fault", r"disk (full|error|fail)|i/o error|filesystem|raid degraded|smart", 0.5),
    _p("traffic", "Traffic anomaly", r"traffic .*(above|spike|surge)|syn (flood|rate)|ddos|storm|broadcast storm", 0.5, "link_util_pct"),
    _p("threshold", "Threshold crossed", r"exceeded critical threshold", 0.4),
)


def classify(message: str) -> EventPattern | None:
    for pattern in PATTERNS:
        if pattern.regex.search(message):
            return pattern
    return None


def event_weight(severity: str, message: str) -> tuple[float, EventPattern | None]:
    """Weight of one event: the pattern's weight scaled by severity. Unmatched events weigh by severity alone."""
    scale = _SEVERITY_SCALE.get(severity.lower(), 0.1)
    pattern = classify(message)
    if pattern is None:
        return 0.15 * scale if scale >= 0.8 else 0.0, None
    return pattern.weight * scale, pattern


def decayed(weight: float, age_s: float) -> float:
    return weight * 0.5 ** (max(0.0, age_s) / HALF_LIFE_S)


# ── syslog ────────────────────────────────────────────────────────────────────
_PRI = re.compile(r"^<(\d{1,3})>")
_RFC5424 = re.compile(r"^<\d{1,3}>1 (?P<ts>\S+) (?P<host>\S+) (?P<app>\S+) (?P<pid>\S+) (?P<msgid>\S+) (?P<sd>-|(?:\[[^\]]*\])+) ?(?P<msg>.*)$", re.S)
_RFC3164 = re.compile(r"^<\d{1,3}>(?P<ts>[A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d) (?P<host>\S+) (?P<tag>[^:\[\s]+)(?:\[\d+\])?:? ?(?P<msg>.*)$", re.S)
_MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def parse_syslog(line: str, received_at: float | None = None) -> dict | None:
    """Parse an RFC 5424 or RFC 3164 line into {device_id, ts, severity, facility, message}. None if it is not syslog."""
    line = line.strip("\x00\r\n ")
    pri = _PRI.match(line)
    if not pri:
        return None
    value = int(pri.group(1))
    if value > 191:
        return None
    severity = SEVERITIES[value & 7]
    now = received_at if received_at is not None else time.time()

    m = _RFC5424.match(line)
    if m:
        ts = _iso_to_epoch(m["ts"]) or now
        host = m["host"] if m["host"] != "-" else None
        return _event(host, ts, severity, m["app"], m["msg"])
    m = _RFC3164.match(line)
    if m:
        return _event(m["host"], _bsd_to_epoch(m["ts"], now), severity, m["tag"], m["msg"])
    return None


def _event(host: str | None, ts: float, severity: str, facility: str, message: str) -> dict | None:
    message = message.strip()
    if not host or not message:
        return None
    return {"device_id": host[:128], "ts": ts, "severity": severity, "facility": (facility if facility != "-" else "syslog")[:32], "message": message[:500]}


def _iso_to_epoch(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _bsd_to_epoch(value: str, now: float) -> float:
    try:
        month, day, clock = value.split(None, 2)
        year = datetime.fromtimestamp(now, timezone.utc).year
        hh, mm, ss = (int(x) for x in clock.split(":"))
        ts = datetime(year, _MONTHS[month], int(day), hh, mm, ss, tzinfo=timezone.utc).timestamp()
        return ts if ts <= now + 86400 else now  # a timestamp from "next year" means the host clock is off
    except (ValueError, KeyError):
        return now

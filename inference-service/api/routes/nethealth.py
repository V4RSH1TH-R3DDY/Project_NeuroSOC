"""api/routes/nethealth.py

Network health monitoring and outage prediction.

  GET  /api/v1/nethealth/overview            fleet status, predicted failures, recommended actions
  GET  /api/v1/nethealth/devices/{id}        one device: metric series, signals, events
  GET  /api/v1/nethealth/events              recent device events
  POST /api/v1/nethealth/ingest              push real telemetry (metrics and/or events)
  POST /api/v1/nethealth/simulate            inject a failure scenario (simulator only)
  POST /api/v1/nethealth/devices/{id}/fix    apply a corrective action (simulator only)
  POST /api/v1/nethealth/reset               restart the simulated fleet
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.nethealth import NetHealthMonitor

log = logging.getLogger("neurosoc.nethealth")

OPERATOR_ROLES = {"operator", "admin", "platform-admin"}


class TelemetrySample(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    ts: float | None = None
    metrics: dict[str, float] = Field(default_factory=dict, max_length=32)
    name: str | None = Field(default=None, max_length=128)
    device_type: str = "router"
    site: str = Field(default="default", max_length=64)


class DeviceEvent(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    ts: float | None = None
    severity: str = Field(default="info", max_length=16)
    facility: str = Field(default="syslog", max_length=32)
    message: str = Field(min_length=1, max_length=500)


class IngestBody(BaseModel):
    samples: list[TelemetrySample] = Field(default_factory=list, max_length=500)
    events: list[DeviceEvent] = Field(default_factory=list, max_length=500)


class SimulateBody(BaseModel):
    scenario: str
    device_id: str
    intensity: float = Field(default=1.0, ge=0.3, le=3.0)


class FixBody(BaseModel):
    fix: str | None = Field(default=None, max_length=64)


def build_router(monitor: NetHealthMonitor, roles_of: Callable[[Request], set[str] | None]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/nethealth", tags=["nethealth"])

    def require_operator(request: Request) -> None:
        roles = roles_of(request)
        if roles is not None and not roles & OPERATOR_ROLES:
            raise HTTPException(status_code=403, detail="This action requires the operator or admin role.")

    @router.get("/overview")
    async def overview() -> dict[str, Any]:
        return monitor.overview()

    @router.get("/devices/{device_id}")
    async def device(device_id: str) -> dict[str, Any]:
        try:
            return monitor.device_detail(device_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown device.")

    @router.get("/events")
    async def events(limit: int = Query(100, ge=1, le=500), device_id: str | None = None) -> dict[str, Any]:
        return {"events": monitor.recent_events(limit, device_id)}

    @router.post("/ingest")
    async def ingest(body: IngestBody, request: Request) -> dict[str, Any]:
        require_operator(request)
        now = time.time()
        accepted = 0
        for sample in body.samples:
            if sample.device_id not in monitor.devices:
                try:
                    monitor.register_device(sample.device_id, sample.name, sample.device_type, sample.site)
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc))
            accepted += monitor.ingest(sample.device_id, sample.ts or now, sample.metrics)
        for ev in body.events:
            monitor.ingest_event(ev.device_id, ev.ts or now, ev.severity, ev.facility, ev.message)
        return {"accepted_metrics": accepted, "accepted_events": len(body.events)}

    @router.post("/simulate")
    async def simulate(body: SimulateBody, request: Request) -> dict[str, Any]:
        require_operator(request)
        try:
            return monitor.inject(body.scenario, body.device_id, body.intensity)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0]))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @router.post("/devices/{device_id}/fix")
    async def fix(device_id: str, request: Request, body: FixBody | None = None) -> dict[str, Any]:
        require_operator(request)
        try:
            return monitor.apply_fix(device_id, body.fix if body else None)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown device.")

    @router.post("/reset")
    async def reset(request: Request) -> dict[str, str]:
        require_operator(request)
        if monitor.simulator is None:
            raise HTTPException(status_code=409, detail="The simulator is not enabled.")
        monitor.reset()
        return {"status": "reset"}

    return router


async def run_simulator(monitor: NetHealthMonitor, interval_s: float) -> None:
    """Advance the simulated fleet one minute every ``interval_s`` seconds, until cancelled."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            # In a thread: a tick can call the traffic models, which must not block the event loop.
            await asyncio.to_thread(monitor.tick)
        except Exception:  # noqa: BLE001 - a bad tick must not stop the feed
            log.exception("Network health simulator tick failed.")


async def run_host_collector(monitor: NetHealthMonitor, collector, interval_s: float) -> None:
    """Report this machine's real CPU, memory, temperature and network counters as a live device."""
    monitor.register_device(collector.device_id, f"{collector.device_id} (this host, live)", "host", "Local")
    while True:
        try:
            sample = await asyncio.to_thread(collector.sample)
            if sample:
                monitor.ingest(collector.device_id, time.time(), sample)
        except Exception:  # noqa: BLE001
            log.exception("Host collector failed.")
        await asyncio.sleep(interval_s)


async def run_prometheus_collector(monitor: NetHealthMonitor, collector, interval_s: float) -> None:
    """Pull device metrics from a Prometheus server (node_exporter, snmp_exporter, ...)."""
    while True:
        try:
            now = time.time()
            for device_id, metrics in (await asyncio.to_thread(collector.collect)).items():
                if device_id not in monitor.devices:
                    monitor.register_device(device_id, device_id, "server", "Prometheus")
                monitor.ingest(device_id, now, metrics)
        except Exception:  # noqa: BLE001
            log.exception("Prometheus collector failed.")
        await asyncio.sleep(interval_s)

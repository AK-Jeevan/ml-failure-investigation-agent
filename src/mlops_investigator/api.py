"""FastAPI application for asynchronous investigation management."""

from __future__ import annotations

import os
import hmac
import json
import logging
import threading
import time
import uuid
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from .config import load_local_environment
from .engine import InvestigationEngine
from .llm import NvidiaProvider
from .models import Incident, Investigation, InvestigationStatus
from .remediation import ApprovalError, RemediationApprovalService
from .storage import InvestigationStore
from .tools import ReadOnlyToolRegistry


ScenarioName = Literal["feature_schema_change", "data_drift", "api_regression", "healthy"]

load_local_environment()

logger = logging.getLogger("mlops_investigator.api")


class _JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("event", "request_id", "method", "route", "status_code", "duration_ms", "investigation_id", "status", "error_type"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(_JsonLogFormatter())
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

_metrics_lock = threading.Lock()
_api_metrics = {"requests": 0, "errors": 0, "request_duration_seconds_sum": 0.0}


def require_api_access(authorization: str | None = Header(default=None)) -> None:
    configured_token = os.getenv("API_ACCESS_TOKEN", "")
    if not configured_token:
        raise HTTPException(status_code=503, detail="API_ACCESS_TOKEN is not configured")
    scheme, separator, supplied_token = (authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not hmac.compare_digest(supplied_token, configured_token):
        raise HTTPException(status_code=401, detail="A valid bearer token is required")


class InvestigationRequest(BaseModel):
    description: str = Field(min_length=1, max_length=4_000)
    service: str = Field(default="fraud-score-api", min_length=1, max_length=200)
    scenario: ScenarioName = "feature_schema_change"


class ApprovalRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=5, max_length=1_000)


class InvestigationManager:
    """Runs investigations in FastAPI background tasks and persists progress."""

    def __init__(self, store: InvestigationStore | None = None) -> None:
        self.store = store or InvestigationStore(os.getenv("INVESTIGATION_DB", "data/investigations.sqlite3"))
        self._active: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def start(self, incident: Incident, background_tasks: BackgroundTasks) -> None:
        cancel_event = threading.Event()
        with self._lock:
            self._active[incident.incident_id] = cancel_event
        try:
            self.store.save(Investigation(incident=incident))
            background_tasks.add_task(self._run, incident, cancel_event)
            logger.info(
                "Investigation accepted",
                extra={"event": "investigation_started", "investigation_id": incident.incident_id, "status": InvestigationStatus.RUNNING.value},
            )
        except Exception:
            with self._lock:
                self._active.pop(incident.incident_id, None)
            raise

    def cancel(self, investigation_id: str) -> bool:
        with self._lock:
            event = self._active.get(investigation_id)
        if event is None:
            return False
        event.set()
        return True

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def _run(self, incident: Incident, cancel_event: threading.Event) -> None:
        try:
            provider = None
            api_key = os.getenv("NVIDIA_API_KEY")
            model = os.getenv("NVIDIA_MODEL")
            provider_enabled = os.getenv("NVIDIA_PROVIDER_ENABLED", "true").strip().lower() not in {
                "false", "0", "no", "off"
            }
            if provider_enabled and api_key and model:
                provider = NvidiaProvider(
                    api_key=api_key,
                    model=model,
                    base_url=os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
                )
            engine = InvestigationEngine(
                tools=ReadOnlyToolRegistry(service_url=os.getenv("SIMULATION_API_URL")),
                store=self.store,
                provider=provider,
            )
            result = engine.investigate(incident, cancellation_requested=cancel_event.is_set)
            logger.info(
                "Investigation finished",
                extra={"event": "investigation_finished", "investigation_id": incident.incident_id, "status": result.status.value},
            )
        except Exception as exc:
            logger.error(
                "Investigation failed",
                extra={"event": "investigation_failed", "investigation_id": incident.incident_id, "status": InvestigationStatus.FAILED.value, "error_type": type(exc).__name__},
            )
            payload = self.store.load_payload(incident.incident_id)
            if payload is None:
                failed = Investigation(incident=incident, status=InvestigationStatus.FAILED)
                failed.conclusion = "Investigation failed before a final result was produced."
                failed.add_event("investigation_failed", "Investigation failed safely", error_type=type(exc).__name__)
                failed.report = {
                    "investigation_id": incident.incident_id,
                    "status": InvestigationStatus.FAILED.value,
                    "incident_summary": incident.description,
                    "probable_root_cause": None,
                    "uncertainty": ["Investigation ended because an internal component failed."],
                }
                self.store.save(failed)
            else:
                payload["status"] = InvestigationStatus.FAILED.value
                payload["conclusion"] = "Investigation failed before a final result was produced."
                payload.setdefault("trajectory", []).append({
                    "kind": "investigation_failed",
                    "message": "Investigation failed safely",
                    "details": {"error_type": type(exc).__name__},
                })
                self.store.save_payload(incident.incident_id, payload)
        finally:
            with self._lock:
                self._active.pop(incident.incident_id, None)


manager = InvestigationManager()
app = FastAPI(
    title="AI/ML Production Failure Investigation Agent",
    version="0.1.0",
    description="Evidence-driven ML incident investigations with bounded tools and human-reviewed remediation.",
)


@app.middleware("http")
async def observe_request(request, call_next):
    supplied_id = request.headers.get("x-request-id", "")
    try:
        request_id = str(uuid.UUID(supplied_id))
    except (ValueError, AttributeError):
        request_id = str(uuid.uuid4())
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        duration = time.perf_counter() - started
        with _metrics_lock:
            _api_metrics["requests"] += 1
            _api_metrics["errors"] += int(status_code >= 500)
            _api_metrics["request_duration_seconds_sum"] += duration
        route = request.scope.get("route")
        logger.info(
            "HTTP request completed",
            extra={
                "event": "http_request",
                "request_id": request_id,
                "method": request.method,
                "route": getattr(route, "path", "unmatched"),
                "status_code": status_code,
                "duration_ms": round(duration * 1_000, 3),
            },
        )


@app.get("/health")
def health() -> dict[str, str]:
    try:
        manager.store.ping()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Investigation storage is unavailable") from exc
    return {"status": "healthy", "database": "reachable"}


@app.get("/metrics", dependencies=[Depends(require_api_access)])
def metrics() -> dict[str, object]:
    with _metrics_lock:
        request_metrics = dict(_api_metrics)
    return {
        "active_investigations": manager.active_count,
        "investigations_by_status": manager.store.count_by_status(),
        "api_requests_total": request_metrics["requests"],
        "api_errors_total": request_metrics["errors"],
        "request_duration_seconds_sum": round(request_metrics["request_duration_seconds_sum"], 6),
    }


@app.post("/investigate", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_api_access)])
def investigate(request: InvestigationRequest, background_tasks: BackgroundTasks) -> dict[str, str]:
    incident = Incident(
        description=request.description,
        service=request.service,
        scenario=request.scenario,
    )
    manager.start(incident, background_tasks)
    return {"investigation_id": incident.incident_id, "status": InvestigationStatus.RUNNING.value}


@app.get("/investigations/{investigation_id}", dependencies=[Depends(require_api_access)])
def get_investigation(investigation_id: str) -> dict:
    payload = manager.store.load_payload(investigation_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    return payload


@app.get("/investigations/{investigation_id}/status", dependencies=[Depends(require_api_access)])
def get_investigation_status(investigation_id: str) -> dict:
    result = manager.store.get_status(investigation_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    with manager._lock:
        cancellation_requested = bool(
            investigation_id in manager._active and manager._active[investigation_id].is_set()
        )
    return {**result, "cancellation_requested": cancellation_requested}


@app.get("/investigations/{investigation_id}/report", dependencies=[Depends(require_api_access)])
def get_investigation_report(investigation_id: str) -> dict:
    payload = manager.store.load_payload(investigation_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    if not isinstance(payload.get("report"), dict):
        raise HTTPException(status_code=409, detail="Investigation report is not ready")
    return payload["report"]


@app.post("/investigations/{investigation_id}/approve", dependencies=[Depends(require_api_access)])
def approve_remediation(investigation_id: str, request: ApprovalRequest) -> dict:
    payload = manager.store.load_payload(investigation_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    try:
        updated = RemediationApprovalService().decide(
            payload,
            "approved",
            request.reviewer,
            request.reason,
        )
        manager.store.save_payload(investigation_id, updated)
    except ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"investigation_id": investigation_id, "remediation": updated.get("remediation")}


@app.post("/investigations/{investigation_id}/reject", dependencies=[Depends(require_api_access)])
def reject_remediation(investigation_id: str, request: ApprovalRequest) -> dict:
    payload = manager.store.load_payload(investigation_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    try:
        updated = RemediationApprovalService().decide(
            payload,
            "rejected",
            request.reviewer,
            request.reason,
        )
        manager.store.save_payload(investigation_id, updated)
    except ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"investigation_id": investigation_id, "remediation": updated.get("remediation")}


@app.post(
    "/investigations/{investigation_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_access)],
)
def cancel_investigation(investigation_id: str) -> dict[str, str]:
    current = manager.store.get_status(investigation_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    if current["status"] != InvestigationStatus.RUNNING.value:
        raise HTTPException(status_code=409, detail=f"Investigation is already {current['status']}")
    if not manager.cancel(investigation_id):
        raise HTTPException(status_code=409, detail="Investigation is not active in this API process")
    return {"investigation_id": investigation_id, "status": "cancellation_requested"}

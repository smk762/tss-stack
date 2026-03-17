import time
from typing import Optional

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


REQ_COUNT = Counter(
    "http_requests_total",
    "Total number of HTTP requests handled by the gateway.",
    ["method", "path", "status_code"],
)

REQ_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["method", "path", "status_code"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

JOB_ENQUEUED = Counter(
    "job_queue_enqueued_total",
    "Total number of jobs enqueued by kind.",
    ["job_kind"],
)

JOB_TRANSITIONS = Counter(
    "job_status_transitions_total",
    "Total number of terminal job status transitions by kind.",
    ["job_kind", "status"],
)

JOB_DURATION = Histogram(
    "job_duration_seconds",
    "End-to-end job duration in seconds until a terminal status is reached.",
    ["job_kind", "status"],
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0),
)

metrics_router = APIRouter()
_SKIP_PATHS = {"/health", "/metrics"}


def job_kind_for_type(job_type: str) -> Optional[str]:
    if job_type == "tts.synthesize":
        return "tts"
    if job_type in {"stt.transcribe", "whisper.transcribe"}:
        return "stt"
    return None


def observe_request(method: str, path: str, status_code: int, duration_seconds: float) -> None:
    labels = {
        "method": method,
        "path": path,
        "status_code": str(status_code),
    }
    REQ_COUNT.labels(**labels).inc()
    REQ_LATENCY.labels(**labels).observe(max(0.0, duration_seconds))


def observe_job_enqueued(job_kind: str) -> None:
    JOB_ENQUEUED.labels(job_kind=job_kind).inc()


def observe_job_status(job_kind: str, status: str) -> None:
    JOB_TRANSITIONS.labels(job_kind=job_kind, status=status).inc()


def observe_job_duration(job_kind: str, status: str, duration_seconds: float) -> None:
    JOB_DURATION.labels(job_kind=job_kind, status=status).observe(max(0.0, duration_seconds))


def _request_path(request: Request) -> str:
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    return route_path or request.url.path


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            if request.url.path not in _SKIP_PATHS:
                observe_request(
                    method=request.method,
                    path=_request_path(request),
                    status_code=status_code,
                    duration_seconds=time.perf_counter() - started,
                )


@metrics_router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

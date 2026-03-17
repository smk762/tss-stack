import asyncio
import os
import uuid
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse

from app.routers import provider
from app.routers.v1 import capabilities, jobs, stt, tts, whisper
from app.db.job_store import JobStore
from app.metrics import (
    MetricsMiddleware,
    job_kind_for_type,
    metrics_router,
    observe_job_duration,
    observe_job_status,
)
from app.storage.minio_store import MinioStore


app = FastAPI(title="TSS Gateway", version="v1")

app.add_middleware(MetricsMiddleware)
app.include_router(provider.router)
app.include_router(capabilities.router, prefix="/v1")
app.include_router(stt.router, prefix="/v1")
app.include_router(tts.router, prefix="/v1")
app.include_router(whisper.router, prefix="/v1")
app.include_router(jobs.router, prefix="/v1")
app.include_router(metrics_router)


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _job_duration_seconds(created_at: str | None, finished_at: str | None) -> float | None:
    created = _parse_iso8601(created_at)
    finished = _parse_iso8601(finished_at)
    if created is None or finished is None:
        return None
    duration = (finished - created).total_seconds()
    return duration if duration >= 0 else None


async def _terminal_job_metrics_poller(started_at: datetime) -> None:
    observed_terminal_states: set[tuple[str, str]] = set()
    while True:
        for row in JobStore().list_terminal_jobs():
            if not row.finished_at:
                continue
            finished_at = _parse_iso8601(row.finished_at)
            if finished_at is None or finished_at < started_at:
                continue

            job_kind = job_kind_for_type(row.type)
            if not job_kind:
                continue

            state_key = (row.id, row.status)
            if state_key in observed_terminal_states:
                continue

            observe_job_status(job_kind, row.status)
            duration_seconds = _job_duration_seconds(row.created_at, row.finished_at)
            if duration_seconds is not None:
                observe_job_duration(job_kind, row.status, duration_seconds)
            observed_terminal_states.add(state_key)

        await asyncio.sleep(2.0)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    req_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-Id"] = req_id
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Avoid leaking internals; keep it predictable for clients.
    req_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": "Internal error", "details": {}}},
        headers={"X-Request-Id": req_id},
    )


@app.get("/health")
async def health():
    return {"ok": True, "service": "gateway", "env": os.getenv("ENV", "dev")}

@app.get("/ui")
async def ui():
    # Minimal dev UI served by the gateway (no separate frontend build).
    here = os.path.dirname(__file__)
    return FileResponse(os.path.join(here, "ui", "index.html"))


@app.on_event("startup")
async def startup():
    JobStore().init()
    MinioStore().ensure_bucket()
    app.state.terminal_job_metrics_task = asyncio.create_task(_terminal_job_metrics_poller(datetime.now(timezone.utc)))


@app.on_event("shutdown")
async def shutdown():
    task = getattr(app.state, "terminal_job_metrics_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


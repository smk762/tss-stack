import os
import uuid
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse

from app.routers.v1 import capabilities, jobs, stt, tts, whisper
from app.db.job_store import JobStore
from app.storage.minio_store import MinioStore


app = FastAPI(title="TSS Gateway", version="v1")

app.include_router(capabilities.router, prefix="/v1")
app.include_router(stt.router, prefix="/v1")
app.include_router(tts.router, prefix="/v1")
app.include_router(whisper.router, prefix="/v1")
app.include_router(jobs.router, prefix="/v1")


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


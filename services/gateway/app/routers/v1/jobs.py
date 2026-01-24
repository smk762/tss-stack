import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header

from app.core import config
from app.core.errors import http_error
from app.db.job_store import JobStore
from app.storage.minio_store import MinioStore


router = APIRouter(tags=["jobs"])


def _parse_details(s: Optional[str]) -> Dict[str, Any]:
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}


def _expires_at_iso(ttl_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, x_user_id: Optional[str] = Header(default=None, convert_underscores=False)):
    store = JobStore()
    row = store.get_job(job_id)
    if not row:
        raise http_error(404, "not_found", "Job not found", {"job_id": job_id})
    if row.owner_id and x_user_id and row.owner_id != x_user_id:
        # Dev guardrail; real auth will replace this.
        raise http_error(404, "not_found", "Job not found", {"job_id": job_id})

    result = None
    if row.status == "succeeded" and row.result_bucket and row.result_object and row.result_content_type:
        m = MinioStore()
        url = m.presign_get(row.result_bucket, row.result_object, config.RESULT_URL_TTL_SECONDS)
        result = {
            "content_type": row.result_content_type,
            "result_url": url,
            "expires_at": _expires_at_iso(config.RESULT_URL_TTL_SECONDS),
            "sha256": row.result_sha256,
            "bytes": row.result_bytes,
        }

    err = None
    if row.status == "failed":
        err = {"code": row.error_code or "error", "message": row.error_message or "Job failed", "details": _parse_details(row.error_details_json)}
    if row.status == "cancelled":
        err = {"code": "cancelled", "message": "Job cancelled", "details": {}}

    return {
        "id": row.id,
        "type": row.type,
        "status": row.status,
        "created_at": row.created_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "progress": row.progress,
        "error": err,
        "result": result,
    }


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str, x_user_id: Optional[str] = Header(default=None, convert_underscores=False)):
    store = JobStore()
    row = store.get_job(job_id)
    if not row:
        raise http_error(404, "not_found", "Job not found", {"job_id": job_id})
    if row.owner_id and x_user_id and row.owner_id != x_user_id:
        raise http_error(404, "not_found", "Job not found", {"job_id": job_id})
    updated = store.cancel_job(job_id)
    assert updated is not None
    return await get_job(job_id, x_user_id=x_user_id)


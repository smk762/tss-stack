import base64
import uuid
from typing import Optional

from fastapi import APIRouter, Header, UploadFile, File, Form

from app.core import config
from app.core.errors import http_error
from app.db.job_store import JobStore
from app.queue.redis_queue import RedisQueue
from app.storage.minio_store import MinioStore


router = APIRouter(tags=["stt"])


def _validate_mime(mime: Optional[str]) -> None:
    if not mime:
        return
    if mime not in config.STT_SUPPORTED_MIME_TYPES:
        raise http_error(400, "invalid_request", "Unsupported audio_mime_type", {"audio_mime_type": mime, "supported": config.STT_SUPPORTED_MIME_TYPES})


@router.post("/stt/transcribe", status_code=202)
async def transcribe(
    # multipart inputs
    audio: Optional[UploadFile] = File(default=None),
    audio_mime_type: Optional[str] = Form(default=None),
    # shared params
    language: Optional[str] = Form(default=None),
    prompt: Optional[str] = Form(default=None),
    temperature: Optional[float] = Form(default=None),
    diarize: Optional[bool] = Form(default=None),
    timestamps: Optional[bool] = Form(default=None),
    output_format: Optional[str] = Form(default=None),
    # json base64 fallback (FastAPI can't do union bodies cleanly in one function; accept via header-driven clients)
    x_audio_b64: Optional[str] = Header(default=None, convert_underscores=False),
    x_audio_mime_type: Optional[str] = Header(default=None, convert_underscores=False),
    idempotency_key: Optional[str] = Header(default=None, convert_underscores=False, alias="Idempotency-Key"),
    x_user_id: Optional[str] = Header(default=None, convert_underscores=False),
):
    """
    Accept multipart/form-data with `audio` file.

    JSON base64 mode is supported via headers for now (`x-audio-b64`, `x-audio-mime-type`) to keep the handler simple.
    """
    store = JobStore()
    store.init()

    # best-effort idempotency
    existing = store.get_or_create_idempotency(idempotency_key or "", x_user_id, "stt.transcribe")
    if existing:
        return {"job_id": existing, "status_url": f"/v1/jobs/{existing}"}

    if output_format and output_format not in config.STT_OUTPUT_FORMATS:
        raise http_error(400, "invalid_request", "Invalid output_format", {"output_format": output_format, "supported": config.STT_OUTPUT_FORMATS})

    mstore = MinioStore()
    mstore.ensure_bucket()

    job_id = str(uuid.uuid4())

    # resolve audio bytes
    data: bytes
    mime: Optional[str] = None

    if audio is not None:
        mime = audio_mime_type or audio.content_type
        _validate_mime(mime)
        data = await audio.read()
    elif x_audio_b64 and x_audio_mime_type:
        mime = x_audio_mime_type
        _validate_mime(mime)
        try:
            data = base64.b64decode(x_audio_b64, validate=True)
        except Exception:
            raise http_error(400, "invalid_request", "Invalid base64 audio", {})
    else:
        raise http_error(400, "invalid_request", "Missing audio. Provide multipart `audio` or base64 headers.", {})

    if len(data) > config.STT_MAX_BYTES:
        raise http_error(413, "payload_too_large", "Audio too large", {"max_bytes": config.STT_MAX_BYTES, "bytes": len(data)})

    # Store job parameters including original filename
    job_params = {
        "output_format": output_format or "json",
        "language": language,
        "prompt": prompt,
        "temperature": temperature,
        "diarize": diarize,
        "timestamps": timestamps,
    }
    if audio and audio.filename:
        job_params["original_filename"] = audio.filename

    store.create_job(job_id, "stt.transcribe", owner_id=x_user_id, params=job_params)
    store.set_idempotency(idempotency_key or "", x_user_id, "stt.transcribe", job_id)

    ext = mstore.guess_ext(mime)
    input_object = f"uploads/{job_id}/input.{ext}"
    mstore.put_bytes(input_object, data, content_type=mime or "application/octet-stream")

    q = RedisQueue()
    await q.enqueue(
        config.QUEUE_STT,
        {
            "job_id": job_id,
            "type": "stt.transcribe",
            "owner_id": x_user_id,
            "input": {
                "bucket": config.MINIO_BUCKET,
                "object": input_object,
                "content_type": mime or "application/octet-stream",
            },
            "params": {
                "language": language,
                "prompt": prompt,
                "temperature": temperature,
                "diarize": diarize,
                "timestamps": timestamps,
                "output_format": output_format or "json",
            },
        },
    )

    return {"job_id": job_id, "status_url": f"/v1/jobs/{job_id}"}


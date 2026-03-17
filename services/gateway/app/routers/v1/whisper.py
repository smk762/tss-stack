import base64
import uuid
from typing import Optional

from fastapi import APIRouter, Header, UploadFile, File, Form

from app.core import config
from app.core.errors import http_error
from app.db.job_store import JobStore
from app.metrics import observe_job_enqueued
from app.queue.redis_queue import RedisQueue
from app.storage.minio_store import MinioStore


router = APIRouter(tags=["whisper"])


def _validate_mime(mime: Optional[str]) -> None:
    if not mime:
        return
    if mime not in config.STT_SUPPORTED_MIME_TYPES:
        raise http_error(400, "invalid_request", "Unsupported audio_mime_type", {"audio_mime_type": mime, "supported": config.STT_SUPPORTED_MIME_TYPES})


@router.post("/whisper/transcribe", status_code=202)
async def transcribe(
    # multipart inputs
    audio: Optional[UploadFile] = File(default=None),
    audio_mime_type: Optional[str] = Form(default=None),
    # whisper-specific params
    language: Optional[str] = Form(default=None),
    prompt: Optional[str] = Form(default=None),
    temperature: Optional[float] = Form(default=None),
    output_format: Optional[str] = Form(default=None),
    familiar_id: Optional[str] = Form(default=None),
    familiar_adapter_id: Optional[str] = Form(default=None),
    # json base64 fallback
    x_audio_b64: Optional[str] = Header(default=None, convert_underscores=False),
    x_audio_mime_type: Optional[str] = Header(default=None, convert_underscores=False),
    idempotency_key: Optional[str] = Header(default=None, convert_underscores=False, alias="Idempotency-Key"),
    x_user_id: Optional[str] = Header(default=None, convert_underscores=False),
):
    """
    Transcribe audio using OpenAI Whisper.
    
    Accept multipart/form-data with `audio` file.
    JSON base64 mode is supported via headers for now (`x-audio-b64`, `x-audio-mime-type`) to keep the handler simple.
    
    Whisper-specific parameters:
    - language: Language code (e.g., 'en', 'es', 'fr') or 'auto' for auto-detection
    - prompt: Initial prompt to guide the transcription
    - temperature: Sampling temperature (0.0 to 1.0)
    - output_format: Output format (json, text, srt, vtt)
    """
    store = JobStore()
    store.init()

    # best-effort idempotency
    existing = store.get_or_create_idempotency(idempotency_key or "", x_user_id, "whisper.transcribe")
    if existing:
        return {"job_id": existing, "status_url": f"/v1/jobs/{existing}"}

    if output_format and output_format not in config.STT_OUTPUT_FORMATS:
        raise http_error(400, "invalid_request", "Invalid output_format", {"output_format": output_format, "supported": config.STT_OUTPUT_FORMATS})
    if familiar_adapter_id and not familiar_id:
        raise http_error(
            400,
            "invalid_request",
            "familiar_id is required when familiar_adapter_id is set.",
            {},
        )

    mstore = MinioStore()
    mstore.ensure_bucket()

    job_id = str(uuid.uuid4())
    
    # Store job parameters including original filename
    job_params = {
        "output_format": output_format or "json",
        "language": language,
        "prompt": prompt,
        "temperature": temperature,
        "familiar_id": familiar_id,
        "familiar_adapter_id": familiar_adapter_id,
    }
    
    # Add original filename if available
    if audio and audio.filename:
        job_params["original_filename"] = audio.filename
    
    store.create_job(job_id, "whisper.transcribe", owner_id=x_user_id, params=job_params)

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

    ext = mstore.guess_ext(mime)
    input_object = f"uploads/{job_id}/input.{ext}"
    mstore.put_bytes(input_object, data, content_type=mime or "application/octet-stream")

    q = RedisQueue()
    await q.enqueue(
        "queue:whisper.transcribe",  # Use dedicated Whisper queue
        {
            "job_id": job_id,
            "type": "whisper.transcribe",
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
                "output_format": output_format or "json",
                "familiar_id": familiar_id,
                "familiar_adapter_id": familiar_adapter_id,
            },
        },
    )
    observe_job_enqueued(job_kind="stt")

    return {"job_id": job_id, "status_url": f"/v1/jobs/{job_id}"}
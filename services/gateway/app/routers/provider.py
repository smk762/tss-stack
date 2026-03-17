from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Dict, Literal, Optional
from urllib.parse import unquote, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from app.core import config
from app.db.job_store import JobRow, JobStore
from app.media import guess_mime_type, probe_duration_seconds, safe_voice_path, sniff_audio_mime, suffix_for_mime
from app.metrics import observe_job_enqueued
from app.queue.redis_queue import RedisQueue
from app.storage.minio_store import MinioStore


router = APIRouter(tags=["provider"])
_webhook_tasks: set[asyncio.Task[Any]] = set()
logger = logging.getLogger(__name__)


class VoicePreset(BaseModel):
    id: str
    name: str
    language: str
    gender: str
    sample_url: str


class VoicePresetListResponse(BaseModel):
    data: list[VoicePreset]


class VoiceJobAcceptedResponse(BaseModel):
    id: str
    status: Literal["queued", "processing"]
    estimated_wait_seconds: Optional[int] = None
    queue_position: Optional[int] = None
    cost_gems: Optional[int] = None
    event_stream_url: Optional[str] = None


class AudioPayload(BaseModel):
    url: Optional[str] = None
    base64: Optional[str] = None
    content_type: Optional[str] = None


class TTSJobCreateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    voice_id: Optional[str] = None
    language: Optional[str] = None
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    format: Literal["mp3", "wav", "ogg"] = "mp3"
    webhook_url: Optional[str] = None
    client_request_id: Optional[str] = None


class TTSJobResult(BaseModel):
    audio: AudioPayload
    duration_seconds: float
    format: Literal["mp3", "wav", "ogg"]
    voice_id: Optional[str] = None
    language: Optional[str] = None
    cost_gems: Optional[int] = None


class TTSJobStatusResponse(BaseModel):
    id: str
    status: Literal["queued", "processing", "completed", "failed", "dead_letter", "cancelled"]
    progress_pct: int
    estimated_wait_seconds: Optional[int] = None
    queue_position: Optional[int] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result: Optional[TTSJobResult] = None


class STTJobCreateRequest(BaseModel):
    audio_url: Optional[str] = None
    audio_base64: Optional[str] = None
    language: Optional[str] = None
    webhook_url: Optional[str] = None
    client_request_id: Optional[str] = None

    @model_validator(mode="after")
    def validate_audio_source(self) -> "STTJobCreateRequest":
        if not self.audio_url and not self.audio_base64:
            raise ValueError("Provide audio_url or audio_base64.")
        return self


class STTJobResult(BaseModel):
    text: str
    language_detected: str
    confidence: float
    duration_seconds: float
    cost_gems: Optional[int] = None


class STTJobStatusResponse(BaseModel):
    id: str
    status: Literal["queued", "processing", "completed", "failed", "dead_letter", "cancelled"]
    progress_pct: int
    estimated_wait_seconds: Optional[int] = None
    queue_position: Optional[int] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result: Optional[STTJobResult] = None


class VoiceJobEvent(BaseModel):
    id: str
    status: Literal["queued", "processing", "completed", "failed", "dead_letter", "cancelled"]
    progress_pct: int
    message: Optional[str] = None
    error_message: Optional[str] = None


def provider_error(status_code: int, code: str, message: str, details: Optional[Dict[str, Any]] = None) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error": {"code": code, "message": message, "details": details or {}}})


def _voice_gender(voice_id: str) -> str:
    normalized = voice_id.strip().lower()
    if any(token in normalized for token in ("female", "woman", "girl", "inara")):
        return "female"
    if any(token in normalized for token in ("male", "man", "boy")):
        return "male"
    return "unknown"


def _voice_name(voice_id: str) -> str:
    return voice_id.replace("_", " ").replace("-", " ").strip().title() or voice_id


def _provider_status(status: str) -> Literal["queued", "processing", "completed", "failed", "dead_letter", "cancelled"]:
    mapped = {
        "queued": "queued",
        "running": "processing",
        "succeeded": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(status)
    return mapped or "dead_letter"


def _provider_progress_pct(row: JobRow) -> int:
    if row.status == "queued":
        return 0
    if row.status == "running":
        if row.progress is not None:
            try:
                pct = max(0, min(100, int(round(float(row.progress) * 100))))
                if pct not in (0, 100):
                    return pct
            except Exception:
                pass
        return 50
    if row.status in {"succeeded", "failed", "cancelled"}:
        return 100
    if row.progress is not None:
        try:
            return max(0, min(100, int(round(float(row.progress) * 100))))
        except Exception:
            pass
    return 0


def _parse_params(row: JobRow) -> Dict[str, Any]:
    if not row.params_json:
        return {}
    try:
        data = json.loads(row.params_json)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _job_timestamps(row: JobRow) -> Dict[str, Optional[str]]:
    return {
        "created_at": row.created_at,
        "started_at": row.started_at,
        "completed_at": row.finished_at,
    }


def _job_event_stream_url(request: Request, route_name: str, job_id: str) -> str:
    return str(request.url_for(route_name, job_id=job_id))


def _job_event_name(status: Literal["queued", "processing", "completed", "failed", "dead_letter", "cancelled"]) -> str:
    if status == "completed":
        return "job.done"
    if status in {"failed", "dead_letter", "cancelled"}:
        return "job.error"
    return "job.status"


def _job_event_message(status: Literal["queued", "processing", "completed", "failed", "dead_letter", "cancelled"]) -> Optional[str]:
    if status == "queued":
        return "Job queued."
    if status == "processing":
        return "Job processing."
    if status == "completed":
        return "Job completed."
    if status == "cancelled":
        return "Job cancelled."
    if status == "dead_letter":
        return "Job moved to dead letter state."
    return None


def _build_job_event(row: JobRow) -> VoiceJobEvent:
    status = _provider_status(row.status)
    error_message = row.error_message or ("Job cancelled." if status == "cancelled" else None)
    return VoiceJobEvent(
        id=row.id,
        status=status,
        progress_pct=_provider_progress_pct(row),
        message=_job_event_message(status),
        error_message=error_message,
    )


def _encode_sse(event: str, data: Dict[str, Any]) -> bytes:
    payload = json.dumps(data, separators=(",", ":"), sort_keys=True)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
    try:
        data = json.loads(row.params_json)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _audio_format_from_row(row: JobRow) -> Literal["mp3", "wav", "ogg"]:
    content_type = (row.result_content_type or "").lower()
    object_name = (row.result_object or "").lower()
    if "ogg" in content_type or object_name.endswith(".ogg"):
        return "ogg"
    if "mpeg" in content_type or "mp3" in content_type or object_name.endswith(".mp3"):
        return "mp3"
    return "wav"


def _normalize_language_for_engine(language: Optional[str]) -> Optional[str]:
    if not language:
        return None
    value = language.strip()
    if not value:
        return None
    token = re.split(r"[-_]", value, maxsplit=1)[0].strip().lower()
    return token or None


def _estimate_stt_confidence(text: str, duration_seconds: float, requested_language: Optional[str], detected_language: str) -> float:
    cleaned = text.strip()
    if not cleaned:
        return 0.0

    words = max(1, len(re.findall(r"\b\w+\b", cleaned)))
    confidence = 0.85

    if duration_seconds > 0:
        words_per_second = words / max(duration_seconds, 0.1)
        if words_per_second < 0.5 or words_per_second > 4.5:
            confidence -= 0.20
        elif words_per_second < 0.8 or words_per_second > 3.5:
            confidence -= 0.10

    if len(cleaned) < 12:
        confidence -= 0.10

    if requested_language and detected_language and requested_language.lower() != detected_language.lower():
        confidence -= 0.15

    return round(max(0.25, min(0.98, confidence)), 2)


def _decode_base64_audio(raw: str) -> tuple[bytes, Optional[str]]:
    payload = raw.strip()
    mime: Optional[str] = None
    if payload.startswith("data:"):
        try:
            header, payload = payload.split(",", 1)
        except ValueError as exc:
            raise provider_error(400, "VALIDATION_ERROR", "Invalid data URL audio payload.") from exc
        meta = header[5:]
        if ";base64" not in meta:
            raise provider_error(400, "VALIDATION_ERROR", "audio_base64 data URL must be base64 encoded.")
        mime = meta.split(";", 1)[0].strip().lower() or None
    try:
        return base64.b64decode(payload, validate=True), mime
    except Exception as exc:
        raise provider_error(400, "VALIDATION_ERROR", "Invalid base64 audio payload.") from exc


async def _resolve_stt_input(body: STTJobCreateRequest) -> tuple[bytes, str, float]:
    if body.audio_url:
        parsed = urlparse(body.audio_url)
        if parsed.scheme in {"http", "https"} and parsed.hostname in {"localhost", "127.0.0.1"}:
            path_parts = [part for part in parsed.path.split("/") if part]
            if len(path_parts) >= 2:
                bucket = path_parts[0]
                object_name = unquote("/".join(path_parts[1:]))
                try:
                    store = MinioStore()
                    response = store.get_object(bucket, object_name)
                    try:
                        audio_bytes = response.read()
                    finally:
                        response.close()
                        response.release_conn()
                    mime_hint = guess_mime_type(object_name, fallback=None)
                except Exception as exc:
                    raise provider_error(400, "VALIDATION_ERROR", "Failed to fetch local audio_url.", {"audio_url": body.audio_url, "reason": str(exc)}) from exc
            else:
                raise provider_error(
                    400,
                    "VALIDATION_ERROR",
                    "Invalid local MinIO audio_url path.",
                    {"audio_url": body.audio_url},
                )
        else:
            try:
                async with httpx.AsyncClient(timeout=config.PROVIDER_FETCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
                    response = await client.get(body.audio_url)
                    response.raise_for_status()
            except Exception as exc:
                raise provider_error(400, "VALIDATION_ERROR", "Failed to fetch audio_url.", {"audio_url": body.audio_url, "reason": str(exc)}) from exc
            audio_bytes = response.content
            mime_hint = guess_mime_type(body.audio_url, fallback=response.headers.get("content-type"))
    else:
        audio_bytes, mime_hint = _decode_base64_audio(body.audio_base64 or "")

    if not audio_bytes:
        raise provider_error(400, "VALIDATION_ERROR", "Audio payload is empty.")
    if len(audio_bytes) > config.STT_MAX_BYTES:
        raise provider_error(400, "VALIDATION_ERROR", "Audio too large.", {"max_bytes": config.STT_MAX_BYTES, "bytes": len(audio_bytes)})

    mime = sniff_audio_mime(audio_bytes, fallback=mime_hint)
    if not mime:
        raise provider_error(400, "VALIDATION_ERROR", "Could not determine audio content type.")
    if mime not in config.STT_SUPPORTED_MIME_TYPES:
        raise provider_error(400, "VALIDATION_ERROR", "Unsupported audio content type.", {"audio_mime_type": mime, "supported": config.STT_SUPPORTED_MIME_TYPES})

    duration_seconds = probe_duration_seconds(audio_bytes, suffix=suffix_for_mime(mime)) or 0.0
    return audio_bytes, mime, duration_seconds


def _load_tts_duration_seconds(row: JobRow) -> float:
    if not row.result_bucket or not row.result_object:
        return 0.0
    store = MinioStore()
    response = store.get_object(row.result_bucket, row.result_object)
    try:
        payload = response.read()
    finally:
        response.close()
        response.release_conn()
    return probe_duration_seconds(payload, suffix=suffix_for_mime(row.result_content_type)) or 0.0


async def _stream_job_events(
    request: Request,
    job_id: str,
    expected_type: str,
    response_builder: Callable[[JobRow], BaseModel],
) -> AsyncIterator[bytes]:
    poll_interval = max(0.1, float(config.PROVIDER_WEBHOOK_POLL_INTERVAL_SECONDS))
    keepalive_seconds = max(15.0, poll_interval * 5)
    last_payload: Optional[str] = None
    last_emit = asyncio.get_running_loop().time()

    while True:
        if await request.is_disconnected():
            break

        row = _require_job(job_id, expected_type)
        event = _build_job_event(row)
        payload = event.model_dump(exclude_none=True)
        payload_signature = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        now = asyncio.get_running_loop().time()

        if payload_signature != last_payload:
            yield _encode_sse(_job_event_name(event.status), payload)
            last_payload = payload_signature
            last_emit = now
            if row.status in {"succeeded", "failed", "cancelled"}:
                break
        elif now - last_emit >= keepalive_seconds:
            current = response_builder(row).model_dump(exclude_none=True)
            keepalive_id = json.dumps(
                {
                    "id": current["id"],
                    "status": current["status"],
                    "progress_pct": current["progress_pct"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            yield f": keep-alive {keepalive_id}\n\n".encode("utf-8")
            last_emit = now

        await asyncio.sleep(poll_interval)


async def _wait_for_terminal_job(job_id: str, expected_type: str) -> JobRow:
    started_at = time.monotonic()
    max_wait_seconds = max(
        float(config.PROVIDER_WEBHOOK_POLL_INTERVAL_SECONDS),
        float(config.PROVIDER_WEBHOOK_MAX_WAIT_SECONDS),
    )
    while True:
        row = _require_job(job_id, expected_type)
        if row.status in {"succeeded", "failed", "cancelled"}:
            return row
        if time.monotonic() - started_at >= max_wait_seconds:
            raise TimeoutError(f"Timed out waiting for provider job {job_id} to finish.")
        await asyncio.sleep(config.PROVIDER_WEBHOOK_POLL_INTERVAL_SECONDS)


def _track_background_task(task: asyncio.Task[Any]) -> None:
    _webhook_tasks.add(task)
    task.add_done_callback(_webhook_tasks.discard)


def _require_job(job_id: str, expected_type: str) -> JobRow:
    store = JobStore()
    row = store.get_job(job_id)
    if not row or row.type != expected_type:
        raise provider_error(404, "NOT_FOUND", "Job not found.", {"job_id": job_id})
    return row


def _build_tts_job_response(row: JobRow) -> TTSJobStatusResponse:
    result = None
    error_message = None
    params = _parse_params(row)

    if row.status == "succeeded" and row.result_bucket and row.result_object and row.result_content_type:
        store = MinioStore()
        result = TTSJobResult(
            audio=AudioPayload(
                url=store.presign_get(row.result_bucket, row.result_object, config.RESULT_URL_TTL_SECONDS),
                content_type=row.result_content_type,
            ),
            duration_seconds=_load_tts_duration_seconds(row),
            format=_audio_format_from_row(row),
            voice_id=params.get("voice_id"),
            language=params.get("provider_requested_language") or params.get("language"),
        )
    elif row.status in {"failed", "cancelled"}:
        error_message = row.error_message or ("Job cancelled." if row.status == "cancelled" else "Job failed.")

    return TTSJobStatusResponse(
        id=row.id,
        status=_provider_status(row.status),
        progress_pct=_provider_progress_pct(row),
        error_message=error_message,
        **_job_timestamps(row),
        result=result,
    )


def _build_stt_job_response(row: JobRow) -> STTJobStatusResponse:
    result = None
    error_message = None
    params = _parse_params(row)

    if row.status == "succeeded" and row.result_bucket and row.result_object and row.result_content_type:
        store = MinioStore()
        payload_raw = store.get_object_content(row.result_bucket, row.result_object)
        if payload_raw:
            try:
                payload = json.loads(payload_raw)
            except Exception:
                payload = {}
            text = str(payload.get("text") or "")
            requested_language = params.get("provider_requested_language") or params.get("language")
            detected_language = str(payload.get("language") or requested_language or "unknown")
            duration_seconds = float(params.get("provider_input_duration_seconds") or 0.0)
            result = STTJobResult(
                text=text,
                language_detected=detected_language,
                confidence=_estimate_stt_confidence(text, duration_seconds, requested_language, detected_language),
                duration_seconds=duration_seconds,
            )
    elif row.status in {"failed", "cancelled"}:
        error_message = row.error_message or ("Job cancelled." if row.status == "cancelled" else "Job failed.")

    return STTJobStatusResponse(
        id=row.id,
        status=_provider_status(row.status),
        progress_pct=_provider_progress_pct(row),
        error_message=error_message,
        **_job_timestamps(row),
        result=result,
    )


async def _post_webhook(url: str, payload: Dict[str, Any]) -> None:
    async with httpx.AsyncClient(timeout=config.PROVIDER_WEBHOOK_TIMEOUT_SECONDS) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()


async def _notify_tts_webhook(job_id: str, webhook_url: str) -> None:
    try:
        row = await _wait_for_terminal_job(job_id, "tts.synthesize")
        payload = _build_tts_job_response(row).model_dump(exclude_none=True)
        await _post_webhook(webhook_url, payload)
    except Exception:
        logger.exception("Provider webhook delivery failed for TTS job %s to %s", job_id, webhook_url)


async def _notify_stt_webhook(job_id: str, webhook_url: str) -> None:
    try:
        row = await _wait_for_terminal_job(job_id, "stt.transcribe")
        payload = _build_stt_job_response(row).model_dump(exclude_none=True)
        await _post_webhook(webhook_url, payload)
    except Exception:
        logger.exception("Provider webhook delivery failed for STT job %s to %s", job_id, webhook_url)


@router.get("/voices", response_model=VoicePresetListResponse)
@router.get("/v1/voices", response_model=VoicePresetListResponse)
async def list_voices(request: Request) -> VoicePresetListResponse:
    root = Path(config.VOICES_DIR)
    voice_ids = sorted(path.stem for path in root.glob("*.wav") if path.is_file()) if root.is_dir() else []
    return VoicePresetListResponse(
        data=[
            VoicePreset(
                id=voice_id,
                name=_voice_name(voice_id),
                language=config.DEFAULT_VOICE_LANGUAGE,
                gender=_voice_gender(voice_id),
                sample_url=str(request.url_for("voice_sample", voice_id=voice_id)),
            )
            for voice_id in voice_ids
        ]
    )


@router.get("/voices/{voice_id}/sample", name="voice_sample", include_in_schema=False)
async def voice_sample(voice_id: str) -> FileResponse:
    try:
        sample_path = safe_voice_path(config.VOICES_DIR, voice_id)
    except ValueError as exc:
        raise provider_error(400, "VALIDATION_ERROR", str(exc)) from exc
    if not sample_path.is_file():
        raise provider_error(404, "NOT_FOUND", "Voice sample not found.", {"voice_id": voice_id})
    return FileResponse(sample_path, media_type="audio/wav", filename=sample_path.name)


@router.post("/tts/jobs", response_model=VoiceJobAcceptedResponse, status_code=202)
async def create_tts_job(body: TTSJobCreateRequest, request: Request) -> VoiceJobAcceptedResponse:
    voice_id = body.voice_id or config.DEFAULT_VOICE_ID
    if not voice_id:
        raise provider_error(400, "VALIDATION_ERROR", "voice_id is required when no default voice is configured.")
    try:
        voice_path = safe_voice_path(config.VOICES_DIR, voice_id)
    except ValueError as exc:
        raise provider_error(400, "VALIDATION_ERROR", str(exc)) from exc
    if not voice_path.is_file():
        raise provider_error(400, "VALIDATION_ERROR", "Voice not found.", {"voice_id": voice_id})
    if body.format not in config.TTS_OUTPUT_FORMATS:
        raise provider_error(400, "VALIDATION_ERROR", "Unsupported format.", {"format": body.format, "supported": config.TTS_OUTPUT_FORMATS})

    engine_language = _normalize_language_for_engine(body.language)
    job_id = str(uuid.uuid4())
    params = {
        "text": body.text,
        "voice_id": voice_id,
        "language": engine_language,
        "provider_requested_language": body.language,
        "output_format": body.format,
        "controls": {"speed": body.speed},
        "provider_webhook_url": body.webhook_url,
        "client_request_id": body.client_request_id,
    }

    store = JobStore()
    store.init()
    store.create_job(job_id, "tts.synthesize", owner_id=None, params=params)
    await RedisQueue().enqueue(
        config.QUEUE_TTS,
        {
            "job_id": job_id,
            "type": "tts.synthesize",
            "owner_id": None,
            "params": params,
        },
    )
    observe_job_enqueued(job_kind="tts")
    if body.webhook_url:
        _track_background_task(asyncio.create_task(_notify_tts_webhook(job_id, body.webhook_url)))
    return VoiceJobAcceptedResponse(
        id=job_id,
        status="queued",
        event_stream_url=_job_event_stream_url(request, "tts_job_events", job_id),
    )


@router.get("/tts/jobs/{job_id}", response_model=TTSJobStatusResponse)
async def get_tts_job(job_id: str) -> TTSJobStatusResponse:
    row = _require_job(job_id, "tts.synthesize")
    return _build_tts_job_response(row)


@router.get("/tts/jobs/{job_id}/events", name="tts_job_events")
async def stream_tts_job_events(job_id: str, request: Request) -> StreamingResponse:
    _require_job(job_id, "tts.synthesize")
    return StreamingResponse(
        _stream_job_events(request, job_id, "tts.synthesize", _build_tts_job_response),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/stt/jobs", response_model=VoiceJobAcceptedResponse, status_code=202)
async def create_stt_job(body: STTJobCreateRequest, request: Request) -> VoiceJobAcceptedResponse:
    audio_bytes, mime, duration_seconds = await _resolve_stt_input(body)
    engine_language = _normalize_language_for_engine(body.language)

    job_id = str(uuid.uuid4())
    store = JobStore()
    store.init()
    minio_store = MinioStore()
    minio_store.ensure_bucket()

    input_object = f"uploads/{job_id}/input{suffix_for_mime(mime)}"
    minio_store.put_bytes(input_object, audio_bytes, content_type=mime)

    params = {
        "language": engine_language,
        "provider_requested_language": body.language,
        "output_format": "json",
        "provider_webhook_url": body.webhook_url,
        "provider_input_duration_seconds": duration_seconds,
        "client_request_id": body.client_request_id,
    }
    store.create_job(job_id, "stt.transcribe", owner_id=None, params=params)
    await RedisQueue().enqueue(
        config.QUEUE_WHISPER,
        {
            "job_id": job_id,
            "type": "stt.transcribe",
            "owner_id": None,
            "input": {
                "bucket": config.MINIO_BUCKET,
                "object": input_object,
                "content_type": mime,
            },
            "params": {
                "language": engine_language,
                "output_format": "json",
            },
        },
    )
    observe_job_enqueued(job_kind="stt")
    if body.webhook_url:
        _track_background_task(asyncio.create_task(_notify_stt_webhook(job_id, body.webhook_url)))
    return VoiceJobAcceptedResponse(
        id=job_id,
        status="queued",
        event_stream_url=_job_event_stream_url(request, "stt_job_events", job_id),
    )


@router.get("/stt/jobs/{job_id}", response_model=STTJobStatusResponse)
async def get_stt_job(job_id: str) -> STTJobStatusResponse:
    row = _require_job(job_id, "stt.transcribe")
    return _build_stt_job_response(row)


@router.get("/stt/jobs/{job_id}/events", name="stt_job_events")
async def stream_stt_job_events(job_id: str, request: Request) -> StreamingResponse:
    _require_job(job_id, "stt.transcribe")
    return StreamingResponse(
        _stream_job_events(request, job_id, "stt.transcribe", _build_stt_job_response),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

import json
import uuid
import struct
from typing import Any, Dict, Optional, Literal, Iterator

from fastapi import APIRouter, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core import config
from app.core.errors import http_error
from app.db.job_store import JobStore
from app.queue.redis_queue import RedisQueue
from app.storage.minio_store import MinioStore


router = APIRouter(tags=["tts"])


class TtsControls(BaseModel):
    speed: Optional[float] = None
    pitch_semitones: Optional[float] = None
    formant_shift: Optional[float] = None
    energy: Optional[float] = None
    pause_ms: Optional[int] = None
    stability: Optional[float] = None
    # VOICES.md extensions (engine-agnostic; enabled/disabled via /v1/capabilities)
    pause_variance_ms: Optional[int] = Field(default=None, ge=0, le=5000)
    sentence_pause_ms: Optional[int] = Field(default=None, ge=0, le=5000)
    prosody_depth: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    tempo_variance: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    breathiness: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    nasality: Optional[float] = Field(default=None, ge=0.0, le=0.6)
    intensity: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    emphasis_strength: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    variation: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    clarity_boost: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    articulation: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    loudness_db: Optional[float] = Field(default=None, ge=-60.0, le=24.0)
    punctuation_weight: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    sentence_split_aggressiveness: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    repeat_emphasis: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    post_eq_profile: Optional[Literal["neutral", "warm", "broadcast", "crisp"]] = None
    latency_mode: Optional[Literal["quality", "balanced", "realtime"]] = None
    stream_chunk_ms: Optional[int] = Field(default=None, ge=40, le=400)
    # Raw engine control (optional; engine-dependent)
    engine_temperature: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class PlaybackSnapcast(BaseModel):
    mode: str = Field(default="snapcast", description="Playback mode identifier.")
    targets: Optional[list[str]] = None
    target_groups: Optional[list[str]] = None
    pre_chime: bool = False
    night_mode: bool = False
    volume_percent: Optional[int] = Field(default=None, ge=0, le=100)
    dry_run: bool = False


class TtsSynthesizeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    voice_id: str = Field(min_length=1, max_length=256)
    language: Optional[str] = None
    output_format: Optional[str] = None
    sample_rate_hz: Optional[int] = Field(default=None, ge=8000, le=48000)
    seed: Optional[int] = None
    controls: Optional[TtsControls] = None
    playback: Optional[PlaybackSnapcast] = None


@router.post("/tts/synthesize", status_code=202)
async def synthesize(
    body: TtsSynthesizeRequest,
    idempotency_key: Optional[str] = Header(default=None, convert_underscores=False, alias="Idempotency-Key"),
    x_user_id: Optional[str] = Header(default=None, convert_underscores=False),
):
    store = JobStore()
    store.init()

    existing = store.get_or_create_idempotency(idempotency_key or "", x_user_id, "tts.synthesize")
    if existing:
        return {"job_id": existing, "status_url": f"/v1/jobs/{existing}"}

    if body.output_format and body.output_format not in config.TTS_OUTPUT_FORMATS:
        raise http_error(400, "invalid_request", "Invalid output_format", {"output_format": body.output_format, "supported": config.TTS_OUTPUT_FORMATS})

    if body.playback and body.playback.mode != "snapcast":
        raise http_error(400, "invalid_request", "Invalid playback mode", {"mode": body.playback.mode, "supported": ["snapcast"]})

    job_id = str(uuid.uuid4())
    store.create_job(job_id, "tts.synthesize", owner_id=x_user_id, params=body.model_dump())
    store.set_idempotency(idempotency_key or "", x_user_id, "tts.synthesize", job_id)

    payload: Dict[str, Any] = {
        "job_id": job_id,
        "type": "tts.synthesize",
        "owner_id": x_user_id,
        "params": body.model_dump(),
    }
    q = RedisQueue()
    await q.enqueue(config.QUEUE_TTS, payload)

    return {"job_id": job_id, "status_url": f"/v1/jobs/{job_id}"}


def _extract_stream_chunk_ms(params: Dict[str, Any]) -> int:
    controls = params.get("controls") or {}
    if not isinstance(controls, dict):
        controls = {}
    raw = controls.get("stream_chunk_ms")
    if raw is None:
        # Default chunk size can depend on latency_mode.
        lm = controls.get("latency_mode")
        if isinstance(lm, str):
            m = lm.strip().lower()
            if m == "realtime":
                return 80
            if m == "quality":
                return 200
        return 120
    try:
        v = int(raw)
    except Exception:
        return 120
    return max(40, min(400, v))


def _try_parse_wav_byte_rate(prefix: bytes) -> Optional[int]:
    """
    Best-effort parse of WAV 'fmt ' chunk to extract byte_rate (bytes/second).
    Works for common PCM WAVs.
    """
    if len(prefix) < 32:
        return None
    if prefix[0:4] != b"RIFF" or prefix[8:12] != b"WAVE":
        return None
    i = 12
    # Scan chunks inside the provided prefix.
    while i + 8 <= len(prefix):
        chunk_id = prefix[i : i + 4]
        chunk_size = struct.unpack_from("<I", prefix, i + 4)[0]
        data_off = i + 8
        if chunk_id == b"fmt ":
            # Need at least 16 bytes for PCM base fmt.
            if data_off + 16 > len(prefix):
                return None
            # format, channels, sample_rate, byte_rate
            _audio_format, _channels, _sr, byte_rate = struct.unpack_from("<HHII", prefix, data_off)
            return int(byte_rate) if byte_rate > 0 else None
        # chunks are word-aligned
        i = data_off + chunk_size + (chunk_size % 2)
    return None


@router.get("/tts/stream/{job_id}")
async def stream_tts_result(
    job_id: str,
    x_user_id: Optional[str] = Header(default=None, convert_underscores=False),
):
    """
    Stream the synthesized audio bytes for a completed job.

    Phase 1 streaming: stream the final artifact from MinIO. Chunk sizing is derived from
    `controls.stream_chunk_ms` (delivery chunking). True engine streaming is future work.
    """
    store = JobStore()
    row = store.get_job(job_id)
    if not row:
        raise http_error(404, "not_found", "Job not found", {"job_id": job_id})
    if row.owner_id and x_user_id and row.owner_id != x_user_id:
        raise http_error(404, "not_found", "Job not found", {"job_id": job_id})

    if row.status != "succeeded":
        raise http_error(409, "not_ready", "Job result is not ready", {"job_id": job_id, "status": row.status})
    if not row.result_bucket or not row.result_object or not row.result_content_type:
        raise http_error(500, "internal_error", "Job is succeeded but result is missing", {"job_id": job_id})

    # Extract stream_chunk_ms from stored params (best-effort for old jobs).
    chunk_ms = 120
    if row.params_json:
        try:
            params = json.loads(row.params_json)
            if isinstance(params, dict):
                chunk_ms = _extract_stream_chunk_ms(params)
        except Exception:
            pass

    m = MinioStore()
    resp = m.get_object(row.result_bucket, row.result_object)

    def _iter_bytes() -> Iterator[bytes]:
        try:
            prefix = resp.read(4096)
            is_wav = row.result_content_type.lower().startswith("audio/wav")
            byte_rate = _try_parse_wav_byte_rate(prefix) if is_wav else None
            if byte_rate:
                chunk_bytes = max(1024, int(byte_rate * (chunk_ms / 1000.0)))
            else:
                chunk_bytes = 64 * 1024

            for off in range(0, len(prefix), chunk_bytes):
                yield prefix[off : off + chunk_bytes]

            while True:
                b = resp.read(chunk_bytes)
                if not b:
                    break
                yield b
        finally:
            try:
                resp.close()
            finally:
                try:
                    resp.release_conn()
                except Exception:
                    pass

    ext = m.guess_ext(row.result_content_type)
    headers = {"Content-Disposition": f'inline; filename="{job_id}.{ext}"'}
    return StreamingResponse(_iter_bytes(), media_type=row.result_content_type, headers=headers)


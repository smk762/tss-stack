import uuid
from typing import Any, Dict, Optional, Literal

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field

from app.core import config
from app.core.errors import http_error
from app.db.job_store import JobStore
from app.queue.redis_queue import RedisQueue


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
    store.create_job(job_id, "tts.synthesize", owner_id=x_user_id)
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


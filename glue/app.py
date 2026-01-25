import os
import uuid
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from qdrant_routes import router as qdrant_router
from self_lora_routes import router as self_lora_router

log = logging.getLogger("glue.app")

app = FastAPI(title="Voice Glue")
app.include_router(qdrant_router)
app.include_router(self_lora_router)


# ---------- Config ----------
XTTS_URL = os.getenv("XTTS_URL", "http://xtts:8020/tts_to_file")
VOICES_DIR = os.getenv("VOICES_DIR", "/voices")
OUTPUT_DIR = os.getenv("XTTS_OUTPUT_DIR", "/output")
XTTS_LANG = os.getenv("XTTS_LANG", "en")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "30"))
DEFAULT_SPEAKER = os.getenv("DEFAULT_SPEAKER", "female")

Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)


# ---------- Models ----------
class TtsToFileRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    speaker: str = Field(default=DEFAULT_SPEAKER, description='Voice name (e.g. "female") or filename (e.g. "amy.wav").')
    language: str = Field(default=XTTS_LANG)
    timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)


def _resolve_voice_path(speaker: str) -> str:
    # allow "name" -> /voices/name.wav, or explicit "name.wav"
    if speaker.endswith(".wav"):
        p = Path(speaker)
        return str(p) if p.is_absolute() else str(Path(VOICES_DIR) / speaker)
    return str(Path(VOICES_DIR) / f"{speaker}.wav")


def _safe_output_path() -> str:
    # Always write into OUTPUT_DIR (guardrail).
    return str(Path(OUTPUT_DIR) / f"tts-{uuid.uuid4().hex}.wav")


def list_available_voice_names() -> List[str]:
    items: List[str] = []
    p = Path(VOICES_DIR)
    if not p.is_dir():
        return items
    for f in sorted(p.iterdir()):
        if f.is_file() and f.name.lower().endswith(".wav"):
            items.append(f.stem)
    return items


@app.get("/health")
async def health() -> Dict[str, Any]:
    """
    Lightweight health check. Only verifies the XTTS server is reachable.
    """
    ok = True
    detail: Optional[str] = None
    try:
        base = XTTS_URL.split("/tts_to_file")[0]
        async with httpx.AsyncClient(timeout=3) as cli:
            await cli.get(base, timeout=3)
    except Exception as e:
        ok = False
        detail = str(e)
    return {"ok": ok, "xtts": ok, "detail": detail}


@app.get("/voices")
async def voices() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    p = Path(VOICES_DIR)
    if not p.is_dir():
        return items
    for f in sorted(p.iterdir()):
        if f.is_file() and f.name.lower().endswith(".wav"):
            items.append(
                {
                    "name": f.stem,
                    "path": str(f),
                    "rel": f.name,
                    "is_default": (f.stem == DEFAULT_SPEAKER),
                }
            )
    return items


@app.post("/tts_to_file")
async def tts_to_file(body: TtsToFileRequest) -> Dict[str, Any]:
    """
    Synthesize speech to a WAV file in OUTPUT_DIR and return its path.

    Note: This does not do any playback. Playback is intentionally local-only in bare-metal deployments.
    """
    voice_path = _resolve_voice_path(body.speaker)
    # Guardrail: only allow voices under VOICES_DIR.
    try:
        vp = Path(voice_path).resolve()
        vd = Path(VOICES_DIR).resolve()
        if vd not in vp.parents and vp != vd:
            raise HTTPException(status_code=400, detail={"error": "voice must be under VOICES_DIR", "speaker": body.speaker})
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "invalid voice path", "speaker": body.speaker})

    if not Path(voice_path).is_file():
        raise HTTPException(
            status_code=400,
            detail={
                "error": f"Voice not found: '{body.speaker}'",
                "resolved_path": voice_path,
                "available": list_available_voice_names(),
            },
        )

    out_path = _safe_output_path()
    payload = {
        "text": body.text,
        "speaker_wav": voice_path,
        "language": body.language,
        "file_name_or_path": out_path,
    }

    try:
        async with httpx.AsyncClient(timeout=body.timeout_seconds) as cli:
            r = await cli.post(XTTS_URL, json=payload)
            r.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TTS failed: {e}")

    if not Path(out_path).is_file():
        raise HTTPException(status_code=502, detail={"error": "TTS reported success but output file missing", "out_path": out_path})

    return {"ok": True, "wav_path": out_path}


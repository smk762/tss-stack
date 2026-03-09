import os
import uuid
import logging
import subprocess
import asyncio
from pathlib import Path
from typing import Optional, List, Dict, Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


log = logging.getLogger("glue.app")

app = FastAPI(title="Voice Glue")


# ---------- Config ----------
XTTS_URL = os.getenv("XTTS_URL", "http://xtts:8020/tts_to_file")
VOICES_DIR = os.getenv("VOICES_DIR", "/voices/presets")
OUTPUT_DIR = os.getenv("XTTS_OUTPUT_DIR", "/output")
XTTS_LANG = os.getenv("XTTS_LANG", "en")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "30"))
DEFAULT_SPEAKER = os.getenv("DEFAULT_SPEAKER", "female")

Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

# Optional Snapcast announce support (OFF by default)
SNAPCAST_ENABLED = os.getenv("SNAPCAST_ENABLED", "0").lower() in ("1", "true", "yes", "on")
SNAPCAST_FIFO = os.getenv("SNAPCAST_FIFO", "/run/snapcast/snapfifo")
SNAPCAST_RPC = os.getenv("SNAPCAST_RPC", "http://snapserver:1780/jsonrpc")
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")


# ---------- Models ----------
class TtsToFileRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    speaker: str = Field(default=DEFAULT_SPEAKER, description='Voice name (e.g. "female") or filename (e.g. "amy.wav").')
    language: str = Field(default=XTTS_LANG)
    timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)

class AnnounceRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    speaker: str = Field(default=DEFAULT_SPEAKER)
    language: str = Field(default=XTTS_LANG)
    timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)


def _resolve_voice_path(speaker: str) -> str:
    # allow "name" -> VOICES_DIR/name.wav, or explicit "name.wav"
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


async def _snapcast_rpc(method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
    """
    Small helper to call Snapserver's JSON-RPC endpoint.
    """
    if not SNAPCAST_ENABLED:
        raise HTTPException(status_code=409, detail="Snapcast is disabled (set SNAPCAST_ENABLED=1 and enable the snapcast compose profile).")
    payload: Dict[str, Any] = {"id": 1, "jsonrpc": "2.0", "method": method}
    if params:
        payload["params"] = params
    try:
        async with httpx.AsyncClient(timeout=timeout) as cli:
            resp = await cli.post(SNAPCAST_RPC, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail={"error": "snapcast rpc failed", "method": method, "rpc_endpoint": SNAPCAST_RPC, "detail": str(e)},
        )
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail={"error": "unexpected snapcast response", "method": method})
    if data.get("error"):
        raise HTTPException(status_code=502, detail={"error": "snapcast rpc error", "method": method, "rpc_error": data.get("error")})
    if "result" not in data:
        raise HTTPException(status_code=502, detail={"error": "missing result from snapcast", "method": method})
    return data["result"]


def _extract_snapcast_clients(status: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Flatten Snapserver groups -> clients into a simple list.
    """
    server = status.get("server", {}) if isinstance(status, dict) else {}
    groups = server.get("groups", []) or []
    streams = server.get("streams", []) or []
    stream_lookup = {s.get("id"): s for s in streams if isinstance(s, dict)}

    items: List[Dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_id = group.get("id")
        stream_id = group.get("stream_id")
        stream = stream_lookup.get(stream_id, {})
        stream_uri = stream.get("uri", {}) if isinstance(stream, dict) else {}
        stream_label = stream_uri.get("id") or stream_uri.get("raw")

        for client in group.get("clients", []) or []:
            if not isinstance(client, dict):
                continue
            host = client.get("host", {}) if isinstance(client.get("host"), dict) else {}
            cfg = client.get("config", {}) if isinstance(client.get("config"), dict) else {}
            vol = cfg.get("volume", {}) if isinstance(cfg.get("volume"), dict) else {}
            snapclient_cfg = cfg.get("snapclient", {}) if isinstance(cfg.get("snapclient"), dict) else {}
            version = client.get("version", {}) if isinstance(client.get("version"), dict) else {}
            items.append(
                {
                    "id": client.get("id"),
                    "name": cfg.get("name") or host.get("name"),
                    "ip": host.get("ip"),
                    "mac": host.get("mac"),
                    "connected": client.get("connected"),
                    "last_seen": client.get("lastSeen"),
                    "muted": vol.get("muted"),
                    "percent": vol.get("percent"),
                    "latency": snapclient_cfg.get("latency"),
                    "snapclient_version": version.get("client"),
                    "protocol_version": version.get("protocol"),
                    "group_id": group_id,
                    "group_name": group.get("name"),
                    "stream_id": stream_id,
                    "stream_label": stream_label,
                }
            )
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
    return {"ok": ok, "xtts": ok, "snapcast_enabled": SNAPCAST_ENABLED, "detail": detail}


@app.get("/snapcast/status")
async def snapcast_status() -> Dict[str, Any]:
    """
    Return Snapserver's full status (streams, groups, clients).
    """
    status = await _snapcast_rpc("Server.GetStatus")
    return {"ok": True, "status": status}


@app.get("/snapcast/clients")
async def snapcast_clients() -> Dict[str, Any]:
    """
    Return a simplified list of connected Snapclients with group/stream info.
    """
    status = await _snapcast_rpc("Server.GetStatus")
    clients = _extract_snapcast_clients(status)
    return {"ok": True, "count": len(clients), "clients": clients}


@app.get("/snapcast/info")
async def snapcast_info() -> Dict[str, Any]:
    """
    Return Snapserver version and RPC version information.
    """
    info = await _snapcast_rpc("Server.GetInfo")
    rpc = await _snapcast_rpc("Server.GetRPCVersion")
    return {"ok": True, "info": info, "rpc": rpc}


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

    rel = Path(out_path).name
    return {"ok": True, "file": rel, "wav_path": rel, "relative_path": rel, "stored_under": "XTTS_OUTPUT_DIR"}

def _stream_wav_to_snapcast_fifo(wav_path: str) -> None:
    if not SNAPCAST_ENABLED:
        raise HTTPException(status_code=409, detail="Snapcast is disabled (set SNAPCAST_ENABLED=1 and enable the snapcast compose profile).")
    if not Path(SNAPCAST_FIFO).exists():
        raise HTTPException(
            status_code=502,
            detail={
                "error": "Snapcast FIFO not found",
                "fifo": SNAPCAST_FIFO,
                "hint": "Start snapserver with: COMPOSE_PROFILES=snapcast docker compose up -d",
            },
        )
    # snapserver.conf uses codec=flac, so write FLAC frames into the FIFO.
    cmd = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-y",
        "-i",
        wav_path,
        "-ac",
        "2",
        "-ar",
        "48000",
        "-f",
        "flac",
        "-",
    ]
    try:
        with open(SNAPCAST_FIFO, "wb") as fifo:
            subprocess.run(cmd, check=True, stdout=fifo)
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail=f"ffmpeg not found: {FFMPEG_BIN}")
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"ffmpeg failed: {e}")

@app.post("/announce")
async def announce(body: AnnounceRequest) -> Dict[str, Any]:
    """
    Optional side-effect endpoint: synthesize speech and stream it into Snapcast's FIFO.
    Disabled by default; enable with SNAPCAST_ENABLED=1 and the snapcast Compose profile.
    """
    # Reuse the same XTTS payload format as /tts_to_file.
    voice_path = _resolve_voice_path(body.speaker)
    if not Path(voice_path).is_file():
        raise HTTPException(
            status_code=400,
            detail={"error": f"Voice not found: '{body.speaker}'", "resolved_path": voice_path, "available": list_available_voice_names()},
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

    # Stream in a worker thread so we don't block the event loop.
    await asyncio.to_thread(_stream_wav_to_snapcast_fifo, out_path)
    rel = Path(out_path).name
    return {"ok": True, "announced": True, "wav_path": rel}

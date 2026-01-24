import os
import time
import subprocess
import asyncio
import tempfile
import uuid
import ipaddress
import json
import contextlib
import logging
from typing import List, Optional, Tuple, Dict, Any
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException, Request, Header
from pydantic import BaseModel
from fastapi import BackgroundTasks
import wave

from visuals import notify_visuals
from qdrant_routes import router as qdrant_router
from self_lora_routes import router as self_lora_router

log = logging.getLogger("glue.app")

app = FastAPI(title="Voice Glue → Snapcast")
app.include_router(qdrant_router)
app.include_router(self_lora_router)


# ---------- Config ----------
XTTS_URL        = os.getenv("XTTS_URL", "http://xtts:8020/tts_to_file")
SNAP_RPC_URL    = os.getenv("SNAPCAST_RPC", "http://snapserver:1780/jsonrpc")
SNAP_FIFO       = os.getenv("SNAPCAST_FIFO", "/run/snapcast/snapfifo")
VOICES_DIR      = os.getenv("VOICES_DIR", "/voices")
OUTPUT_DIR      = os.getenv("XTTS_OUTPUT_DIR", "/output")
XTTS_LANG       = os.getenv("XTTS_LANG", "en")
TTS_TIMEOUT     = float(os.getenv("XTTS_TIMEOUT", "15"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "30"))
FFMPEG_BIN      = os.getenv("FFMPEG_BIN", "ffmpeg")
DEFAULT_SPEAKER = os.getenv("DEFAULT_SPEAKER", "female")  
 
os.makedirs(OUTPUT_DIR, exist_ok=True)


# Enhanced features
PLAYLOCK = asyncio.Lock()

# Simple TTL cache for idempotency
_IDEM_CACHE: Dict[str, float] = {}
_IDEM_TTL = 60.0  # seconds

# Optional strictness: error if explicit targets/groups resolve to 0
STRICT_TARGET_RESOLUTION = os.getenv("STRICT_TARGET_RESOLUTION", "false").lower() in ("1","true","yes")

VISUAL_PREROLL_MS = int(os.getenv("VISUAL_PREROLL_MS", "150"))

# ---------- Models ----------
class SpeakAndPushRequest(BaseModel):
    text: str
    speaker: str = DEFAULT_SPEAKER            # name or "amy.wav"
    language: str = XTTS_LANG
    targets: Optional[List[str]] = None          # names, ids, MACs, IPs
    target_groups: Optional[List[str]] = None    # Snapcast group names
    pre_chime: bool = False
    night_mode: bool = False
    volume_percent: Optional[int] = None         # 0..100 for target clients
    timeout_seconds: float = TTS_TIMEOUT
    dry_run: bool = False
    visual_media: str = "/opt/argus-visual/inara.mp4"
    visual_loop: bool = True

# ---------- New: Play an existing WAV from shared output ----------
class PlayWavRequest(BaseModel):
    wav_path: str
    targets: Optional[List[str]] = None
    target_groups: Optional[List[str]] = None
    pre_chime: bool = False
    night_mode: bool = False
    volume_percent: Optional[int] = None
    timeout_seconds: float = TTS_TIMEOUT
    dry_run: bool = False

# Legacy model for backward compatibility
class SpeakReq(BaseModel):
    text: str
    speaker: str = DEFAULT_SPEAKER
    # Target by exact client IDs or friendly names (case-insensitive)
    targets: Optional[List[str]] = None
    # Target by group IDs or names (case-insensitive)
    target_groups: Optional[List[str]] = None

# ---------- Enhanced utility functions ----------
def _purge_idem():
    now = time.time()
    for k, t in list(_IDEM_CACHE.items()):
        if now - t > _IDEM_TTL:
            _IDEM_CACHE.pop(k, None)

def _mark_idem(key: str) -> bool:
    _purge_idem()
    if not key:
        return False
    if key in _IDEM_CACHE:
        return True
    _IDEM_CACHE[key] = time.time()
    return False

def _norm(s: str) -> str:
    return s.strip().lower()

def _is_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False

def _resolve_voice_path(speaker: str) -> str:
    # allow "name" -> /voices/name.wav, or explicit "name.wav"
    if speaker.endswith(".wav"):
        return speaker if speaker.startswith("/") else os.path.join(VOICES_DIR, speaker)
    return os.path.join(VOICES_DIR, f"{speaker}.wav")

def _flatten_clients(status: dict) -> List[dict]:
    out = []
    for g in status["server"]["groups"]:
        for c in g["clients"]:
            cc = dict(c)
            cc["_group_id"] = g["id"]
            cc["_group_name"] = g["name"]
            out.append(cc)
    return out

def _pick_targets(all_clients: List[dict],
                  targets: Optional[List[str]],
                  target_groups: Optional[List[str]]) -> Tuple[List[dict], List[dict]]:
    if not targets and not target_groups:
        # broadcast
        return all_clients, []
    tset = set()
    gnames = set(_norm(x) for x in (target_groups or []))

    # group-based
    for c in all_clients:
        if _norm(c.get("_group_name","")) in gnames:
            tset.add(c["id"])

    # client-based
    lookup_idx = {}
    for c in all_clients:
        # keys: id, name, mac, ip, host.name
        keys = {
            str(c["id"]),
            _norm((c.get("config", {}).get("name") or c.get("host", {}).get("name") or "")),
            _norm(c.get("host", {}).get("name") or ""),
        }
        mac = c.get("host", {}).get("mac") or c.get("config", {}).get("mac")
        if mac:
            keys.add(_norm(mac))
        ip = c.get("host", {}).get("ip")
        if ip:
            keys.add(_norm(ip))
        for k in keys:
            if k:
                lookup_idx.setdefault(k, set()).add(c["id"])

    for raw in (targets or []):
        key = _norm(raw)
        if key in lookup_idx:
            tset.update(lookup_idx[key])
        elif _is_ip(key):
            # if not matched above, try direct IP fallback
            for c in all_clients:
                if _norm(c.get("host", {}).get("ip","")) == key:
                    tset.add(c["id"])

    tgt = [c for c in all_clients if c["id"] in tset]
    oth = [c for c in all_clients if c["id"] not in tset]
    return tgt, oth

def _snapshot_volumes(clients: List[dict]) -> Dict[str, Dict[str, Any]]:
    snap = {}
    for c in clients:
        cfg = c.get("config", {})
        vol = (cfg.get("volume") or {})
        snap[str(c["id"])] = {"muted": bool(vol.get("muted", False)), "percent": int(vol.get("percent", 0))}
    return snap


def _wav_duration_ms(path: str) -> int:
    with wave.open(path, "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate() or 1
        return int(frames * 1000 / rate)

# ---------- Enhanced Snapcast helpers ----------
async def snap_rpc(method: str, params: Optional[dict] = None) -> Any:
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.post(SNAP_RPC_URL, json={"id":1,"jsonrpc":"2.0","method":method,"params":params or {}})
        r.raise_for_status()
        js = r.json()
        if "error" in js:
            raise RuntimeError(f"Snap RPC error: {js['error']}")
        return js.get("result")

async def snap_status() -> dict:
    return await snap_rpc("Server.GetStatus")

async def _set_muted(client_id: str, muted: bool):
    await snap_rpc("Client.SetVolume", {"id": client_id, "volume": {"muted": muted}})

async def _set_percent(client_id: str, percent: int):
    percent = max(0, min(100, int(percent)))
    await snap_rpc("Client.SetVolume", {"id": client_id, "volume": {"percent": percent}})

async def _restore_volumes(snapshot: Dict[str, Dict[str, Any]]):
    # best-effort restore
    for cid, state in snapshot.items():
        try:
            await snap_rpc("Client.SetVolume", {"id": cid, "volume": {"muted": state["muted"], "percent": state["percent"]}})
        except Exception:
            pass

def list_available_voice_names() -> list[str]:
    if not os.path.isdir(VOICES_DIR):
        return []
    return sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(VOICES_DIR)
        if f.lower().endswith(".wav")
    )

# Legacy sync functions for backward compatibility
def _rpc(method: str, params: dict | None = None, _id: int = 1) -> dict:
    payload = {"id": _id, "jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    import requests
    r = requests.post(SNAP_RPC_URL, json=payload, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"Snapcast RPC error: {data['error']}")
    return data

def _rpc_batch(calls: List[dict]) -> list:
    import requests
    r = requests.post(SNAP_RPC_URL, json=calls, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        for item in data:
            if "error" in item:
                raise RuntimeError(f"Snapcast RPC error (batch): {item['error']}")
    return data

def get_status() -> dict:
    return _rpc("Server.GetStatus")

def build_client_index(status: dict) -> Tuple[Dict[str, dict], Dict[str, str]]:
    """Return (id->client, name->id). Name from client config name or host name."""
    id2c: Dict[str, dict] = {}
    name2id: Dict[str, str] = {}
    groups = status.get("result", {}).get("server", {}).get("groups", []) or []
    for g in groups:
        for c in g.get("clients", []) or []:
            cid = c["id"]
            id2c[cid] = c
            nm = ((c.get("config") or {}).get("name") or "") or c.get("host", {}).get("name", "")
            if nm:
                name2id[nm] = cid
    return id2c, name2id

def build_group_index(status: dict) -> Tuple[Dict[str, dict], Dict[str, str], Dict[str, List[str]]]:
    """Return (id->group, name->id, gid->client_ids)."""
    id2g: Dict[str, dict] = {}
    name2id: Dict[str, str] = {}
    g_clients: Dict[str, List[str]] = {}
    groups = status.get("result", {}).get("server", {}).get("groups", []) or []
    for g in groups:
        gid = g["id"]
        id2g[gid] = g
        gname = g.get("name", "")
        if gname:
            name2id[gname] = gid
        g_clients[gid] = [c["id"] for c in g.get("clients", []) or []]
    return id2g, name2id, g_clients

def resolve_targets(
    requested_clients: Optional[List[str]],
    requested_groups: Optional[List[str]],
    id2c: Dict[str, dict],
    name2id_client: Dict[str, str],
    id2g: Dict[str, dict],
    name2id_group: Dict[str, str],
    g_clients: Dict[str, List[str]],
) -> List[str]:
    """
    Compute the set of client IDs to target from client IDs/names and group IDs/names.
    Empty lists/None => broadcast (return []).
    """
    if not requested_clients and not requested_groups:
        return []  # empty => broadcast to all

    out: set[str] = set()

    # helper maps for case-insensitive name match
    client_lower = {k.lower(): v for k, v in name2id_client.items()}
    group_lower = {k.lower(): v for k, v in name2id_group.items()}

    # resolve client targets
    for t in requested_clients or []:
        if t in id2c:
            out.add(t)
            continue
        lc = t.lower()
        if lc in client_lower:
            out.add(client_lower[lc])
            continue
        # allow exact case-insensitive match via scan
        for nm, cid in name2id_client.items():
            if nm.lower() == lc:
                out.add(cid)
                break

    # resolve group targets -> expand to client IDs
    for g in requested_groups or []:
        gid = g if g in id2g else group_lower.get(g.lower(), "")
        if gid and gid in g_clients:
            out.update(g_clients[gid])

    return list(out)

# ---------- Enhanced TTS + streaming ----------

async def _run_ffmpeg_to_fifo(input_wav: Optional[str] = None, sine: Optional[Tuple[int,float]] = None):
    if sine:
        freq, dur = sine
        cmd = [FFMPEG_BIN,"-hide_banner","-loglevel","error","-y",
               "-f","lavfi","-i", f"sine=frequency={freq}:duration={dur}",
               "-f","s16le","-ar","48000","-ac","2", SNAP_FIFO]
    else:
        if not input_wav or not os.path.exists(input_wav):
            raise FileNotFoundError("TTS WAV missing")
        cmd = [FFMPEG_BIN,"-hide_banner","-loglevel","error","-y",
               "-i", input_wav,
               "-f","s16le","-ar","48000","-ac","2", SNAP_FIFO]
    proc = await asyncio.create_subprocess_exec(*cmd)
    rc = await proc.wait()
    if rc != 0:
        raise RuntimeError(f"ffmpeg exited {rc}")


async def _synthesize_tts_to(tmp_wav: str, text: str, voice_path: str, language: str, timeout: float):
    payload = {
        "text": text,
        "speaker_wav": voice_path,
        "language": language,
        "file_name_or_path": tmp_wav,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as cli:
            r = await cli.post(XTTS_URL, json=payload)
            r.raise_for_status()
    except httpx.HTTPStatusError as e:
        # include a compact snippet of server text to aid debugging
        body = (e.response.text or "")[:240].replace("\n", " ")
        raise HTTPException(
            status_code=502,
            detail=f"XTTS error {e.response.status_code}: {body or 'no body'}"
        ) from e
    if not os.path.exists(tmp_wav):
        raise RuntimeError("XTTS reported success but output WAV was not found")



def tts_to_file(text: str, speaker: str) -> str:
    """
    daswer123/xtts-api-server expects:
      - text
      - speaker_wav (path inside the xtts container)
      - language
      - file_name_or_path (output path inside container, usually /output/*.wav)
    """
    # Map a logical name like "amy" to /app/example/amy.wav by default
    # If 'speaker' already ends with .wav, treat it as a direct path.
    if speaker.lower().endswith(".wav"):
        speaker_wav = speaker
    else:
        speaker_wav = f"{VOICES_DIR}/{speaker}.wav"

    # unique output name in /output (one timestamp; reuse for payload)
    ts = int(time.time() * 1000)
    out_path = f"{OUTPUT_DIR}/speech_{ts}.wav"

    payload = {
        "text": text,
        "speaker_wav": speaker_wav,
        "language": XTTS_LANG,
        "file_name_or_path": out_path
    }
    log.info(f"XTTS payload: {payload}")
    try:
        import requests
        r = requests.post(XTTS_URL, json=payload, timeout=REQUEST_TIMEOUT)
        log.info(f"XTTS response: {r.json()}")
        r.raise_for_status()
        return out_path
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TTS failed: {e}")

def stream_wav_to_fifo(wav_path: str):
    if not os.path.exists(SNAP_FIFO):
        raise HTTPException(status_code=500, detail=f"FIFO not found: {SNAP_FIFO}")
    cmd = [
        FFMPEG_BIN, "-hide_banner", "-nostdin", "-y", "-re",
        "-i", wav_path, "-f", "s16le", "-ar", "48000", "-ac", "2", SNAP_FIFO
    ]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"ffmpeg failed: {e}")

# ---------- Enhanced API ----------
@app.get("/health")
async def health():
    # XTTS health check
    xtts_ok = True
    try:
        async with httpx.AsyncClient(timeout=3) as cli:
            # fire a tiny no-op; some servers allow a GET to root, others require POST — we just check TCP connect here
            await cli.get(XTTS_URL.split("/tts_to_file")[0], timeout=3)
    except Exception:
        xtts_ok = False
    
    # SNAP health check
    snap_ok = True
    try:
        await snap_status()
    except Exception:
        snap_ok = False
    
    return {"ok": xtts_ok and snap_ok, "xtts": xtts_ok, "snapcast": snap_ok}

@app.get("/snapcast/clients")
async def list_clients():
    st = await snap_status()
    out = []
    for c in _flatten_clients(st):
        out.append({
            "id": c["id"],
            "name": c.get("config",{}).get("name") or c.get("host",{}).get("name"),
            "ip": c.get("host",{}).get("ip"),
            "mac": c.get("host",{}).get("mac") or c.get("config",{}).get("mac"),
            "group": c.get("_group_name"),
        })
    return out

@app.get("/snapcast/groups")
async def list_groups():
    st = await snap_status()
    groups = []
    for g in st["server"]["groups"]:
        groups.append({
            "id": g["id"],
            "name": g["name"],
            "stream_id": g.get("stream_id"),
            "clients": [ {"id": c["id"], "name": (c.get("config",{}).get("name") or c.get("host",{}).get("name"))}
                        for c in g["clients"] ],
        })
    return groups

@app.post("/speak_and_push_legacy")
def speak_and_push_legacy(req: SpeakReq):
    """Legacy TTS → temporarily (un)mute targets → stream over Snapcast FIFO → restore volumes."""
    wav_path = tts_to_file(req.text, req.speaker)

    # Snapshot / indexes
    st = get_status()
    id2c, name2id_c = build_client_index(st)
    id2g, name2id_g, g_clients = build_group_index(st)
    targets = resolve_targets(req.targets, req.target_groups, id2c, name2id_c, id2g, name2id_g, g_clients)  # [] => broadcast

    # Save original mute/volume
    original: Dict[str, dict] = {}
    for cid, c in id2c.items():
        vol = ((c.get("config") or {}).get("volume") or {})
        original[cid] = {"muted": bool(vol.get("muted", False)),
                         "percent": int(vol.get("percent", 100))}

    # Mute non-targets; unmute targets
    calls = []
    for cid in id2c.keys():
        muted = (cid not in targets) if targets else False
        calls.append({
            "id": 1000, "jsonrpc": "2.0",
            "method": "Client.SetVolume",
            "params": {"id": cid, "volume": {"muted": muted}}
        })
    if calls:
        _rpc_batch(calls)

    try:
        stream_wav_to_fifo(wav_path)
    finally:
        # Restore original
        restore = []
        for cid, v in original.items():
            restore.append({
                "id": 1001, "jsonrpc": "2.0",
                "method": "Client.SetVolume",
                "params": {"id": cid, "volume": v}
            })
        if restore:
            _rpc_batch(restore)

    return {
        "file": wav_path,
        "targets": targets or "all",
        "fifo": SNAP_FIFO
    }



@app.post("/speak_and_push")
async def speak_and_push(
    body: SpeakAndPushRequest,
    request: Request,
    x_idempotency_key: Optional[str] = Header(default=None, convert_underscores=False),
    background_tasks: BackgroundTasks = None,
):
    """Enhanced TTS → temporarily (un)mute targets → stream over Snapcast FIFO → restore volumes."""
    # idempotency
    if _mark_idem(x_idempotency_key or ""):
        return {"status": "duplicate_ignored", "idempotency_key": x_idempotency_key}

    # serialize requests
    async with PLAYLOCK:
        st = await snap_status()
        clients_all = _flatten_clients(st)
        targets, others = _pick_targets(clients_all, body.targets, body.target_groups)

        if not targets:
            if (body.targets or body.target_groups) and STRICT_TARGET_RESOLUTION:
                raise HTTPException(status_code=400, detail={
                    "error": "No targets resolved",
                    "targets_requested": body.targets or [],
                    "groups_requested": body.target_groups or [],
                    "hint": "Check /snapcast/clients and /snapcast/groups for valid names/ids/MACs."
                })
            # permissive broadcast
            targets = clients_all
            others = []

        # snapshot volumes before any changes
        snap_before = _snapshot_volumes(clients_all)

        # compute target volume
        if body.night_mode and body.volume_percent is None:
            target_vol = 30
        else:
            target_vol = None if body.volume_percent is None else max(0, min(100, int(body.volume_percent)))

        # resolve voice
        voice_path = _resolve_voice_path(body.speaker)
        if not voice_path.startswith(VOICES_DIR + os.sep):
            raise HTTPException(status_code=400, detail={
                "error": f"Voice path must be under {VOICES_DIR}",
                "speaker": body.speaker,
                "resolved_path": voice_path,
                "hint": "Use GET /voices to list voices."
            })
        if not os.path.isfile(voice_path):
            available = list_available_voice_names()
            raise HTTPException(status_code=400, detail={
                "error": f"Voice not found: '{body.speaker}'",
                "resolved_path": voice_path,
                "available": available
            })

        # dry-run preview
        preview = {
            "targets": [{"id": c["id"], "name": c.get("config", {}).get("name") or c.get("host", {}).get("name")} for c in targets],
            "others":  [{"id": c["id"], "name": c.get("config", {}).get("name") or c.get("host", {}).get("name")} for c in others],
            "target_volume_percent": target_vol,
        }
        if body.dry_run:
            return {"status": "dry_run", "plan": preview}
        out_wav = None
        try:
            # 1) Mute/unmute + set volumes
            for c in others:
                await _set_muted(c["id"], True)
            for c in targets:
                await _set_muted(c["id"], False)
                if target_vol is not None:
                    await _set_percent(c["id"], target_vol)

            # 2) Optional pre-chime (use your existing generator)
            pre_ms = 0
            if body.pre_chime:
                pre_sec = 0.25
                await _run_ffmpeg_to_fifo(sine=(880, pre_sec))
                pre_ms = int(pre_sec * 1000)

            # 3) Synthesize speech to WAV
            out_wav = os.path.join(OUTPUT_DIR, f"tts-{uuid.uuid4().hex}.wav")
            try:
                await _synthesize_tts_to(out_wav, body.text, voice_path, body.language, body.timeout_seconds)
            except HTTPException:
                # already a formatted API error
                raise
            except Exception as e:
                # Fallback chimes then bubble 502
                await _run_ffmpeg_to_fifo(sine=(660, 0.15))
                await _run_ffmpeg_to_fifo(sine=(550, 0.15))
                raise HTTPException(status_code=502, detail=f"TTS failed (played fallback chime): {e}")

            # 4) Determine real duration (wav length + pre-chime + small grace)
            try:
                wav_ms = _wav_duration_ms(out_wav)
            except Exception:
                wav_ms = 0
            grace_ms = 300
            total_ms = pre_ms + wav_ms + grace_ms

            # 5) Kick off visual in parallel (right BEFORE audio playback)
            await notify_visuals(
                targets=body.targets or [],
                text=body.text,
                duration_ms=total_ms,
                media_override=getattr(body, "visual_media", None),
                loop_override=getattr(body, "visual_loop", None),
                background_tasks=None,   # <-- key change
            )
            # Small preroll so the screen is up before audio starts (optional)
            if VISUAL_PREROLL_MS > 0:
                await asyncio.sleep(VISUAL_PREROLL_MS / 1000)

            # 6) Stream the actual TTS audio to Snapcast FIFO
            await _run_ffmpeg_to_fifo(input_wav=out_wav)

        finally:
            # always attempt to restore volumes
            await _restore_volumes(snap_before)
            # cleanup temp file
            with contextlib.suppress(Exception):
                os.remove(out_wav)

        return {"status": "ok", "duration_ms": total_ms, "applied": preview}


@app.post("/play_wav")
async def play_wav(
    body: PlayWavRequest,
    request: Request,
    x_idempotency_key: Optional[str] = Header(default=None, convert_underscores=False),
):
    """
    Stream an existing WAV file to Snapcast FIFO, using the same target selection / mute logic as `/speak_and_push`.

    Intended to be used by other services that generated an audio file into the shared `/output` volume.
    """
    if _mark_idem(x_idempotency_key or ""):
        return {"status": "duplicate_ignored", "idempotency_key": x_idempotency_key}

    async with PLAYLOCK:
        st = await snap_status()
        clients_all = _flatten_clients(st)
        targets, others = _pick_targets(clients_all, body.targets, body.target_groups)

        if not targets:
            if (body.targets or body.target_groups) and STRICT_TARGET_RESOLUTION:
                raise HTTPException(status_code=400, detail={
                    "error": "No targets resolved",
                    "targets_requested": body.targets or [],
                    "groups_requested": body.target_groups or [],
                    "hint": "Check /snapcast/clients and /snapcast/groups for valid names/ids/MACs/IPs."
                })
            targets = clients_all
            others = []

        snap_before = _snapshot_volumes(clients_all)

        if body.night_mode and body.volume_percent is None:
            target_vol = 30
        else:
            target_vol = None if body.volume_percent is None else max(0, min(100, int(body.volume_percent)))

        wav_path = body.wav_path
        # Guardrail: only allow playback from OUTPUT_DIR
        if not wav_path.startswith(OUTPUT_DIR + os.sep):
            raise HTTPException(status_code=400, detail={
                "error": f"wav_path must be under {OUTPUT_DIR}",
                "wav_path": wav_path,
            })
        if not os.path.isfile(wav_path):
            raise HTTPException(status_code=400, detail={
                "error": "wav_path not found",
                "wav_path": wav_path,
            })

        preview = {
            "targets": [{"id": c["id"], "name": c.get("config", {}).get("name") or c.get("host", {}).get("name")} for c in targets],
            "others":  [{"id": c["id"], "name": c.get("config", {}).get("name") or c.get("host", {}).get("name")} for c in others],
            "target_volume_percent": target_vol,
            "wav_path": wav_path,
        }
        if body.dry_run:
            return {"status": "dry_run", "plan": preview}

        try:
            for c in others:
                await _set_muted(c["id"], True)
            for c in targets:
                await _set_muted(c["id"], False)
                if target_vol is not None:
                    await _set_percent(c["id"], target_vol)

            pre_ms = 0
            if body.pre_chime:
                pre_sec = 0.25
                await _run_ffmpeg_to_fifo(sine=(880, pre_sec))
                pre_ms = int(pre_sec * 1000)

            # Determine duration for visuals (optional) and then stream
            try:
                wav_ms = _wav_duration_ms(wav_path)
            except Exception:
                wav_ms = 0
            total_ms = pre_ms + wav_ms + 300

            await _run_ffmpeg_to_fifo(input_wav=wav_path)
        finally:
            await _restore_volumes(snap_before)

        return {"status": "ok", "duration_ms": total_ms, "applied": preview}


@app.get("/voices")
async def list_voices():
    items = []
    if os.path.isdir(VOICES_DIR):
        for f in sorted(os.listdir(VOICES_DIR)):
            if f.lower().endswith(".wav"):
                name = os.path.splitext(f)[0]
                items.append({
                    "name": name,
                    "path": os.path.join(VOICES_DIR, f),
                    "rel": f,
                    "is_default": (name == DEFAULT_SPEAKER)
                })
    return items

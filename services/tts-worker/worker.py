import json
import math
import os
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
import random
import re
from typing import Any, Dict, Optional

import httpx
import redis
from minio import Minio


def env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return default if v is None or v == "" else v


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return int(v)


DATA_DIR = env_str("DATA_DIR", "/data")
DB_PATH = env_str("JOBS_DB_PATH", os.path.join(DATA_DIR, "jobs.db"))

REDIS_URL = env_str("REDIS_URL", "redis://redis:6379/0")
QUEUE_TTS = env_str("QUEUE_TTS", "queue:tts.synthesize")

MINIO_ENDPOINT = env_str("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = env_str("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = env_str("MINIO_SECRET_KEY", "minioadmin")
MINIO_SECURE = env_str("MINIO_SECURE", "false").lower() in ("1", "true", "yes")
MINIO_BUCKET = env_str("MINIO_BUCKET", "artifacts")

XTTS_URL = env_str("XTTS_URL", "http://xtts:8020/tts_to_file")
VOICES_DIR = env_str("VOICES_DIR", "/voices")
XTTS_OUTPUT_DIR = env_str("XTTS_OUTPUT_DIR", "/output")
REQUEST_TIMEOUT = float(env_str("REQUEST_TIMEOUT", "60"))
XTTS_STARTUP_GRACE_SECONDS = env_int("XTTS_STARTUP_GRACE_SECONDS", 120)

SNAPCAST_GLUE_URL = env_str("SNAPCAST_GLUE_URL", "http://xtts-glue:9000/play_wav")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def job_status(job_id: str) -> Optional[str]:
    with db_connect() as conn:
        cur = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        r = cur.fetchone()
        return str(r["status"]) if r else None


def mark_running(job_id: str) -> None:
    with db_connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, started_at = COALESCE(started_at, ?) WHERE id = ? AND status = 'queued'",
            ("running", now_iso(), job_id),
        )
        conn.commit()


def mark_failed(job_id: str, code: str, message: str) -> None:
    with db_connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, finished_at = ?, error_code = ?, error_message = ? WHERE id = ? AND status != 'cancelled'",
            ("failed", now_iso(), code, message, job_id),
        )
        conn.commit()


def mark_succeeded(job_id: str, bucket: str, object_name: str, content_type: str, bytes_: int) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, finished_at = ?, result_bucket = ?, result_object = ?, result_content_type = ?, result_bytes = ?
            WHERE id = ? AND status != 'cancelled'
            """,
            ("succeeded", now_iso(), bucket, object_name, content_type, bytes_, job_id),
        )
        conn.commit()


def resolve_voice_path(voice_id: str) -> str:
    if voice_id.endswith(".wav"):
        p = Path(voice_id)
        if p.is_absolute():
            return str(p)
        return str(Path(VOICES_DIR) / voice_id)
    return str(Path(VOICES_DIR) / f"{voice_id}.wav")


def output_ext(fmt: Optional[str]) -> str:
    if not fmt:
        return "wav"
    f = fmt.lower().strip()
    if f in ("wav", "mp3", "flac"):
        return f
    return "wav"


def _read_controls(params: Dict[str, Any]) -> Dict[str, Any]:
    c = params.get("controls") or {}
    return c if isinstance(c, dict) else {}


def _atempo_chain(speed: float) -> str:
    """
    ffmpeg atempo supports 0.5..2.0 per filter. Chain if needed.
    """
    speed = max(0.25, min(4.0, float(speed)))
    parts = []
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    parts.append(f"atempo={speed:.6f}")
    return ",".join(parts)


def apply_dsp_inplace(wav_path: str, controls: Dict[str, Any]) -> None:
    """
    Best-effort neutral DSP post-processing using ffmpeg.
    - speed: time-stretch via atempo
    - pitch_semitones: pitch shift via asetrate + atempo (duration preserved), then speed applied
    - energy: volume scalar
    - pause_ms: append trailing silence via apad (simple implementation)

    Note: formant_shift not implemented here (requires more specialized processing).
    """
    speed = controls.get("speed")
    pitch_semitones = controls.get("pitch_semitones")
    energy = controls.get("energy")
    pause_ms = controls.get("pause_ms")
    loudness_db = controls.get("loudness_db")
    clarity_boost = controls.get("clarity_boost")
    breathiness = controls.get("breathiness")
    post_eq_profile = controls.get("post_eq_profile")
    nasality = controls.get("nasality")
    formant_shift = controls.get("formant_shift")
    emphasis_strength = controls.get("emphasis_strength")
    latency_mode = controls.get("latency_mode")

    # If nothing set, do nothing.
    if (
        speed is None
        and pitch_semitones is None
        and energy is None
        and pause_ms is None
        and loudness_db is None
        and clarity_boost is None
        and breathiness is None
        and post_eq_profile is None
        and nasality is None
        and formant_shift is None
        and emphasis_strength is None
    ):
        return

    # Latency mode affects DSP depth/quality (best-effort). Default to balanced.
    mode = str(latency_mode).strip().lower() if latency_mode is not None else "balanced"
    if mode not in ("quality", "balanced", "realtime"):
        mode = "balanced"

    filters: list[str] = []
    used_rubberband_formant = False

    # Pitch shift: factor = 2^(semitones/12). Use asetrate + atempo to keep duration.
    if pitch_semitones is not None:
        try:
            st = float(pitch_semitones)
            st = max(-12.0, min(12.0, st))
            pf = math.pow(2.0, st / 12.0)
            # Use input sample rate (sample_rate) rather than assuming a constant.
            filters.append(f"asetrate=sample_rate*{pf:.8f}")
            filters.append(f"atempo={1.0/pf:.8f}")
        except Exception:
            pass

    # Speed: apply after pitch normalization so it affects cadence.
    if speed is not None:
        try:
            sp = float(speed)
            filters.append(_atempo_chain(sp))
        except Exception:
            pass

    # Energy: simple gain. Map 0..1 -> 0.6..1.6 (default 0.5 => 1.1; we clamp).
    if energy is not None:
        try:
            e = float(energy)
            e = max(0.0, min(1.0, e))
            gain = 0.6 + (e * 1.0)  # 0.6..1.6
            filters.append(f"volume={gain:.3f}")
        except Exception:
            pass

    # Loudness: dB adjustment (applied after energy).
    if loudness_db is not None:
        try:
            db = float(loudness_db)
            db = max(-24.0, min(12.0, db))
            filters.append(f"volume={db:.2f}dB")
        except Exception:
            pass

    # EQ preset profile.
    if post_eq_profile is not None:
        p = str(post_eq_profile).strip().lower()
        if p == "warm":
            filters.append("lowpass=f=9000")
            filters.append("equalizer=f=180:t=q:w=1.0:g=2")
        elif p == "broadcast":
            filters.append("highpass=f=80")
            filters.append("equalizer=f=3000:t=q:w=1.2:g=3")
            filters.append("acompressor=threshold=0.2:ratio=3:attack=5:release=50")
        elif p == "crisp":
            filters.append("highpass=f=90")
            filters.append("equalizer=f=6000:t=q:w=1.0:g=4")
        # neutral / unknown => no-op

    # Formant shift (Stage 3: "true-ish" formant processing):
    # Use ffmpeg's `rubberband` filter, which supports `formant=shifted|preserved`.
    #
    # Trick to shift formants while keeping pitch about the same:
    # - Step A: pitch-shift with formant shifted (moves pitch + formants)
    # - Step B: reverse pitch shift with formant preserved (restores pitch, keeps shifted formants)
    #
    # If rubberband isn't available at runtime, we fall back to a bounded EQ tilt (Stage 2 proxy).
    def _rubberband_chain_for_formant_shift(fs: float) -> Optional[list[str]]:
        fs = max(-1.0, min(1.0, float(fs)))
        if abs(fs) < 1e-6:
            return None

        # Map -1..1 to +/- 4 semitones of formant movement (audible but not grotesque).
        semis = fs * 4.0
        pf = math.pow(2.0, semis / 12.0)

        # Quality knobs: "quality" spends more CPU; "realtime" tries to be faster.
        if mode == "quality":
            pitchq = "quality"
            window = "long"
            smoothing = "on"
            transients = "mixed"
        elif mode == "realtime":
            pitchq = "speed"
            window = "short"
            smoothing = "off"
            transients = "crisp"
        else:
            pitchq = "consistency"
            window = "standard"
            smoothing = "off"
            transients = "mixed"

        a = f"rubberband=pitch={pf:.8f}:formant=shifted:pitchq={pitchq}:window={window}:smoothing={smoothing}:transients={transients}"
        b = f"rubberband=pitch={1.0/pf:.8f}:formant=preserved:pitchq={pitchq}:window={window}:smoothing={smoothing}:transients={transients}"
        return [a, b]

    if formant_shift is not None:
        try:
            fs = float(formant_shift)
            rb = _rubberband_chain_for_formant_shift(fs)
            if rb:
                filters.extend(rb)
                used_rubberband_formant = True
            else:
                # no-op
                pass
        except Exception:
            rb = None

        # If rubberband chain couldn't be built (or fails later), we still add a safe EQ tilt proxy.
        if (formant_shift is not None) and (not rb):
            try:
                fs = float(formant_shift)
                fs = max(-1.0, min(1.0, fs))
                if abs(fs) > 1e-6:
                    low_gain = -fs * 4.0   # +4dB when fs=-1, -4dB when fs=+1
                    high_gain = fs * 6.0   # -6dB when fs=-1, +6dB when fs=+1
                    filters.append(f"equalizer=f=180:t=q:w=0.8:g={low_gain:.2f}")
                    filters.append(f"equalizer=f=6500:t=q:w=0.9:g={high_gain:.2f}")
            except Exception:
                pass

    # Nasality: emphasize nasal resonance band (~900–1400Hz) and attenuate low/high.
    # Range per VOICES.md is 0.0–0.6; we map it to a *strong* but bounded effect so it's audible.
    if nasality is not None:
        try:
            n = float(nasality)
            n = max(0.0, min(0.6, n))
            if n > 0:
                # Core nasal band boost.
                band_gain = (n / 0.6) * 12.0  # 0..12 dB
                # Trim lows/highs more noticeably as nasality increases.
                low_cut_db = -(n / 0.6) * 4.0
                hi_cut_db = -(n / 0.6) * 4.0
                # Slight "honk" secondary boost.
                honk_gain = (n / 0.6) * 5.0

                # Wider band around 1.1kHz + a narrower boost around ~900Hz.
                filters.append(f"equalizer=f=1100:t=q:w=0.8:g={band_gain:.2f}")
                filters.append(f"equalizer=f=900:t=q:w=1.2:g={honk_gain:.2f}")
                filters.append(f"equalizer=f=140:t=q:w=1.0:g={low_cut_db:.2f}")
                filters.append(f"equalizer=f=9000:t=q:w=1.0:g={hi_cut_db:.2f}")
        except Exception:
            pass

    # Emphasis strength: post-DSP "punch" shaping (audible, bounded).
    # This is not true token-level emphasis, but it does increase perceived emphasis/impact.
    if emphasis_strength is not None:
        try:
            es = float(emphasis_strength)
            es = max(0.0, min(1.0, es))
        except Exception:
            es = 0.0
        if es > 0:
            if mode != "realtime":
                # Gentle upward compression + limiter; parameters scale with es.
                thr = 0.25 - (0.10 * es)          # ~0.25 -> 0.15
                ratio = 1.5 + (2.5 * es)          # 1.5 -> 4.0
                atk = 8 - (4 * es)                # 8ms -> 4ms
                rel = 90 + (70 * es)              # 90ms -> 160ms
                filters.append(f"acompressor=threshold={thr:.3f}:ratio={ratio:.2f}:attack={atk:.1f}:release={rel:.1f}:makeup=1.0")
                filters.append("alimiter=limit=0.98:level=disabled")
            # Presence lift to make consonants feel more emphatic (0..1 -> 0..3dB)
            filters.append(f"equalizer=f=2800:t=q:w=1.0:g={es*3.0:.2f}")

    # Clarity boost: simple high-mid lift (0..1 -> 0..4dB).
    if clarity_boost is not None:
        try:
            cb = float(clarity_boost)
            cb = max(0.0, min(1.0, cb))
            if cb > 0:
                filters.append(f"equalizer=f=3500:t=q:w=1.0:g={cb*4.0:.2f}")
        except Exception:
            pass

    # Pause padding: append silence at end.
    if pause_ms is not None:
        try:
            ms = int(pause_ms)
            ms = max(0, min(2000, ms))
            if ms > 0:
                filters.append(f"apad=pad_dur={ms/1000.0:.3f}")
        except Exception:
            pass

    if not filters:
        return

    tmp_path = wav_path + ".dsp.wav"
    # Output sample rate: for realtime, lower to reduce CPU (Snapcast glue resamples anyway).
    target_sr = 48000 if mode == "quality" else (16000 if mode == "realtime" else 22050)
    # Breathiness: mix in "air-band" noise (filtered) and duck it slightly with the voice so it stays natural.
    # Implemented by adding a second input.
    if breathiness is not None and mode != "realtime":
        try:
            b = float(breathiness)
            b = max(0.0, min(1.0, b))
        except Exception:
            b = 0.0
        # Map 0..1 -> noise weight 0..0.40 (stronger / more audible)
        noise_w = b * 0.40
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            wav_path,
            "-f",
            "lavfi",
            "-i",
            # Pink-ish noise, filtered into a "breath/air" band.
            "anoisesrc=color=pink:amplitude=0.06",
            "-filter_complex",
            # 1) process voice
            # 2) shape noise into air-band, scale to breathiness level
            # 3) duck noise with sidechain so it tucks under speech
            # 4) mix
            f"[0:a]{','.join(filters)}[a0];"
            f"[1:a]highpass=f=2500,lowpass=f=9000,volume={noise_w:.4f}[n0];"
            f"[n0][a0]sidechaincompress=threshold=0.08:ratio=8:attack=10:release=150[n1];"
            f"[a0][n1]amix=inputs=2:weights=1 1:normalize=0",
            "-acodec",
            "pcm_s16le",
            "-ar",
            str(int(target_sr)),
            tmp_path,
        ]
    else:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            wav_path,
            "-filter:a",
            ",".join(filters),
            "-acodec",
            "pcm_s16le",
            "-ar",
            str(int(target_sr)),
            tmp_path,
        ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.replace(tmp_path, wav_path)
        return
    except subprocess.CalledProcessError:
        # If rubberband-based formant shift failed at runtime (older ffmpeg build, missing library),
        # fall back to the EQ-tilt approximation rather than dropping DSP entirely.
        if used_rubberband_formant:
            safe_filters = [f for f in filters if not f.strip().startswith("rubberband=")]
            try:
                fs = float(formant_shift) if formant_shift is not None else 0.0
                fs = max(-1.0, min(1.0, fs))
                if abs(fs) > 1e-6:
                    low_gain = -fs * 4.0
                    high_gain = fs * 6.0
                    safe_filters.append(f"equalizer=f=180:t=q:w=0.8:g={low_gain:.2f}")
                    safe_filters.append(f"equalizer=f=6500:t=q:w=0.9:g={high_gain:.2f}")
                if safe_filters:
                    cmd2 = [
                        "ffmpeg",
                        "-hide_banner",
                        "-nostdin",
                        "-y",
                        "-i",
                        wav_path,
                        "-filter:a",
                        ",".join(safe_filters),
                        "-acodec",
                        "pcm_s16le",
                        "-ar",
                        str(int(target_sr)),
                        tmp_path,
                    ]
                    subprocess.run(cmd2, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    os.replace(tmp_path, wav_path)
                    return
            except Exception:
                pass
        raise


def preprocess_text(text: str, controls: Dict[str, Any]) -> str:
    """
    Cheap engine-agnostic prosody shaping via punctuation/spaces.
    This is intentionally conservative: it won't use special tokens.
    """
    if not text:
        return text

    sentence_pause_ms = controls.get("sentence_pause_ms")
    pause_variance_ms = controls.get("pause_variance_ms")
    repeat_emphasis = controls.get("repeat_emphasis")
    punctuation_weight = controls.get("punctuation_weight")
    sentence_split_aggressiveness = controls.get("sentence_split_aggressiveness")

    # If none set, keep text unchanged.
    if (
        sentence_pause_ms is None
        and pause_variance_ms is None
        and repeat_emphasis is None
        and punctuation_weight is None
        and sentence_split_aggressiveness is None
    ):
        return text

    try:
        sp = int(sentence_pause_ms) if sentence_pause_ms is not None else 0
        sp = max(0, min(1200, sp))
    except Exception:
        sp = 0

    try:
        pv = int(pause_variance_ms) if pause_variance_ms is not None else 0
        pv = max(0, min(300, pv))
    except Exception:
        pv = 0

    # Heuristic: every "~150ms" of desired pause add one ellipsis token.
    ellipses = max(0, min(6, round(sp / 150))) if sp > 0 else 0

    # Randomness in punctuation: occasionally add an extra comma (small effect).
    comma_prob = min(0.35, pv / 300.0) if pv > 0 else 0.0

    # Punctuation weight: 0..1.
    # - lower => reduce comma/semicolon density (less pausing)
    # - higher => reinforce punctuation-driven pauses (more pausing)
    try:
        pw = float(punctuation_weight) if punctuation_weight is not None else 0.7
        pw = max(0.0, min(1.0, pw))
    except Exception:
        pw = 0.7

    # Repeat emphasis reduction:
    # For consecutive repeated words ("no no", "very very"), insert light punctuation to avoid robotic stress.
    # This is conservative (only touches immediate repeats) and tries to preserve meaning.
    try:
        re_strength = float(repeat_emphasis) if repeat_emphasis is not None else 0.0
        re_strength = max(0.0, min(1.0, re_strength))
    except Exception:
        re_strength = 0.0

    if re_strength > 0:
        # Only for immediate duplicates, case-insensitive, alphabetic words.
        # Example: "no no" -> "no, no" (or "no... no" at higher strength)
        def _fix_immediate_repeats(s: str) -> str:
            def repl(m: re.Match) -> str:
                w = m.group(1)
                # Choose separator based on strength.
                sep = ", " if re_strength < 0.7 else "... "
                return f"{w}{sep}{w}"

            return re.sub(r"\b([A-Za-z']{2,})\s+\1\b", repl, s, flags=re.IGNORECASE)

        text = _fix_immediate_repeats(text)

    # Apply punctuation_weight conservatively:
    # - For pw < 0.5, soften commas/semicolons/colons by turning some into spaces.
    # - For pw > 0.7, occasionally add a comma before conjunctions to create micro-pauses.
    if pw < 0.5:
        # Drop a fraction of commas/semicolons/colons.
        # Strength: pw=0 -> drop ~70%, pw=0.49 -> drop ~20%
        drop_p = 0.2 + (0.7 - 0.2) * (1.0 - (pw / 0.5))
        out2 = []
        for ch in text:
            if ch in ",;:" and random.random() < drop_p:
                out2.append(" ")
            else:
                out2.append(ch)
        text = "".join(out2)
    elif pw > 0.7:
        add_p = min(0.25, (pw - 0.7) / 0.3 * 0.25)  # 0..0.25
        # Add comma before common conjunctions when there's a reasonable clause boundary.
        text = re.sub(
            r"(?i)(\w)(\s+)(and|but|so|or)(\s+)",
            lambda m: f"{m.group(1)}, {m.group(3)} " if random.random() < add_p else m.group(0),
            text,
        )

    # Sentence split aggressiveness (0..1): encourage chunking by inserting paragraph breaks.
    # This does not do multi-request synthesis; it only nudges the engine with structure.
    try:
        ssa = float(sentence_split_aggressiveness) if sentence_split_aggressiveness is not None else 0.0
        ssa = max(0.0, min(1.0, ssa))
    except Exception:
        ssa = 0.0

    if ssa > 0 and len(text) > 400:
        # Target max chars per chunk: 900 (low) -> 220 (high)
        max_chars = int(900 - (ssa * 680))
        max_chars = max(180, min(1200, max_chars))

        # Split into sentence-ish segments, keeping punctuation.
        parts = re.split(r"(?<=[.!?])\s+", text.strip())
        chunks: list[str] = []
        cur = ""
        for p in parts:
            if not p:
                continue
            if not cur:
                cur = p
                continue
            if len(cur) + 1 + len(p) <= max_chars:
                cur = f"{cur} {p}"
            else:
                chunks.append(cur)
                cur = p
        if cur:
            chunks.append(cur)

        # Join chunks with blank lines to strongly suggest a pause/breath.
        if len(chunks) > 1:
            text = "\n\n".join(chunks)

    out = []
    for ch in text:
        out.append(ch)
        if ch in ".!?":
            if ellipses:
                out.append(" " + ("..." * ellipses) + " ")
            # punctuation_weight influences how often we inject extra commas near sentence ends.
            if comma_prob and random.random() < (comma_prob * (0.5 + pw)):
                out.append(", ")
    return "".join(out)

def main() -> None:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    m = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY, secure=MINIO_SECURE)
    if not m.bucket_exists(MINIO_BUCKET):
        m.make_bucket(MINIO_BUCKET)

    print(f"[tts-worker] queue={QUEUE_TTS} redis={REDIS_URL} xtts={XTTS_URL} db={DB_PATH}")

    while True:
        item = r.brpop(QUEUE_TTS, timeout=5)
        if not item:
            continue
        _, raw = item
        try:
            msg = json.loads(raw)
        except Exception:
            print("[tts-worker] invalid json message; skipping")
            continue

        job_id = msg.get("job_id")
        if not job_id:
            print("[tts-worker] missing job_id; skipping")
            continue

        if job_status(job_id) == "cancelled":
            print(f"[tts-worker] job cancelled; skipping {job_id}")
            continue

        mark_running(job_id)
        params: Dict[str, Any] = msg.get("params") or {}
        controls = _read_controls(params)
        text = preprocess_text(params.get("text") or "", controls)
        voice_id = params.get("voice_id") or ""
        language = params.get("language") or "en"
        fmt = params.get("output_format") or "wav"

        voice_path = resolve_voice_path(voice_id)
        if not os.path.isfile(voice_path):
            mark_failed(job_id, "invalid_request", f"Voice not found: {voice_id}")
            continue

        # IMPORTANT: XTTS writes the output file on the XTTS container filesystem.
        # Therefore, we must request an output path that exists in BOTH containers via a shared volume.
        os.makedirs(XTTS_OUTPUT_DIR, exist_ok=True)
        out_wav = os.path.join(XTTS_OUTPUT_DIR, f"tts-{job_id}.wav")

        # Best-effort cleanup in case a previous run left a partial file.
        try:
            if os.path.exists(out_wav):
                os.remove(out_wav)
        except Exception:
            pass

        payload = {"text": text, "speaker_wav": voice_path, "language": language, "file_name_or_path": out_wav}

        last_err: Optional[str] = None
        with httpx.Client(timeout=REQUEST_TIMEOUT) as cli:
            for attempt in range(1, max(1, int(XTTS_STARTUP_GRACE_SECONDS / 3)) + 1):
                try:
                    resp = cli.post(XTTS_URL, json=payload)
                    resp.raise_for_status()
                    last_err = None
                    break
                except Exception as e:
                    # Common during cold start / model download, or XTTS restarting.
                    # Also happens if the output path is not accessible to XTTS.
                    detail = getattr(e, "response", None)
                    if detail is not None:
                        try:
                            last_err = f"{e} body={detail.text[:500]}"
                        except Exception:
                            last_err = str(e)
                    else:
                        last_err = str(e)
                    if attempt == 1:
                        print(f"[tts-worker] XTTS not ready yet; retrying for up to {XTTS_STARTUP_GRACE_SECONDS}s...")
                    import time

                    time.sleep(3)

        if last_err is not None:
            mark_failed(job_id, "engine_error", f"XTTS failed: {last_err}")
            continue

        if not os.path.isfile(out_wav):
            mark_failed(job_id, "engine_error", f"XTTS reported success but output file missing: {out_wav}")
            continue

        # Apply neutral post-processing controls (makes UI sliders audible).
        try:
            apply_dsp_inplace(out_wav, controls)
        except Exception as e:
            # DSP is optional; fall back to raw XTTS output.
            print(f"[tts-worker] DSP failed for {job_id}: {e}")

        # Optional playback side-effect (e.g. Snapcast).
        playback = params.get("playback") or {}
        if isinstance(playback, dict) and playback.get("mode") == "snapcast":
            try:
                with httpx.Client(timeout=REQUEST_TIMEOUT) as cli:
                    resp = cli.post(
                        SNAPCAST_GLUE_URL,
                        json={
                            "wav_path": out_wav,
                            "targets": playback.get("targets"),
                            "target_groups": playback.get("target_groups"),
                            "pre_chime": bool(playback.get("pre_chime", False)),
                            "night_mode": bool(playback.get("night_mode", False)),
                            "volume_percent": playback.get("volume_percent"),
                            "dry_run": bool(playback.get("dry_run", False)),
                        },
                    )
                    resp.raise_for_status()
            except Exception as e:
                # Playback is optional. We still succeed the job (artifact is produced),
                # but we log the failure for troubleshooting.
                print(f"[tts-worker] snapcast playback failed for {job_id}: {e}")

        object_name = f"outputs/{job_id}/audio.wav"
        size = os.path.getsize(out_wav)
        with open(out_wav, "rb") as f:
            m.put_object(MINIO_BUCKET, object_name, f, length=size, content_type="audio/wav")
        mark_succeeded(job_id, MINIO_BUCKET, object_name, "audio/wav", size)
        print(f"[tts-worker] succeeded {job_id} -> {MINIO_BUCKET}/{object_name}")

        # Cleanup output file from shared volume (optional; keep if you want local caching)
        try:
            os.remove(out_wav)
        except Exception:
            pass


if __name__ == "__main__":
    main()


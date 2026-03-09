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
from typing import Any, Dict, Optional, Tuple
from minio.error import S3Error
SUPPORTED_TTS_FORMATS: Dict[str, str] = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "flac": "audio/flac",
}


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
VOICES_DIR = env_str("VOICES_DIR", "/voices/presets")
XTTS_OUTPUT_DIR = env_str("XTTS_OUTPUT_DIR", "/output")
REQUEST_TIMEOUT = float(env_str("REQUEST_TIMEOUT", "60"))
XTTS_STARTUP_GRACE_SECONDS = env_int("XTTS_STARTUP_GRACE_SECONDS", 120)

DEBUG_PREPROCESS_TEXT = env_str("DEBUG_PREPROCESS_TEXT", "false").lower() in ("1", "true", "yes")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_connect() -> sqlite3.Connection:
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def job_status(job_id: str) -> Optional[str]:
    conn = db_connect()
    try:
        cur = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        r = cur.fetchone()
        return str(r["status"]) if r else None
    finally:
        conn.close()


def mark_running(job_id: str) -> None:
    conn = db_connect()
    try:
        conn.execute(
            "UPDATE jobs SET status = ?, started_at = COALESCE(started_at, ?) WHERE id = ? AND status = 'queued'",
            ("running", now_iso(), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_failed(job_id: str, code: str, message: str) -> None:
    conn = db_connect()
    try:
        conn.execute(
            "UPDATE jobs SET status = ?, finished_at = ?, error_code = ?, error_message = ? WHERE id = ? AND status != 'cancelled'",
            ("failed", now_iso(), code, message, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_progress(job_id: str, progress: float) -> None:
    conn = db_connect()
    try:
        conn.execute(
            "UPDATE jobs SET progress = ? WHERE id = ? AND status = 'running'",
            (max(0.0, min(1.0, float(progress))), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_succeeded(job_id: str, bucket: str, object_name: str, content_type: str, bytes_: int) -> None:
    conn = db_connect()
    try:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, finished_at = ?, progress = ?, result_bucket = ?, result_object = ?, result_content_type = ?, result_bytes = ?
            WHERE id = ? AND status != 'cancelled'
            """,
            ("succeeded", now_iso(), 1.0, bucket, object_name, content_type, bytes_, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def resolve_voice_path(voice_id: str) -> str:
    """
    Resolve a user-provided voice_id to a file path inside VOICES_DIR.
    Rejects attempts to escape VOICES_DIR (../ or absolute paths).
    """
    if not str(voice_id).strip():
        raise ValueError("voice_id is required")
    root = Path(VOICES_DIR).resolve()
    candidate = Path(voice_id)
    if not candidate.suffix:
        candidate = Path(VOICES_DIR) / f"{voice_id}.wav"
    elif not candidate.is_absolute():
        candidate = Path(VOICES_DIR) / candidate

    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except Exception:
        raise ValueError(f"voice_id must resolve under VOICES_DIR ({VOICES_DIR})")
    return str(resolved)


def normalize_output_format(fmt: Optional[str]) -> str:
    if not fmt:
        return "wav"
    f = str(fmt).strip().lower()
    if f not in SUPPORTED_TTS_FORMATS:
        raise ValueError(f"Unsupported output_format '{fmt}'. Supported: {sorted(SUPPORTED_TTS_FORMATS.keys())}")
    return f


def normalize_sample_rate(sample_rate: Optional[Any]) -> Optional[int]:
    if sample_rate is None:
        return None
    try:
        sr = int(sample_rate)
    except Exception:
        raise ValueError("sample_rate_hz must be an integer")
    if not 8000 <= sr <= 48000:
        raise ValueError("sample_rate_hz must be between 8000 and 48000")
    return sr


def finalize_audio(input_wav: str, fmt: str, sample_rate_hz: Optional[int]) -> Tuple[str, int, str]:
    """
    Optionally resample + transcode the WAV into the requested format.
    Returns (path, size_bytes, content_type).
    """
    if fmt == "wav" and sample_rate_hz is None:
        return input_wav, os.path.getsize(input_wav), SUPPORTED_TTS_FORMATS["wav"]

    target_path = input_wav if fmt == "wav" else os.path.splitext(input_wav)[0] + f".{fmt}"
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        input_wav,
    ]
    if sample_rate_hz is not None:
        cmd += ["-ar", str(sample_rate_hz)]

    if fmt == "wav":
        cmd += ["-acodec", "pcm_s16le", target_path]
    elif fmt == "mp3":
        cmd += ["-acodec", "libmp3lame", target_path]
    elif fmt == "ogg":
        cmd += ["-acodec", "libvorbis", target_path]
    elif fmt == "flac":
        cmd += ["-acodec", "flac", target_path]
    else:
        raise ValueError(f"Unsupported output format {fmt}")

    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return target_path, os.path.getsize(target_path), SUPPORTED_TTS_FORMATS[fmt]


def _read_controls(params: Dict[str, Any]) -> Dict[str, Any]:
    c = params.get("controls") or {}
    return c if isinstance(c, dict) else {}

def _coerce_temperature(controls: Dict[str, Any]) -> Optional[float]:
    raw = controls.get("engine_temperature")
    if raw is None:
        return None
    try:
        t = float(raw)
    except Exception:
        return None
    return max(0.0, min(1.0, t))


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
    # Output sample rate: for realtime, lower to reduce CPU.
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

    # Guardrail: XTTS-style engines can behave oddly on newlines / weird whitespace.
    # We'll do our shaping, then normalize whitespace at the end.

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

    # Conservative pause shaping:
    # Avoid inserting punctuation like ",", "...", etc. Those can confuse some TTS models and cause artifacts.
    # Instead we only add *extra whitespace* after sentence boundaries.
    extra_space_after_sentence = 1 if sp >= 150 else 0
    extra_space_prob = min(0.20, pv / 600.0) if pv > 0 else 0.0

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

    # Apply punctuation_weight safely:
    # We do NOT delete punctuation or insert commas inside clauses.
    # Instead we scale the amount of extra sentence-boundary spacing we add.
    boundary_space_scale = 0.5 + (pw * 1.0)  # 0.5..1.5

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

        # Join chunks without newlines; newlines can trigger odd model behavior in some engines.
        if len(chunks) > 1:
            glued: list[str] = []
            for c in chunks:
                c = c.strip()
                if not c:
                    continue
                # Ensure chunk ends in sentence punctuation to help the model reset prosody.
                if c[-1] not in ".!?":
                    c = c + "."
                glued.append(c)
            text = " ".join(glued)

    out = []
    for ch in text:
        out.append(ch)
        if ch in ".!?":
            # Extra sentence pause via whitespace only (safe).
            if extra_space_after_sentence:
                out.append(" " * int(round(extra_space_after_sentence * boundary_space_scale)))
            if extra_space_prob and random.random() < extra_space_prob:
                out.append(" ")

    # Final sanitize: collapse whitespace and strip control chars/newlines.
    cleaned = "".join(out)
    cleaned = cleaned.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    # Avoid pathological punctuation runs.
    cleaned = re.sub(r"\.{4,}", "...", cleaned)
    cleaned = re.sub(r",\s*,+", ", ", cleaned)
    return cleaned

def _validate_job_id(job_id: str) -> str:
    try:
        uuid.UUID(str(job_id))
        return str(job_id)
    except Exception:
        raise ValueError("invalid job_id")


def main() -> None:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    m = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY, secure=MINIO_SECURE)
    try:
        if not m.bucket_exists(MINIO_BUCKET):
            m.make_bucket(MINIO_BUCKET)
    except S3Error as e:
        if getattr(e, "code", "") not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise

    print(f"[tts-worker] queue={QUEUE_TTS} redis={REDIS_URL} xtts={XTTS_URL} db={DB_PATH}")

    while True:
        item = r.brpop(QUEUE_TTS, timeout=5)
        if not item:
            continue
        _, raw = item
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            print("[tts-worker] invalid json message; skipping")
            continue

        job_id = msg.get("job_id")
        if not job_id:
            print("[tts-worker] missing job_id; skipping")
            continue
        try:
            job_id = _validate_job_id(str(job_id))
        except ValueError:
            print("[tts-worker] invalid job_id format; skipping")
            continue

        if job_status(job_id) == "cancelled":
            print(f"[tts-worker] job cancelled; skipping {job_id}")
            continue

        mark_running(job_id)
        set_progress(job_id, 0.08)
        params: Dict[str, Any] = msg.get("params") or {}
        # Make preprocessing randomness deterministic per job unless caller provided a seed.
        try:
            seed = params.get("seed")
            if seed is None:
                seed = uuid.UUID(str(job_id)).int & 0xFFFFFFFF
            random.seed(int(seed))
        except Exception:
            pass
        controls = _read_controls(params)
        original_text = params.get("text") or ""
        text = preprocess_text(original_text, controls)
        set_progress(job_id, 0.15)
        if DEBUG_PREPROCESS_TEXT:
            o = re.sub(r"\s+", " ", str(original_text)).strip()
            p = re.sub(r"\s+", " ", str(text)).strip()
            print(f"[tts-worker] text_preprocess job={job_id} orig_len={len(o)} proc_len={len(p)} orig='{o[:160]}' proc='{p[:160]}'")
        voice_id = params.get("voice_id") or ""
        language = params.get("language") or "en"
        try:
            fmt = normalize_output_format(params.get("output_format"))
        except ValueError as e:
            mark_failed(job_id, "invalid_request", str(e))
            continue
        try:
            sample_rate_hz = normalize_sample_rate(params.get("sample_rate_hz"))
        except ValueError as e:
            mark_failed(job_id, "invalid_request", str(e))
            continue

        try:
            voice_path = resolve_voice_path(voice_id)
        except ValueError as e:
            mark_failed(job_id, "invalid_request", str(e))
            continue
        if not os.path.isfile(voice_path):
            mark_failed(job_id, "invalid_request", f"Voice not found: {voice_id}")
            continue
        set_progress(job_id, 0.22)

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
        # Raw engine temperature (engine-dependent). We try to pass it through, but fall back if unsupported.
        eng_temp = _coerce_temperature(controls)
        if eng_temp is not None:
            payload["temperature"] = eng_temp

        last_err: Optional[str] = None
        with httpx.Client(timeout=REQUEST_TIMEOUT) as cli:
            for attempt in range(1, max(1, int(XTTS_STARTUP_GRACE_SECONDS / 3)) + 1):
                try:
                    resp = cli.post(XTTS_URL, json=payload)
                    resp.raise_for_status()
                    last_err = None
                    break
                except httpx.HTTPStatusError as e:
                    # If the engine rejects unknown fields (common with strict schemas),
                    # retry once without temperature so the request still succeeds.
                    try:
                        status = int(e.response.status_code)
                        body = (e.response.text or "")[:400].lower()
                    except Exception:
                        status = 0
                        body = ""
                    if (
                        400 <= status < 500
                        and "temperature" in payload
                        and any(x in body for x in ("extra fields", "unexpected", "unknown", "forbidden", "not permitted"))
                    ):
                        print(f"[tts-worker] XTTS rejected temperature; retrying without it (status={status})")
                        payload.pop("temperature", None)
                        continue
                    # Otherwise handle like generic failure below.
                    last_err = f"{e} body={(e.response.text or '')[:500]}"
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
        set_progress(job_id, 0.62)

        if not os.path.isfile(out_wav):
            mark_failed(job_id, "engine_error", f"XTTS reported success but output file missing: {out_wav}")
            continue

        # Apply neutral post-processing controls (makes UI sliders audible).
        try:
            apply_dsp_inplace(out_wav, controls)
        except Exception as e:
            # DSP is optional; fall back to raw XTTS output.
            print(f"[tts-worker] DSP failed for {job_id}: {e}")
        set_progress(job_id, 0.78)

        try:
            final_path, final_size, content_type = finalize_audio(out_wav, fmt, sample_rate_hz)
        except Exception as e:
            mark_failed(job_id, "processing_error", f"Failed to finalize audio: {e}")
            continue
        set_progress(job_id, 0.9)

        object_name = f"outputs/{job_id}/audio.{fmt}"
        with open(final_path, "rb") as f:
            m.put_object(MINIO_BUCKET, object_name, f, length=final_size, content_type=content_type)
        set_progress(job_id, 0.97)
        mark_succeeded(job_id, MINIO_BUCKET, object_name, content_type, final_size)
        print(f"[tts-worker] succeeded {job_id} -> {MINIO_BUCKET}/{object_name}")

        # Cleanup output file from shared volume (optional; keep if you want local caching)
        try:
            os.remove(out_wav)
        except Exception:
            pass
        if final_path != out_wav:
            try:
                os.remove(final_path)
            except Exception:
                pass


if __name__ == "__main__":
    main()


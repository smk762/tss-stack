from __future__ import annotations

import mimetypes
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


def guess_mime_type(path_or_name: Optional[str], fallback: Optional[str] = None) -> Optional[str]:
    if fallback:
        return fallback.split(";", 1)[0].strip().lower()
    if not path_or_name:
        return None
    guessed, _ = mimetypes.guess_type(path_or_name)
    return guessed.lower() if guessed else None


def sniff_audio_mime(data: bytes, fallback_name: Optional[str] = None, fallback: Optional[str] = None) -> Optional[str]:
    explicit = guess_mime_type(fallback_name, fallback=fallback)
    if explicit:
        return explicit
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"ID3") or data[:2] == b"\xff\xfb":
        return "audio/mpeg"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if len(data) > 12 and data[4:8] == b"ftyp":
        return "video/mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "audio/webm"
    return None


def suffix_for_mime(mime: Optional[str], default: str = ".bin") -> str:
    if not mime:
        return default
    mime = mime.lower()
    if "wav" in mime:
        return ".wav"
    if "mpeg" in mime or "mp3" in mime:
        return ".mp3"
    if "ogg" in mime:
        return ".ogg"
    if "webm" in mime:
        return ".webm"
    if "quicktime" in mime:
        return ".mov"
    if "mp4" in mime:
        return ".mp4"
    return default


def probe_duration_seconds(data: bytes, suffix: str = ".bin") -> Optional[float]:
    if not data:
        return None

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                tmp_path,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return None
        value = (proc.stdout or "").strip()
        if not value:
            return None
        return max(0.0, float(value))
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def safe_voice_path(voices_dir: str, voice_id: str) -> Path:
    root = Path(voices_dir).resolve()
    candidate = Path(voices_dir) / f"{voice_id}.wav"
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except Exception as exc:
        raise ValueError(f"voice_id must resolve under VOICES_DIR ({voices_dir})") from exc
    return resolved

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI


GATEWAY_ROOT = Path(__file__).resolve().parents[1]
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

from app.routers import provider


@pytest.fixture
def provider_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir()
    (voices_dir / "female.wav").write_bytes(b"RIFF1234WAVEtest")
    (voices_dir / "inara.wav").write_bytes(b"RIFF5678WAVEtest")

    monkeypatch.setattr(provider.config, "VOICES_DIR", str(voices_dir))
    monkeypatch.setattr(provider.config, "DEFAULT_VOICE_ID", "female")
    monkeypatch.setattr(provider.config, "DEFAULT_VOICE_LANGUAGE", "en-US")
    monkeypatch.setattr(provider.config, "TTS_OUTPUT_FORMATS", ["wav", "mp3", "ogg"])
    monkeypatch.setattr(provider.config, "MINIO_BUCKET", "artifacts")

    app = FastAPI()
    app.include_router(provider.router)
    return app

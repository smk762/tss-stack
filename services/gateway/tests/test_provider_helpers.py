from __future__ import annotations

import base64

import pytest
from fastapi import HTTPException

from app.db.job_store import JobRow
from app.media import safe_voice_path
from app.routers import provider


def make_row(**overrides: object) -> JobRow:
    values = {
        "id": "job-123",
        "type": "tts.synthesize",
        "status": "queued",
        "created_at": "2026-03-09T00:00:00+00:00",
        "started_at": None,
        "finished_at": None,
        "progress": 0.0,
        "error_code": None,
        "error_message": None,
        "error_details_json": None,
        "result_bucket": None,
        "result_object": None,
        "result_content_type": None,
        "result_bytes": None,
        "result_sha256": None,
        "owner_id": None,
        "params_json": None,
    }
    values.update(overrides)
    return JobRow(**values)


def test_safe_voice_path_rejects_traversal() -> None:
    try:
        safe_voice_path("/tmp/voices", "../secret")
    except ValueError:
        return
    raise AssertionError("Expected safe_voice_path to reject traversal.")


def test_decode_base64_audio_supports_data_urls() -> None:
    payload = base64.b64encode(b"RIFF1234WAVEtest").decode("ascii")
    raw, mime = provider._decode_base64_audio(f"data:audio/wav;base64,{payload}")

    assert raw == b"RIFF1234WAVEtest"
    assert mime == "audio/wav"


def test_normalize_language_for_engine_uses_primary_tag() -> None:
    assert provider._normalize_language_for_engine("en-US") == "en"
    assert provider._normalize_language_for_engine("pt_BR") == "pt"
    assert provider._normalize_language_for_engine("") is None


def test_provider_progress_pct_prefers_intermediate_progress() -> None:
    row = make_row(status="running", progress=0.78)

    assert provider._provider_progress_pct(row) == 78


def test_provider_progress_pct_marks_terminal_jobs_complete() -> None:
    row = make_row(status="failed", progress=0.12)

    assert provider._provider_progress_pct(row) == 100


def test_estimate_stt_confidence_penalizes_short_mismatched_audio() -> None:
    strong = provider._estimate_stt_confidence(
        text="this is a normal length transcription",
        duration_seconds=4.0,
        requested_language="en",
        detected_language="en",
    )
    weak = provider._estimate_stt_confidence(
        text="hi",
        duration_seconds=10.0,
        requested_language="en",
        detected_language="fr",
    )

    assert strong > weak


@pytest.mark.anyio
async def test_resolve_stt_input_rejects_invalid_local_minio_path() -> None:
    body = provider.STTJobCreateRequest(audio_url="http://localhost:9010/only-bucket")

    with pytest.raises(HTTPException) as exc_info:
        await provider._resolve_stt_input(body)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.anyio
async def test_wait_for_terminal_job_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider.config, "PROVIDER_WEBHOOK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(provider.config, "PROVIDER_WEBHOOK_MAX_WAIT_SECONDS", 0.02)
    monkeypatch.setattr(provider, "_require_job", lambda job_id, expected_type: make_row(status="queued"))

    with pytest.raises(TimeoutError):
        await provider._wait_for_terminal_job("job-123", "tts.synthesize")

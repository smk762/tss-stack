from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.db.job_store import JobRow
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


class FakeJobStore:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.rows: dict[str, JobRow] = {}

    def init(self) -> None:
        return None

    def create_job(self, job_id: str, job_type: str, owner_id: str | None, params: dict[str, object] | None = None) -> JobRow:
        row = make_row(id=job_id, type=job_type, status="queued", params_json=json.dumps(params or {}))
        self.created.append({"job_id": job_id, "job_type": job_type, "owner_id": owner_id, "params": params or {}})
        self.rows[job_id] = row
        return row

    def get_job(self, job_id: str) -> JobRow | None:
        return self.rows.get(job_id)


class FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def enqueue(self, queue_name: str, payload: dict[str, object]) -> None:
        self.calls.append((queue_name, payload))


class FakeMinioStore:
    def __init__(self) -> None:
        self.put_calls: list[tuple[str, bytes, str]] = []

    def ensure_bucket(self) -> None:
        return None

    def put_bytes(self, object_name: str, data: bytes, content_type: str) -> int:
        self.put_calls.append((object_name, data, content_type))
        return len(data)

    def presign_get(self, bucket: str, object_name: str, ttl_seconds: int) -> str:
        return f"https://example.test/{bucket}/{object_name}?ttl={ttl_seconds}"

    def get_object_content(self, bucket: str, object_name: str) -> str:
        return json.dumps({"text": "transcribed text", "language": "en"})


def test_list_voices_returns_presets(provider_app) -> None:
    client = TestClient(provider_app)

    response = client.get("/voices")

    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload["data"]] == ["female", "inara"]
    assert payload["data"][0]["sample_url"].endswith("/voices/female/sample")


def test_create_tts_job_enqueues_provider_job(provider_app, monkeypatch) -> None:
    store = FakeJobStore()
    queue = FakeQueue()
    monkeypatch.setattr(provider, "JobStore", lambda: store)
    monkeypatch.setattr(provider, "RedisQueue", lambda: queue)

    client = TestClient(provider_app)
    response = client.post(
        "/tts/jobs",
        json={
            "text": "Hello from tests",
            "voice_id": "inara",
            "language": "en-US",
            "format": "ogg",
            "speed": 1.1,
            "client_request_id": "req-tts-1",
        },
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["event_stream_url"].endswith(f"/tts/jobs/{payload['id']}/events")
    assert store.created[0]["job_type"] == "tts.synthesize"
    assert store.created[0]["params"]["language"] == "en"
    assert store.created[0]["params"]["provider_requested_language"] == "en-US"
    assert store.created[0]["params"]["client_request_id"] == "req-tts-1"
    assert queue.calls[0][0] == provider.config.QUEUE_TTS
    assert queue.calls[0][1]["params"]["output_format"] == "ogg"


def test_create_stt_job_uploads_input_and_enqueues_job(provider_app, monkeypatch) -> None:
    store = FakeJobStore()
    queue = FakeQueue()
    minio = FakeMinioStore()

    async def fake_resolve_stt_input(body):
        return b"OggSfake-audio", "audio/ogg", 2.5

    monkeypatch.setattr(provider, "JobStore", lambda: store)
    monkeypatch.setattr(provider, "RedisQueue", lambda: queue)
    monkeypatch.setattr(provider, "MinioStore", lambda: minio)
    monkeypatch.setattr(provider, "_resolve_stt_input", fake_resolve_stt_input)

    client = TestClient(provider_app)
    response = client.post(
        "/stt/jobs",
        json={"audio_base64": "ignored", "language": "en-US", "client_request_id": "req-stt-1"},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["event_stream_url"].endswith(f"/stt/jobs/{payload['id']}/events")
    assert minio.put_calls[0][0].startswith("uploads/")
    assert minio.put_calls[0][2] == "audio/ogg"
    assert store.created[0]["params"]["provider_input_duration_seconds"] == 2.5
    assert store.created[0]["params"]["client_request_id"] == "req-stt-1"
    assert queue.calls[0][0] == provider.config.QUEUE_WHISPER


def test_get_tts_job_returns_completed_result(provider_app, monkeypatch) -> None:
    row = make_row(
        status="succeeded",
        started_at="2026-03-09T00:00:01+00:00",
        finished_at="2026-03-09T00:00:02+00:00",
        result_bucket="artifacts",
        result_object="outputs/job-123/audio.ogg",
        result_content_type="audio/ogg",
        params_json=json.dumps({"voice_id": "inara", "provider_requested_language": "en-US"}),
    )

    monkeypatch.setattr(provider, "_require_job", lambda job_id, expected_type: row)
    monkeypatch.setattr(provider, "MinioStore", lambda: FakeMinioStore())
    monkeypatch.setattr(provider, "_load_tts_duration_seconds", lambda current_row: 1.75)

    client = TestClient(provider_app)
    response = client.get("/tts/jobs/job-123")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["progress_pct"] == 100
    assert payload["created_at"] == "2026-03-09T00:00:00+00:00"
    assert payload["started_at"] == "2026-03-09T00:00:01+00:00"
    assert payload["completed_at"] == "2026-03-09T00:00:02+00:00"
    assert payload["result"]["format"] == "ogg"
    assert payload["result"]["language"] == "en-US"


def test_get_stt_job_returns_completed_result(provider_app, monkeypatch) -> None:
    row = make_row(
        type="stt.transcribe",
        status="succeeded",
        started_at="2026-03-09T00:00:01+00:00",
        finished_at="2026-03-09T00:00:03+00:00",
        result_bucket="artifacts",
        result_object="results/job-123/transcription.json",
        result_content_type="application/json",
        params_json=json.dumps({"provider_requested_language": "en-US", "provider_input_duration_seconds": 3.2}),
    )

    monkeypatch.setattr(provider, "_require_job", lambda job_id, expected_type: row)
    monkeypatch.setattr(provider, "MinioStore", lambda: FakeMinioStore())

    client = TestClient(provider_app)
    response = client.get("/stt/jobs/job-123")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["completed_at"] == "2026-03-09T00:00:03+00:00"
    assert payload["result"]["text"] == "transcribed text"
    assert payload["result"]["duration_seconds"] == 3.2
    assert 0.0 < payload["result"]["confidence"] <= 1.0


def test_get_tts_job_preserves_cancelled_status(provider_app, monkeypatch) -> None:
    row = make_row(
        status="cancelled",
        started_at="2026-03-09T00:00:01+00:00",
        finished_at="2026-03-09T00:00:02+00:00",
        error_message=None,
    )

    monkeypatch.setattr(provider, "_require_job", lambda job_id, expected_type: row)

    client = TestClient(provider_app)
    response = client.get("/tts/jobs/job-123")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "cancelled"
    assert payload["error_message"] == "Job cancelled."
    assert payload["completed_at"] == "2026-03-09T00:00:02+00:00"


def test_tts_sse_stream_emits_job_lifecycle_events(provider_app, monkeypatch) -> None:
    rows = iter(
        [
            make_row(status="queued", progress=0.0),
            make_row(status="queued", progress=0.0),
            make_row(status="running", progress=0.45, started_at="2026-03-09T00:00:01+00:00"),
            make_row(
                status="succeeded",
                progress=1.0,
                started_at="2026-03-09T00:00:01+00:00",
                finished_at="2026-03-09T00:00:02+00:00",
            ),
        ]
    )
    last_row = make_row(status="succeeded", progress=1.0)

    def fake_require_job(job_id: str, expected_type: str) -> JobRow:
        nonlocal last_row
        try:
            last_row = next(rows)
        except StopIteration:
            pass
        return last_row

    monkeypatch.setattr(provider, "_require_job", fake_require_job)
    monkeypatch.setattr(provider.config, "PROVIDER_WEBHOOK_POLL_INTERVAL_SECONDS", 0.01)

    client = TestClient(provider_app)
    with client.stream("GET", "/tts/jobs/job-123/events") as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: job.status" in body
    assert '"progress_pct":0' in body
    assert '"progress_pct":45' in body
    assert "event: job.done" in body
    assert '"status":"completed"' in body


def test_stt_sse_stream_emits_cancelled_terminal_event(provider_app, monkeypatch) -> None:
    rows = iter(
        [
            make_row(type="stt.transcribe", status="queued", progress=0.0),
            make_row(type="stt.transcribe", status="queued", progress=0.0),
            make_row(type="stt.transcribe", status="cancelled", progress=1.0, finished_at="2026-03-09T00:00:02+00:00"),
        ]
    )
    last_row = make_row(type="stt.transcribe", status="cancelled", progress=1.0)

    def fake_require_job(job_id: str, expected_type: str) -> JobRow:
        nonlocal last_row
        try:
            last_row = next(rows)
        except StopIteration:
            pass
        return last_row

    monkeypatch.setattr(provider, "_require_job", fake_require_job)
    monkeypatch.setattr(provider.config, "PROVIDER_WEBHOOK_POLL_INTERVAL_SECONDS", 0.01)

    client = TestClient(provider_app)
    with client.stream("GET", "/stt/jobs/job-123/events") as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert "event: job.error" in body
    assert '"status":"cancelled"' in body
    assert '"error_message":"Job cancelled."' in body

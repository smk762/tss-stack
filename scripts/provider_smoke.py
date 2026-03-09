#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
BASE_URL = os.getenv("PROVIDER_BASE_URL", "http://localhost:9001").rstrip("/")
VOICE_ID = os.getenv("PROVIDER_SMOKE_VOICE_ID", "female")
TEST_AUDIO_PATH = Path(
    os.getenv("PROVIDER_SMOKE_AUDIO", str(ROOT_DIR / "test-audio" / "thank_you_mono.wav"))
)
HEALTH_TIMEOUT_SECONDS = float(os.getenv("PROVIDER_SMOKE_HEALTH_TIMEOUT_SECONDS", "60"))
JOB_TIMEOUT_SECONDS = float(os.getenv("PROVIDER_SMOKE_JOB_TIMEOUT_SECONDS", "180"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("PROVIDER_SMOKE_HTTP_TIMEOUT_SECONDS", "30"))
POLL_INTERVAL_SECONDS = float(os.getenv("PROVIDER_SMOKE_POLL_INTERVAL_SECONDS", "1"))


class SmokeTestError(RuntimeError):
    pass


def _request(
    method: str,
    path_or_url: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, str], bytes]:
    url = path_or_url if path_or_url.startswith("http") else f"{BASE_URL}{path_or_url}"
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)

    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SmokeTestError(f"{method} {url} failed with HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SmokeTestError(f"{method} {url} failed: {exc}") from exc


def _request_json(
    method: str,
    path_or_url: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    status, response_headers, body = _request(method, path_or_url, payload=payload, headers=headers, timeout=timeout)
    if status < 200 or status >= 300:
        raise SmokeTestError(f"Unexpected HTTP status {status} for {path_or_url}")
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise SmokeTestError(f"Expected JSON from {path_or_url}, got: {body[:200]!r}") from exc


def _wait_for_health() -> None:
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            health = _request_json("GET", "/health", timeout=5)
            if health.get("ok") is True:
                return
        except SmokeTestError:
            pass
        time.sleep(1)
    raise SmokeTestError(f"Gateway health check did not become ready within {HEALTH_TIMEOUT_SECONDS:.0f}s")


def _poll_job(job_path: str) -> dict[str, Any]:
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        payload = _request_json("GET", job_path)
        status = payload.get("status")
        if status in {"completed", "failed", "cancelled", "dead_letter"}:
            return payload
        time.sleep(POLL_INTERVAL_SECONDS)
    raise SmokeTestError(f"Timed out polling {job_path} after {JOB_TIMEOUT_SECONDS:.0f}s")


def _stream_events(event_stream_url: str) -> list[tuple[str, dict[str, Any]]]:
    request = urllib.request.Request(
        event_stream_url,
        headers={"Accept": "text/event-stream"},
        method="GET",
    )
    events: list[tuple[str, dict[str, Any]]] = []
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS

    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get("Content-Type", "")
            if "text/event-stream" not in content_type:
                raise SmokeTestError(
                    f"Expected text/event-stream from {event_stream_url}, got {content_type or 'missing content type'}"
                )

            event_name = "message"
            data_lines: list[str] = []

            while time.monotonic() < deadline:
                raw_line = response.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8").rstrip("\r\n")
                if not line:
                    if data_lines:
                        payload = json.loads("\n".join(data_lines))
                        events.append((event_name, payload))
                        status = payload.get("status")
                        if status in {"completed", "failed", "cancelled", "dead_letter"}:
                            return events
                    event_name = "message"
                    data_lines = []
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_name = line.partition(":")[2].strip() or "message"
                    continue
                if line.startswith("data:"):
                    data_lines.append(line.partition(":")[2].lstrip())

    except urllib.error.URLError as exc:
        raise SmokeTestError(f"Failed reading SSE stream {event_stream_url}: {exc}") from exc

    if not events:
        raise SmokeTestError(f"SSE stream {event_stream_url} produced no events")
    return events


def _download_audio(audio_url: str) -> bytes:
    _, _, body = _request("GET", audio_url, headers={"Accept": "*/*"})
    if not body:
        raise SmokeTestError(f"Downloaded empty audio payload from {audio_url}")
    return body


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeTestError(message)


def _run_tts_smoke() -> None:
    accepted = _request_json(
        "POST",
        "/tts/jobs",
        payload={
            "text": "Provider smoke test from tss-stack.",
            "voice_id": VOICE_ID,
            "language": "en-US",
            "format": "mp3",
            "client_request_id": "smoke-tts-1",
        },
    )
    job_id = accepted["id"]
    event_stream_url = accepted.get("event_stream_url")
    _assert(bool(event_stream_url), "TTS create response did not include event_stream_url")

    events = _stream_events(str(event_stream_url))
    final_status = events[-1][1].get("status")
    _assert(final_status == "completed", f"TTS SSE ended in unexpected status: {final_status}")
    _assert(events[-1][0] == "job.done", f"TTS final SSE event type was {events[-1][0]!r}, expected 'job.done'")

    job = _poll_job(f"/tts/jobs/{job_id}")
    _assert(job.get("status") == "completed", f"TTS polling returned unexpected status: {job.get('status')}")
    _assert(job.get("created_at"), "TTS status response missing created_at")
    _assert(job.get("completed_at"), "TTS status response missing completed_at")

    result = job.get("result") or {}
    audio = result.get("audio") or {}
    audio_url = audio.get("url")
    _assert(bool(audio_url), "TTS result missing downloadable audio URL")
    audio_bytes = _download_audio(str(audio_url))
    _assert(len(audio_bytes) > 128, "Downloaded TTS audio was unexpectedly small")


def _run_stt_smoke() -> None:
    if not TEST_AUDIO_PATH.is_file():
        raise SmokeTestError(f"STT smoke fixture not found: {TEST_AUDIO_PATH}")
    audio_b64 = base64.b64encode(TEST_AUDIO_PATH.read_bytes()).decode("ascii")

    accepted = _request_json(
        "POST",
        "/stt/jobs",
        payload={
            "audio_base64": audio_b64,
            "language": "en-US",
            "client_request_id": "smoke-stt-1",
        },
    )
    job_id = accepted["id"]
    event_stream_url = accepted.get("event_stream_url")
    _assert(bool(event_stream_url), "STT create response did not include event_stream_url")

    events = _stream_events(str(event_stream_url))
    final_status = events[-1][1].get("status")
    _assert(final_status == "completed", f"STT SSE ended in unexpected status: {final_status}")
    _assert(events[-1][0] == "job.done", f"STT final SSE event type was {events[-1][0]!r}, expected 'job.done'")

    job = _poll_job(f"/stt/jobs/{job_id}")
    _assert(job.get("status") == "completed", f"STT polling returned unexpected status: {job.get('status')}")
    _assert(job.get("created_at"), "STT status response missing created_at")
    _assert(job.get("completed_at"), "STT status response missing completed_at")

    result = job.get("result") or {}
    text = str(result.get("text") or "").strip()
    _assert(bool(text), "STT result text was empty")
    confidence = result.get("confidence")
    _assert(isinstance(confidence, (float, int)), "STT result confidence missing or invalid")


def main() -> int:
    try:
        print(f"Waiting for gateway at {BASE_URL} ...")
        _wait_for_health()

        voices = _request_json("GET", "/voices")
        voice_ids = [item.get("id") for item in voices.get("data", [])]
        _assert(VOICE_ID in voice_ids, f"Configured smoke voice {VOICE_ID!r} not found in /voices: {voice_ids}")

        print("Running TTS provider smoke test ...")
        _run_tts_smoke()

        print("Running STT provider smoke test ...")
        _run_stt_smoke()

        print("Provider smoke test passed.")
        return 0
    except SmokeTestError as exc:
        print(f"Provider smoke test failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

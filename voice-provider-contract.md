# Voice Provider Contract

This document defines the preferred async-only external XTTS/STT gateway
contract for `fastapi-prod-skeleton`.

Machine-readable contract:
- `docs/private/voice-provider-openapi.yaml`

## Goal

The app should integrate with a stable voice gateway, not with raw engine
endpoints directly, and it should treat TTS/STT as background jobs rather than
blocking request/response calls.

That means the separate voice stack should hide engine-specific details like:
- XTTS `speaker_wav` vs speaker aliases
- XTTS local output file paths
- Whisper multipart upload requirements
- engine-specific query params or response shapes

## Expected Endpoints

- `GET /voices`
- `POST /tts/jobs`
- `GET /tts/jobs/{job_id}`
- `GET /tts/jobs/{job_id}/events`
- `POST /stt/jobs`
- `GET /stt/jobs/{job_id}`
- `GET /stt/jobs/{job_id}/events`

## Required Behavior

### `GET /voices`

Return:
- `data[]` with `id`, `name`, `language`, `gender`, `sample_url`

This lets the app expose stable voice presets and validate `voice_id`.

### `POST /tts/jobs`

Accept:
- `text`
- `voice_id`
- `language`
- `speed`
- `format`

Return immediately with:
- `id`
- `status`
- optional `estimated_wait_seconds`
- optional `queue_position`
- optional `cost_gems`
- optional `event_stream_url`

The gateway must not wait for synthesis completion here.

### `GET /tts/jobs/{job_id}`

Return:
- `id`
- `status`
- `progress_pct`
- optional queue metadata
- optional `error_message`
- optional timestamps like `created_at`, `started_at`, `completed_at`
- `result` only when completed

When completed, `result` must include:
- `audio.url` preferred
- `audio.base64` allowed
- `duration_seconds`
- `format`

Important:
- Returning only a server-local path like `/output/foo.wav` is not enough.
- The gateway must either publish a usable URL or return base64 audio.

### `GET /tts/jobs/{job_id}/events`

This should expose an SSE stream for lifecycle updates so clients can layer
realtime UI on top of the async job model.

Recommended event types:
- `job.status`
- `job.done`
- `job.error`

### `POST /stt/jobs`

Accept:
- `audio_url` or `audio_base64`
- optional `language`

Return immediately with:
- `id`
- `status`
- optional `estimated_wait_seconds`
- optional `queue_position`
- optional `event_stream_url`

### `GET /stt/jobs/{job_id}`

Return:
- `id`
- `status`
- `progress_pct`
- optional queue metadata
- optional `error_message`
- optional timestamps like `created_at`, `started_at`, `completed_at`
- `result` only when completed

When completed, `result` must include:
- `text`
- `language_detected`
- `confidence`
- `duration_seconds`

Important:
- If Whisper only accepts multipart uploads, the gateway must perform the
  URL-download or base64 decode step internally.

### `GET /stt/jobs/{job_id}/events`

This should expose an SSE stream for lifecycle updates so clients can layer
realtime UI on top of the async job model.

## Mapping To Your Current Stack

For `tss-stack`, the best place to implement this is the gateway layer
described in [smk762/tss-stack](https://github.com/smk762/tss-stack).

Recommended mapping:
- gateway `GET /voices` -> XTTS voice registry / local voice config
- gateway `POST /tts/jobs` -> enqueue XTTS job
- gateway `GET /tts/jobs/{job_id}` -> read TTS job state/result
- gateway `GET /tts/jobs/{job_id}/events` -> stream TTS job updates
- gateway `POST /stt/jobs` -> enqueue Whisper job
- gateway `GET /stt/jobs/{job_id}` -> read STT job state/result
- gateway `GET /stt/jobs/{job_id}/events` -> stream STT job updates

## Current Mismatch To Avoid

Your current raw services are close, but not yet ideal as direct app-facing
contracts:
- Voice Glue `POST /tts_to_file` is synchronous and file-oriented.
- Whisper `POST /asr` is synchronous and expects multipart file upload.

The gateway should normalize both into async job submission plus polling.

## Realtime Overlay

SSE should be the baseline realtime transport because it is simple to test and
easy to put behind reverse proxies.

WebSocket support is still a good optional enhancement, but it should be an
overlay on top of the same underlying job state machine rather than the only
way to observe progress.

Recommended model:
1. Submit a job.
2. Return a `job_id` immediately.
3. Optionally subscribe to SSE for progress.
4. Poll `GET /.../jobs/{job_id}` as the fallback path.

## Important Note

This is the desired provider contract for the voice stack. The current
`fastapi-prod-skeleton` app code still exposes synchronous `/v1/voice/tts` and
`/v1/voice/stt` routes today, so a later app change is still needed to align
the application with this async-only provider contract end-to-end.

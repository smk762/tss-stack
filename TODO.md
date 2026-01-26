# XTTS + Whisper v3: Local Dev → Multi‑User Architecture Blueprint

This document captures a **future‑proof architecture** for running **Whisper v3 (STT)** and **XTTS v2 (TTS)** locally during development, while ensuring that scaling to **multi‑user hosted deployment** later requires *mostly hardware/model swaps*, not rewrites.

The guiding principle: **lock stable contracts now, change engines later**.

## Streaming TTS (future)

For the concrete implementation checklist + staged rollout to add a **real streaming path** (so `stream_chunk_ms` can be enabled honestly), see `STREAMING_TODO.md`.

## Phase 7 progress (controls)

- Implemented **raw engine temperature** as `tts.controls.engine_temperature` (engine-dependent; best-effort passthrough).
- Other “excluded for now” knobs remain excluded unless explicitly added later.

---

## 1. Lock the Contract: Stable API Between UI ↔ Backend

Treat the UI as a thin client that talks to a **versioned, engine‑agnostic API**.

### Core Endpoints (minimum set)

* `POST /v1/stt/transcribe`
  Audio → text

* `POST /v1/tts/synthesize`
  Text + voice parameters → audio

* `GET /v1/voices`
  Available voice presets / speaker IDs

* `GET /v1/capabilities`
  What sliders & ranges this deployment supports

* `POST /v1/sessions` *(optional)*
  Conversation / assistant state (future‑proofing)

**Rule:** if these schemas never change, you can freely swap:

* Whisper runtime (faster‑whisper, whisper.cpp, cloud later)
* TTS engine (XTTS → something else)
* Hardware (single GPU → multi GPU → CPU fallback)

### Why `/capabilities` Matters

The UI **must not assume sliders exist**.

Instead, it renders controls dynamically based on:

```json
{
  "tts": {
    "speed": {"min": 0.8, "max": 1.3, "default": 1.0},
    "pitch_semitones": {"min": -3, "max": 3},
    "formant_shift": {"min": -1.0, "max": 1.0},
    "energy": {"min": 0.0, "max": 1.0}
  }
}
```

Later, on hosted infra, you can:

* clamp ranges
* disable expensive controls
* enable more controls on stronger GPUs

**No UI changes required.**

---

## 2. Neutral Voice Parameter Model (UI‑Safe)

Do **not** expose XTTS‑specific internals directly. Define a **neutral voice control schema**:

* `voice_id` – preset / speaker embedding reference
* `speed` – cadence / speaking rate
* `pitch_semitones` – post‑processing pitch shift
* `formant_shift` – post‑processing formant shift ("gender tilt")
* `energy` – expressiveness / randomness (mapped per engine)
* `pause_ms` – silence insertion / punctuation padding
* `stability` – optional (engine dependent)

XTTS today may implement some natively and others via DSP. A future TTS engine may do the opposite.

**The UI never knows or cares.**

---

## 3. Service Split by Function (Not Convenience)

Even in local dev, keep **logical separation**:

* **api‑gateway** (FastAPI)

  * auth (future)
  * rate limits
  * job submission
  * job status

* **stt‑worker**

  * Whisper / faster‑whisper

* **tts‑worker**

  * XTTS v2
  * DSP post‑processing (pitch/formant)

* **queue**

  * Redis (simple & sufficient)

* **storage** *(optional but recommended)*

  * MinIO / filesystem for audio artifacts

* **db**

  * SQLite locally
  * Postgres when hosted

Containers can all live in one compose file locally; later they scale independently.

---

## 4. Make Everything Async Now

Even for local dev, implement **job‑based execution**.

### Pattern

* `POST /v1/tts/synthesize` → `{ job_id }`
* `GET /v1/jobs/{job_id}` → status + result URL

Optional later:

* WebSocket / SSE for progress
* streaming audio chunks

### Why This Matters

You *will* need this later for:

* backpressure
* fairness
* retries
* per‑user quotas
* GPU scheduling

Doing it now avoids a painful refactor later.

---

## 5. GPU Strategy That Scales by Swapping Hardware

### Local Dev (single GPU)

* **TTS on GPU** (XTTS v2)
* **STT** via faster‑whisper:

  * CPU by default
  * GPU only if VRAM allows

Concurrency: **1 job per worker** to avoid VRAM thrash.

### Hosted / Multi‑User

Create **separate worker pools**:

* `tts-worker-gpu`
* `stt-worker-cpu`
* optional `stt-worker-gpu` for low‑latency tier

Routing rules:

* TTS → GPU pool
* STT → CPU pool unless low latency required

Scaling becomes infra‑only.

---

## 6. Voice Presets + “Gender” Slider (Without Lying)

There is no real "gender" knob in XTTS.

Correct UX approach:

* **Voice presets** (male/female/neutral styles)
* Optional **tilt slider** implemented as:

  * mild pitch shift
  * mild formant shift

Presets do the real work. Sliders add fine control.

Future‑proofs nicely across engines.

---

## 7. Stateless Workers

Model containers should be **stateless**:

Allowed:

* model weights cache
* temp files

All durable state goes to:

* DB (users, jobs, voices)
* object storage (audio, reference clips)
* vector DB later (if you add RAG)

This makes rolling upgrades and scaling trivial.

---

## 8. What To Implement *Now*

If you do nothing else, do these:

1. Define OpenAPI schemas for `/v1/stt/transcribe` and `/v1/tts/synthesize` using neutral params.
2. Implement `/v1/capabilities` and make the UI render sliders dynamically.
3. Implement job queue pattern (even if jobs complete instantly).
4. Centralize config:

   * model IDs
   * device preference (cpu/cuda)
   * max concurrency
   * feature flags
5. Put DSP post‑processing behind a toggle.

If all of the above exists, **hosting later is mostly infrastructure work**.

---

## Summary

Designing for scale early does *not* mean over‑engineering.

It means:

* stable contracts
* neutral parameter models
* async execution
* stateless workers

With this in place, you can:

> swap models, swap GPUs, add users — without touching the UI or core logic.

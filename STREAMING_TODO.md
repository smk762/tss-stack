# Streaming TTS Path — TODO (Future Work)

This doc tracks the work needed to support **true streaming audio** for `/v1/tts/synthesize` so that Stage 3 controls like `stream_chunk_ms` are *actually applied* (and not placebo).

## Goals

- **Low perceived latency**: start playback while synthesis is still running.
- **Cancelable**: stop generation mid-stream and reclaim GPU/CPU.
- **Artifact-free audio**: no clicks/pops at chunk boundaries (overlap/crossfade).
- **Contract-driven**: keep UI engine-agnostic; all toggles via `/v1/capabilities`.

## Non-goals (initial)

- Perfect prosody alignment across chunk boundaries on all engines.
- Multi-speaker mixing.
- Per-phoneme/token emphasis controls beyond safe heuristics.

---

## 1) Contract / API changes

- **Add streaming endpoints**
  - `POST /v1/tts/synthesize:stream` (or `POST /v1/tts/stream`) → streams audio chunks
  - Decide transport:
    - **Option A**: HTTP chunked transfer (`audio/wav` or `audio/mpeg`)
    - **Option B**: WebSocket (binary frames)
    - **Option C**: SSE for metadata + separate binary fetch (usually awkward)

- **Define response framing**
  - If HTTP chunked audio:
    - Choose container: `wav` (harder to stream correctly), `mp3` (easy), `ogg/opus` (best for streaming if allowed)
  - If WS:
    - Define `{seq, is_final, content_type, bytes}` envelope

- **Capabilities**
  - Flip `tts.controls.stream_chunk_ms.enabled=true` only when streaming path exists.
  - Add `tts.streaming` feature flag(s), e.g.:
    - `tts.streaming.enabled`
    - `tts.streaming.transports: ["http_chunked", "websocket"]`
    - `tts.streaming.output_formats: ["mp3","opus"]`

- **Job model**
  - Decide if streaming is still job-based:
    - **Option A**: streaming request returns bytes immediately (no job id)
    - **Option B**: still create a job id for auditing + cancellation; stream is the “result body”

---

## 2) Gateway changes (`services/gateway`)

- **Router**
  - Add streaming route next to `POST /v1/tts/synthesize`.
  - Enforce the same request schema (`TtsSynthesizeRequest`) + validate `stream_chunk_ms` bounds.

- **Queue / worker protocol**
  - Current protocol is “enqueue job → worker writes artifact → job succeeded”.
  - Streaming needs either:
    - **Direct worker connection** from gateway (gateway proxies stream), or
    - A message-bus stream (Redis streams / pubsub) with chunk payloads, or
    - Worker writes chunk objects to MinIO and gateway polls (higher latency).

- **Cancellation**
  - Map `DELETE /v1/jobs/{job_id}` to a **worker cancel signal** (Redis pubsub or a “cancelled” set).

- **Result storage**
  - For streaming, decide whether to also persist a final artifact (optional):
    - Store full file after stream completes, or
    - Store only if requested (`persist=true`), or
    - Store nothing (pure stream).

---

## 3) TTS Worker changes (`services/tts-worker`)

- **Engine output streaming**
  - Current XTTS endpoint is `tts_to_file` → not streaming.
  - Options:
    - If XTTS server supports streaming endpoints, integrate them.
    - Otherwise implement “pseudo-streaming”:
      - generate ahead (still file-based) and stream chunks out of the WAV (low benefit).

- **Chunking implementation**
  - `stream_chunk_ms` should map to:
    - chunk size in samples = `sample_rate * stream_chunk_ms / 1000`
    - envelope at edges (fade in/out)
    - overlap (e.g. 10–30ms) + crossfade to avoid clicks

- **DSP on streaming**
  - Decide what runs per-chunk vs over the full signal:
    - Per-chunk safe: EQ, gain, mild compression (with careful state), breathiness mixing (with deterministic noise seed)
    - Risky without state: heavy dynamics, long-window filters
  - Add a streaming-safe DSP mode for `latency_mode=realtime`.

- **Stateful filters**
  - If using ffmpeg per chunk, you lose filter state unless you keep a persistent process.
  - Prefer:
    - Persistent DSP process (ffmpeg as a subprocess with stdin/stdout), or
    - Python DSP pipeline with maintained state.

- **Backpressure**
  - Bound queue depth and memory used by chunk buffers.
  - If consumer (gateway/client) is slow, worker should pause or drop according to policy.

---

## 5) Testing / observability

- **Correctness**
  - Unit test chunk boundary stitching (no discontinuity spike).
  - Validate duration and ordering.
  - Verify cancellation halts generation within N ms.

- **Performance**
  - Measure: time-to-first-audio, CPU usage, memory, network overhead.
  - Benchmark with `stream_chunk_ms`: 60 / 120 / 240 ms.

- **Tracing**
  - Add request id propagation from gateway → worker → glue.
  - Log chunk seq + timings at debug level (sampled).

---

## 6) Rollout plan (recommended)

- **Phase 1**: Streaming endpoint that streams a pre-generated WAV (minimal changes; validates plumbing only).
- **Phase 2**: True engine streaming (or as close as XTTS allows).
- **Phase 3**: Streaming DSP pipeline (stateful), enable `stream_chunk_ms`.


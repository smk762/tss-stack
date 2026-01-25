# tss-stack

Local-first stack for voice + retrieval, with **engine-agnostic contracts** so you can swap runtimes later.

## Services (today)

- **`gateway`**: FastAPI contract service (job-based) on `:9001`
  - `/v1/stt/*`, `/v1/tts/*`, `/v1/jobs/*`, `/v1/capabilities`
- **`redis`**: job queue
- **`minio`**: artifact storage (S3-compatible) + presigned `result_url`s
- **`stt-worker`**: scaffolded worker (no engine wired yet)
- **`tts-worker`**: XTTS job worker (calls `xtts` HTTP API, writes audio to MinIO)
- **`xtts`**: XTTS engine server (defaults to CPU unless you opt into GPU)
- **`xtts-glue`**: Voice Glue API (qdrant helpers + self-lora scaffolding; no playback side-effects)
- **`qdrant`**: vector DB (existing)

## Docker Compose profiles

By default, `docker compose up` runs a **CPU-safe baseline** that works without NVIDIA runtime.

### Baseline (no profiles)

```bash
docker compose up -d --build
```

- Gateway: `http://localhost:9001/health`
- MinIO S3 API: `http://localhost:9010`
- MinIO console: `http://localhost:9011`

Note: the gateway returns presigned URLs signed for `localhost:9010` by default via `MINIO_PRESIGN_ENDPOINT`.

## Scaling strategy (minimize friction)

The key trick is to keep **stable HTTP contracts** at the edges, and swap engines behind them:

- **UI ↔ gateway**: stable `/v1/*` API (OpenAPI in `contracts/openapi.v1.yaml`)
- **gateway ↔ workers**: stable job payloads over Redis + stable artifact format in MinIO
- **LLM**: treat it as a swappable **OpenAI-compatible endpoint** (typically remote behind Cloudflare Access)

So “scaling up” is mostly:
- increase worker replicas
- move SQLite → Postgres (gateway job store)
- move MinIO → S3
- point clients at a different OpenAI-compatible LLM base URL without rewriting client code


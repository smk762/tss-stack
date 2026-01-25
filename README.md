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

### Local LLM (CPU, llama.cpp)

Runs an **OpenAI-compatible** HTTP server on `http://localhost:8000`.

1) Put a GGUF model into the `llm_models` volume (or copy into it), and set:
- `LLM_GGUF=model.gguf`

2) Start:

```bash
docker compose --profile llm up -d
```

### Local LLM (GPU, llama.cpp CUDA)

Best for GPUs like a GTX 1070 **if the image is compatible with your driver**.

```bash
docker compose --profile llm-gpu up -d
```

Knobs:
- `LLM_GPU_LAYERS` (default `999` = “offload as much as possible”)

### Production-style LLM (GPU, vLLM)

On newer GPU servers, enable the `gpu` profile:

```bash
docker compose --profile gpu up -d
```

Notes:
- `llm` (vLLM) also binds `:8000`. Don’t run `--profile llm`/`llm-gpu` at the same time unless you change ports.

## Scaling strategy (minimize friction)

The key trick is to keep **stable HTTP contracts** at the edges, and swap engines behind them:

- **UI ↔ gateway**: stable `/v1/*` API (OpenAPI in `contracts/openapi.v1.yaml`)
- **gateway ↔ workers**: stable job payloads over Redis + stable artifact format in MinIO
- **LLM**: treat it as a swappable **OpenAI-compatible endpoint**
  - dev: `llm-local` (llama.cpp) on CPU or GPU
  - prod: `llm` (vLLM) on stronger GPUs

So “scaling up” is mostly:
- increase worker replicas
- move SQLite → Postgres (gateway job store)
- move MinIO → S3
- swap `llm-local` → `llm` (vLLM) without rewriting client code


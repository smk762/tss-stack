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
- **`xtts-glue`**: Voice Glue API (no playback side-effects)

## Docker Compose profiles

By default, `docker compose up` runs a **CPU-safe baseline** that works without NVIDIA runtime.

### Baseline (no profiles)

```bash
docker compose up -d --build
```

- Gateway: `http://localhost:9001/health`
- MinIO S3 API: `http://localhost:9010`
- MinIO console: `http://localhost:9011`
- Gateway Dev UI (TTS + STT upload/mic): `http://localhost:9001/ui` (mic capture requires HTTPS or localhost)

Required secrets (no insecure defaults):

```bash
export MINIO_ROOT_USER=<choose-unique-user>
export MINIO_ROOT_PASSWORD=<choose-strong-password>
```

Note: the gateway returns presigned URLs signed for `localhost:9010` by default via `MINIO_PRESIGN_ENDPOINT`.

### Optional: Snapcast announcements (profile + env flag)

Snapcast is bundled but **disabled by default**.

- **Enable Snapserver** (adds the `snapserver` service):

```bash
export COMPOSE_PROFILES=snapcast
docker compose up -d --build
```

- **Enable announce endpoint in `xtts-glue`** (side-effectful playback):

```bash
export SNAPCAST_ENABLED=1
export COMPOSE_PROFILES=snapcast
docker compose up -d --build
```

Then you can broadcast an update over Snapcast via `xtts-glue`:

```bash
curl -sS http://localhost:9000/announce \
  -H 'Content-Type: application/json' \
  -d '{"text":"Status update. All systems online.","speaker":"female"}'
```

Snapcast ports (host):
- `1704` TCP/UDP (audio)
- `1705` TCP (control)
- `1780` TCP (JSON-RPC)

### Cloudflare Access (Zero Trust)

- Configure a Cloudflare Access application that fronts the gateway (and glue if exposed).  
- Ensure your reverse proxy forwards the `Cf-Access-Jwt-Assertion` header to the services; the OpenAPI contract now declares this as the auth scheme.  
- Lock down host ports as needed (e.g., only expose via your Access tunnel and avoid publishing Redis/MinIO directly).  
- Clients must present the Access JWT on every request; browser-based clients get it from the Access login flow, headless clients should supply the token in that header.  

## Scaling strategy (minimize friction)

The key trick is to keep **stable HTTP contracts** at the edges, and swap engines behind them:

- **UI ↔ gateway**: stable `/v1/*` API (OpenAPI in `contracts/openapi.v1.yaml`)
- **gateway ↔ workers**: stable job payloads over Redis + stable artifact format in MinIO

So “scaling up” is mostly:
- increase worker replicas
- move SQLite → Postgres (gateway job store)
- move MinIO → S3

## CI/CD

- GitHub Actions use the shared reusable workflows from [`smk762/gha-docker-shared-ci`](https://github.com/smk762/gha-docker-shared-ci) to lint Dockerfiles, validate compose, build, and scan images.
- Configure repository secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (Docker Hub access token). Set repository variable `DOCKERHUB_ORG` to the Docker Hub org/user used for publishing (defaults to the GitHub org/user).
- CI runs on pull requests and pushes to `main`; release builds push images on `main` and `v*` tags for: `gateway`, `xtts`, `xtts-glue`, `tts-worker`, `whisper-worker`.

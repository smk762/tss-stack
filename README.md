# tss-stack

Local-first stack for voice + retrieval, with **engine-agnostic contracts** so you can swap runtimes later.

## Services (today)

- **`gateway`**: FastAPI contract service (job-based) on `:9001`
  - Internal async API: `/v1/stt/*`, `/v1/tts/*`, `/v1/jobs/*`, `/v1/capabilities`, `/v1/voices`
  - External async provider API: `/voices`, `/tts/jobs`, `/tts/jobs/{job_id}`, `/tts/jobs/{job_id}/events`, `/stt/jobs`, `/stt/jobs/{job_id}`, `/stt/jobs/{job_id}/events`
- **`redis`**: job queue
- **`minio`**: artifact storage (S3-compatible) + presigned `result_url`s
- **`whisper-worker`**: Whisper/STT job worker (transcribes queued jobs and writes artifacts to MinIO)
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
- Provider contract surface: `http://localhost:9001/voices`, `http://localhost:9001/tts/jobs`, `http://localhost:9001/tts/jobs/{job_id}`, `http://localhost:9001/tts/jobs/{job_id}/events`, `http://localhost:9001/stt/jobs`, `http://localhost:9001/stt/jobs/{job_id}`, `http://localhost:9001/stt/jobs/{job_id}/events`
- Provider voice presets come from `./voices/presets/*.wav`; reference clips live under `./voices/samples/`

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
- **External app ↔ gateway**: async provider API aligned with `voice-provider-openapi.yaml`
- **gateway ↔ workers**: stable job payloads over Redis + stable artifact format in MinIO

So “scaling up” is mostly:
- increase worker replicas
- move SQLite → Postgres (gateway job store)
- move MinIO → S3

## CI/CD

- Local fast-path testing is available without GitHub Actions or a running stack.
- Install the gateway test dependencies with `python3 -m pip install -r services/gateway/requirements.txt -r requirements-dev.txt`.
- Run `./scripts/run-local-tests.sh` to validate `docker compose` config and execute the offline `pytest` suite.
- Use `SKIP_COMPOSE_VALIDATE=1 ./scripts/run-local-tests.sh -k provider` when you only want the Python tests or a narrower subset.
- Run `./scripts/run-provider-smoke.sh` for a Docker-backed provider smoke test that exercises `/voices`, async TTS/STT jobs, polling, and SSE event streams end to end.
- Use `RUN_PROVIDER_SMOKE=1 ./scripts/run-local-tests.sh` to append the live provider smoke test after the offline suite. Add `SKIP_BUILD=1` for faster reruns against an already rebuilt stack.
- GitHub Actions use the shared reusable workflows from [`smk762/gha-docker-shared-ci`](https://github.com/smk762/gha-docker-shared-ci) to lint Dockerfiles, validate compose, build, and scan images.
- Configure repository secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (Docker Hub access token). Set repository variable `DOCKERHUB_ORG` to the Docker Hub org/user used for publishing (defaults to the GitHub org/user).
- CI runs on pull requests and pushes to `main`; release builds push images on `main` and `v*` tags for: `gateway`, `xtts`, `xtts-glue`, `tts-worker`, `whisper-worker`.

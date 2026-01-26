## glue (Voice Glue)

`glue` is a small FastAPI service intended to run alongside the rest of the stack.

- **No Snapcast**: this branch removes Snapcast playback entirely.
- **Optional**: a tiny XTTS “synthesize to file” helper endpoint (no playback).

### Quickstart (docker compose)

From repo root:

```bash
docker compose up -d --build
```

- Glue docs: `http://localhost:9000/docs`

### Quickstart (local `.venv`)

```bash
cd /path/to/tss-stack
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r glue/requirements.txt

cd glue
uvicorn app:app --host 0.0.0.0 --port 9000 --reload
```

### Common endpoints

- **Health**: `GET /health`
- **Voices**: `GET /voices` (lists `*.wav` under `VOICES_DIR`, typically mounted from `./voices`)
- **Synthesize to file**: `POST /tts_to_file` (writes into `XTTS_OUTPUT_DIR`, typically mounted from `xtts_output`)

### Examples

```bash
curl -sS http://localhost:9000/health | jq
curl -sS http://localhost:9000/voices | jq

curl -sS http://localhost:9000/tts_to_file \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "Hello from glue.",
    "speaker": "female",
    "language": "en"
  }' | jq
```


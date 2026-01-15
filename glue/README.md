## glue (Voice Glue → Snapcast)

`glue` is a small FastAPI service that:

- Calls an XTTS server to synthesize speech to a WAV file
- Temporarily mutes non-target Snapcast clients (optionally sets target volume)
- Streams audio to Snapcast via the Snapserver FIFO
- Restores client volumes/mute state afterward

### Quickstart (docker compose)

- **Bring the stack up** (from repo root):

```bash
docker compose up -d
```

- **Open the interactive API docs**:
  - `http://localhost:9000/docs`
  - (or replace `localhost` with your host/IP)

### Common endpoints

- **Health**: `GET /health`
- **Voices**: `GET /voices` (lists `*.wav` in `VOICES_DIR`, typically mounted from `./voices`)
- **Snapcast clients**: `GET /snapcast/clients`
- **Snapcast groups**: `GET /snapcast/groups`
- **Speak (enhanced)**: `POST /speak_and_push`
- **Speak (legacy)**: `POST /speak_and_push_legacy`

### Examples (copy/paste)

Assuming `glue` is exposed on `localhost:9000`.

- **Health check**

```bash
curl -sS http://localhost:9000/health | jq
```

- **List available voices**

```bash
curl -sS http://localhost:9000/voices | jq
```

- **List Snapcast clients + groups (use these names/ids to target)**

```bash
curl -sS http://localhost:9000/snapcast/clients | jq
curl -sS http://localhost:9000/snapcast/groups  | jq
```

- **Broadcast speech to all clients (default voice)**

```bash
curl -sS http://localhost:9000/speak_and_push \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "Test announcement. This is a broadcast.",
    "speaker": "female"
  }' | jq
```

- **Dry-run (see which targets would be affected; no audio is played)**

```bash
curl -sS http://localhost:9000/speak_and_push \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "Hello targets",
    "speaker": "female",
    "targets": ["kitchen-speaker"],
    "dry_run": true
  }' | jq
```

- **Target specific clients**
  - `targets` can match **client id**, **friendly name**, **host name**, **MAC**, or **IP**

```bash
curl -sS http://localhost:9000/speak_and_push \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "Kitchen only.",
    "speaker": "female",
    "targets": ["kitchen-speaker", "192.168.1.50", "aa:bb:cc:dd:ee:ff"]
  }' | jq
```

- **Target groups by Snapcast group name**

```bash
curl -sS http://localhost:9000/speak_and_push \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "Downstairs group only.",
    "speaker": "female",
    "target_groups": ["Downstairs"]
  }' | jq
```

- **Night mode (auto volume 30% if you don’t set `volume_percent`)**

```bash
curl -sS http://localhost:9000/speak_and_push \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "Quiet update.",
    "speaker": "female",
    "target_groups": ["Bedroom"],
    "night_mode": true
  }' | jq
```

- **Force target volume + optional pre-chime**

```bash
curl -sS http://localhost:9000/speak_and_push \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "Attention please.",
    "speaker": "female",
    "targets": ["kitchen-speaker"],
    "volume_percent": 60,
    "pre_chime": true
  }' | jq
```

- **Idempotency (duplicates within ~60s are ignored)**

```bash
curl -sS http://localhost:9000/speak_and_push \
  -H 'Content-Type: application/json' \
  -H 'X-Idempotency-Key: demo-123' \
  -d '{
    "text": "This should only play once if repeated quickly.",
    "speaker": "female"
  }' | jq
```

### Speaker / voice resolution

- In `POST /speak_and_push`, `speaker` is resolved as:
  - `"amy"` → `/voices/amy.wav`
  - `"amy.wav"` → `/voices/amy.wav`
- Use `GET /voices` to discover what’s available.

### Legacy endpoint

`POST /speak_and_push_legacy` accepts a smaller body:

```json
{
  "text": "hello",
  "speaker": "female",
  "targets": ["kitchen-speaker"],
  "target_groups": ["Downstairs"]
}
```

### Configuration (env vars)

Common ones (see `docker-compose.yml` for the defaults used in this repo):

- **XTTS_URL**: XTTS endpoint (default `http://xtts:8020/tts_to_file`)
- **SNAPCAST_RPC**: Snapserver JSON-RPC (default `http://snapserver:1780/jsonrpc`)
- **SNAPCAST_FIFO**: Path to snapfifo (default `/run/snapcast/snapfifo`)
- **VOICES_DIR**: Where voice WAVs live (default `/voices`)
- **XTTS_OUTPUT_DIR**: Where synthesized WAVs are written (default `/output`)
- **DEFAULT_SPEAKER**: Default voice name (default `female`)
- **STRICT_TARGET_RESOLUTION**: If true, requesting targets/groups that resolve to 0 returns HTTP 400

Visual integration (optional):

- `glue` can also trigger an Argus “visuals play” webhook for any entries in `targets` that match known visual targets (currently: `argus`).
- If you want **visuals + audio**, include `argus` **and** your Snapcast targets/groups. If you send only `["argus"]`, it will not resolve to an audio target and may fall back to broadcast (unless `STRICT_TARGET_RESOLUTION=true`).

- **ARGUS_VISUAL_URL**: default `http://argus:5055/visuals/play`
- **ARGUS_VISUAL_TOKEN**: token sent as `X-Argus-Token`


# Digital Familiar — task tracker

Status key: ✅ done · 🔧 in progress · ⬜ not started · ⏭ skipped

Sequence and ownership in `docs/implementation-plan.md`.
Architecture and spec in `docs/digital-familiar.md`.

---

## `kimini-api` — control plane

**Repo**: `/home/hound/kimini-api` · **Branch**: `familiars` · **Port**: `8000`

### Phase 1 — complete ✅

- ✅ Familiar domain DB tables and Alembic migrations:
  - `familiars` — profile, consent, visibility, owner
  - `familiar_dataset_versions` — image/caption counts, triangulation score, manifest JSON, storage prefix
  - `familiar_personality_versions` — YAML card, summary, is_active flag
  - `familiar_adapter_versions` — modality, adapter_ref, eval_score, trainer_backend, base_model
  - `familiar_audit_events` — append-only event log
  - `image_jobs.familiar_id` / `video_jobs.familiar_id` / `voice_jobs.familiar_id` FK columns
- ✅ `/v1/familiars/*` CRUD + lifecycle endpoints
- ✅ `/v1/familiars/{id}/datasets`, `/personalities`, `/adapters` sub-resource endpoints
- ✅ `/v1/familiars/{id}/adapters/{adapter_id}/promote` — sets `active_*_adapter_id` on familiar
- ✅ `/v1/familiars/{id}/generate/image|video|voice/*` — familiar-scoped generation with adapter injection
- ✅ `/v1/familiars/{id}/generate/jobs` — unified generation timeline across modalities
- ✅ HITL: `/v1/hitl/assets`, `/ratings`, `/fine-tunes` endpoints
- ✅ Batch rating via `Promise.allSettled` pattern (client-side, no server batch endpoint needed)

### Phase 2 — partially complete

- ✅ `hitl_comparisons` DB table and Alembic migration (`e5f6a7b8c9d0`)
- ✅ `familiar_eval_runs` DB table in same migration
- ✅ `GET /v1/hitl/assets/pair` — returns two random `GeneratedImage` rows as `HitlAssetPairOut`
- ✅ `POST /v1/hitl/comparisons` — records a pairwise result (winner_id nullable = tie)
- ✅ `GET /v1/hitl/comparisons` — cursor-paginated comparison history
- ✅ `POST /v1/familiars/{id}/evals/runs` — creates `FamiliarEvalRun` row with `status=queued`
- ✅ `GET /v1/familiars/{id}/evals/runs` — cursor-paginated run list
- ✅ `GET /v1/familiars/{id}/evals/runs/{run_id}` — single run detail

#### ⬜ Eval run execution task

The `FamiliarEvalRun` row is created but nothing executes it. The execution must happen in a kimini background task following the existing pattern in `app/tasks/lora.py` and `app/tasks/hitl.py`.

**What to add:**

1. `app/tasks/familiars.py` — add task:

   ```python
   @broker.task(retry_on_error=True, max_retries=1)
   def run_familiar_eval_task(run_id: str) -> dict:
       """Execute a FamiliarEvalRun: generate N images via imogen, compute score, store results."""
   ```

   **Task steps:**
   a. Load `FamiliarEvalRun` row; set `status = "processing"`
   b. Load parent `Familiar` to resolve `adapter_id` (fall back to `active_image_adapter_id`)
   c. Load `FamiliarAdapterVersion` to get `adapter_ref` (weights URL or LoRA ref)
   d. Use `IMAGE_BACKEND` / `IMAGE_API_*` config (same as `app/domain/imagegen.py`) to submit N image
      generation jobs to imogen, each with:
      - `prompt`: run's prompt or a default eval prompt pack (e.g. "a photo of {trigger_word}",
        "portrait of {trigger_word} in natural light", "side profile of {trigger_word}")
      - `lora_weights_url`: adapter's weights URL
      - `lora_strength`: 0.8 default
      - `seed`: fixed seeds per sample (e.g. `[42, 137, 256, 512][:num_samples]`) for reproducibility
      - `model`: "flux_dev" default
   e. Poll each job to completion (reuse existing poll pattern from `app/domain/imagegen.py`)
   f. Collect resulting image URLs and any per-image quality signals (nsfw_score, etc.)
   g. Compute `eval_score` (0.0–1.0): suggested formula = `mean(1 - nsfw_score)` or a future scoring
      model call. Store raw per-sample data in `results_json`.
   h. Write back:
      - `FamiliarEvalRun.status = "completed"`, `results_json = {...}`, `error = None`
      - If any image job failed: `status = "failed"`, `error = <message>`
   i. Optionally update `FamiliarAdapterVersion.eval_score` with the computed score if this is the
      active adapter and no manual score exists.

2. `app/api/v1/routes/familiars.py` — update `create_eval_run` to enqueue after commit:

   ```python
   # after session.commit() / session.refresh(run)
   from app.tasks.familiars import run_familiar_eval_task
   await run_familiar_eval_task.kiq(run.id)
   ```

3. `app/api/v1/routes/familiars.py` — add PATCH endpoint for result write-back:

   ```
   PATCH /v1/familiars/{familiar_id}/evals/runs/{run_id}
   ```

   Schema `FamiliarEvalRunUpdate`:
   ```python
   class FamiliarEvalRunUpdate(BaseModel):
       status: str | None = Field(default=None, pattern=r"^(queued|processing|completed|failed)$")
       results: dict[str, Any] | None = None
       error: str | None = None
   ```

   This endpoint is also callable by gothmog workflows (see gothmog section).

#### ⬜ Comparison-to-adapter ranking job

Pairwise comparison records accumulate in `hitl_comparisons` but do not currently feed back into `FamiliarAdapterVersion.eval_score`.

**What to add:**

- `POST /v1/familiars/{id}/evals/rank` — trigger Elo/Bradley-Terry aggregation:
  - Fetch all `HitlComparison` rows where both `asset_a_id` and `asset_b_id` are images associated
    with adapters of this familiar (via `ImageJob.familiar_adapter_id`)
  - Run Bradley-Terry model or simple win-rate calculation per adapter
  - Update `FamiliarAdapterVersion.eval_score` for all affected adapters
  - Return a ranked list of adapter IDs with scores
- Alternatively enqueue this as a background task triggered nightly or after N new comparisons.

#### ⏭ Skipped (pending legal)

- Consent/audit hardening and export
- Bulk asset import endpoint

---

## `somnus` — operator UI

**Repo**: `/home/hound/somnus` · **Port**: `3000`

### Phase 1 — complete ✅

- ✅ App shell, auth guard, JWT httpOnly cookie, token refresh, `/api/kimini` proxy
- ✅ HITL: asset review grid, batch rate/delete, fine-tune launch + status
- ✅ Asset detail page with generation params and rating timeline
- ✅ Familiar Studio: all sub-routes (dataset, personality, training, review, evals)
- ✅ Adapter promotion UI, eval score bar chart, generation history

### Phase 2 — complete ✅

- ✅ `/review/compare` — pairwise A/B page with history
- ✅ `/familiars/[id]/evals` — tabbed: Adapter Scores · Benchmark Runs · Generation History
- ✅ Benchmark run creation dialog (adapter, prompt, num_samples)

### Phase 2 — pending (blocked on kimini tasks above)

- ⬜ Eval run status live-update — add `useJobWs` subscription to eval run IDs with `status=queued|processing`
  (wire after kimini task emits WS events)
- ⬜ `/familiars/[id]/evals` — show per-sample image grid in run detail (expand row or detail drawer)
- ⬜ `/review/compare` — show per-familiar comparison win-rates once ranking endpoint exists

### Phase 3 — not started

- ⬜ Archival bundle export download button
- ⬜ Retraining reminder banner (surface when `created_at` of active adapter > 60 days)
- ⬜ Personality memory integration UI (agent-composer / mimiri hooks)

---

## `gothmog` — orchestration

**Repo**: `/home/hound/gothmog` · **Port**: `8030`
**API**: `POST /v1/orchestrate/run` → `{ workflow, input }` → `202 { run_id }`
**Poll**: `GET /v1/orchestrate/runs/{run_id}`

Existing workflows: `image_generate`, `image_to_video`, `image_chain_sdxl_flux_wan`,
`character_create`, `style_transfer`, `batch_generate`.

All workflows are defined in `server/graphs.py` as LangGraph `StateGraph` objects.
Tools that call external services are in `server/tools.py` using a submit→poll pattern.

### ⬜ New workflow: `familiar_eval_run`

**Purpose**: Execute a benchmark eval run on behalf of kimini when `IMAGE_BACKEND` is live.
This provides richer evaluation than the simple kimini task: multiple prompt variants,
LLM-based quality scoring via ollama, and structured result objects.

**Input schema**:
```json
{
  "workflow": "familiar_eval_run",
  "input": {
    "run_id": "<FamiliarEvalRun.id>",
    "familiar_id": "<Familiar.id>",
    "adapter_ref": "<FamiliarAdapterVersion.adapter_ref>",
    "lora_weights_url": "<FamiliarAdapterVersion weights URL>",
    "prompt": "<optional override prompt>",
    "num_samples": 4,
    "kimini_base_url": "http://192.168.1.128:8000",
    "kimini_token": "<service-to-service JWT>"
  }
}
```

**Graph steps**:
1. `build_eval_prompts` — if `prompt` given, use it N times with varied seeds; otherwise call LLM
   (ollama qwen3:8b) to generate N diverse eval prompts using the adapter description
2. `generate_eval_images` — fan out N image generation calls to imogen via `generate_image` tool,
   each with:
   - `lora_url` = `lora_weights_url`
   - `lora_strength` = 0.8
   - `seed` = fixed per-sample seed
   - `model` = "flux_dev"
3. `score_results` — for each generated image call LLM to rate identity consistency (0.0–1.0)
   given the prompt and image URL; aggregate mean score into `eval_score`
4. `write_results_to_kimini` — `PATCH /v1/familiars/{familiar_id}/evals/runs/{run_id}` with:
   ```json
   {
     "status": "completed",
     "results": {
       "eval_score": 0.85,
       "samples": [
         { "prompt": "...", "seed": 42, "url": "...", "score": 0.87 }
       ]
     }
   }
   ```
   On any workflow failure, PATCH with `{ "status": "failed", "error": "<message>" }`.

**Registration** in `WORKFLOW_REGISTRY`:
```python
"familiar_eval_run": {
    "name": "Familiar Eval Run",
    "description": "Generate N benchmark images for a familiar adapter and score identity consistency",
    "graph": build_familiar_eval_run_graph,
    "state_class": FamiliarEvalRunState,
    "timeout_s": 900,
}
```

**Input validation** (add to `main.py` validation block):
- `run_id`, `familiar_id`, `adapter_ref`, `lora_weights_url` are required
- `num_samples` ∈ [1, 20]
- `kimini_base_url` and `kimini_token` are required for step 4

**Note**: kimini must set `ORCHESTRATOR_BACKEND=live` and point `ORCHESTRATOR_API_BASE_URL=http://192.168.1.128:8030`
to route `run_familiar_eval_task` through gothmog instead of executing it inline.

---

### ⬜ New workflow: `familiar_train_and_eval`

**Purpose**: Full pipeline — submit training to loraline, wait for completion, register adapter in
kimini, trigger eval run.

**Input schema**:
```json
{
  "workflow": "familiar_train_and_eval",
  "input": {
    "familiar_id": "<Familiar.id>",
    "dataset_version_id": "<FamiliarDatasetVersion.id>",
    "training_images": ["https://...", "https://..."],
    "trigger_word": "dhounddog",
    "base_model": "flux_dev",
    "training_steps": 2000,
    "eval_prompt": "a portrait of dhounddog in natural light",
    "kimini_base_url": "http://192.168.1.128:8000",
    "kimini_token": "<service-to-service JWT>"
  }
}
```

**Graph steps**:
1. `submit_training` — `POST http://192.168.1.138:8010/v1/lora/jobs` with training payload;
   poll until `status=completed`; extract `weights_url`, `provider_job_id`
2. `register_adapter` — `POST /v1/familiars/{familiar_id}/adapters` to kimini:
   ```json
   {
     "modality": "image",
     "status": "ready",
     "adapter_ref": "<weights_url>",
     "adapter_job_id": "<loraline_job_id>",
     "trainer_backend": "onetrainer",
     "base_model": "flux_dev"
   }
   ```
   Capture returned `adapter_id`.
3. `trigger_eval` — `POST /v1/familiars/{familiar_id}/evals/runs` to kimini:
   ```json
   { "adapter_id": "<adapter_id>", "prompt": "<eval_prompt>", "num_samples": 4 }
   ```
4. `report_output` — return `{ adapter_id, eval_run_id, weights_url }` as workflow output

**Timeout**: 7200s (training can take up to 2 hours)

---

### ⬜ New workflow: `familiar_periodic_revalidate`

**Purpose**: Re-evaluate all active adapters across all familiars for a given user.
Intended to be triggered on a schedule (e.g. monthly) via kimini or an external cron.

**Input schema**:
```json
{
  "workflow": "familiar_periodic_revalidate",
  "input": {
    "user_id": "<user UUID>",
    "kimini_base_url": "http://192.168.1.128:8000",
    "kimini_token": "<service-to-service JWT>"
  }
}
```

**Graph steps**:
1. `fetch_familiars` — `GET /v1/familiars` from kimini; filter to those with an `active_image_adapter_id`
2. `fan_out_evals` — for each familiar, `POST /v1/familiars/{id}/evals/runs` with default prompt
3. `collect_results` — poll all created eval run IDs until `status=completed|failed`; timeout 3600s
4. `report_summary` — return per-familiar eval scores and any failures

**Timeout**: 10800s (3 hours for large familiar rosters)

---

## `imogen` — image runtime

**Host**: `192.168.1.138:8000`
**Metrics**: `http://192.168.1.138:8000/metrics`

imogen is the GPU image generation service used by kimini, gothmog, and the eval pipeline.

### ⬜ Deterministic eval mode

For the eval pipeline (both kimini task and gothmog workflow) to produce reproducible, comparable results, imogen jobs need to echo full generation metadata back in the job result.

**Required additions to `GET /images/jobs/{job_id}` response**:

```json
{
  "id": "...",
  "status": "completed",
  "result": {
    "images": [...],
    "metadata": {
      "model_resolved": "flux_dev",
      "lora_ref": "s3://bucket/familiar/adapters/uuid.safetensors",
      "lora_strength_applied": 0.8,
      "seed_used": 42,
      "sampler": "dpmpp_2m",
      "steps": 30,
      "guidance_scale": 7.5,
      "width": 1024,
      "height": 1024
    }
  }
}
```

This metadata is stored in `FamiliarEvalRun.results_json` for traceability and comparison across runs.

**Acceptance criteria**: When a job is submitted with a `seed` field, the same seed must produce the same image output for a given model+LoRA combination, and the seed must appear in the response metadata.

---

## `loraline` — training control plane

**Host**: `192.168.1.138:8010`
**Metrics**: `http://192.168.1.138:8010/metrics`

loraline is the LoRA training gateway used by kimini's `train_lora_task` via `LORA_API_*` config.

### ⬜ trainer_backend field

Kimini's `FamiliarAdapterVersion` has a `trainer_backend` column (String 30) intended to record
which trainer produced the adapter. Loraline must include this in the completed job result.

**Required addition to `GET /v1/lora/jobs/{job_id}` response**:

```json
{
  "id": "...",
  "status": "completed",
  "trainer_backend": "onetrainer",
  "base_model": "flux_dev",
  "weights_url": "https://cdn.example.com/loras/uuid.safetensors",
  "provider_job_id": "loraline-internal-id"
}
```

Kimini reads `trainer_backend` and `base_model` from the result and writes them into
`FamiliarAdapterVersion` when registering the completed adapter.

### ⬜ Dataset manifest hash and artifact checksums

For reproducibility and audit trail, loraline should compute and return:

```json
{
  "dataset_manifest_hash": "sha256:abc123...",
  "weights_sha256": "sha256:def456...",
  "config_sha256": "sha256:ghi789..."
}
```

- `dataset_manifest_hash`: SHA-256 of the sorted list of training image URLs (or the manifest JSON)
- `weights_sha256`: SHA-256 of the final `.safetensors` file
- `config_sha256`: SHA-256 of the training config YAML used

These are stored in `FamiliarAdapterVersion.metadata` JSON column by kimini.
Enables verification that a specific adapter came from a specific dataset version.

**Acceptance criteria**: Kimini `train_lora_task` (in `app/tasks/lora.py`) reads these fields from the
result and includes them in the `update_job_status` call that completes the `LoraJob`.

---

## `vidita` — video runtime

**Host**: `192.168.1.138:8001`
**Metrics**: `http://192.168.1.138:8001/metrics`

### ⬜ Familiar adapter metadata in job outputs

When a video job is created with a `familiar_id` and `familiar_adapter_id`, the completed job result
should echo these back:

```json
{
  "id": "...",
  "status": "completed",
  "familiar_id": "...",
  "familiar_adapter_id": "...",
  "adapter_ref": "..."
}
```

This allows somnus's `/familiars/[id]/review` page to group video outputs by adapter version
and surface lineage information.

### ⬜ Frame-consistency metric (optional)

For eval pipeline extension to video: when a batch of frames from a video job are evaluated,
return a `frame_consistency_score` (0.0–1.0) indicating how consistently the identity token
appears across frames. This feeds into `FamiliarEvalRun.results_json` for video modality eval runs.

---

## `tss-stack` — voice runtime

### ⬜ Voice profile familiar references

When a TTS/STT job is associated with a familiar, store and return `familiar_id` and
`familiar_adapter_id` in the job record and result payload. This mirrors the pattern already
implemented for image (`image_jobs.familiar_id`) and video (`video_jobs.familiar_id`) in kimini.

**Kimini already has `VoiceJob.familiar_id` and `VoiceJob.familiar_adapter_id` columns.**
tss-stack needs to accept and return these fields so kimini can populate them.

---

## `test_dbs` — shared data layer

**Host**: `192.168.1.128` · Postgres: `5432` · Redis: `6380` · MinIO: `9000` · Qdrant: `6333`

### ✅ Phase 1 tables (applied)

All familiar domain tables are live in Postgres via Alembic migrations through migration `e5f6a7b8c9d0`.

### ⬜ MinIO bucket prefixes for familiar artifacts

The MinIO bucket should enforce the following prefix conventions for familiar pipeline artifacts
(document in `test_dbs` runbooks and apply lifecycle/retention policies as appropriate):

| Prefix | Contents | Retention |
|---|---|---|
| `familiar/datasets/{familiar_id}/{version}/` | Training images, resized copies | Indefinite |
| `familiar/captions/{familiar_id}/{version}/` | Caption `.txt` files per image | Indefinite |
| `familiar/adapters/{familiar_id}/{adapter_id}/` | `.safetensors` weights, config YAML | Indefinite |
| `familiar/evals/{familiar_id}/{run_id}/` | Eval-generated images + metadata JSON | 90 days |
| `familiar/archive/{familiar_id}/` | Full archival bundles (zip/tar) | Indefinite |
| `familiar/personality/{familiar_id}/{version}/` | Personality card YAML exports | Indefinite |

These prefixes correspond to `FamiliarDatasetVersion.storage_prefix` values stored in Postgres.

### ⬜ Redis key namespacing

Gothmog uses `orcq:runs` (queue) and `orcr:{run_id}` (cache). No conflicts with kimini's taskiq
queues (`taskiq:gen:high`, `taskiq:gen:default`, `taskiq:gen:low`). No action needed unless
gothmog familiar workflows are added to a separate priority queue.

---

## `sauron` — observability

**Grafana**: `http://192.168.1.128:3001`
**Prometheus**: `http://192.168.1.128:9090`

### ⬜ Familiar pipeline dashboards

Add panels to an existing or new "Familiar Pipeline" dashboard in Grafana:

**Training**:
- `lora_jobs_total{status}` — count by status (queued / processing / completed / failed)
- `lora_job_duration_seconds` — histogram of training duration
- Dead-letter rate: `rate(lora_jobs_total{status="dead_letter"}[5m])`

**Eval runs**:
- `familiar_eval_runs_total{status}` — count by status
- `familiar_eval_run_duration_seconds` — histogram (once kimini exposes the metric)
- `familiar_eval_score` gauge — latest eval score per familiar_id (if kimini exposes via pushgateway)

**Comparisons**:
- `hitl_comparisons_total` — cumulative count of pairwise comparisons recorded

**Source**: kimini exposes Prometheus metrics at `http://192.168.1.128:8000/metrics`.
Sauron already scrapes kimini — verify the scrape target is active and add these panel queries.

### ⬜ Alerts

Add to Prometheus alert rules (`prometheus/rules/`):

```yaml
- alert: FamiliarEvalRunStuck
  expr: |
    (time() - familiar_eval_run_created_seconds{status="queued"}) > 1800
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "FamiliarEvalRun stuck in queued for >30 min"

- alert: FamiliarTrainingDeadLetter
  expr: rate(lora_jobs_total{status="dead_letter"}[15m]) > 0
  for: 2m
  labels:
    severity: critical
  annotations:
    summary: "LoRA training job entered dead_letter state"
```

---

## `gothmog` (infra) — `gothmog.runs` schema note

The `gothmog.runs` Postgres table already exists. The `familiar_eval_run`,
`familiar_train_and_eval`, and `familiar_periodic_revalidate` workflows will store their run records
in this table automatically (no schema changes needed). The `input` and `output` JSONB columns
accommodate any workflow-specific payload.

---

## Milestone gates

| Gate | Condition | Status |
|---|---|---|
| **MVP image identity pipeline** | kimini + somnus + loraline working end-to-end; adapter promoted from UI | ✅ UI complete, loraline integration mock |
| **Eval pipeline live** | `run_familiar_eval_task` executes, scores written back, shown in somnus | ⬜ |
| **Comparison ranking live** | Win-rate/Elo updates `eval_score`; rank shown in somnus | ⬜ |
| **Governance** | Consent/audit hardening, archival bundle export | ⏭ legal pending |
| **Multimodal** | vidita + tss-stack adapter promotion in somnus | ⬜ Phase 3 |
| **Orchestrated pipeline** | gothmog `familiar_train_and_eval` runs end-to-end | ⬜ Phase 3 |
| **Long-term persistence** | Archival export + periodic retraining reminders | ⬜ Phase 4 |

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/self_lora", tags=["self_lora"])


SELF_LORA_DATA_DIR = os.getenv("SELF_LORA_DATA_DIR", "/data/self_lora")
FEEDBACK_PATH = Path(SELF_LORA_DATA_DIR) / "feedback.jsonl"
ADAPTERS_PATH = Path(SELF_LORA_DATA_DIR) / "adapters.json"
TRAIN_RUNS_DIR = Path(SELF_LORA_DATA_DIR) / "train_runs"


def _ensure_dirs() -> None:
    Path(SELF_LORA_DATA_DIR).mkdir(parents=True, exist_ok=True)
    TRAIN_RUNS_DIR.mkdir(parents=True, exist_ok=True)


def _append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    _ensure_dirs()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _write_json(path: Path, obj: Any) -> None:
    _ensure_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


class FeedbackEvent(BaseModel):
    """
    Minimal signal for Self-LoRA bootstrapping.
    This is intentionally flexible: you can send it from a UI, an agent, or a server-side ranker.
    """

    query: str = Field(..., min_length=1, max_length=5_000)
    collection: Optional[str] = Field(default=None, description="Collection used for retrieval, if known.")
    shown_results: Optional[List[Dict[str, Any]]] = Field(
        default=None, description="Compact list of what the user saw (ids/scores/payload excerpts)."
    )
    chosen: Optional[Dict[str, Any]] = Field(
        default=None, description="What the user picked/clicked (id + optional payload)."
    )
    rating: Optional[float] = Field(default=None, description="Optional scalar feedback (e.g., -1..1 or 1..5).")
    intent: Optional[str] = Field(default=None, description="Intent used/guessed during retrieval.")
    user_id: Optional[str] = Field(default=None, description="Optional user identifier (dev only).")
    session_id: Optional[str] = Field(default=None, description="Optional session/correlation id.")
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TrainRequest(BaseModel):
    """
    Initial training stub: creates a 'train run' manifest from collected feedback.
    A real LoRA trainer can later read this manifest and produce adapter weights.
    """

    base_model_id: str = Field(default="Qwen/Qwen2.5-3B-Instruct-AWQ")
    max_events: int = Field(default=2000, ge=1, le=200000)
    intent: Optional[str] = Field(default=None, description="If set, build a dataset subset for this intent only.")
    dry_run: bool = Field(default=False, description="If true, only validate and return counts.")


@router.post("/feedback")
def submit_feedback(ev: FeedbackEvent) -> Dict[str, Any]:
    rec = ev.model_dump()
    rec["_id"] = uuid.uuid4().hex
    rec["_ts_unix"] = time.time()
    _append_jsonl(FEEDBACK_PATH, rec)
    return {"ok": True, "stored": True, "id": rec["_id"]}


@router.get("/adapters")
def list_adapters() -> Dict[str, Any]:
    data = _read_json(ADAPTERS_PATH, default={"adapters": []})
    if not isinstance(data, dict) or "adapters" not in data:
        data = {"adapters": []}
    return {"ok": True, **data}


class RegisterAdapterRequest(BaseModel):
    """
    Register a LoRA adapter artifact for later selection (by intent, user, etc.).
    This doesn't require training to be implemented yet.
    """

    name: str = Field(..., min_length=1, max_length=128)
    path: str = Field(..., min_length=1, max_length=1024, description="Path/URI to adapter weights (mounted path or s3://...).")
    intent: Optional[str] = None
    user_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


@router.post("/adapters/register")
def register_adapter(req: RegisterAdapterRequest) -> Dict[str, Any]:
    data = _read_json(ADAPTERS_PATH, default={"adapters": []})
    if not isinstance(data, dict) or "adapters" not in data:
        data = {"adapters": []}
    adapters = data.get("adapters") or []
    if not isinstance(adapters, list):
        adapters = []

    rec = {
        "name": req.name,
        "path": req.path,
        "intent": req.intent,
        "user_id": req.user_id,
        "metadata": req.metadata,
        "registered_at_unix": time.time(),
    }
    adapters.append(rec)
    data["adapters"] = adapters
    _write_json(ADAPTERS_PATH, data)
    return {"ok": True, "adapter": rec}


def _build_train_manifest(req: TrainRequest) -> Dict[str, Any]:
    # Load feedback events (jsonl)
    _ensure_dirs()
    events: List[Dict[str, Any]] = []
    if FEEDBACK_PATH.exists():
        with FEEDBACK_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue

    if req.intent:
        events = [e for e in events if (e.get("intent") == req.intent)]

    if len(events) > int(req.max_events):
        events = events[-int(req.max_events) :]

    run_id = uuid.uuid4().hex
    out_dir = TRAIN_RUNS_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "run_id": run_id,
        "created_at_unix": time.time(),
        "base_model_id": req.base_model_id,
        "intent": req.intent,
        "num_events": len(events),
        "feedback_path": str(FEEDBACK_PATH),
        "output_dir": str(out_dir),
        "status": "created",
        "notes": "This is a stub. A real LoRA trainer should read feedback.jsonl and write adapter weights to output_dir.",
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _train_background(manifest: Dict[str, Any]) -> None:
    # Placeholder for future: kick off PEFT/LoRA training.
    out_dir = Path(manifest["output_dir"])
    done = dict(manifest)
    done["status"] = "ready_for_training"
    (out_dir / "manifest.json").write_text(json.dumps(done, indent=2), encoding="utf-8")


@router.post("/train")
def train(req: TrainRequest, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    manifest = _build_train_manifest(req)
    if req.dry_run:
        return {"ok": True, "dry_run": True, "manifest": manifest}

    if background_tasks is None:
        raise HTTPException(status_code=500, detail="BackgroundTasks not available")

    background_tasks.add_task(_train_background, manifest)
    return {"ok": True, "accepted": True, "manifest": manifest}


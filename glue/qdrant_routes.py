from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchAny, MatchValue, PointStruct

from sentence_transformers import SentenceTransformer

# -----------------------------
# Config
# -----------------------------

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
EMBED_MODEL_NAME = os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5")  # 384 dims
DEFAULT_TOP_K = int(os.getenv("QDRANT_TOP_K", "8"))

# Allowlist collections to prevent "oops index into random collection"
ALLOWED_COLLECTIONS = set(os.getenv("QDRANT_ALLOWED_COLLECTIONS", "recipes,memories,conversations,kb").split(","))

# Allowlist operations (no deletes by default)
ALLOW_CREATE_COLLECTION = os.getenv("QDRANT_ALLOW_CREATE", "false").lower() == "true"


# -----------------------------
# Models: Plans the LLM produces
# -----------------------------

class Payload(BaseModel):
    # free-form metadata, but we still validate "reasonable" size / types
    data: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("data")
    @classmethod
    def validate_payload(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        # Simple safety: prevent huge payload blobs
        # (Qdrant supports large payloads, but you probably don't want megabytes per point)
        approx = len(str(v).encode("utf-8"))
        if approx > 50_000:
            raise ValueError("payload too large")
        return v


class IndexPoint(BaseModel):
    id: str = Field(..., description="Deterministic ID strongly recommended: recipe_id:chunk_index etc.")
    text: str = Field(..., min_length=1, max_length=20_000)
    payload: Payload = Field(default_factory=Payload)


class IndexPlan(BaseModel):
    op: Literal["index"] = "index"
    collection: str
    points: List[IndexPoint]
    dedupe_mode: Literal["upsert_by_id"] = "upsert_by_id"
    timestamp_utc: Optional[str] = Field(default=None, description="ISO8601; if omitted server sets")

    @field_validator("collection")
    @classmethod
    def validate_collection(cls, v: str) -> str:
        if v not in ALLOWED_COLLECTIONS:
            raise ValueError(f"collection not allowed: {v}")
        return v


class MatchSpec(BaseModel):
    value: Optional[str] = None
    any: Optional[List[str]] = None


class FilterSpec(BaseModel):
    must: Optional[List[Dict[str, Any]]] = None


class SearchPlan(BaseModel):
    op: Literal["search"] = "search"
    collection: str
    query_text: str = Field(..., min_length=1, max_length=5_000)
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=50)
    filters: Optional[FilterSpec] = None

    @field_validator("collection")
    @classmethod
    def validate_collection(cls, v: str) -> str:
        if v not in ALLOWED_COLLECTIONS:
            raise ValueError(f"collection not allowed: {v}")
        return v


# -----------------------------
# Router
# -----------------------------

router = APIRouter(prefix="/qdrant", tags=["qdrant"])

_client = QdrantClient(url=QDRANT_URL)
_embedder = SentenceTransformer(EMBED_MODEL_NAME)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_collection_exists(collection: str, dim: int) -> None:
    try:
        info = _client.get_collection(collection)
        # Validate vector size matches embedding model
        # Qdrant may have named vectors; this assumes default single vector.
        size = info.config.params.vectors.size  # type: ignore
        if size != dim:
            raise HTTPException(
                status_code=500,
                detail=f"Vector size mismatch for collection '{collection}': collection={size}, embedder={dim}",
            )
    except Exception as e:
        # If collection doesn't exist, Qdrant throws; optionally create.
        if not ALLOW_CREATE_COLLECTION:
            raise HTTPException(
                status_code=500,
                detail=f"Collection '{collection}' missing or unreadable. Set QDRANT_ALLOW_CREATE=true to auto-create. ({e})",
            )
        _client.create_collection(
            collection_name=collection,
            vectors_config={"size": dim, "distance": "Cosine"},
        )


def _build_filter(filter_spec: Optional[FilterSpec]) -> Optional[Filter]:
    """
    Supports simple 'must' clauses in the form:
    { "key": "tags", "match": { "any": ["keto","quick"] } }
    { "key": "type", "match": { "value": "recipe" } }
    """
    if not filter_spec or not filter_spec.must:
        return None

    conditions: List[FieldCondition] = []
    for clause in filter_spec.must:
        key = clause.get("key")
        match = clause.get("match") or {}
        if not key:
            continue

        if "value" in match and match["value"] is not None:
            conditions.append(FieldCondition(key=key, match=MatchValue(value=match["value"])))
        elif "any" in match and match["any"]:
            conditions.append(FieldCondition(key=key, match=MatchAny(any=match["any"])))
        else:
            # ignore unsupported clauses
            continue

    return Filter(must=conditions) if conditions else None


@router.post("/index-plan")
def index_plan(plan: IndexPlan) -> Dict[str, Any]:
    # Embedder dimension
    test_vec = _embedder.encode(["dim_check"], normalize_embeddings=True)[0]
    dim = int(len(test_vec))

    _ensure_collection_exists(plan.collection, dim)

    ts = plan.timestamp_utc or _now_iso()

    texts = [p.text for p in plan.points]
    vectors = _embedder.encode(texts, normalize_embeddings=True)

    points = []
    for p, vec in zip(plan.points, vectors):
        payload = dict(p.payload.data)
        payload.setdefault("_indexed_at", ts)
        payload.setdefault("_collection", plan.collection)
        points.append(
            PointStruct(
                id=p.id,
                vector=vec.tolist(),
                payload=payload,
            )
        )

    _client.upsert(collection_name=plan.collection, points=points)
    return {
        "ok": True,
        "collection": plan.collection,
        "indexed": len(points),
        "timestamp_utc": ts,
    }


@router.post("/search-plan")
def search_plan(plan: SearchPlan) -> Dict[str, Any]:
    test_vec = _embedder.encode(["dim_check"], normalize_embeddings=True)[0]
    dim = int(len(test_vec))
    _ensure_collection_exists(plan.collection, dim)

    qvec = _embedder.encode([plan.query_text], normalize_embeddings=True)[0]
    qfilter = _build_filter(plan.filters)

    hits = _client.search(
        collection_name=plan.collection,
        query_vector=qvec.tolist(),
        query_filter=qfilter,
        limit=plan.top_k,
        with_payload=True,
    )

    # Return compact results (you can include stored "text" in payload if you choose)
    results = []
    for h in hits:
        results.append(
            {
                "id": str(h.id),
                "score": float(h.score),
                "payload": h.payload or {},
            }
        )

    return {
        "ok": True,
        "collection": plan.collection,
        "query": plan.query_text,
        "top_k": plan.top_k,
        "results": results,
    }

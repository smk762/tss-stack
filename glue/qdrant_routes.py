from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchAny, MatchValue, PointStruct

from sentence_transformers import SentenceTransformer

from intent_match import DEFAULT_INTENTS, infer_intent_heuristic, payload_fit_score

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
    id: str = Field(
        ...,
        description=(
            "External ID (string). Qdrant point IDs must be uint or UUID; the server will deterministically map "
            "this external id to a Qdrant-compatible id and store the original as payload['_external_id']."
        ),
    )
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
    # intent match-to-fit layer (optional; backward compatible)
    intent: Optional[str] = Field(default=None, description="If set, results are reranked/filtered by fit.")
    intent_mode: Literal["off", "infer", "use"] = Field(
        default="off",
        description="off: no intent logic. infer: infer intent if not provided. use: require provided intent.",
    )
    intent_min_fit: float = Field(default=0.0, ge=0.0, le=1.0, description="Filter out results with fit below this.")
    intent_weight: float = Field(default=0.35, ge=0.0, le=1.0, description="Blend weight for fit vs vector score.")
    intent_debug: bool = Field(default=False, description="If true, include intent scoring debug fields.")

    @field_validator("collection")
    @classmethod
    def validate_collection(cls, v: str) -> str:
        if v not in ALLOWED_COLLECTIONS:
            raise ValueError(f"collection not allowed: {v}")
        return v


class MultiSearchPlan(BaseModel):
    """
    Search across multiple collections and optionally apply the same intent match-to-fit rerank/filter
    across the merged candidate set.
    """

    op: Literal["multi_search"] = "multi_search"
    query_text: str = Field(..., min_length=1, max_length=5_000)
    collections: Optional[List[str]] = Field(
        default=None,
        description="If omitted, uses all allowed collections. If provided, must be subset of allowed.",
    )
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=50, description="Final merged top_k")
    per_collection_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=50, description="Per-collection candidate limit")
    filters: Optional[FilterSpec] = None
    intent: Optional[str] = None
    intent_mode: Literal["off", "infer", "use"] = "off"
    intent_min_fit: float = Field(default=0.0, ge=0.0, le=1.0)
    intent_weight: float = Field(default=0.35, ge=0.0, le=1.0)
    intent_debug: bool = False

    @field_validator("collections")
    @classmethod
    def validate_collections(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return None
        bad = [c for c in v if c not in ALLOWED_COLLECTIONS]
        if bad:
            raise ValueError(f"collections not allowed: {bad}")
        # preserve order but unique
        seen = set()
        out = []
        for c in v:
            if c not in seen:
                out.append(c)
                seen.add(c)
        return out


# -----------------------------
# Router
# -----------------------------

router = APIRouter(prefix="/qdrant", tags=["qdrant"])

_client = QdrantClient(url=QDRANT_URL)
_embedder = SentenceTransformer(EMBED_MODEL_NAME)

_ID_NAMESPACE = uuid.UUID(os.getenv("QDRANT_ID_NAMESPACE", "c7c0d1c1-3f4c-4b2b-9f9e-3a8f9b39d4a9"))


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


def _coerce_point_id(external_id: str, collection: str) -> Any:
    """
    Qdrant point IDs are either:
    - unsigned integer
    - UUID

    We accept an external string id and map it deterministically:
    - if it's a valid UUID -> use that UUID
    - if it's digits -> use int(external_id)
    - else -> uuid5(namespace, f"{collection}:{external_id}")
    """
    s = (external_id or "").strip()
    if not s:
        raise HTTPException(status_code=400, detail={"error": "id must be non-empty"})

    try:
        return uuid.UUID(s)
    except Exception:
        pass

    if s.isdigit():
        try:
            n = int(s)
        except Exception as e:
            raise HTTPException(status_code=400, detail={"error": f"invalid numeric id: {s}", "detail": str(e)})
        if n < 0:
            raise HTTPException(status_code=400, detail={"error": "id must be unsigned"})
        return n

    return uuid.uuid5(_ID_NAMESPACE, f"{collection}:{s}")


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


def _minmax_norm(values: List[float]) -> List[float]:
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if hi - lo <= 1e-9:
        return [1.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def _qdrant_search_points(
    collection: str,
    query_vector: List[float],
    qfilter: Optional[Filter],
    limit: int,
) -> List[Any]:
    """
    Qdrant client compatibility:
    - Older clients: QdrantClient.search(...) -> List[ScoredPoint]
    - Newer clients (>=1.16): QdrantClient.query_points(...) -> QueryResponse(points=[...])
    """
    if hasattr(_client, "query_points"):
        resp = _client.query_points(
            collection_name=collection,
            query=query_vector,
            query_filter=qfilter,
            limit=limit,
            with_payload=True,
        )
        # QueryResponse(points=[ScoredPoint,...])
        return list(getattr(resp, "points", []) or [])

    # Fallback for older qdrant-client releases
    if hasattr(_client, "search"):
        return list(
            _client.search(
                collection_name=collection,
                query_vector=query_vector,
                query_filter=qfilter,
                limit=limit,
                with_payload=True,
            )
            or []
        )

    raise HTTPException(status_code=500, detail={"error": "Unsupported qdrant-client: no query_points/search method"})


def _resolve_intent(plan: SearchPlan) -> Tuple[Optional[str], float, Dict[str, Any]]:
    if plan.intent_mode == "off":
        return None, 0.0, {"mode": "off"}
    if plan.intent:
        return plan.intent, 1.0, {"mode": "provided"}
    if plan.intent_mode == "use":
        # caller said they will provide it
        return None, 0.0, {"mode": "missing_required"}
    # infer
    name, conf, dbg = infer_intent_heuristic(plan.query_text, DEFAULT_INTENTS)
    return name, conf, {"mode": "infer", **dbg}


def _resolve_intent_multi(plan: MultiSearchPlan) -> Tuple[Optional[str], float, Dict[str, Any]]:
    if plan.intent_mode == "off":
        return None, 0.0, {"mode": "off"}
    if plan.intent:
        return plan.intent, 1.0, {"mode": "provided"}
    if plan.intent_mode == "use":
        return None, 0.0, {"mode": "missing_required"}
    name, conf, dbg = infer_intent_heuristic(plan.query_text, DEFAULT_INTENTS)
    return name, conf, {"mode": "infer", **dbg}


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
    id_map = []
    for p, vec in zip(plan.points, vectors):
        payload = dict(p.payload.data)
        payload.setdefault("_indexed_at", ts)
        payload.setdefault("_collection", plan.collection)
        payload.setdefault("_external_id", p.id)
        qid = _coerce_point_id(p.id, plan.collection)
        points.append(
            PointStruct(
                id=qid,
                vector=vec.tolist(),
                payload=payload,
            )
        )
        id_map.append({"external_id": p.id, "qdrant_id": str(qid)})

    _client.upsert(collection_name=plan.collection, points=points)
    return {
        "ok": True,
        "collection": plan.collection,
        "indexed": len(points),
        "id_map": id_map,
        "timestamp_utc": ts,
    }


@router.post("/search-plan")
def search_plan(plan: SearchPlan) -> Dict[str, Any]:
    test_vec = _embedder.encode(["dim_check"], normalize_embeddings=True)[0]
    dim = int(len(test_vec))
    _ensure_collection_exists(plan.collection, dim)

    qvec = _embedder.encode([plan.query_text], normalize_embeddings=True)[0]
    qfilter = _build_filter(plan.filters)

    hits = _qdrant_search_points(
        collection=plan.collection,
        query_vector=qvec.tolist(),
        qfilter=qfilter,
        limit=plan.top_k,
    )

    intent_name, intent_conf, intent_dbg = _resolve_intent(plan)
    if plan.intent_mode == "use" and not intent_name:
        raise HTTPException(status_code=400, detail={"error": "intent required when intent_mode=use"})

    # Return compact results + optional intent match-to-fit rerank/filter.
    raw_scores = [float(h.score) for h in hits]
    norm_scores = _minmax_norm(raw_scores)
    weight = float(plan.intent_weight) if intent_name else 0.0

    scored = []
    for h, vs in zip(hits, norm_scores):
        payload = h.payload or {}
        fit, fit_dbg = payload_fit_score(payload, intent_name or "", DEFAULT_INTENTS) if intent_name else (0.0, {})
        combined = (1.0 - weight) * float(vs) + weight * float(fit)
        scored.append((h, payload, float(vs), float(fit), float(combined), fit_dbg))

    if intent_name and plan.intent_min_fit > 0:
        scored = [x for x in scored if x[3] >= float(plan.intent_min_fit)]

    scored.sort(key=lambda x: x[4], reverse=True)

    results = []
    for h, payload, vs, fit, combined, fit_dbg in scored:
        item: Dict[str, Any] = {
            "id": str(h.id),
            "score": float(h.score),  # raw Qdrant score
            "payload": payload,
        }
        if intent_name:
            item["vector_score_norm"] = vs
            item["fit_score"] = fit
            item["combined_score"] = combined
            if plan.intent_debug:
                item["fit_debug"] = fit_dbg
        results.append(item)

    return {
        "ok": True,
        "collection": plan.collection,
        "query": plan.query_text,
        "top_k": plan.top_k,
        "intent": intent_name,
        "intent_confidence": intent_conf if intent_name else 0.0,
        "intent_debug": intent_dbg if plan.intent_debug else None,
        "results": results,
    }


@router.post("/multi-search")
def multi_search(plan: MultiSearchPlan) -> Dict[str, Any]:
    test_vec = _embedder.encode(["dim_check"], normalize_embeddings=True)[0]
    dim = int(len(test_vec))

    cols = plan.collections or sorted(ALLOWED_COLLECTIONS)
    for c in cols:
        _ensure_collection_exists(c, dim)

    qvec = _embedder.encode([plan.query_text], normalize_embeddings=True)[0]
    qfilter = _build_filter(plan.filters)

    intent_name, intent_conf, intent_dbg = _resolve_intent_multi(plan)
    if plan.intent_mode == "use" and not intent_name:
        raise HTTPException(status_code=400, detail={"error": "intent required when intent_mode=use"})

    merged: List[Tuple[str, Any]] = []
    for c in cols:
        hits = _qdrant_search_points(
            collection=c,
            query_vector=qvec.tolist(),
            qfilter=qfilter,
            limit=plan.per_collection_k,
        )
        for h in hits:
            merged.append((c, h))

    raw_scores = [float(h.score) for _, h in merged]
    norm_scores = _minmax_norm(raw_scores)
    weight = float(plan.intent_weight) if intent_name else 0.0

    scored = []
    for (c, h), vs in zip(merged, norm_scores):
        payload = h.payload or {}
        payload.setdefault("_collection", c)  # ensure present even for old points
        fit, fit_dbg = payload_fit_score(payload, intent_name or "", DEFAULT_INTENTS) if intent_name else (0.0, {})
        combined = (1.0 - weight) * float(vs) + weight * float(fit)
        scored.append((c, h, payload, float(vs), float(fit), float(combined), fit_dbg))

    if intent_name and plan.intent_min_fit > 0:
        scored = [x for x in scored if x[4] >= float(plan.intent_min_fit)]

    scored.sort(key=lambda x: x[5], reverse=True)
    scored = scored[: int(plan.top_k)]

    results = []
    for c, h, payload, vs, fit, combined, fit_dbg in scored:
        item: Dict[str, Any] = {
            "collection": c,
            "id": str(h.id),
            "score": float(h.score),
            "payload": payload,
        }
        if intent_name:
            item["vector_score_norm"] = vs
            item["fit_score"] = fit
            item["combined_score"] = combined
            if plan.intent_debug:
                item["fit_debug"] = fit_dbg
        results.append(item)

    return {
        "ok": True,
        "collections": cols,
        "query": plan.query_text,
        "top_k": plan.top_k,
        "per_collection_k": plan.per_collection_k,
        "intent": intent_name,
        "intent_confidence": intent_conf if intent_name else 0.0,
        "intent_debug": intent_dbg if plan.intent_debug else None,
        "results": results,
    }

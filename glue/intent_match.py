from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class IntentDef:
    name: str
    description: str
    keywords: Tuple[str, ...] = ()
    # Optional hints used by match-to-fit scoring.
    prefer_collections: Tuple[str, ...] = ()
    prefer_types: Tuple[str, ...] = ()


DEFAULT_INTENTS: Tuple[IntentDef, ...] = (
    IntentDef(
        name="recipe",
        description="Cooking and recipes: ingredients, steps, substitutions, timings.",
        keywords=("recipe", "cook", "bake", "ingredients", "oven", "air fryer", "substitute", "prep"),
        prefer_collections=("recipes",),
        prefer_types=("recipe",),
    ),
    IntentDef(
        name="kb_lookup",
        description="Knowledge base lookup: factual reference, docs, notes, how it works.",
        keywords=("docs", "documentation", "reference", "explain", "what is", "how does", "guide", "how to"),
        prefer_collections=("kb",),
        prefer_types=("kb", "doc", "note"),
    ),
    IntentDef(
        name="troubleshooting",
        description="Debugging and troubleshooting: errors, failures, fixing issues, why something broke.",
        keywords=("error", "failed", "failure", "fix", "debug", "traceback", "stack trace", "crash"),
        prefer_collections=("kb", "conversations"),
        prefer_types=("kb", "doc", "conversation"),
    ),
    IntentDef(
        name="memory_recall",
        description="Personal memory recall: past events, previous messages, reminders, preferences.",
        keywords=("remember", "remind", "last time", "previous", "we talked", "earlier"),
        prefer_collections=("memories", "conversations"),
        prefer_types=("memory", "conversation"),
    ),
)


def _tokenize_lower(s: str) -> List[str]:
    return [t for t in "".join((ch.lower() if ch.isalnum() else " ") for ch in s).split() if t]


def infer_intent_heuristic(query_text: str, intents: Iterable[IntentDef] = DEFAULT_INTENTS) -> Tuple[Optional[str], float, Dict[str, Any]]:
    """
    Cheap intent guesser (no model): keyword voting + tiny phrase heuristics.
    Returns (intent_name, confidence_0_to_1, debug).
    """
    q = query_text.strip().lower()
    toks = _tokenize_lower(q)
    if not toks:
        return None, 0.0, {"reason": "empty"}

    scores: Dict[str, float] = {}
    for idef in intents:
        s = 0.0
        for kw in idef.keywords:
            kwl = kw.lower()
            if " " in kwl:
                if kwl in q:
                    s += 2.0
            else:
                if kwl in toks:
                    s += 1.0
        scores[idef.name] = s

    # minor boosts
    if "ingredients" in toks or "preheat" in toks:
        scores["recipe"] = scores.get("recipe", 0.0) + 1.0
    if "traceback" in toks or "stacktrace" in toks or "stack" in toks and "trace" in toks:
        scores["troubleshooting"] = scores.get("troubleshooting", 0.0) + 1.0

    best = max(scores.items(), key=lambda kv: kv[1]) if scores else (None, 0.0)
    best_name, best_score = best
    total = sum(max(0.0, v) for v in scores.values()) or 0.0
    conf = float(best_score / total) if total > 0 else 0.0

    if best_score <= 0.0:
        return None, 0.0, {"scores": scores, "reason": "no_keyword_hits"}
    return best_name, min(1.0, conf), {"scores": scores}


def payload_fit_score(payload: Dict[str, Any], intent: str, intents: Iterable[IntentDef] = DEFAULT_INTENTS) -> Tuple[float, Dict[str, Any]]:
    """
    Match-to-fit rule: compute a 0..1 score based on payload tags/types/collection hints.

    Supported payload conventions:
    - intent_tags: string or list[str]
    - type/doc_type: string
    - _collection: string (server-injected on index)
    """
    if not intent:
        return 0.0, {"reason": "no_intent"}

    idef = next((x for x in intents if x.name == intent), None)
    if idef is None:
        return 0.0, {"reason": "unknown_intent"}

    intent_tags = payload.get("intent_tags")
    tags: List[str] = []
    if isinstance(intent_tags, str) and intent_tags.strip():
        tags = [intent_tags.strip().lower()]
    elif isinstance(intent_tags, list):
        tags = [str(t).strip().lower() for t in intent_tags if str(t).strip()]

    if tags:
        if idef.name.lower() in tags:
            return 1.0, {"matched": "intent_tags", "tags": tags}
        # partial credit if any tag shares a keyword
        for kw in idef.keywords:
            if kw.lower() in tags:
                return 0.8, {"matched": "intent_tags_keyword", "tags": tags}
        return 0.1, {"matched": "intent_tags_mismatch", "tags": tags}

    # fallback: type match
    typ = payload.get("type") or payload.get("doc_type")
    if isinstance(typ, str) and typ.strip():
        tl = typ.strip().lower()
        if any(tl == x.lower() for x in idef.prefer_types):
            return 0.6, {"matched": "type", "type": tl}
        return 0.2, {"matched": "type_mismatch", "type": tl}

    # fallback: collection match
    col = payload.get("_collection")
    if isinstance(col, str) and col.strip():
        cl = col.strip().lower()
        if any(cl == x.lower() for x in idef.prefer_collections):
            return 0.4, {"matched": "_collection", "collection": cl}
        return 0.15, {"matched": "_collection_mismatch", "collection": cl}

    return 0.0, {"reason": "no_payload_signals"}


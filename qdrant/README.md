# Qdrant Routes (LLM-Assisted Indexing & Search)

This module exposes **guarded FastAPI endpoints** that allow an LLM to *plan* Qdrant indexing and search operations, while **this service executes them safely**.

The goal is to let an LLM:
- decide **what** to store or retrieve
- decide **where** (collection, filters, top-k)

…without allowing it to:
- generate embeddings directly
- break schema invariants
- delete or corrupt the vector database

This pattern is sometimes called **“LLM-authored plans, code-executed actions.”**

---

## Why this exists

Qdrant requires:
- numeric vectors with fixed dimensionality
- correct collection schemas
- valid filter syntax

LLMs are excellent at:
- deciding *what text matters*
- choosing metadata and filters
- structuring plans

LLMs are **not** reliable at:
- producing embeddings
- matching vector dimensions
- safely executing destructive DB operations

So we split responsibilities:

| Role | Responsibility |
|---|---|
| LLM | Emits **IndexPlan** / **SearchPlan** JSON |
| This service | Embeds text, validates plans, calls Qdrant |

---

## Exposed Endpoints

### `POST /qdrant/index-plan`

Indexes one or more text chunks into a Qdrant collection.

**What the LLM provides**
- collection name
- deterministic point IDs
- text to embed
- structured payload metadata

**What this service does**
- validates collection allowlist
- generates embeddings
- ensures vector dimensionality matches collection
- upserts points into Qdrant

#### Example request
```json
{
  "op": "index",
  "collection": "recipes",
  "points": [
    {
      "id": "lentil_halloumi_curry:0",
      "text": "Ingredients: red lentils, halloumi, tomatoes, coconut milk...",
      "payload": {
        "data": {
          "type": "recipe",
          "recipe_id": "lentil_halloumi_curry",
          "tags": ["vegetarian", "spicy"]
        }
      }
    }
  ]
}
```

#### Example response
```json
{
  "ok": true,
  "collection": "recipes",
  "indexed": 1,
  "timestamp_utc": "2026-01-24T02:18:41Z"
}
```

---

### `POST /qdrant/search-plan`

Performs a semantic search against a Qdrant collection.

**What the LLM provides**
- collection to search
- query text
- optional metadata filters
- desired `top_k`

**What this service does**
- embeds the query text
- builds a validated Qdrant filter
- executes the search
- returns scored payloads

#### Example request
```json
{
  "op": "search",
  "collection": "recipes",
  "query_text": "quick keto curry like Durban style",
  "top_k": 8,
  "filters": {
    "must": [
      { "key": "tags", "match": { "any": ["keto"] } }
    ]
  }
}
```

#### Example response
```json
{
  "ok": true,
  "collection": "recipes",
  "query": "quick keto curry like Durban style",
  "top_k": 8,
  "results": [
    {
      "id": "lentil_halloumi_curry:0",
      "score": 0.82,
      "payload": {
        "recipe_id": "lentil_halloumi_curry",
        "tags": ["vegetarian", "spicy"]
      }
    }
  ]
}
```

---

## Collections & Allowlist

Allowed collections are controlled via environment variable:

```bash
QDRANT_ALLOWED_COLLECTIONS=recipes,memories,conversations,kb
```

Requests targeting collections outside this list are rejected.

This prevents:
- accidental indexing into wrong collections
- prompt-injection attempts to create/delete arbitrary collections

---

## Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `QDRANT_URL` | Qdrant HTTP endpoint | `http://localhost:6333` |
| `EMBED_MODEL` | Sentence-transformers model | `BAAI/bge-small-en-v1.5` |
| `QDRANT_TOP_K` | Default search result count | `8` |
| `QDRANT_ALLOWED_COLLECTIONS` | Collection allowlist | `recipes,memories,conversations,kb` |
| `QDRANT_ALLOW_CREATE` | Auto-create collections | `false` |

⚠️ **Auto-create is disabled by default**.  
Enable it only during controlled bootstrapping.

---

## Safety Guarantees

This module enforces:

- ✅ No delete or drop operations
- ✅ Deterministic upserts only
- ✅ Vector dimensionality checks
- ✅ Payload size limits
- ✅ Collection allowlisting
- ✅ Filter syntax validation

This makes it safe to use with:
- abliterated / unguarded LLMs
- autonomous agents
- background indexing jobs

---

## Recommended Usage Pattern

1. **LLM emits a plan**
   - `IndexPlan` when new knowledge appears
   - `SearchPlan` when answering a question
2. **Service executes the plan**
3. **LLM consumes results**
4. **Optional write-back**
   - promote stable facts to `memories`
   - store summaries into `conversations`

This creates a **read → reason → act → remember** loop.

---

## What this module intentionally does *not* do

- ❌ Generate embeddings inside the LLM
- ❌ Expose raw Qdrant endpoints
- ❌ Handle business logic or agent reasoning
- ❌ Decide *what* is worth remembering

Those responsibilities live in the agent layer.

---

## Typical Use Cases

- Conversational long-term memory
- Recipe and kitchen knowledge retrieval
- Project / infrastructure recall
- Tool-using agents with persistent context
- Voice assistants with recall beyond prompt limits

---

## TL;DR

> **The LLM decides.  
> The service verifies.  
> Qdrant stores.**

This is the safest way to give an autonomous agent long-term memory without giving it a loaded foot-gun.
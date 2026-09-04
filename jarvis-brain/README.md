# Jarvis Brain — v0.3.0 foundation + vector/sidecar layer

This directory is the isolated persistence/state-transition foundation for the assistant Brain.
It still does **not** replace Odysseus memory yet.

Core invariant:

> Models may propose evidence-backed meaning; Python owns persistence semantics.

## Foundation contracts

- owner-scoped SQLite authority
- explicit schema migrations; a newer unknown DB fails closed
- immutable conversation transcript storage for user/assistant/system/tool messages
- immutable evidence rows + immediate episodic capture for observations
- pending semantic-job rows for later asynchronous consolidation
- semantic current state + revision history
- exact revision→evidence provenance
- final semantic text must have its own claim-level provenance verdict
- Python derives aggregate grounded/not-grounded state
- Python alone maps semantic relation → CREATE / UPDATE / DUPLICATE / CONFLICT
- controller idempotency keys make semantic commits replay-safe
- user memory-control operations (PIN/FORGET/ERASE) require evidence and are audited
- FORGET vs ERASE semantics
- pinning changes retrieval priority, not semantic truth timestamps
- hybrid lexical + vector candidate retrieval
- vector UUIDs must resolve through owner-scoped SQLite before becoming recall hits
- idempotent external message/observation capture; mismatched replays fail closed

## Vector layer

The real vector adapter uses:

- FastEmbed for local embeddings
- ChromaDB over `chromadb.HttpClient`
- separate collections:
  - `jarvis_brain_semantic_v1`
  - `jarvis_brain_episodic_v1`
- `owner_id` metadata filtering on every vector query
- cosine-distance collections
- a persistent FastEmbed cache under `/data/fastembed-cache`

Default embedding model:

```text
sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

Chroma is disposable derived state. `BrainMemoryService.rebuild_vector_index()`
recreates owner-scoped vector contents from SQLite truth.

If Chroma/FastEmbed cannot initialize, runtime falls back to lexical recall via
`NullVectorIndex`; SQLite operation continues.

## HTTP sidecar boundary

`brain-foundation serve` exposes:

```text
GET  /health
GET  /v1/status?owner_id=...
POST /v1/capture/message
POST /v1/capture/observation
POST /v1/search
POST /v1/rebuild-index
```

`/health` is intentionally non-sensitive and unauthenticated for container health
checks. All `/v1/*` routes require:

```text
Authorization: Bearer <JARVIS_BRAIN_API_KEY>
```

The key must be at least 32 characters. Query-string API keys are not accepted.

The HTTP API never exposes direct SQLite access.

## Run isolated tests

```bash
cd jarvis-brain
python selftest.py
python -m jarvis_brain --db /tmp/brain-test.db health
```

## Optional real vector dependencies

```bash
pip install -e '.[vector]'
```

The tests do not require Chroma/FastEmbed or network access; they use a deterministic
fake Chroma boundary.

## Live autonomous ingestion (v0.3.0)

The shadow Brain now mirrors persisted Odysseus conversation rows and runs a
continuous semantic worker in `jarvis-brain-semantic-worker`.

- user messages atomically create transcript + evidence + episode + semantic job;
- assistant/system/tool messages remain transcript-only;
- the worker leases ready jobs, reasons with the configured OpenAI-compatible model,
  and persists a validated plan before semantic commits;
- Qwen is explicitly configured with `BRAIN_LLM_REASONING_EFFORT=medium` by default
  for this bounded structured-memory workload rather than inheriting Qwen3.8's
  `xhigh` default;
- literal evidence quotes and claim-level provenance are verified before persistence;
- semantic relation classification cannot return a database action;
- Python derives CREATE / UPDATE / DUPLICATE / CONFLICT;
- UPDATE consolidation receives a final provenance pass before persistence;
- semantic commit idempotency protects partial-plan replay;
- model/transport/structured-output failures retry the entire job and are never
  disguised as semantic candidate rejection;
- safe reasoner diagnostics record finish reason and token/field lengths without
  storing prompts or private reasoning text.

Still intentionally deferred:

- no Odysseus `MEMORY_BACKEND=jarvis_brain` switch;
- no current `memory.json` import;
- no old `jarvis.db` import;
- no Brain-derived context injection into Gwen yet (planned recall milestone).

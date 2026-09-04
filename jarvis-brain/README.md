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

## Deliberately not live yet

- no Odysseus `MEMORY_BACKEND=jarvis_brain` switch
- no current `memory.json` import
- no old `jarvis.db` import
- no model worker for episodic/semantic consolidation
- no live `BrainMemoryAdapter` wired into `assistant-main`
- no production Docker start/recreate

Those arrive only after this sidecar/vector layer passes real-machine acceptance.


## Semantic worker core (v0.3.0)

The worker core is present but no live LLM client or background loop is enabled yet.

- semantic jobs are leased with expiry and retry/backoff state;
- a validated plan is persisted before semantic commits, so crash recovery replays the same plan;
- natural observations can yield candidate proposals only;
- literal evidence quotes are verified by Python before relation reasoning;
- claim-level provenance is checked with one repair pass and a verify-only pass;
- semantic relation classification cannot return a database action;
- Python derives CREATE / UPDATE / DUPLICATE / CONFLICT;
- UPDATE consolidation receives a final provenance pass before persistence;
- semantic commit idempotency protects partial-plan replay.

The live shadow Brain should not consume pending jobs until a real Qwen client is added and separately accepted.

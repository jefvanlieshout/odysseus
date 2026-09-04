# Brain recovery basis

This foundation is reconstructed from two authoritative ancestors:

1. **Jarvis v0.5.x implementation** — proven local mechanics for permanent messages,
   episodic capture, semantic revisions, evidence links, state checks and read-only recall.
2. **Jarvis Brain v1 architecture** — the target hardening contract:
   SQLite is truth, vectors are candidate search only, owner isolation is mandatory,
   and Qwen/model output classifies semantic relation while Python derives persistence action.

## Intentionally kept from v0.5

- preserve the complete conversation transcript
- preserve raw evidence before semantic reasoning
- immediate episode creation for observations
- semantic current state + revision history
- exact evidence links
- separate episodic/semantic recall concepts
- conservative conflict behavior
- non-destructive history

## Intentionally changed for Brain v1

- owner-scoped UUID public identifiers
- SQLite WAL / foreign keys / busy timeout / `BEGIN IMMEDIATE`
- explicit versioned schema migrations instead of stamping a version onto unknown structure
- model cannot choose CREATE / UPDATE / DUPLICATE / CONFLICT
- final semantic text must be the exact text evaluated by claim-level provenance
- Python derives aggregate provenance truth from the claim statuses
- vector results are never authoritative records
- FORGET vs ERASE semantics
- idempotent external observation/message references
- idempotent semantic commits for safe worker retries
- evidence-backed/audited PIN/FORGET/ERASE operations
- explicit pending semantic-job row for asynchronous consolidation

## Audit findings fixed before live integration

The first isolated slice passed its original tests, but review found several issues that were
safe while isolated and unacceptable once connected to Gwen:

- it preserved user observations but not the *entire* conversation transcript
- duplicate external IDs with changed content were silently treated as normal retries
- semantic CREATE/UPDATE operations had no controller idempotency ledger
- the final consolidated text could differ from the text whose provenance had been checked
- direct memory-control operations could bypass the evidence trail
- pinning changed semantic `updated_at`
- vector text was not refreshed after episode consolidation and forgotten memories stayed indexed
- schema initialization overwrote the schema-version marker without a real migration path
- retrieval stopwords accidentally hard-coded assistant/user names

The hardened foundation closes those gaps before any production-memory adapter is added.

## Not yet wired

- Odysseus live memory backend
- current `memory.json` import
- old `jarvis.db` import
- model-driven episodic consolidation
- model-driven semantic candidate/provenance/relation pipeline
- real Chroma/FastEmbed adapter + rebuild/sync tooling
- HTTP sidecar/API authentication

Those remain deferred until the hardened storage/state-transition contracts pass on the real host.

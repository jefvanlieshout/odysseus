#!/usr/bin/env bash
set -euo pipefail

BRAIN_CONTAINER="jarvis-brain-shadow"
WORKER_CONTAINER="jarvis-brain-semantic-worker"

echo "Containers:"
docker ps -a --filter "name=${BRAIN_CONTAINER}" --filter "name=${WORKER_CONTAINER}" \
  --format '  {{.Names}}  {{.Status}}'

echo
echo "Brain state:"
docker exec -i "$BRAIN_CONTAINER" python - <<'PY'
import json
import os
import sqlite3

db = sqlite3.connect(f"file:{os.environ.get('BRAIN_DB_PATH', '/data/brain.db')}?mode=ro", uri=True)
db.row_factory = sqlite3.Row
try:
    statuses = {
        row["status"]: int(row["n"])
        for row in db.execute(
            "SELECT status, COUNT(*) AS n FROM semantic_jobs GROUP BY status ORDER BY status"
        )
    }
    counts = {
        name: int(db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
        for name in (
            "semantic_memories",
            "memory_revisions",
            "semantic_commits",
            "semantic_state_checks",
            "knowledge_relations",
        )
    }
    recent_failures = [
        dict(row)
        for row in db.execute(
            "SELECT uuid AS job_uuid, status, attempt_count, last_error, next_attempt_at "
            "FROM semantic_jobs WHERE status IN ('retry','failed') "
            "ORDER BY updated_at DESC, id DESC LIMIT 5"
        ).fetchall()
    ]
    print(json.dumps({
        "schema_version": int(db.execute(
            "SELECT value FROM brain_meta WHERE key='schema_version'"
        ).fetchone()[0]),
        "integrity": db.execute("PRAGMA integrity_check").fetchone()[0],
        "job_statuses": statuses,
        "recent_job_failures": recent_failures,
        **counts,
    }, indent=2, sort_keys=True))
finally:
    db.close()
PY

echo
echo "Recent worker events:"
docker logs --tail 30 "$WORKER_CONTAINER" 2>&1 || true

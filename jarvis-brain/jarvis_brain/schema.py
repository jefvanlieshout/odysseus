from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 3

V1_DDL = r"""
CREATE TABLE IF NOT EXISTS brain_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    external_source_ref TEXT,
    raw_text TEXT NOT NULL,
    session_id TEXT,
    occurred_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(owner_id, source_kind, external_source_ref)
);
CREATE INDEX IF NOT EXISTS idx_evidence_owner_time
    ON evidence(owner_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    evidence_id INTEGER NOT NULL UNIQUE,
    summary TEXT,
    scope TEXT NOT NULL DEFAULT 'unspecified',
    importance REAL NOT NULL DEFAULT 0.5,
    activation REAL NOT NULL DEFAULT 1.0,
    status TEXT NOT NULL DEFAULT 'pending',
    semantic_candidate INTEGER NOT NULL DEFAULT 0,
    occurred_at_text TEXT,
    consolidation_reason TEXT,
    created_at TEXT NOT NULL,
    consolidated_at TEXT,
    FOREIGN KEY(evidence_id) REFERENCES evidence(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_episodes_owner_status
    ON episodes(owner_id, status);
CREATE INDEX IF NOT EXISTS idx_episodes_owner_time
    ON episodes(owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS semantic_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    current_content TEXT NOT NULL,
    memory_type TEXT NOT NULL DEFAULT 'other',
    scope TEXT NOT NULL DEFAULT 'unspecified',
    confidence REAL NOT NULL DEFAULT 1.0,
    status TEXT NOT NULL DEFAULT 'current',
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_semantic_owner_status
    ON semantic_memories(owner_id, status);
CREATE INDEX IF NOT EXISTS idx_semantic_owner_scope
    ON semantic_memories(owner_id, scope);

CREATE TABLE IF NOT EXISTS memory_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    memory_id INTEGER NOT NULL,
    revision_no INTEGER NOT NULL,
    operation TEXT NOT NULL,
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    scope TEXT NOT NULL,
    confidence REAL NOT NULL,
    change_reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(memory_id, revision_no),
    FOREIGN KEY(memory_id) REFERENCES semantic_memories(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_revisions_memory
    ON memory_revisions(memory_id, revision_no DESC);

CREATE TABLE IF NOT EXISTS revision_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL,
    evidence_id INTEGER NOT NULL,
    relation_type TEXT NOT NULL DEFAULT 'SUPPORTS',
    confidence REAL NOT NULL DEFAULT 1.0,
    details TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(revision_id, evidence_id, relation_type),
    FOREIGN KEY(revision_id) REFERENCES memory_revisions(id) ON DELETE CASCADE,
    FOREIGN KEY(evidence_id) REFERENCES evidence(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_revision_evidence_revision
    ON revision_evidence(revision_id);
CREATE INDEX IF NOT EXISTS idx_revision_evidence_evidence
    ON revision_evidence(evidence_id);

CREATE TABLE IF NOT EXISTS knowledge_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_uuid TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_uuid TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 0.9,
    details TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relations_owner_source
    ON knowledge_relations(owner_id, source_kind, source_uuid);
CREATE INDEX IF NOT EXISTS idx_relations_owner_target
    ON knowledge_relations(owner_id, target_kind, target_uuid);

CREATE TABLE IF NOT EXISTS semantic_state_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    evidence_id INTEGER NOT NULL,
    target_memory_uuid TEXT,
    semantic_relation TEXT NOT NULL,
    python_action TEXT NOT NULL,
    relation_confidence REAL NOT NULL DEFAULT 0.5,
    explanation TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(evidence_id) REFERENCES evidence(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_state_checks_owner_time
    ON semantic_state_checks(owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS semantic_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    evidence_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_id, evidence_id),
    FOREIGN KEY(evidence_id) REFERENCES evidence(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status
    ON semantic_jobs(status, created_at);

CREATE TABLE IF NOT EXISTS recall_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    query TEXT NOT NULL,
    result_count INTEGER NOT NULL,
    vector_used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""

V2_DDL = r"""
CREATE TABLE IF NOT EXISTS conversation_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    external_session_ref TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(owner_id, external_session_ref)
);
CREATE INDEX IF NOT EXISTS idx_conversation_sessions_owner
    ON conversation_sessions(owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    session_id INTEGER NOT NULL,
    external_message_ref TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user','assistant','system','tool')),
    content TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(owner_id, external_message_ref),
    FOREIGN KEY(session_id) REFERENCES conversation_sessions(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_conversation_messages_owner_time
    ON conversation_messages(owner_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_messages_session
    ON conversation_messages(session_id, occurred_at, id);

CREATE TABLE IF NOT EXISTS semantic_commits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    action TEXT NOT NULL,
    memory_uuid TEXT,
    revision_no INTEGER,
    state_check_uuid TEXT NOT NULL,
    changed INTEGER NOT NULL,
    conflict INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(owner_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_semantic_commits_owner_time
    ON semantic_commits(owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS memory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    target_memory_uuid TEXT NOT NULL,
    action TEXT NOT NULL,
    evidence_id INTEGER NOT NULL,
    result_revision_no INTEGER,
    details TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(owner_id, idempotency_key),
    FOREIGN KEY(evidence_id) REFERENCES evidence(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_memory_events_owner_target
    ON memory_events(owner_id, target_memory_uuid, created_at DESC);
"""


def configure_connection(db: sqlite3.Connection) -> None:
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA busy_timeout=5000")


def _meta_version(db: sqlite3.Connection) -> int:
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='brain_meta'"
    ).fetchone()
    if not exists:
        return 0
    row = db.execute("SELECT value FROM brain_meta WHERE key='schema_version'").fetchone()
    return int(row[0]) if row else 0


def _set_version(db: sqlite3.Connection, version: int) -> None:
    db.execute(
        "INSERT INTO brain_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(version),),
    )


def _migrate_v1_to_v2(db: sqlite3.Connection) -> None:
    db.executescript(V2_DDL)
    columns = {row[1] for row in db.execute("PRAGMA table_info(evidence)").fetchall()}
    if "message_id" not in columns:
        db.execute(
            "ALTER TABLE evidence ADD COLUMN message_id INTEGER REFERENCES conversation_messages(id) ON DELETE RESTRICT"
        )
    db.execute("CREATE INDEX IF NOT EXISTS idx_evidence_message ON evidence(message_id)")




def _migrate_v2_to_v3(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(semantic_jobs)").fetchall()}
    additions = {
        "lease_token": "TEXT",
        "lease_expires_at": "TEXT",
        "next_attempt_at": "TEXT",
        "plan_json": "TEXT",
        "result_json": "TEXT",
        "finished_at": "TEXT",
    }
    for name, column_type in additions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE semantic_jobs ADD COLUMN {name} {column_type}")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_ready "
        "ON semantic_jobs(status, next_attempt_at, created_at)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_lease "
        "ON semantic_jobs(status, lease_expires_at)"
    )

def initialize_schema(db: sqlite3.Connection) -> None:
    configure_connection(db)
    # WAL is a database-level setting. Apply it during initialization/migration,
    # not on every read connection.
    db.execute("PRAGMA journal_mode=WAL")

    version = _meta_version(db)
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Brain DB schema {version} is newer than supported schema {SCHEMA_VERSION}"
        )

    if version == 0:
        db.executescript(V1_DDL)
        _set_version(db, 1)
        db.commit()
        version = 1

    if version == 1:
        _migrate_v1_to_v2(db)
        _set_version(db, 2)
        db.commit()
        version = 2

    if version == 2:
        _migrate_v2_to_v3(db)
        _set_version(db, 3)
        db.commit()
        version = 3

    if version != SCHEMA_VERSION:
        raise RuntimeError(
            f"Brain DB migration stopped at {version}, expected {SCHEMA_VERSION}"
        )

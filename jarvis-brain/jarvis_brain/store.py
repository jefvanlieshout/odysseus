from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .schema import initialize_schema, configure_connection


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_uuid() -> str:
    return str(uuid.uuid4())


class BrainStore:
    """Authoritative SQLite persistence boundary for Brain.

    All writes use BEGIN IMMEDIATE. External callers never receive this connection.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = self.connect()
        try:
            initialize_schema(db)
        finally:
            db.close()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=5.0)
        configure_connection(db)
        return db

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def schema_version(self) -> int:
        with self.read() as db:
            row = db.execute(
                "SELECT value FROM brain_meta WHERE key='schema_version'"
            ).fetchone()
            return int(row[0]) if row else 0

    def counts(self, owner_id: str | None = None) -> dict[str, int]:
        result: dict[str, int] = {}
        with self.read() as db:
            for table in (
                "conversation_sessions",
                "conversation_messages",
                "evidence",
                "episodes",
                "semantic_memories",
                "memory_revisions",
                "revision_evidence",
                "knowledge_relations",
                "semantic_state_checks",
                "semantic_jobs",
                "semantic_commits",
                "memory_events",
                "recall_events",
            ):
                if owner_id and table in {
                    "conversation_sessions", "conversation_messages", "evidence", "episodes",
                    "semantic_memories", "knowledge_relations", "semantic_state_checks",
                    "semantic_jobs", "semantic_commits", "memory_events", "recall_events",
                }:
                    row = db.execute(
                        f'SELECT COUNT(*) FROM "{table}" WHERE owner_id=?',
                        (owner_id,),
                    ).fetchone()
                elif owner_id and table in {"memory_revisions", "revision_evidence"}:
                    if table == "memory_revisions":
                        row = db.execute(
                            "SELECT COUNT(*) FROM memory_revisions r "
                            "JOIN semantic_memories m ON m.id=r.memory_id "
                            "WHERE m.owner_id=?",
                            (owner_id,),
                        ).fetchone()
                    else:
                        row = db.execute(
                            "SELECT COUNT(*) FROM revision_evidence re "
                            "JOIN memory_revisions r ON r.id=re.revision_id "
                            "JOIN semantic_memories m ON m.id=r.memory_id "
                            "WHERE m.owner_id=?",
                            (owner_id,),
                        ).fetchone()
                else:
                    row = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
                result[table] = int(row[0])
        return result

    def dump_json(self, value: dict | None) -> str:
        return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)

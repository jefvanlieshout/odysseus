from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .retrieval import NullVectorIndex, VectorIndex, lexical_score
from .store import BrainStore, new_uuid, utc_now
from .transitions import derive_persistence_action, relation_edge_type
logger = logging.getLogger(__name__)


from .types import (
    ClaimStatus,
    MemoryStatus,
    PersistenceAction,
    ProvenanceCheck,
    RelationDecision,
    SearchHit,
    SemanticCandidate,
    SemanticRelation,
    SourceKind,
    TransitionResult,
    claims_are_grounded,
)


class BrainError(RuntimeError):
    pass


class NotFound(BrainError):
    pass


class OwnershipError(BrainError):
    pass


class GroundingError(BrainError):
    pass


class IdempotencyConflict(BrainError):
    pass


class BrainMemoryService:
    """Single persistence authority for Brain v1-style state.

    Phase-1 deliberately contains no model calls. Models may later propose a
    SemanticCandidate + RelationDecision, but only this service commits state.
    """

    def __init__(self, db_path: str | Path, *, vector_index: VectorIndex | None = None):
        self.store = BrainStore(db_path)
        self.vector = vector_index or NullVectorIndex()

    def health(self) -> dict:
        payload = {
            "ok": True,
            "schema_version": self.store.schema_version(),
            "vector": "healthy" if self.vector.healthy else "degraded",
            "vector_backend": type(self.vector).__name__,
            "llm_enabled": False,
            "phase": "semantic-worker-core",
            "semantic_worker_core": True,
            "brain_recall_core": True,
        }
        identity = getattr(self.vector, "identity", None)
        if identity is not None:
            payload["vector_identity"] = identity
        return payload

    def capture_message(
        self,
        *,
        owner_id: str,
        external_session_ref: str,
        external_message_ref: str,
        role: str,
        content: str,
        occurred_at: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Persist one immutable conversation message without promoting it to memory.

        User messages that should also become Brain evidence should normally enter
        through ``capture_observation(..., source_kind=USER_MESSAGE)`` so the
        message, evidence, episode and semantic job are committed atomically.
        """
        owner_id = self._require_text(owner_id, "owner_id")
        external_session_ref = self._require_text(external_session_ref, "external_session_ref")
        external_message_ref = self._require_text(external_message_ref, "external_message_ref")
        content = self._require_text(content, "content")
        role = self._require_text(role, "role").casefold()
        if role not in {"user", "assistant", "system", "tool"}:
            raise ValueError("role must be user, assistant, system, or tool")
        supplied_occurred_at = occurred_at is not None
        now = utc_now()
        occurred_at = occurred_at or now
        metadata_json = self.store.dump_json(metadata)

        with self.store.write() as db:
            session_id, session_uuid = self._ensure_session(
                db, owner_id, external_session_ref, now=now
            )
            existing = db.execute(
                "SELECT cm.*, cs.external_session_ref FROM conversation_messages cm "
                "JOIN conversation_sessions cs ON cs.id=cm.session_id "
                "WHERE cm.owner_id=? AND cm.external_message_ref=?",
                (owner_id, external_message_ref),
            ).fetchone()
            if existing:
                self._assert_idempotent_message(
                    existing,
                    role=role,
                    content=content,
                    external_session_ref=external_session_ref,
                    occurred_at=(occurred_at if supplied_occurred_at else None),
                    metadata_json=(metadata_json if metadata is not None else None),
                )
                return {
                    "created": False,
                    "session_uuid": str(session_uuid),
                    "message_uuid": str(existing["uuid"]),
                }

            message_uuid = new_uuid()
            db.execute(
                "INSERT INTO conversation_messages(uuid, owner_id, session_id, external_message_ref, role, "
                "content, occurred_at, metadata_json, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (message_uuid, owner_id, session_id, external_message_ref, role, content, occurred_at, metadata_json, now),
            )
            return {
                "created": True,
                "session_uuid": str(session_uuid),
                "message_uuid": message_uuid,
            }

    def capture_observation(
        self,
        *,
        owner_id: str,
        raw_text: str,
        external_source_ref: str,
        session_id: str | None = None,
        source_kind: SourceKind | str = SourceKind.USER_MESSAGE,
        occurred_at: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        owner_id = self._require_text(owner_id, "owner_id")
        raw_text = self._require_text(raw_text, "raw_text")
        external_source_ref = self._require_text(external_source_ref, "external_source_ref")
        source_kind = SourceKind(source_kind)
        if source_kind is SourceKind.USER_MESSAGE and not str(session_id or "").strip():
            raise ValueError("session_id is required for USER_MESSAGE evidence")
        supplied_occurred_at = occurred_at is not None
        now = utc_now()
        occurred_at = occurred_at or now
        metadata_json = self.store.dump_json(metadata)

        with self.store.write() as db:
            existing = db.execute(
                "SELECT e.*, ep.uuid AS episode_uuid, j.uuid AS job_uuid "
                "FROM evidence e JOIN episodes ep ON ep.evidence_id=e.id "
                "LEFT JOIN semantic_jobs j ON j.evidence_id=e.id AND j.owner_id=e.owner_id "
                "WHERE e.owner_id=? AND e.source_kind=? AND e.external_source_ref=?",
                (owner_id, source_kind.value, external_source_ref),
            ).fetchone()
            if existing:
                if str(existing["raw_text"]) != raw_text:
                    raise IdempotencyConflict("external_source_ref replay changed raw_text")
                if session_id is not None and (existing["session_id"] or None) != session_id:
                    raise IdempotencyConflict("external_source_ref replay changed session_id")
                if metadata is not None and str(existing["metadata_json"]) != metadata_json:
                    raise IdempotencyConflict("external_source_ref replay changed metadata")
                if supplied_occurred_at and str(existing["occurred_at"]) != occurred_at:
                    raise IdempotencyConflict("external_source_ref replay changed occurred_at")

                # v1 databases migrated to schema v2 have no conversation row for
                # already-captured evidence. Backfill only on an exact replay.
                if source_kind is SourceKind.USER_MESSAGE and existing["message_id"] is None:
                    message_id, _message_uuid, _session_uuid = self._ensure_user_message(
                        db, owner_id=owner_id, external_session_ref=str(session_id),
                        external_message_ref=external_source_ref, content=raw_text,
                        occurred_at=str(existing["occurred_at"]), metadata_json=str(existing["metadata_json"]), now=now,
                    )
                    db.execute("UPDATE evidence SET message_id=? WHERE id=?", (message_id, int(existing["id"])))

                return {
                    "created": False,
                    "evidence_uuid": str(existing["uuid"]),
                    "episode_uuid": str(existing["episode_uuid"]),
                    "job_uuid": existing["job_uuid"],
                }

            message_id = None
            if source_kind is SourceKind.USER_MESSAGE:
                message_id, _message_uuid, _session_uuid = self._ensure_user_message(
                    db, owner_id=owner_id, external_session_ref=str(session_id),
                    external_message_ref=external_source_ref, content=raw_text,
                    occurred_at=occurred_at, metadata_json=metadata_json, now=now,
                )

            evidence_uuid = new_uuid()
            episode_uuid = new_uuid()
            job_uuid = new_uuid()
            cursor = db.execute(
                "INSERT INTO evidence(uuid, owner_id, source_kind, external_source_ref, raw_text, "
                "session_id, occurred_at, metadata_json, created_at, message_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (evidence_uuid, owner_id, source_kind.value, external_source_ref, raw_text, session_id, occurred_at, metadata_json, now, message_id),
            )
            evidence_id = int(cursor.lastrowid)
            db.execute(
                "INSERT INTO episodes(uuid, owner_id, evidence_id, created_at) VALUES(?,?,?,?)",
                (episode_uuid, owner_id, evidence_id, now),
            )
            db.execute(
                "INSERT INTO semantic_jobs(uuid, owner_id, evidence_id, status, created_at, updated_at) "
                "VALUES(?,?,?,'pending',?,?)",
                (job_uuid, owner_id, evidence_id, now, now),
            )

        try:
            self.vector.upsert(owner_id=owner_id, kind="episode", uuid=episode_uuid, text=raw_text)
        except Exception as exc:
            logger.warning(
                "Brain derived-vector side effect failed: %s: %s",
                type(exc).__name__,
                exc,
            )
        return {
            "created": True,
            "evidence_uuid": evidence_uuid,
            "episode_uuid": episode_uuid,
            "job_uuid": job_uuid,
        }

    def commit_semantic_candidate(
        self,
        *,
        owner_id: str,
        candidate: SemanticCandidate,
        decision: RelationDecision,
        provenance: ProvenanceCheck,
        idempotency_key: str,
        final_content: str | None = None,
        change_reason: str | None = None,
    ) -> TransitionResult:
        """Commit one model proposal through Python-owned authority gates.

        The final text must be the exact text checked by provenance, every
        claim status must pass Python aggregation, and the controller supplies
        an idempotency key so retries cannot create duplicate semantic state.
        """
        owner_id = self._require_text(owner_id, "owner_id")
        idempotency_key = self._require_text(idempotency_key, "idempotency_key")
        content = self._require_text(final_content or candidate.content, "content")
        checked_content = self._require_text(provenance.checked_content, "provenance.checked_content")
        if checked_content != content:
            raise GroundingError("provenance verdict does not apply to the exact final content")
        if not claims_are_grounded(provenance.claim_statuses):
            raise GroundingError("final semantic content contains ungrounded or blocked claims")

        confidence = self._clamp01(candidate.confidence)
        relation_confidence = self._clamp01(decision.confidence)
        request_hash = self._hash_payload({
            "candidate": {
                "content": candidate.content, "memory_type": candidate.memory_type,
                "scope": candidate.scope, "confidence": confidence,
                "evidence_uuid": candidate.evidence_uuid, "evidence_quote": candidate.evidence_quote,
            },
            "decision": {
                "relation": SemanticRelation(decision.relation).value,
                "target_memory_uuid": decision.target_memory_uuid,
                "confidence": relation_confidence, "explanation": decision.explanation,
            },
            "final_content": content, "change_reason": change_reason,
            "claim_statuses": [ClaimStatus(status).value for status in provenance.claim_statuses],
        })

        with self.store.write() as db:
            replay = db.execute(
                "SELECT * FROM semantic_commits WHERE owner_id=? AND idempotency_key=?",
                (owner_id, idempotency_key),
            ).fetchone()
            if replay:
                if str(replay["request_hash"]) != request_hash:
                    raise IdempotencyConflict("semantic idempotency key was reused for a different request")
                return TransitionResult(
                    PersistenceAction(str(replay["action"])),
                    replay["memory_uuid"],
                    int(replay["revision_no"]) if replay["revision_no"] is not None else None,
                    str(replay["state_check_uuid"]),
                    bool(replay["changed"]),
                    bool(replay["conflict"]),
                )

            evidence = self._require_evidence(db, owner_id, candidate.evidence_uuid)
            quote = self._require_text(candidate.evidence_quote, "evidence_quote")
            if quote not in str(evidence["raw_text"]):
                raise GroundingError("candidate evidence_quote is not a literal span of authoritative evidence")

            target = None
            if decision.target_memory_uuid:
                target = db.execute(
                    "SELECT * FROM semantic_memories WHERE owner_id=? AND uuid=?",
                    (owner_id, decision.target_memory_uuid),
                ).fetchone()
                if target is None:
                    other = db.execute(
                        "SELECT owner_id FROM semantic_memories WHERE uuid=?",
                        (decision.target_memory_uuid,),
                    ).fetchone()
                    if other:
                        raise OwnershipError("target memory does not belong to owner")

            action = derive_persistence_action(
                decision.relation,
                target_memory_uuid=(str(target["uuid"]) if target else None),
            )
            now = utc_now()
            state_check_uuid = new_uuid()
            db.execute(
                "INSERT INTO semantic_state_checks(uuid, owner_id, evidence_id, target_memory_uuid, "
                "semantic_relation, python_action, relation_confidence, explanation, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (state_check_uuid, owner_id, int(evidence["id"]),
                 str(target["uuid"]) if target else decision.target_memory_uuid,
                 SemanticRelation(decision.relation).value, action.value, relation_confidence,
                 decision.explanation, now),
            )

            if action is PersistenceAction.CONFLICT:
                result = TransitionResult(action, None, None, state_check_uuid, False, True)
            elif action is PersistenceAction.DUPLICATE:
                if not target:
                    result = TransitionResult(PersistenceAction.CONFLICT, None, None, state_check_uuid, False, True)
                else:
                    result = TransitionResult(action, str(target["uuid"]), self._latest_revision_no(db, int(target["id"])), state_check_uuid, False)
            elif action is PersistenceAction.CREATE:
                memory_uuid = new_uuid()
                cursor = db.execute(
                    "INSERT INTO semantic_memories(uuid, owner_id, current_content, memory_type, scope, confidence, "
                    "status, pinned, created_at, updated_at) VALUES(?,?,?,?,?,?,'current',0,?,?)",
                    (memory_uuid, owner_id, content, candidate.memory_type or "other",
                     candidate.scope or "unspecified", confidence, now, now),
                )
                memory_id = int(cursor.lastrowid)
                revision_no, revision_uuid, revision_id = self._insert_revision(
                    db, memory_id=memory_id, operation="CREATE", content=content,
                    memory_type=candidate.memory_type or "other", scope=candidate.scope or "unspecified",
                    confidence=confidence, change_reason=change_reason or decision.explanation, now=now,
                )
                self._link_evidence(db, revision_id, int(evidence["id"]), now)
                self._link_episode_to_revision(db, owner_id, int(evidence["id"]), revision_uuid, now)
                result = TransitionResult(action, memory_uuid, revision_no, state_check_uuid, True)
            else:
                if not target:
                    result = TransitionResult(PersistenceAction.CONFLICT, None, None, state_check_uuid, False, True)
                else:
                    memory_uuid = str(target["uuid"])
                    old_revision = db.execute(
                        "SELECT uuid, revision_no FROM memory_revisions WHERE memory_id=? ORDER BY revision_no DESC LIMIT 1",
                        (int(target["id"]),),
                    ).fetchone()
                    db.execute(
                        "UPDATE semantic_memories SET current_content=?, memory_type=?, scope=?, confidence=?, "
                        "status='current', updated_at=? WHERE id=?",
                        (content, candidate.memory_type or str(target["memory_type"]),
                         candidate.scope or str(target["scope"]), confidence, now, int(target["id"])),
                    )
                    revision_no, revision_uuid, revision_id = self._insert_revision(
                        db, memory_id=int(target["id"]), operation="UPDATE", content=content,
                        memory_type=candidate.memory_type or str(target["memory_type"]),
                        scope=candidate.scope or str(target["scope"]), confidence=confidence,
                        change_reason=change_reason or decision.explanation, now=now,
                    )
                    self._carry_revision_evidence(db, int(target["id"]), revision_id, revision_no, now)
                    self._link_evidence(db, revision_id, int(evidence["id"]), now)
                    self._link_episode_to_revision(db, owner_id, int(evidence["id"]), revision_uuid, now)
                    if old_revision:
                        self._insert_relation(
                            db, owner_id=owner_id, source_kind="memory_revision",
                            source_uuid=str(old_revision["uuid"]), target_kind="memory_revision",
                            target_uuid=revision_uuid, relation_type=relation_edge_type(decision.relation),
                            weight=relation_confidence, details=decision.explanation, now=now,
                        )
                    result = TransitionResult(action, memory_uuid, revision_no, state_check_uuid, True)

            self._record_semantic_commit(
                db, owner_id=owner_id, idempotency_key=idempotency_key, request_hash=request_hash,
                result=result, now=now,
            )

        if result.changed and result.memory_uuid:
            try:
                self.vector.upsert(owner_id=owner_id, kind="semantic", uuid=result.memory_uuid, text=content)
            except Exception as exc:
                logger.warning(
                    "Brain derived-vector side effect failed: %s: %s",
                    type(exc).__name__,
                    exc,
                )
        return result

    def claim_semantic_job(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> dict | None:
        """Atomically lease one ready/recoverable semantic job.

        A crashed worker's ``processing`` job becomes claimable again only after
        its lease expires.  The plan, if one already exists, is preserved so a
        retry replays deterministic validated work rather than asking the model
        to invent a fresh plan after a partial commit.
        """
        worker_id = self._require_text(worker_id, "worker_id")
        lease_seconds = max(5, min(int(lease_seconds), 3600))
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease_expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        lease_token = new_uuid()

        with self.store.write() as db:
            row = db.execute(
                "SELECT j.* FROM semantic_jobs j "
                "WHERE ("
                "  (j.status IN ('pending','retry') AND "
                "   (j.next_attempt_at IS NULL OR j.next_attempt_at<=?)) "
                "  OR "
                "  (j.status='processing' AND j.lease_expires_at IS NOT NULL "
                "   AND j.lease_expires_at<=?)"
                ") "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM semantic_jobs older "
                "  WHERE older.owner_id=j.owner_id "
                "    AND older.status IN ('pending','retry','processing') "
                "    AND ("
                "      older.created_at < j.created_at "
                "      OR (older.created_at=j.created_at AND older.id < j.id)"
                "    )"
                ") "
                "ORDER BY j.created_at, j.id LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None

            db.execute(
                "UPDATE semantic_jobs SET status='processing', attempt_count=attempt_count+1, "
                "lease_token=?, lease_expires_at=?, next_attempt_at=NULL, last_error=NULL, updated_at=? "
                "WHERE id=?",
                (lease_token, lease_expires_at, now, int(row["id"])),
            )
            return {
                "job_uuid": str(row["uuid"]),
                "owner_id": str(row["owner_id"]),
                "attempt_count": int(row["attempt_count"]) + 1,
                "lease_token": lease_token,
                "lease_expires_at": lease_expires_at,
                "worker_id": worker_id,
            }

    def semantic_job_context(self, *, job_uuid: str, lease_token: str) -> dict:
        job_uuid = self._require_text(job_uuid, "job_uuid")
        lease_token = self._require_text(lease_token, "lease_token")
        with self.store.read() as db:
            row = db.execute(
                "SELECT j.uuid AS job_uuid, j.owner_id, j.status, j.attempt_count, "
                "j.lease_token, j.lease_expires_at, j.plan_json, j.result_json, "
                "e.uuid AS evidence_uuid, e.raw_text, e.source_kind, e.external_source_ref, "
                "e.session_id, e.occurred_at, e.metadata_json, ep.uuid AS episode_uuid "
                "FROM semantic_jobs j "
                "JOIN evidence e ON e.id=j.evidence_id "
                "JOIN episodes ep ON ep.evidence_id=e.id AND ep.owner_id=j.owner_id "
                "WHERE j.uuid=?",
                (job_uuid,),
            ).fetchone()
            if row is None:
                raise NotFound("semantic job not found")
            if str(row["status"]) != "processing" or str(row["lease_token"] or "") != lease_token:
                raise IdempotencyConflict("semantic job lease is not current")
            return dict(row)

    def save_semantic_job_plan(self, *, job_uuid: str, lease_token: str, plan_json: str) -> None:
        job_uuid = self._require_text(job_uuid, "job_uuid")
        lease_token = self._require_text(lease_token, "lease_token")
        plan_json = self._require_text(plan_json, "plan_json")
        # Require valid JSON now so crash recovery never discovers a malformed
        # plan after the model call is gone.
        json.loads(plan_json)
        with self.store.write() as db:
            row = db.execute(
                "SELECT id, status, lease_token, plan_json FROM semantic_jobs WHERE uuid=?",
                (job_uuid,),
            ).fetchone()
            if row is None:
                raise NotFound("semantic job not found")
            if str(row["status"]) != "processing" or str(row["lease_token"] or "") != lease_token:
                raise IdempotencyConflict("semantic job lease is not current")
            existing = row["plan_json"]
            if existing is not None and str(existing) != plan_json:
                raise IdempotencyConflict("semantic job already has a different persisted plan")
            if existing is None:
                db.execute(
                    "UPDATE semantic_jobs SET plan_json=?, updated_at=? WHERE id=?",
                    (plan_json, utc_now(), int(row["id"])),
                )

    def complete_semantic_job(self, *, job_uuid: str, lease_token: str, result_json: str) -> None:
        job_uuid = self._require_text(job_uuid, "job_uuid")
        lease_token = self._require_text(lease_token, "lease_token")
        result_json = self._require_text(result_json, "result_json")
        json.loads(result_json)
        now = utc_now()
        with self.store.write() as db:
            row = db.execute(
                "SELECT id, status, lease_token FROM semantic_jobs WHERE uuid=?",
                (job_uuid,),
            ).fetchone()
            if row is None:
                raise NotFound("semantic job not found")
            if str(row["status"]) != "processing" or str(row["lease_token"] or "") != lease_token:
                raise IdempotencyConflict("semantic job lease is not current")
            db.execute(
                "UPDATE semantic_jobs SET status='done', result_json=?, finished_at=?, "
                "lease_token=NULL, lease_expires_at=NULL, next_attempt_at=NULL, last_error=NULL, updated_at=? "
                "WHERE id=?",
                (result_json, now, now, int(row["id"])),
            )

    def retry_semantic_job(
        self,
        *,
        job_uuid: str,
        lease_token: str,
        error: str,
        max_attempts: int = 5,
    ) -> str:
        """Release a failed lease into retry/dead state without discarding plan."""
        job_uuid = self._require_text(job_uuid, "job_uuid")
        lease_token = self._require_text(lease_token, "lease_token")
        max_attempts = max(1, min(int(max_attempts), 20))
        error = str(error or "semantic worker error")[:4000]
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        with self.store.write() as db:
            row = db.execute(
                "SELECT id, status, lease_token, attempt_count FROM semantic_jobs WHERE uuid=?",
                (job_uuid,),
            ).fetchone()
            if row is None:
                raise NotFound("semantic job not found")
            if str(row["status"]) != "processing" or str(row["lease_token"] or "") != lease_token:
                raise IdempotencyConflict("semantic job lease is not current")
            attempts = int(row["attempt_count"])
            if attempts >= max_attempts:
                status = "failed"
                next_attempt_at = None
                finished_at = now
            else:
                status = "retry"
                delay_seconds = min(300, 2 ** min(attempts, 8))
                next_attempt_at = (now_dt + timedelta(seconds=delay_seconds)).isoformat()
                finished_at = None
            db.execute(
                "UPDATE semantic_jobs SET status=?, last_error=?, next_attempt_at=?, finished_at=?, "
                "lease_token=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?",
                (status, error, next_attempt_at, finished_at, now, int(row["id"])),
            )
            return status

    def set_episode_state(
        self,
        *, owner_id: str, episode_uuid: str, summary: str, scope: str,
        importance: float, activation: float, status: str, semantic_candidate: bool,
        reason: str | None = None, occurred_at_text: str | None = None,
    ) -> None:
        owner_id = self._require_text(owner_id, "owner_id")
        summary = self._require_text(summary, "summary")
        if status not in {"active", "archived"}:
            raise ValueError("episode status must be active or archived")
        with self.store.write() as db:
            row = db.execute(
                "SELECT ep.id, e.raw_text FROM episodes ep JOIN evidence e ON e.id=ep.evidence_id "
                "WHERE ep.owner_id=? AND ep.uuid=?",
                (owner_id, episode_uuid),
            ).fetchone()
            if not row:
                raise NotFound("episode not found")
            db.execute(
                "UPDATE episodes SET summary=?, scope=?, importance=?, activation=?, status=?, "
                "semantic_candidate=?, occurred_at_text=?, consolidation_reason=?, consolidated_at=? WHERE id=?",
                (summary, scope.strip() or "unspecified", self._clamp01(importance), self._clamp01(activation),
                 status, 1 if semantic_candidate else 0, occurred_at_text, reason, utc_now(), int(row["id"])),
            )
        try:
            self.vector.upsert(owner_id=owner_id, kind="episode", uuid=episode_uuid, text=summary)
        except Exception as exc:
            logger.warning(
                "Brain derived-vector side effect failed: %s: %s",
                type(exc).__name__,
                exc,
            )

    def forget_memory(
        self, *, owner_id: str, memory_uuid: str, evidence_uuid: str,
        idempotency_key: str, reason: str | None = None,
    ) -> int:
        owner_id = self._require_text(owner_id, "owner_id")
        idempotency_key = self._require_text(idempotency_key, "idempotency_key")
        request_hash = self._hash_payload({"action": "FORGET", "memory_uuid": memory_uuid, "evidence_uuid": evidence_uuid, "reason": reason})
        with self.store.write() as db:
            replay = self._memory_event_replay(db, owner_id, idempotency_key, request_hash)
            if replay:
                return int(replay["result_revision_no"] or 0)
            evidence = self._require_evidence(db, owner_id, evidence_uuid)
            memory = self._require_memory(db, owner_id, memory_uuid)
            now = utc_now()
            if str(memory["status"]) == "forgotten":
                rev_no = self._latest_revision_no(db, int(memory["id"])) or 0
            else:
                db.execute("UPDATE semantic_memories SET status='forgotten', updated_at=? WHERE id=?", (now, int(memory["id"])))
                rev_no, _rev_uuid, rev_id = self._insert_revision(
                    db, memory_id=int(memory["id"]), operation="FORGET",
                    content=str(memory["current_content"]), memory_type=str(memory["memory_type"]),
                    scope=str(memory["scope"]), confidence=float(memory["confidence"]),
                    change_reason=reason, now=now,
                )
                self._carry_revision_evidence(db, int(memory["id"]), rev_id, rev_no, now)
                self._link_evidence(db, rev_id, int(evidence["id"]), now, relation_type="MEMORY_CONTROL")
            self._record_memory_event(db, owner_id, idempotency_key, request_hash, memory_uuid, "FORGET", int(evidence["id"]), rev_no, reason, now)
        try:
            self.vector.delete(owner_id=owner_id, kind="semantic", uuid=memory_uuid)
        except Exception as exc:
            logger.warning(
                "Brain derived-vector side effect failed: %s: %s",
                type(exc).__name__,
                exc,
            )
        return rev_no

    def erase_memory(
        self, *, owner_id: str, memory_uuid: str, evidence_uuid: str, idempotency_key: str,
    ) -> None:
        owner_id = self._require_text(owner_id, "owner_id")
        idempotency_key = self._require_text(idempotency_key, "idempotency_key")
        request_hash = self._hash_payload({"action": "ERASE", "memory_uuid": memory_uuid, "evidence_uuid": evidence_uuid})
        with self.store.write() as db:
            replay = self._memory_event_replay(db, owner_id, idempotency_key, request_hash)
            if replay:
                return None
            evidence = self._require_evidence(db, owner_id, evidence_uuid)
            memory = self._require_memory(db, owner_id, memory_uuid)
            now = utc_now()
            self._record_memory_event(db, owner_id, idempotency_key, request_hash, memory_uuid, "ERASE", int(evidence["id"]), None, None, now)
            revision_uuids = [str(row[0]) for row in db.execute(
                "SELECT uuid FROM memory_revisions WHERE memory_id=?", (int(memory["id"]),)
            ).fetchall()]
            endpoints = [memory_uuid, *revision_uuids]
            if endpoints:
                marks = ",".join("?" for _ in endpoints)
                db.execute(
                    f"DELETE FROM knowledge_relations WHERE owner_id=? AND (source_uuid IN ({marks}) OR target_uuid IN ({marks}))",
                    (owner_id, *endpoints, *endpoints),
                )
            db.execute("DELETE FROM semantic_memories WHERE id=?", (int(memory["id"]),))
        try:
            self.vector.delete(owner_id=owner_id, kind="semantic", uuid=memory_uuid)
        except Exception as exc:
            logger.warning(
                "Brain derived-vector side effect failed: %s: %s",
                type(exc).__name__,
                exc,
            )

    def pin_memory(
        self, *, owner_id: str, memory_uuid: str, pinned: bool, evidence_uuid: str, idempotency_key: str,
    ) -> None:
        owner_id = self._require_text(owner_id, "owner_id")
        idempotency_key = self._require_text(idempotency_key, "idempotency_key")
        request_hash = self._hash_payload({"action": "PIN" if pinned else "UNPIN", "memory_uuid": memory_uuid, "evidence_uuid": evidence_uuid, "pinned": bool(pinned)})
        with self.store.write() as db:
            replay = self._memory_event_replay(db, owner_id, idempotency_key, request_hash)
            if replay:
                return None
            evidence = self._require_evidence(db, owner_id, evidence_uuid)
            memory = self._require_memory(db, owner_id, memory_uuid)
            # Pinning is retrieval priority metadata, not semantic truth. Do not
            # change semantic updated_at or create a semantic revision.
            db.execute("UPDATE semantic_memories SET pinned=? WHERE id=?", (1 if pinned else 0, int(memory["id"])))
            self._record_memory_event(db, owner_id, idempotency_key, request_hash, memory_uuid, "PIN" if pinned else "UNPIN", int(evidence["id"]), None, None, utc_now())


    def rebuild_vector_index(self, *, owner_id: str | None = None) -> dict:
        """Rebuild derived vector state from authoritative SQLite.

        This operation never changes SQLite semantic/episodic truth.
        """
        if not self.vector.healthy:
            raise BrainError("vector index is unavailable")

        owners: list[str]
        with self.store.read() as db:
            if owner_id is not None:
                owners = [self._require_text(owner_id, "owner_id")]
            else:
                owners = sorted({
                    str(row[0])
                    for row in db.execute(
                        "SELECT owner_id FROM semantic_memories "
                        "UNION SELECT owner_id FROM episodes"
                    ).fetchall()
                    if row[0]
                })

        semantic_count = 0
        episodic_count = 0
        for owner in owners:
            self.vector.clear_owner(owner_id=owner, kinds=("semantic", "episode"))
            with self.store.read() as db:
                semantic_rows = db.execute(
                    "SELECT uuid, current_content FROM semantic_memories "
                    "WHERE owner_id=? AND status='current'",
                    (owner,),
                ).fetchall()
                episode_rows = db.execute(
                    "SELECT ep.uuid, COALESCE(ep.summary, e.raw_text) AS text "
                    "FROM episodes ep JOIN evidence e ON e.id=ep.evidence_id "
                    "WHERE ep.owner_id=?",
                    (owner,),
                ).fetchall()
            for row in semantic_rows:
                self.vector.upsert(
                    owner_id=owner, kind="semantic",
                    uuid=str(row["uuid"]), text=str(row["current_content"]),
                )
                semantic_count += 1
            for row in episode_rows:
                self.vector.upsert(
                    owner_id=owner, kind="episode",
                    uuid=str(row["uuid"]), text=str(row["text"]),
                )
                episodic_count += 1

        return {
            "owners": len(owners),
            "semantic": semantic_count,
            "episodes": episodic_count,
        }

    def list_memories(self, *, owner_id: str, include_forgotten: bool = False) -> list[dict]:
        owner_id = self._require_text(owner_id, "owner_id")
        sql = "SELECT * FROM semantic_memories WHERE owner_id=?"
        params: list[object] = [owner_id]
        if not include_forgotten:
            sql += " AND status='current'"
        sql += " ORDER BY pinned DESC, updated_at DESC, id DESC"
        with self.store.read() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    def list_episodes(self, *, owner_id: str, status: str | None = None, limit: int = 100) -> list[dict]:
        owner_id = self._require_text(owner_id, "owner_id")
        sql = (
            "SELECT ep.*, e.raw_text, e.external_source_ref, e.session_id FROM episodes ep "
            "JOIN evidence e ON e.id=ep.evidence_id WHERE ep.owner_id=?"
        )
        params: list[object] = [owner_id]
        if status:
            sql += " AND ep.status=?"
            params.append(status)
        sql += " ORDER BY ep.created_at DESC, ep.id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self.store.read() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    def list_messages(self, *, owner_id: str, external_session_ref: str | None = None, limit: int = 500) -> list[dict]:
        owner_id = self._require_text(owner_id, "owner_id")
        sql = (
            "SELECT cm.uuid, cm.external_message_ref, cm.role, cm.content, cm.occurred_at, cm.metadata_json, "
            "cs.uuid AS session_uuid, cs.external_session_ref "
            "FROM conversation_messages cm JOIN conversation_sessions cs ON cs.id=cm.session_id "
            "WHERE cm.owner_id=?"
        )
        params: list[object] = [owner_id]
        if external_session_ref is not None:
            sql += " AND cs.external_session_ref=?"
            params.append(external_session_ref)
        sql += " ORDER BY cm.occurred_at, cm.id LIMIT ?"
        params.append(max(1, min(int(limit), 5000)))
        with self.store.read() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    def memory_events(self, *, owner_id: str, memory_uuid: str) -> list[dict]:
        owner_id = self._require_text(owner_id, "owner_id")
        with self.store.read() as db:
            return [dict(row) for row in db.execute(
                "SELECT me.*, e.uuid AS evidence_uuid, e.raw_text AS evidence_text FROM memory_events me "
                "JOIN evidence e ON e.id=me.evidence_id WHERE me.owner_id=? AND me.target_memory_uuid=? "
                "ORDER BY me.created_at, me.id",
                (owner_id, memory_uuid),
            ).fetchall()]

    def memory_history(self, *, owner_id: str, memory_uuid: str) -> list[dict]:
        owner_id = self._require_text(owner_id, "owner_id")
        with self.store.read() as db:
            memory = self._require_memory(db, owner_id, memory_uuid)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM memory_revisions WHERE memory_id=? ORDER BY revision_no",
                    (int(memory["id"]),),
                ).fetchall()
            ]

    def memory_evidence(self, *, owner_id: str, memory_uuid: str) -> list[dict]:
        owner_id = self._require_text(owner_id, "owner_id")
        with self.store.read() as db:
            memory = self._require_memory(db, owner_id, memory_uuid)
            rows = db.execute(
                "SELECT r.revision_no, r.uuid AS revision_uuid, e.uuid AS evidence_uuid, "
                "e.source_kind, e.external_source_ref, e.raw_text, e.session_id, e.occurred_at, "
                "re.relation_type, re.confidence, re.details "
                "FROM memory_revisions r "
                "JOIN revision_evidence re ON re.revision_id=r.id "
                "JOIN evidence e ON e.id=re.evidence_id "
                "WHERE r.memory_id=? AND e.owner_id=? "
                "ORDER BY r.revision_no, re.id",
                (int(memory["id"]), owner_id),
            ).fetchall()
            return [dict(row) for row in rows]

    def search(self, *, owner_id: str, query: str, limit: int = 10, include_episodes: bool = True) -> list[SearchHit]:
        owner_id = self._require_text(owner_id, "owner_id")
        query = self._require_text(query, "query")
        limit = max(1, min(int(limit), 50))
        candidates: dict[tuple[str, str], SearchHit] = {}
        with self.store.read() as db:
            for row in db.execute(
                "SELECT uuid, current_content, memory_type, scope, confidence, pinned, updated_at "
                "FROM semantic_memories WHERE owner_id=? AND status='current'",
                (owner_id,),
            ).fetchall():
                text = str(row["current_content"])
                lexical = lexical_score(query, f"{row['scope']} {row['memory_type']} {text}")
                if lexical > 0:
                    score = lexical + (0.35 if int(row["pinned"]) else 0.0)
                    candidates[("semantic", str(row["uuid"]))] = SearchHit(
                        "semantic",
                        str(row["uuid"]),
                        text,
                        score,
                        {
                            "memory_type": row["memory_type"],
                            "scope": row["scope"],
                            "confidence": row["confidence"],
                            "pinned": bool(row["pinned"]),
                            "updated_at": row["updated_at"],
                            "lexical_score": round(lexical, 6),
                            "vector_similarity": 0.0,
                        },
                    )
            if include_episodes:
                for row in db.execute(
                    "SELECT ep.uuid, COALESCE(ep.summary, e.raw_text) AS text, ep.scope, ep.importance, "
                    "ep.activation, ep.status, ep.created_at FROM episodes ep "
                    "JOIN evidence e ON e.id=ep.evidence_id WHERE ep.owner_id=?",
                    (owner_id,),
                ).fetchall():
                    text = str(row["text"])
                    lexical = lexical_score(query, f"{row['scope']} {text}")
                    if lexical > 0:
                        score = lexical
                        if row["status"] == "archived":
                            score *= 0.72
                        score *= 0.85 + 0.15 * float(row["importance"])
                        candidates[("episode", str(row["uuid"]))] = SearchHit(
                            "episode",
                            str(row["uuid"]),
                            text,
                            round(score, 6),
                            {
                                "scope": row["scope"],
                                "importance": row["importance"],
                                "activation": row["activation"],
                                "status": row["status"],
                                "created_at": row["created_at"],
                                "lexical_score": round(lexical, 6),
                                "vector_similarity": 0.0,
                            },
                        )

        vector_used = False
        try:
            if self.vector.healthy:
                vector_used = True
                vector_rows = self.vector.search(
                    owner_id=owner_id,
                    query=query,
                    kinds=("semantic", "episode") if include_episodes else ("semantic",),
                    limit=max(limit * 3, 12),
                )
                with self.store.read() as db:
                    for kind, item_uuid, similarity in vector_rows:
                        if kind not in {"semantic", "episode"}:
                            continue
                        similarity = max(0.0, min(float(similarity), 1.0))
                        key = (kind, item_uuid)
                        hit = candidates.get(key)
                        if hit is not None:
                            metadata = dict(hit.metadata)
                            metadata["vector_similarity"] = max(
                                float(metadata.get("vector_similarity", 0.0) or 0.0),
                                similarity,
                            )
                            candidates[key] = SearchHit(
                                hit.kind,
                                hit.uuid,
                                hit.text,
                                round(hit.score + similarity * 4.0, 6),
                                metadata,
                            )
                            continue

                        # Vector output is CANDIDATE-ONLY. Resolve every UUID back
                        # through owner-scoped SQLite before it can become a hit.
                        if kind == "semantic":
                            row = db.execute(
                                "SELECT uuid, current_content, memory_type, scope, confidence, pinned, updated_at "
                                "FROM semantic_memories WHERE owner_id=? AND uuid=? AND status='current'",
                                (owner_id, item_uuid),
                            ).fetchone()
                            if row:
                                candidates[key] = SearchHit(
                                    "semantic",
                                    str(row["uuid"]),
                                    str(row["current_content"]),
                                    round(similarity * 4.0 + (0.35 if int(row["pinned"]) else 0.0), 6),
                                    {
                                        "memory_type": row["memory_type"],
                                        "scope": row["scope"],
                                        "confidence": row["confidence"],
                                        "pinned": bool(row["pinned"]),
                                        "updated_at": row["updated_at"],
                                        "lexical_score": 0.0,
                                        "vector_similarity": round(similarity, 6),
                                    },
                                )
                        else:
                            row = db.execute(
                                "SELECT ep.uuid, COALESCE(ep.summary, e.raw_text) AS text, ep.scope, ep.importance, "
                                "ep.activation, ep.status, ep.created_at FROM episodes ep "
                                "JOIN evidence e ON e.id=ep.evidence_id "
                                "WHERE ep.owner_id=? AND ep.uuid=?",
                                (owner_id, item_uuid),
                            ).fetchone()
                            if row:
                                score = similarity * 4.0
                                if row["status"] == "archived":
                                    score *= 0.72
                                score *= 0.85 + 0.15 * float(row["importance"])
                                candidates[key] = SearchHit(
                                    "episode",
                                    str(row["uuid"]),
                                    str(row["text"]),
                                    round(score, 6),
                                    {
                                        "scope": row["scope"],
                                        "importance": row["importance"],
                                        "activation": row["activation"],
                                        "status": row["status"],
                                        "created_at": row["created_at"],
                                        "lexical_score": 0.0,
                                        "vector_similarity": round(similarity, 6),
                                    },
                                )
        except Exception as exc:
            logger.warning(
                "Brain vector retrieval degraded to lexical search: %s: %s",
                type(exc).__name__,
                exc,
            )
            vector_used = False

        hits = sorted(candidates.values(), key=lambda h: (h.score, h.uuid), reverse=True)[:limit]
        with self.store.write() as db:
            db.execute(
                "INSERT INTO recall_events(uuid, owner_id, query, result_count, vector_used, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (new_uuid(), owner_id, query, len(hits), 1 if vector_used else 0, utc_now()),
            )
        return hits

    def recall_context(
        self,
        *,
        owner_id: str,
        query: str,
        candidate_limit: int = 16,
        max_items: int = 6,
        max_chars: int = 2800,
        include_episodes: bool = True,
        exclude_external_source_refs: Iterable[str] | None = None,
    ) -> dict:
        # Bounded, provenance-rich recall from authoritative SQLite state.
        owner_id = self._require_text(owner_id, "owner_id")
        query = self._require_text(query, "query")
        candidate_limit = max(4, min(int(candidate_limit), 40))
        max_items = max(1, min(int(max_items), 8))
        max_chars = max(512, min(int(max_chars), 8000))

        if exclude_external_source_refs is None:
            excluded_refs: set[str] = set()
        else:
            if isinstance(exclude_external_source_refs, (str, bytes)):
                raise ValueError(
                    "exclude_external_source_refs must be a sequence, not a string"
                )
            excluded_refs = {
                str(value).strip()
                for value in exclude_external_source_refs
                if str(value or "").strip()
            }
            if len(excluded_refs) > 16:
                raise ValueError("too many excluded external source refs")

        hits = self.search(
            owner_id=owner_id,
            query=query,
            limit=candidate_limit,
            include_episodes=include_episodes,
        )

        semantic_records: list[dict] = []
        episode_records: list[dict] = []

        with self.store.read() as db:
            for hit in hits:
                metadata = dict(hit.metadata or {})
                try:
                    lexical = max(
                        0.0,
                        float(metadata.get("lexical_score", 0.0) or 0.0),
                    )
                except (TypeError, ValueError):
                    lexical = 0.0
                try:
                    vector_similarity = max(
                        0.0,
                        min(
                            float(
                                metadata.get("vector_similarity", 0.0) or 0.0
                            ),
                            1.0,
                        ),
                    )
                except (TypeError, ValueError):
                    vector_similarity = 0.0

                if hit.kind == "semantic":
                    if lexical < 1.5 and vector_similarity < 0.58:
                        continue

                    row = db.execute(
                        "SELECT m.id AS memory_id, m.status, m.memory_type, "
                        "m.scope, m.confidence, m.pinned, "
                        "r.id AS revision_id, r.uuid AS revision_uuid, "
                        "r.revision_no "
                        "FROM semantic_memories m "
                        "JOIN memory_revisions r ON r.memory_id=m.id "
                        "WHERE m.owner_id=? AND m.uuid=? "
                        "AND m.status='current' "
                        "ORDER BY r.revision_no DESC LIMIT 1",
                        (owner_id, hit.uuid),
                    ).fetchone()
                    if row is None:
                        continue

                    evidence_rows = db.execute(
                        "SELECT e.uuid AS evidence_uuid, e.source_kind, "
                        "e.external_source_ref, e.session_id, e.occurred_at "
                        "FROM revision_evidence re "
                        "JOIN evidence e ON e.id=re.evidence_id "
                        "WHERE re.revision_id=? AND e.owner_id=? "
                        "ORDER BY e.occurred_at DESC, e.id DESC LIMIT 4",
                        (int(row["revision_id"]), owner_id),
                    ).fetchall()
                    provenance = [
                        {
                            "evidence_uuid": str(e["evidence_uuid"]),
                            "source_kind": str(e["source_kind"]),
                            "external_source_ref": e["external_source_ref"],
                            "session_id": e["session_id"],
                            "occurred_at": str(e["occurred_at"]),
                        }
                        for e in evidence_rows
                    ]

                    if excluded_refs and any(
                        str(item.get("external_source_ref") or "")
                        in excluded_refs
                        for item in provenance
                    ):
                        continue

                    pinned = bool(row["pinned"])
                    rank_score = (
                        float(hit.score) + 0.80 + (0.20 if pinned else 0.0)
                    )
                    semantic_records.append({
                        "kind": "semantic",
                        "uuid": hit.uuid,
                        "text": hit.text,
                        "score": round(float(hit.score), 6),
                        "rank_score": round(rank_score, 6),
                        "retrieval": {
                            "lexical_score": round(lexical, 6),
                            "vector_similarity": round(
                                vector_similarity, 6
                            ),
                        },
                        "memory_type": str(row["memory_type"]),
                        "scope": str(row["scope"]),
                        "confidence": float(row["confidence"]),
                        "status": "current",
                        "current": True,
                        "revision_no": int(row["revision_no"]),
                        "revision_uuid": str(row["revision_uuid"]),
                        "provenance": provenance,
                    })
                    continue

                if hit.kind != "episode" or not include_episodes:
                    continue
                if lexical < 2.0 and vector_similarity < 0.68:
                    continue

                row = db.execute(
                    "SELECT ep.status, ep.scope, ep.importance, "
                    "ep.activation, ep.created_at, "
                    "e.uuid AS evidence_uuid, e.source_kind, "
                    "e.external_source_ref, e.session_id, e.occurred_at "
                    "FROM episodes ep "
                    "JOIN evidence e ON e.id=ep.evidence_id "
                    "WHERE ep.owner_id=? AND ep.uuid=? AND e.owner_id=?",
                    (owner_id, hit.uuid, owner_id),
                ).fetchone()
                if row is None:
                    continue

                external_ref = str(row["external_source_ref"] or "")
                if external_ref and external_ref in excluded_refs:
                    continue

                status = str(row["status"])
                if (
                    status == "archived"
                    and lexical < 3.0
                    and vector_similarity < 0.75
                ):
                    continue

                importance = float(row["importance"])
                activation = float(row["activation"])
                rank_score = (
                    float(hit.score)
                    + 0.20 * importance
                    + 0.10 * activation
                )
                episode_records.append({
                    "kind": "episode",
                    "uuid": hit.uuid,
                    "text": hit.text,
                    "score": round(float(hit.score), 6),
                    "rank_score": round(rank_score, 6),
                    "retrieval": {
                        "lexical_score": round(lexical, 6),
                        "vector_similarity": round(vector_similarity, 6),
                    },
                    "scope": str(row["scope"]),
                    "importance": importance,
                    "activation": activation,
                    "status": status,
                    "current": False,
                    "provenance": [{
                        "evidence_uuid": str(row["evidence_uuid"]),
                        "source_kind": str(row["source_kind"]),
                        "external_source_ref": row["external_source_ref"],
                        "session_id": row["session_id"],
                        "occurred_at": str(row["occurred_at"]),
                    }],
                })

        semantic_records.sort(
            key=lambda item: (item["rank_score"], item["uuid"]),
            reverse=True,
        )
        episode_records.sort(
            key=lambda item: (item["rank_score"], item["uuid"]),
            reverse=True,
        )

        # Current semantic truth is primary. Episodic history is fallback.
        eligible = semantic_records if semantic_records else episode_records
        selection_mode = (
            "semantic"
            if semantic_records
            else ("episodic" if episode_records else "none")
        )

        selected: list[dict] = []
        for record in eligible[:max_items]:
            proposed = [*selected, record]
            encoded = json.dumps(
                {
                    "brain_recall_version": "0.4.0",
                    "selection_mode": selection_mode,
                    "records": proposed,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if len(encoded) <= max_chars:
                selected.append(record)

        context = ""
        if selected:
            context = json.dumps(
                {
                    "brain_recall_version": "0.4.0",
                    "selection_mode": selection_mode,
                    "records": selected,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )

        return {
            "candidate_count": len(hits),
            "eligible_semantic_count": len(semantic_records),
            "eligible_episode_count": len(episode_records),
            "selected_count": len(selected),
            "selection_mode": selection_mode,
            "selected": selected,
            "context": context,
            "context_chars": len(context),
            "budget_chars": max_chars,
            "vector_candidate_count": sum(
                1
                for hit in hits
                if float(
                    (hit.metadata or {}).get(
                        "vector_similarity", 0.0
                    ) or 0.0
                ) > 0.0
            ),
        }

    def _ensure_session(self, db, owner_id: str, external_session_ref: str, *, now: str) -> tuple[int, str]:
        row = db.execute(
            "SELECT id, uuid FROM conversation_sessions WHERE owner_id=? AND external_session_ref=?",
            (owner_id, external_session_ref),
        ).fetchone()
        if row:
            return int(row["id"]), str(row["uuid"])
        session_uuid = new_uuid()
        cur = db.execute(
            "INSERT INTO conversation_sessions(uuid, owner_id, external_session_ref, created_at) VALUES(?,?,?,?)",
            (session_uuid, owner_id, external_session_ref, now),
        )
        return int(cur.lastrowid), session_uuid

    def _ensure_user_message(self, db, *, owner_id: str, external_session_ref: str, external_message_ref: str, content: str, occurred_at: str, metadata_json: str, now: str) -> tuple[int, str, str]:
        session_id, session_uuid = self._ensure_session(db, owner_id, external_session_ref, now=now)
        row = db.execute(
            "SELECT cm.*, cs.external_session_ref FROM conversation_messages cm "
            "JOIN conversation_sessions cs ON cs.id=cm.session_id "
            "WHERE cm.owner_id=? AND cm.external_message_ref=?",
            (owner_id, external_message_ref),
        ).fetchone()
        if row:
            self._assert_idempotent_message(row, role="user", content=content, external_session_ref=external_session_ref, occurred_at=occurred_at, metadata_json=metadata_json)
            return int(row["id"]), str(row["uuid"]), session_uuid
        message_uuid = new_uuid()
        cur = db.execute(
            "INSERT INTO conversation_messages(uuid, owner_id, session_id, external_message_ref, role, content, occurred_at, metadata_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (message_uuid, owner_id, session_id, external_message_ref, "user", content, occurred_at, metadata_json, now),
        )
        return int(cur.lastrowid), message_uuid, session_uuid

    @staticmethod
    def _assert_idempotent_message(row, *, role: str, content: str, external_session_ref: str, occurred_at: str | None, metadata_json: str | None) -> None:
        if str(row["role"]) != role or str(row["content"]) != content or str(row["external_session_ref"]) != external_session_ref:
            raise IdempotencyConflict("external_message_ref replay changed immutable message fields")
        if occurred_at is not None and str(row["occurred_at"]) != occurred_at:
            raise IdempotencyConflict("external_message_ref replay changed occurred_at")
        if metadata_json is not None and str(row["metadata_json"]) != metadata_json:
            raise IdempotencyConflict("external_message_ref replay changed metadata")

    def _require_evidence(self, db, owner_id: str, evidence_uuid: str):
        row = db.execute("SELECT * FROM evidence WHERE owner_id=? AND uuid=?", (owner_id, evidence_uuid)).fetchone()
        if row:
            return row
        other = db.execute("SELECT owner_id FROM evidence WHERE uuid=?", (evidence_uuid,)).fetchone()
        if other:
            raise OwnershipError("evidence does not belong to owner")
        raise NotFound("evidence not found")

    @staticmethod
    def _hash_payload(payload: dict) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _record_semantic_commit(db, *, owner_id: str, idempotency_key: str, request_hash: str, result: TransitionResult, now: str) -> None:
        db.execute(
            "INSERT INTO semantic_commits(uuid, owner_id, idempotency_key, request_hash, action, memory_uuid, revision_no, "
            "state_check_uuid, changed, conflict, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (new_uuid(), owner_id, idempotency_key, request_hash, result.action.value, result.memory_uuid,
             result.revision_no, result.state_check_uuid, 1 if result.changed else 0, 1 if result.conflict else 0, now),
        )

    @staticmethod
    def _memory_event_replay(db, owner_id: str, idempotency_key: str, request_hash: str):
        row = db.execute(
            "SELECT * FROM memory_events WHERE owner_id=? AND idempotency_key=?",
            (owner_id, idempotency_key),
        ).fetchone()
        if row and str(row["request_hash"]) != request_hash:
            raise IdempotencyConflict("memory-event idempotency key was reused for a different request")
        return row

    @staticmethod
    def _record_memory_event(db, owner_id: str, idempotency_key: str, request_hash: str, target_memory_uuid: str, action: str, evidence_id: int, result_revision_no: int | None, details: str | None, now: str) -> None:
        db.execute(
            "INSERT INTO memory_events(uuid, owner_id, idempotency_key, request_hash, target_memory_uuid, action, "
            "evidence_id, result_revision_no, details, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (new_uuid(), owner_id, idempotency_key, request_hash, target_memory_uuid, action, evidence_id, result_revision_no, details, now),
        )

    def _require_memory(self, db, owner_id: str, memory_uuid: str):
        row = db.execute(
            "SELECT * FROM semantic_memories WHERE owner_id=? AND uuid=?",
            (owner_id, memory_uuid),
        ).fetchone()
        if row:
            return row
        other = db.execute("SELECT owner_id FROM semantic_memories WHERE uuid=?", (memory_uuid,)).fetchone()
        if other:
            raise OwnershipError("memory does not belong to owner")
        raise NotFound("memory not found")

    @staticmethod
    def _require_text(value: str, label: str) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError(f"{label} must not be empty")
        return value

    @staticmethod
    def _clamp01(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    @staticmethod
    def _latest_revision_no(db, memory_id: int) -> int | None:
        row = db.execute(
            "SELECT MAX(revision_no) FROM memory_revisions WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def _insert_revision(self, db, *, memory_id: int, operation: str, content: str, memory_type: str, scope: str, confidence: float, change_reason: str | None, now: str):
        revision_no = (self._latest_revision_no(db, memory_id) or 0) + 1
        revision_uuid = new_uuid()
        cursor = db.execute(
            "INSERT INTO memory_revisions(uuid, memory_id, revision_no, operation, content, memory_type, scope, "
            "confidence, change_reason, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                revision_uuid,
                memory_id,
                revision_no,
                operation,
                content,
                memory_type,
                scope,
                confidence,
                change_reason,
                now,
            ),
        )
        return revision_no, revision_uuid, int(cursor.lastrowid)

    @staticmethod
    def _link_evidence(db, revision_id: int, evidence_id: int, now: str, relation_type: str = "SUPPORTS", confidence: float = 1.0, details: str | None = None):
        db.execute(
            "INSERT OR IGNORE INTO revision_evidence(revision_id, evidence_id, relation_type, confidence, details, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (revision_id, evidence_id, relation_type, confidence, details, now),
        )

    def _carry_revision_evidence(self, db, memory_id: int, new_revision_id: int, new_revision_no: int, now: str) -> None:
        prior = db.execute(
            "SELECT re.evidence_id, re.confidence, re.details FROM memory_revisions r "
            "JOIN revision_evidence re ON re.revision_id=r.id "
            "WHERE r.memory_id=? AND r.revision_no=?",
            (memory_id, new_revision_no - 1),
        ).fetchall()
        for row in prior:
            self._link_evidence(
                db,
                new_revision_id,
                int(row["evidence_id"]),
                now,
                relation_type="HISTORICAL_CONTEXT",
                confidence=min(0.95, float(row["confidence"])),
                details=row["details"],
            )

    def _link_episode_to_revision(self, db, owner_id: str, evidence_id: int, revision_uuid: str, now: str) -> None:
        episode = db.execute(
            "SELECT uuid FROM episodes WHERE owner_id=? AND evidence_id=?",
            (owner_id, evidence_id),
        ).fetchone()
        if episode:
            self._insert_relation(
                db,
                owner_id=owner_id,
                source_kind="episode",
                source_uuid=str(episode["uuid"]),
                target_kind="memory_revision",
                target_uuid=revision_uuid,
                relation_type="SUPPORTS",
                weight=1.0,
                details="Episode evidence supports semantic revision.",
                now=now,
            )

    @staticmethod
    def _insert_relation(db, *, owner_id: str, source_kind: str, source_uuid: str, target_kind: str, target_uuid: str, relation_type: str, weight: float, details: str | None, now: str) -> None:
        db.execute(
            "INSERT INTO knowledge_relations(uuid, owner_id, source_kind, source_uuid, target_kind, target_uuid, "
            "relation_type, weight, details, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                new_uuid(),
                owner_id,
                source_kind,
                source_uuid,
                target_kind,
                target_uuid,
                relation_type,
                max(0.0, min(1.0, float(weight))),
                details,
                now,
            ),
        )

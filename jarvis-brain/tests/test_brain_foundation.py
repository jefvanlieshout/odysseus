from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from jarvis_brain import (
    BrainMemoryService,
    ClaimStatus,
    GroundingError,
    IdempotencyConflict,
    OwnershipError,
    PersistenceAction,
    ProvenanceCheck,
    RelationDecision,
    SemanticCandidate,
    SemanticRelation,
    SourceKind,
    claims_are_grounded,
    derive_persistence_action,
)
from jarvis_brain.retrieval import tokenize
from jarvis_brain.schema import SCHEMA_VERSION, V1_DDL


class FakeVector:
    healthy = True

    def __init__(self):
        self.items = {}
        self.upserts = []
        self.deletes = []

    def upsert(self, *, owner_id, kind, uuid, text):
        self.items[(owner_id, kind, uuid)] = text
        self.upserts.append((owner_id, kind, uuid, text))

    def delete(self, *, owner_id, kind, uuid):
        self.items.pop((owner_id, kind, uuid), None)
        self.deletes.append((owner_id, kind, uuid))

    def search(self, *, owner_id, query, kinds, limit):
        out = [("semantic", "00000000-0000-0000-0000-000000000000", 1.0)]
        for (owner, kind, item_uuid), _text in self.items.items():
            if owner == owner_id and kind in kinds:
                out.append((kind, item_uuid, 0.8))
        return out[:limit]


class BrainFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "brain.db"
        self.vector = FakeVector()
        self.brain = BrainMemoryService(self.db, vector_index=self.vector)
        self._semantic_counter = 0
        self._control_counter = 0

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def provenance(content: str, *statuses: ClaimStatus) -> ProvenanceCheck:
        return ProvenanceCheck(content, tuple(statuses or (ClaimStatus.SUPPORTED_PARAPHRASE,)))

    def capture(self, owner="jef", ref="msg-1", text="I use fish shell on CachyOS.", session="session-1"):
        return self.brain.capture_observation(
            owner_id=owner,
            raw_text=text,
            external_source_ref=ref,
            session_id=session,
            source_kind=SourceKind.USER_MESSAGE,
        )

    def control_evidence(self, text="Please change that memory."):
        self._control_counter += 1
        return self.brain.capture_observation(
            owner_id="jef",
            raw_text=text,
            external_source_ref=f"control-{self._control_counter}",
            session_id="session-1",
            source_kind=SourceKind.EXPLICIT_USER_MEMORY,
        )

    def commit(self, *, owner, candidate, decision, final_content=None, provenance=None, key=None):
        self._semantic_counter += 1
        content = final_content or candidate.content
        return self.brain.commit_semantic_candidate(
            owner_id=owner,
            candidate=candidate,
            decision=decision,
            provenance=provenance or self.provenance(content),
            idempotency_key=key or f"semantic-{self._semantic_counter}",
            final_content=final_content,
        )

    def create_memory(self, owner="jef", ref="msg-1", text="I use fish shell on CachyOS.", key=None):
        obs = self.capture(owner, ref, text)
        candidate = SemanticCandidate(
            content="Jef uses fish shell on CachyOS.",
            memory_type="fact",
            scope="linux_desktop",
            confidence=0.95,
            evidence_uuid=obs["evidence_uuid"],
            evidence_quote="fish shell",
        )
        result = self.commit(
            owner=owner,
            candidate=candidate,
            decision=RelationDecision(SemanticRelation.NOVEL, None, 0.99),
            key=key,
        )
        return obs, result

    def test_schema_and_pragmas(self):
        self.assertEqual(self.brain.store.schema_version(), SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, 3)
        with self.brain.store.read() as db:
            self.assertEqual(int(db.execute("PRAGMA foreign_keys").fetchone()[0]), 1)
            self.assertEqual(str(db.execute("PRAGMA journal_mode").fetchone()[0]).casefold(), "wal")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"conversation_messages", "semantic_commits", "memory_events"} <= tables)
            evidence_columns = {r[1] for r in db.execute("PRAGMA table_info(evidence)")}
            self.assertIn("message_id", evidence_columns)

    def test_v1_database_migrates_explicitly_to_v3(self):
        old = Path(self.tmp.name) / "v1.db"
        db = sqlite3.connect(old)
        db.executescript(V1_DDL)
        db.execute("INSERT INTO brain_meta(key,value) VALUES('schema_version','1')")
        db.commit(); db.close()
        migrated = BrainMemoryService(old)
        self.assertEqual(migrated.store.schema_version(), 3)
        with migrated.store.read() as db2:
            tables = {r[0] for r in db2.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("semantic_commits", tables)
            job_columns = {r[1] for r in db2.execute("PRAGMA table_info(semantic_jobs)")}
            self.assertTrue({"lease_token", "lease_expires_at", "plan_json", "result_json"} <= job_columns)

    def test_future_schema_fails_closed(self):
        future = Path(self.tmp.name) / "future.db"
        db = sqlite3.connect(future)
        db.execute("CREATE TABLE brain_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO brain_meta VALUES('schema_version','999')")
        db.commit(); db.close()
        with self.assertRaises(RuntimeError):
            BrainMemoryService(future)

    def test_capture_is_transactional_idempotent_and_saves_user_message(self):
        first = self.capture()
        second = self.capture()
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["evidence_uuid"], second["evidence_uuid"])
        counts = self.brain.store.counts("jef")
        self.assertEqual(counts["conversation_sessions"], 1)
        self.assertEqual(counts["conversation_messages"], 1)
        self.assertEqual(counts["evidence"], 1)
        self.assertEqual(counts["episodes"], 1)
        self.assertEqual(counts["semantic_jobs"], 1)
        messages = self.brain.list_messages(owner_id="jef")
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"], "I use fish shell on CachyOS.")

    def test_user_evidence_requires_session_for_transcript_provenance(self):
        with self.assertRaises(ValueError):
            self.brain.capture_observation(
                owner_id="jef", raw_text="hello", external_source_ref="m", source_kind=SourceKind.USER_MESSAGE,
            )

    def test_observation_replay_with_changed_content_fails_closed(self):
        self.capture()
        with self.assertRaises(IdempotencyConflict):
            self.capture(text="Different text with same immutable message id")
        self.assertEqual(self.brain.store.counts("jef")["evidence"], 1)

    def test_all_conversation_roles_can_be_saved_without_memory_promotion(self):
        self.capture()
        for idx, role in enumerate(("assistant", "system", "tool"), 2):
            self.brain.capture_message(
                owner_id="jef", external_session_ref="session-1",
                external_message_ref=f"msg-{idx}", role=role, content=f"{role} content",
            )
        self.assertEqual([m["role"] for m in self.brain.list_messages(owner_id="jef")], ["user", "assistant", "system", "tool"])
        self.assertEqual(self.brain.store.counts("jef")["evidence"], 1)

    def test_message_replay_mismatch_fails_closed(self):
        self.brain.capture_message(owner_id="jef", external_session_ref="s", external_message_ref="a1", role="assistant", content="hello")
        with self.assertRaises(IdempotencyConflict):
            self.brain.capture_message(owner_id="jef", external_session_ref="s", external_message_ref="a1", role="assistant", content="changed")

    def test_same_external_refs_are_owner_scoped(self):
        a = self.capture("jef", "msg-1")
        b = self.capture("other", "msg-1")
        self.assertNotEqual(a["evidence_uuid"], b["evidence_uuid"])
        self.brain.capture_message(owner_id="jef", external_session_ref="s2", external_message_ref="a1", role="assistant", content="hello")
        self.brain.capture_message(owner_id="other", external_session_ref="s2", external_message_ref="a1", role="assistant", content="hello")

    def test_identity_names_are_not_hardcoded_stopwords(self):
        tokens = tokenize("Jef asked Gwen about Jarvis")
        self.assertIn("jef", tokens)
        self.assertIn("gwen", tokens)
        self.assertIn("jarvis", tokens)

    def test_python_derives_provenance_grounding(self):
        self.assertTrue(claims_are_grounded([ClaimStatus.SUPPORTED, ClaimStatus.SUPPORTED_PARAPHRASE]))
        self.assertFalse(claims_are_grounded([ClaimStatus.SUPPORTED, ClaimStatus.UNSUPPORTED]))
        self.assertFalse(claims_are_grounded([]))

    def test_python_owns_relation_to_action(self):
        target = "m1"
        self.assertEqual(derive_persistence_action(SemanticRelation.MATCH, target_memory_uuid=target), PersistenceAction.DUPLICATE)
        self.assertEqual(derive_persistence_action(SemanticRelation.STATE_CHANGE, target_memory_uuid=target), PersistenceAction.UPDATE)
        self.assertEqual(derive_persistence_action(SemanticRelation.EXTENSION, target_memory_uuid=None), PersistenceAction.CONFLICT)
        self.assertEqual(derive_persistence_action(SemanticRelation.NOVEL, target_memory_uuid=None), PersistenceAction.CREATE)
        self.assertEqual(derive_persistence_action(SemanticRelation.CONTRADICTION, target_memory_uuid=target), PersistenceAction.CONFLICT)

    def test_create_is_evidence_linked(self):
        _obs, result = self.create_memory()
        self.assertEqual(result.action, PersistenceAction.CREATE)
        self.assertEqual(result.revision_no, 1)
        evidence = self.brain.memory_evidence(owner_id="jef", memory_uuid=result.memory_uuid)
        self.assertEqual(len(evidence), 1)
        self.assertIn("fish shell", evidence[0]["raw_text"])

    def test_literal_grounding_fails_closed(self):
        obs = self.capture()
        candidate = SemanticCandidate("Jef owns a spaceship.", "fact", "other", 0.9, obs["evidence_uuid"], "owns a spaceship")
        with self.assertRaises(GroundingError):
            self.commit(owner="jef", candidate=candidate, decision=RelationDecision(SemanticRelation.NOVEL, None, 0.9))
        self.assertEqual(self.brain.store.counts("jef")["semantic_memories"], 0)

    def test_final_content_requires_its_own_provenance_verdict(self):
        obs = self.capture()
        candidate = SemanticCandidate("Jef uses fish shell.", "fact", "linux", 0.9, obs["evidence_uuid"], "fish shell")
        with self.assertRaises(GroundingError):
            self.commit(
                owner="jef", candidate=candidate,
                decision=RelationDecision(SemanticRelation.NOVEL, None, 0.9),
                final_content="Jef uses fish shell and owns a spaceship.",
                provenance=self.provenance("Jef uses fish shell."),
            )

    def test_blocked_claim_status_fails_closed(self):
        obs = self.capture()
        candidate = SemanticCandidate("Jef uses fish shell.", "fact", "linux", 0.9, obs["evidence_uuid"], "fish shell")
        with self.assertRaises(GroundingError):
            self.commit(
                owner="jef", candidate=candidate,
                decision=RelationDecision(SemanticRelation.NOVEL, None, 0.9),
                provenance=self.provenance(candidate.content, ClaimStatus.SUPPORTED, ClaimStatus.UNSUPPORTED),
            )

    def test_cross_owner_evidence_fails_closed(self):
        obs = self.capture("jef")
        candidate = SemanticCandidate("x", "fact", "x", 1, obs["evidence_uuid"], "fish shell")
        with self.assertRaises(OwnershipError):
            self.commit(owner="other", candidate=candidate, decision=RelationDecision(SemanticRelation.NOVEL, None, 1))

    def test_semantic_commit_is_idempotent(self):
        obs = self.capture()
        candidate = SemanticCandidate("Jef uses fish shell.", "fact", "linux", 0.9, obs["evidence_uuid"], "fish shell")
        decision = RelationDecision(SemanticRelation.NOVEL, None, 0.9)
        one = self.commit(owner="jef", candidate=candidate, decision=decision, key="stable-commit")
        two = self.commit(owner="jef", candidate=candidate, decision=decision, key="stable-commit")
        self.assertEqual(one, two)
        counts = self.brain.store.counts("jef")
        self.assertEqual(counts["semantic_memories"], 1)
        self.assertEqual(counts["memory_revisions"], 1)
        self.assertEqual(counts["semantic_state_checks"], 1)
        self.assertEqual(counts["semantic_commits"], 1)

    def test_semantic_idempotency_key_reuse_with_changed_request_fails(self):
        obs = self.capture()
        c1 = SemanticCandidate("Jef uses fish shell.", "fact", "linux", 0.9, obs["evidence_uuid"], "fish shell")
        self.commit(owner="jef", candidate=c1, decision=RelationDecision(SemanticRelation.NOVEL, None, 0.9), key="stable")
        c2 = SemanticCandidate("Jef uses fish shell on Linux.", "fact", "linux", 0.9, obs["evidence_uuid"], "fish shell")
        with self.assertRaises(IdempotencyConflict):
            self.commit(owner="jef", candidate=c2, decision=RelationDecision(SemanticRelation.NOVEL, None, 0.9), key="stable")

    def test_match_does_not_rewrite_memory(self):
        _obs, created = self.create_memory()
        obs2 = self.capture("jef", "msg-2", "Yep, I still use fish shell.")
        candidate = SemanticCandidate("Jef uses fish shell on CachyOS.", "fact", "linux_desktop", 0.96, obs2["evidence_uuid"], "fish shell")
        result = self.commit(owner="jef", candidate=candidate, decision=RelationDecision(SemanticRelation.MATCH, created.memory_uuid, 0.98))
        self.assertEqual(result.action, PersistenceAction.DUPLICATE)
        self.assertFalse(result.changed)
        self.assertEqual(len(self.brain.memory_history(owner_id="jef", memory_uuid=created.memory_uuid)), 1)

    def test_state_change_updates_and_preserves_history(self):
        _obs, created = self.create_memory()
        obs2 = self.capture("jef", "msg-2", "Actually I switched from fish to zsh today.")
        candidate = SemanticCandidate("Jef now uses zsh.", "fact", "linux_desktop", 0.99, obs2["evidence_uuid"], "switched from fish to zsh")
        result = self.commit(owner="jef", candidate=candidate, decision=RelationDecision(SemanticRelation.STATE_CHANGE, created.memory_uuid, 0.99, "Explicit current-state change"))
        self.assertEqual(result.action, PersistenceAction.UPDATE)
        history = self.brain.memory_history(owner_id="jef", memory_uuid=created.memory_uuid)
        self.assertEqual([h["revision_no"] for h in history], [1, 2])
        self.assertEqual(history[-1]["content"], "Jef now uses zsh.")
        evidence = self.brain.memory_evidence(owner_id="jef", memory_uuid=created.memory_uuid)
        self.assertEqual({e["relation_type"] for e in evidence}, {"SUPPORTS", "HISTORICAL_CONTEXT"})

    def test_contradiction_is_conflict_not_write(self):
        _obs, created = self.create_memory()
        obs2 = self.capture("jef", "msg-2", "Someone else says I use bash, but I did not say that.")
        candidate = SemanticCandidate("Jef uses bash.", "fact", "linux_desktop", 0.4, obs2["evidence_uuid"], "Someone else says I use bash")
        result = self.commit(owner="jef", candidate=candidate, decision=RelationDecision(SemanticRelation.CONTRADICTION, created.memory_uuid, 0.8))
        self.assertEqual(result.action, PersistenceAction.CONFLICT)
        self.assertFalse(result.changed)
        self.assertEqual(len(self.brain.memory_history(owner_id="jef", memory_uuid=created.memory_uuid)), 1)

    def test_forget_is_provenance_linked_and_removes_vector_candidate(self):
        _obs, created = self.create_memory()
        control = self.control_evidence("Forget that fish-shell memory.")
        rev = self.brain.forget_memory(
            owner_id="jef", memory_uuid=created.memory_uuid, evidence_uuid=control["evidence_uuid"],
            idempotency_key="forget-1", reason="User asked to forget",
        )
        self.assertEqual(rev, 2)
        self.assertEqual(self.brain.list_memories(owner_id="jef"), [])
        evidence = self.brain.memory_evidence(owner_id="jef", memory_uuid=created.memory_uuid)
        self.assertIn("MEMORY_CONTROL", {e["relation_type"] for e in evidence})
        self.assertEqual(self.brain.memory_events(owner_id="jef", memory_uuid=created.memory_uuid)[0]["action"], "FORGET")
        self.assertIn(("jef", "semantic", created.memory_uuid), self.vector.deletes)

    def test_forget_retry_is_idempotent(self):
        _obs, created = self.create_memory()
        control = self.control_evidence("Forget it.")
        kwargs = dict(owner_id="jef", memory_uuid=created.memory_uuid, evidence_uuid=control["evidence_uuid"], idempotency_key="forget-1")
        a = self.brain.forget_memory(**kwargs)
        b = self.brain.forget_memory(**kwargs)
        self.assertEqual(a, b)
        self.assertEqual(self.brain.store.counts("jef")["memory_events"], 1)
        self.assertEqual(len(self.brain.memory_history(owner_id="jef", memory_uuid=created.memory_uuid)), 2)

    def test_erase_keeps_raw_evidence_and_action_event(self):
        _obs, created = self.create_memory()
        control = self.control_evidence("Erase that semantic memory.")
        self.brain.erase_memory(owner_id="jef", memory_uuid=created.memory_uuid, evidence_uuid=control["evidence_uuid"], idempotency_key="erase-1")
        counts = self.brain.store.counts("jef")
        self.assertEqual(counts["semantic_memories"], 0)
        self.assertEqual(counts["memory_revisions"], 0)
        self.assertEqual(counts["evidence"], 2)
        self.assertEqual(counts["memory_events"], 1)
        self.assertIn(("jef", "semantic", created.memory_uuid), self.vector.deletes)

    def test_pin_is_priority_not_semantic_updated_at_and_is_audited(self):
        _obs, created = self.create_memory()
        before = self.brain.list_memories(owner_id="jef")[0]
        control = self.control_evidence("Pin that memory.")
        self.brain.pin_memory(owner_id="jef", memory_uuid=created.memory_uuid, pinned=True, evidence_uuid=control["evidence_uuid"], idempotency_key="pin-1")
        after = self.brain.list_memories(owner_id="jef")[0]
        self.assertEqual(before["updated_at"], after["updated_at"])
        self.assertTrue(after["pinned"])
        self.assertEqual(len(self.brain.memory_history(owner_id="jef", memory_uuid=created.memory_uuid)), 1)
        self.assertEqual(self.brain.memory_events(owner_id="jef", memory_uuid=created.memory_uuid)[0]["action"], "PIN")

    def test_episode_consolidation_refreshes_vector_text(self):
        obs = self.capture()
        self.brain.set_episode_state(
            owner_id="jef", episode_uuid=obs["episode_uuid"], summary="Configured fish shell on CachyOS.",
            scope="linux", importance=0.8, activation=1.0, status="active", semantic_candidate=True,
        )
        self.assertEqual(self.vector.items[("jef", "episode", obs["episode_uuid"])], "Configured fish shell on CachyOS.")

    def test_search_is_owner_scoped_and_vector_is_candidate_only(self):
        _obs, created = self.create_memory("jef")
        self.create_memory("other", "other-msg", "I use fish shell too.")
        hits = self.brain.search(owner_id="jef", query="fish shell", limit=10)
        self.assertTrue(any(h.uuid == created.memory_uuid for h in hits))
        self.assertFalse(any(h.uuid == "00000000-0000-0000-0000-000000000000" for h in hits))
        self.assertFalse(any("other" in str(h.metadata) for h in hits))

    def test_vector_candidate_can_surface_only_after_sqlite_resolution(self):
        _obs, created = self.create_memory()
        hits = self.brain.search(owner_id="jef", query="totally unrelated tokens", limit=10, include_episodes=False)
        self.assertTrue(any(h.uuid == created.memory_uuid for h in hits))
        self.assertFalse(any(h.uuid == "00000000-0000-0000-0000-000000000000" for h in hits))


if __name__ == "__main__":
    unittest.main()

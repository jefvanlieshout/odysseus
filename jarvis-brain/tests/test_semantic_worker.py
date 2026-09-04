from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jarvis_brain import (
    BrainMemoryService,
    CandidateProposal,
    ClaimStatus,
    ConsolidationProposal,
    IdempotencyConflict,
    PersistenceAction,
    ProvenanceAssessment,
    ProvenanceCheck,
    RelationDecision,
    SemanticCandidate,
    SemanticRelation,
    SemanticWorker,
    SourceKind,
)
from jarvis_brain.schema import V1_DDL, V2_DDL


class ScriptedReasoner:
    def __init__(self, *, proposals=(), provenance=None, relation=None, consolidation=None):
        self.proposals = list(proposals)
        self.provenance_script = list(provenance or [])
        self.relation_value = relation
        self.consolidation_value = consolidation
        self.calls = {
            "propose": 0,
            "provenance": 0,
            "relation": 0,
            "consolidate": 0,
        }
        self.raise_on_propose = None

    def propose_candidates(self, **kwargs):
        self.calls["propose"] += 1
        if self.raise_on_propose:
            raise self.raise_on_propose
        return tuple(self.proposals)

    def check_provenance(self, **kwargs):
        self.calls["provenance"] += 1
        if self.provenance_script:
            item = self.provenance_script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return ProvenanceAssessment((ClaimStatus.SUPPORTED_PARAPHRASE,), None)

    def classify_relation(self, **kwargs):
        self.calls["relation"] += 1
        if callable(self.relation_value):
            return self.relation_value(**kwargs)
        if self.relation_value is not None:
            return self.relation_value
        return RelationDecision(SemanticRelation.NOVEL, None, 0.95, "new durable state")

    def consolidate(self, **kwargs):
        self.calls["consolidate"] += 1
        if callable(self.consolidation_value):
            return self.consolidation_value(**kwargs)
        if self.consolidation_value is not None:
            return self.consolidation_value
        return ConsolidationProposal(kwargs["candidate"].content)


class SemanticWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "brain.db"
        self.brain = BrainMemoryService(self.db)
        self.ref = 0

    def tearDown(self):
        self.tmp.cleanup()

    def capture(self, text="I prefer root-cause Linux fixes.", owner="jef"):
        self.ref += 1
        return self.brain.capture_observation(
            owner_id=owner,
            raw_text=text,
            external_source_ref=f"user-{self.ref}",
            session_id="session-1",
            source_kind=SourceKind.USER_MESSAGE,
        )

    def worker(self, reasoner, **kwargs):
        return SemanticWorker(
            self.brain,
            reasoner,
            worker_id=kwargs.pop("worker_id", "worker-a"),
            lease_seconds=kwargs.pop("lease_seconds", 30),
            max_attempts=kwargs.pop("max_attempts", 5),
            **kwargs,
        )

    def create_existing_memory(self, text="The user uses fish shell on Linux."):
        obs = self.capture("I use fish shell on Linux.")
        candidate = SemanticCandidate(
            content=text,
            memory_type="fact",
            scope="linux",
            confidence=0.95,
            evidence_uuid=obs["evidence_uuid"],
            evidence_quote="fish shell",
        )
        result = self.brain.commit_semantic_candidate(
            owner_id="jef",
            candidate=candidate,
            decision=RelationDecision(SemanticRelation.NOVEL, None, 0.99),
            provenance=ProvenanceCheck(text, (ClaimStatus.SUPPORTED_PARAPHRASE,)),
            idempotency_key="seed-existing-memory",
        )
        with self.brain.store.write() as db:
            db.execute(
                "UPDATE semantic_jobs SET status='done', finished_at=updated_at "
                "WHERE uuid=?",
                (obs["job_uuid"],),
            )
        return result.memory_uuid

    def test_v2_database_migrates_to_v3_job_lease_columns(self):
        old = Path(self.tmp.name) / "v2.db"
        db = sqlite3.connect(old)
        db.executescript(V1_DDL)
        db.executescript(V2_DDL)
        columns = {row[1] for row in db.execute("PRAGMA table_info(evidence)")}
        if "message_id" not in columns:
            db.execute("ALTER TABLE evidence ADD COLUMN message_id INTEGER")
        db.execute("INSERT INTO brain_meta(key,value) VALUES('schema_version','2')")
        db.commit(); db.close()
        migrated = BrainMemoryService(old)
        self.assertEqual(migrated.store.schema_version(), 3)
        with migrated.store.read() as db2:
            columns = {row[1] for row in db2.execute("PRAGMA table_info(semantic_jobs)")}
        self.assertTrue({
            "lease_token", "lease_expires_at", "next_attempt_at",
            "plan_json", "result_json", "finished_at",
        } <= columns)

    def test_job_claim_is_atomic_and_blocks_second_worker(self):
        self.capture()
        first = self.brain.claim_semantic_job(worker_id="a", lease_seconds=60)
        self.assertIsNotNone(first)
        second = self.brain.claim_semantic_job(worker_id="b", lease_seconds=60)
        self.assertIsNone(second)
        self.assertEqual(first["attempt_count"], 1)

    def test_expired_processing_lease_is_reclaimable(self):
        self.capture()
        first = self.brain.claim_semantic_job(worker_id="a", lease_seconds=60)
        with self.brain.store.write() as db:
            db.execute(
                "UPDATE semantic_jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE uuid=?",
                (first["job_uuid"],),
            )
        second = self.brain.claim_semantic_job(worker_id="b", lease_seconds=60)
        self.assertIsNotNone(second)
        self.assertNotEqual(first["lease_token"], second["lease_token"])
        self.assertEqual(second["attempt_count"], 2)

    def test_retry_backoff_blocks_newer_job_for_same_owner(self):
        first = self.capture("first", owner="jef")
        second = self.capture("second", owner="jef")

        lease = self.brain.claim_semantic_job(worker_id="a", lease_seconds=60)
        self.assertEqual(lease["job_uuid"], first["job_uuid"])
        status = self.brain.retry_semantic_job(
            job_uuid=lease["job_uuid"],
            lease_token=lease["lease_token"],
            error="temporary model failure",
            max_attempts=5,
        )
        self.assertEqual(status, "retry")

        # The older retry is backing off, so the newer job must NOT leapfrog it.
        blocked = self.brain.claim_semantic_job(worker_id="b", lease_seconds=60)
        self.assertIsNone(blocked)

        with self.brain.store.write() as db:
            db.execute(
                "UPDATE semantic_jobs SET next_attempt_at='2000-01-01T00:00:00+00:00' "
                "WHERE uuid=?",
                (first["job_uuid"],),
            )

        retry_lease = self.brain.claim_semantic_job(worker_id="c", lease_seconds=60)
        self.assertEqual(retry_lease["job_uuid"], first["job_uuid"])
        self.brain.complete_semantic_job(
            job_uuid=retry_lease["job_uuid"],
            lease_token=retry_lease["lease_token"],
            result_json='{"committed":[],"rejections":[]}',
        )

        next_lease = self.brain.claim_semantic_job(worker_id="d", lease_seconds=60)
        self.assertEqual(next_lease["job_uuid"], second["job_uuid"])

    def test_retry_backoff_only_blocks_same_owner(self):
        first = self.capture("first", owner="jef")
        other = self.capture("other", owner="alice")

        lease = self.brain.claim_semantic_job(worker_id="a", lease_seconds=60)
        self.assertEqual(lease["job_uuid"], first["job_uuid"])
        self.brain.retry_semantic_job(
            job_uuid=lease["job_uuid"],
            lease_token=lease["lease_token"],
            error="temporary model failure",
            max_attempts=5,
        )

        # Alice is independent and should not be head-of-line blocked by Jef.
        other_lease = self.brain.claim_semantic_job(worker_id="b", lease_seconds=60)
        self.assertEqual(other_lease["job_uuid"], other["job_uuid"])

    def test_active_processing_job_blocks_newer_same_owner(self):
        first = self.capture("first", owner="jef")
        self.capture("second", owner="jef")

        lease = self.brain.claim_semantic_job(worker_id="a", lease_seconds=60)
        self.assertEqual(lease["job_uuid"], first["job_uuid"])

        # Active lease on the owner's head job prevents a second worker from
        # processing a newer semantic event out of order.
        self.assertIsNone(
            self.brain.claim_semantic_job(worker_id="b", lease_seconds=60)
        )

    def test_wrong_lease_cannot_read_save_or_complete_job(self):
        self.capture()
        lease = self.brain.claim_semantic_job(worker_id="a", lease_seconds=60)
        for operation in (
            lambda: self.brain.semantic_job_context(job_uuid=lease["job_uuid"], lease_token="wrong"),
            lambda: self.brain.save_semantic_job_plan(job_uuid=lease["job_uuid"], lease_token="wrong", plan_json='{"transitions":[],"rejections":[]}'),
            lambda: self.brain.complete_semantic_job(job_uuid=lease["job_uuid"], lease_token="wrong", result_json='{}'),
        ):
            with self.assertRaises(IdempotencyConflict):
                operation()

    def test_no_candidates_finishes_job_without_semantic_write(self):
        self.capture("hello qwen")
        reasoner = ScriptedReasoner(proposals=[])
        result = self.worker(reasoner).run_once()
        self.assertEqual(result.status, "done")
        counts = self.brain.store.counts("jef")
        self.assertEqual(counts["semantic_memories"], 0)
        with self.brain.store.read() as db:
            row = db.execute("SELECT status, plan_json, result_json FROM semantic_jobs").fetchone()
        self.assertEqual(row["status"], "done")
        self.assertIsNotNone(row["plan_json"])
        self.assertIsNotNone(row["result_json"])

    def test_novel_grounded_candidate_creates_memory(self):
        self.capture()
        reasoner = ScriptedReasoner(proposals=[
            CandidateProposal(
                "The user prefers root-cause Linux fixes.",
                "preference", "linux", 0.95, "root-cause Linux fixes",
            )
        ])
        result = self.worker(reasoner).run_once()
        self.assertEqual(result.status, "done")
        self.assertEqual(len(result.committed), 1)
        self.assertEqual(result.committed[0].action, PersistenceAction.CREATE)
        memories = self.brain.list_memories(owner_id="jef")
        self.assertEqual(memories[0]["current_content"], "The user prefers root-cause Linux fixes.")

    def test_nonliteral_evidence_quote_is_processing_failure_and_retries(self):
        self.capture()
        reasoner = ScriptedReasoner(proposals=[
            CandidateProposal("The user prefers root-cause Linux fixes.", evidence_quote="Windows workaround")
        ])
        result = self.worker(reasoner).run_once()
        self.assertEqual(result.status, "retry")
        self.assertEqual(len(result.committed), 0)
        self.assertEqual(reasoner.calls["relation"], 0)
        self.assertIn("literal span", result.error or "")

    def test_blocking_provenance_without_repair_rejects_candidate(self):
        self.capture()
        reasoner = ScriptedReasoner(
            proposals=[CandidateProposal("The user prefers Linux.", evidence_quote="Linux fixes")],
            provenance=[ProvenanceAssessment((ClaimStatus.UNSUPPORTED,), None)],
        )
        result = self.worker(reasoner).run_once()
        self.assertEqual(result.status, "done")
        self.assertEqual(len(result.committed), 0)
        self.assertEqual(reasoner.calls["relation"], 0)

    def test_one_repair_pass_then_verify_can_create(self):
        self.capture()
        reasoner = ScriptedReasoner(
            proposals=[CandidateProposal(
                "The user always refuses all workarounds.",
                "preference", "linux", 0.9, "root-cause Linux fixes",
            )],
            provenance=[
                ProvenanceAssessment(
                    (ClaimStatus.UNSUPPORTED,),
                    "The user prefers root-cause Linux fixes.",
                ),
                ProvenanceAssessment((ClaimStatus.SUPPORTED_PARAPHRASE,), None),
            ],
        )
        result = self.worker(reasoner).run_once()
        self.assertEqual(result.status, "done")
        self.assertEqual(len(result.committed), 1)
        self.assertEqual(
            self.brain.list_memories(owner_id="jef")[0]["current_content"],
            "The user prefers root-cause Linux fixes.",
        )
        self.assertEqual(reasoner.calls["provenance"], 2)

    def test_relation_target_outside_retrieved_owner_set_retries_job(self):
        self.capture()
        reasoner = ScriptedReasoner(
            proposals=[CandidateProposal(
                "The user prefers root-cause Linux fixes.",
                evidence_quote="root-cause Linux fixes",
            )],
            relation=RelationDecision(
                SemanticRelation.STATE_CHANGE,
                "00000000-0000-4000-8000-000000000123",
                0.99,
            ),
        )
        result = self.worker(reasoner).run_once()
        self.assertEqual(result.status, "retry")
        self.assertEqual(len(result.committed), 0)
        self.assertIn("outside", result.error or "")

    def test_match_is_duplicate_and_does_not_rewrite(self):
        target = self.create_existing_memory()
        self.capture("I use fish shell on Linux.")
        reasoner = ScriptedReasoner(
            proposals=[CandidateProposal(
                "The user uses fish shell on Linux.", "fact", "linux", 0.95, "fish shell",
            )],
            relation=lambda **kwargs: RelationDecision(
                SemanticRelation.MATCH, kwargs["neighbors"][0].uuid, 0.99, "same fact"
            ),
        )
        before = self.brain.memory_history(owner_id="jef", memory_uuid=target)
        result = self.worker(reasoner).run_once()
        after = self.brain.memory_history(owner_id="jef", memory_uuid=target)
        self.assertEqual(result.committed[0].action, PersistenceAction.DUPLICATE)
        self.assertEqual(len(before), len(after))

    def test_state_change_uses_consolidator_and_final_provenance_before_update(self):
        target = self.create_existing_memory()
        self.capture("I switched from fish shell to zsh shell on Linux.")
        reasoner = ScriptedReasoner(
            proposals=[CandidateProposal(
                "The user now uses zsh shell on Linux.", "fact", "linux", 0.95, "zsh shell",
            )],
            provenance=[
                ProvenanceAssessment((ClaimStatus.SUPPORTED_PARAPHRASE,), None),
                ProvenanceAssessment((ClaimStatus.SUPPORTED_PARAPHRASE,), None),
            ],
            relation=lambda **kwargs: RelationDecision(
                SemanticRelation.STATE_CHANGE, kwargs["neighbors"][0].uuid, 0.99, "new shell state"
            ),
            consolidation=ConsolidationProposal(
                "The user currently uses zsh shell on Linux.",
                memory_type="fact",
                scope="linux",
                confidence=0.94,
                change_reason="Switched from fish to zsh.",
            ),
        )
        result = self.worker(reasoner).run_once()
        self.assertEqual(result.committed[0].action, PersistenceAction.UPDATE)
        self.assertEqual(reasoner.calls["consolidate"], 1)
        self.assertEqual(reasoner.calls["provenance"], 2)
        memory = self.brain.list_memories(owner_id="jef")[0]
        self.assertEqual(memory["current_content"], "The user currently uses zsh shell on Linux.")
        self.assertEqual(len(self.brain.memory_history(owner_id="jef", memory_uuid=target)), 2)

    def test_blocking_final_provenance_prevents_update(self):
        target = self.create_existing_memory()
        self.capture("I switched from fish shell to zsh shell on Linux.")
        reasoner = ScriptedReasoner(
            proposals=[CandidateProposal(
                "The user now uses zsh shell on Linux.", "fact", "linux", 0.95, "zsh shell",
            )],
            provenance=[
                ProvenanceAssessment((ClaimStatus.SUPPORTED_PARAPHRASE,), None),
                ProvenanceAssessment((ClaimStatus.UNSUPPORTED,), None),
            ],
            relation=lambda **kwargs: RelationDecision(
                SemanticRelation.STATE_CHANGE, kwargs["neighbors"][0].uuid, 0.99
            ),
            consolidation=ConsolidationProposal("The user uses zsh because fish is unreliable."),
        )
        result = self.worker(reasoner).run_once()
        self.assertEqual(len(result.committed), 0)
        self.assertEqual(len(self.brain.memory_history(owner_id="jef", memory_uuid=target)), 1)

    def test_model_exception_during_provenance_requeues_job(self):
        self.capture()
        reasoner = ScriptedReasoner(
            proposals=[CandidateProposal(
                "The user prefers root-cause Linux fixes.",
                "preference", "linux", 0.95, "root-cause Linux fixes",
            )],
            provenance=[RuntimeError("provenance model failed")],
        )
        result = self.worker(reasoner, max_attempts=3).run_once()
        self.assertEqual(result.status, "retry")
        self.assertIn("provenance model failed", result.error or "")

    def test_model_exception_during_relation_requeues_job(self):
        self.capture()

        def broken_relation(**kwargs):
            raise RuntimeError("relation model failed")

        reasoner = ScriptedReasoner(
            proposals=[CandidateProposal(
                "The user prefers root-cause Linux fixes.",
                "preference", "linux", 0.95, "root-cause Linux fixes",
            )],
            relation=broken_relation,
        )
        result = self.worker(reasoner, max_attempts=3).run_once()
        self.assertEqual(result.status, "retry")
        self.assertIn("relation model failed", result.error or "")

    def test_model_exception_during_consolidation_requeues_job(self):
        target = self.create_existing_memory()
        self.capture("I switched from fish shell to zsh shell on Linux.")

        def broken_consolidation(**kwargs):
            raise RuntimeError("consolidation model failed")

        reasoner = ScriptedReasoner(
            proposals=[CandidateProposal(
                "The user now uses zsh shell on Linux.",
                "fact", "linux", 0.95, "zsh shell",
            )],
            relation=lambda **kwargs: RelationDecision(
                SemanticRelation.STATE_CHANGE,
                kwargs["neighbors"][0].uuid,
                0.99,
                "new shell state",
            ),
            consolidation=broken_consolidation,
        )
        result = self.worker(reasoner, max_attempts=3).run_once()
        self.assertEqual(result.status, "retry")
        self.assertIn("consolidation model failed", result.error or "")
        self.assertEqual(len(self.brain.memory_history(owner_id="jef", memory_uuid=target)), 1)

    def test_model_exception_requeues_job_without_losing_evidence(self):
        obs = self.capture()
        reasoner = ScriptedReasoner()
        reasoner.raise_on_propose = RuntimeError("model offline")
        result = self.worker(reasoner, max_attempts=3).run_once()
        self.assertEqual(result.status, "retry")
        with self.brain.store.read() as db:
            row = db.execute("SELECT status, attempt_count, last_error FROM semantic_jobs WHERE uuid=?", (obs["job_uuid"],)).fetchone()
        self.assertEqual(row["status"], "retry")
        self.assertEqual(row["attempt_count"], 1)
        self.assertIn("model offline", row["last_error"])
        self.assertEqual(self.brain.store.counts("jef")["evidence"], 1)

    def test_persisted_plan_replays_without_reasoning_again(self):
        obs = self.capture()
        lease = self.brain.claim_semantic_job(worker_id="planner", lease_seconds=60)
        context = self.brain.semantic_job_context(job_uuid=lease["job_uuid"], lease_token=lease["lease_token"])
        first_reasoner = ScriptedReasoner(proposals=[CandidateProposal(
            "The user prefers root-cause Linux fixes.", "preference", "linux", 0.95, "root-cause Linux fixes",
        )])
        planner = self.worker(first_reasoner)
        plan = planner._build_plan(context)
        self.brain.save_semantic_job_plan(job_uuid=lease["job_uuid"], lease_token=lease["lease_token"], plan_json=plan.to_json())
        self.brain.retry_semantic_job(job_uuid=lease["job_uuid"], lease_token=lease["lease_token"], error="simulated restart", max_attempts=5)
        with self.brain.store.write() as db:
            db.execute("UPDATE semantic_jobs SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE uuid=?", (obs["job_uuid"],))

        bomb = ScriptedReasoner()
        bomb.raise_on_propose = AssertionError("reasoner must not be called when plan_json exists")
        result = self.worker(bomb, worker_id="replay").run_once()
        self.assertEqual(result.status, "done")
        self.assertEqual(bomb.calls["propose"], 0)
        self.assertEqual(self.brain.store.counts("jef")["semantic_memories"], 1)

    def test_partial_commit_retry_replays_same_plan_idempotently(self):
        obs = self.capture("I prefer root-cause Linux fixes and I use fish shell.")
        reasoner = ScriptedReasoner(proposals=[
            CandidateProposal("The user prefers root-cause Linux fixes.", "preference", "linux", 0.95, "root-cause Linux fixes"),
            CandidateProposal("The user uses fish shell.", "fact", "linux", 0.9, "fish shell"),
        ])
        worker = self.worker(reasoner)

        original_complete = self.brain.complete_semantic_job
        called = {"n": 0}

        def fail_complete_once(**kwargs):
            called["n"] += 1
            if called["n"] == 1:
                raise RuntimeError("crash after semantic commits")
            return original_complete(**kwargs)

        self.brain.complete_semantic_job = fail_complete_once  # type: ignore[method-assign]
        first = worker.run_once()
        self.assertEqual(first.status, "retry")
        self.assertEqual(self.brain.store.counts("jef")["semantic_memories"], 2)
        self.assertEqual(self.brain.store.counts("jef")["semantic_commits"], 2)

        self.brain.complete_semantic_job = original_complete  # type: ignore[method-assign]
        with self.brain.store.write() as db:
            db.execute("UPDATE semantic_jobs SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE uuid=?", (obs["job_uuid"],))

        bomb = ScriptedReasoner()
        bomb.raise_on_propose = AssertionError("saved plan must be replayed")
        second = self.worker(bomb, worker_id="worker-b").run_once()
        self.assertEqual(second.status, "done")
        self.assertEqual(bomb.calls["propose"], 0)
        self.assertEqual(self.brain.store.counts("jef")["semantic_memories"], 2)
        self.assertEqual(self.brain.store.counts("jef")["semantic_commits"], 2)


if __name__ == "__main__":
    unittest.main()

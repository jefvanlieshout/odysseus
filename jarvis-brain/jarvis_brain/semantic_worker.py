from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, Sequence

from .service import BrainMemoryService, IdempotencyConflict
from .transitions import derive_persistence_action
from .types import (
    ClaimStatus,
    PersistenceAction,
    ProvenanceCheck,
    RelationDecision,
    SearchHit,
    SemanticCandidate,
    SemanticRelation,
    TransitionResult,
    claims_are_grounded,
)


_ALLOWED_MEMORY_TYPES = frozenset({
    "preference",
    "fact",
    "project",
    "constraint",
    "relationship",
    "other",
})


@dataclass(frozen=True)
class CandidateProposal:
    content: str
    memory_type: str = "other"
    scope: str = "unspecified"
    confidence: float = 0.5
    evidence_quote: str = ""


@dataclass(frozen=True)
class ProvenanceAssessment:
    claim_statuses: tuple[ClaimStatus, ...]
    repaired_content: str | None = None


@dataclass(frozen=True)
class ConsolidationProposal:
    content: str
    memory_type: str | None = None
    scope: str | None = None
    confidence: float | None = None
    change_reason: str | None = None


class SemanticReasoner(Protocol):
    """Pure reasoning surface used by the background semantic worker.

    The model may propose content and classify relationships.  It never returns
    CREATE/UPDATE/DUPLICATE/CONFLICT; Python derives persistence from
    ``SemanticRelation``.
    """

    def propose_candidates(
        self,
        *,
        evidence_text: str,
        evidence_uuid: str,
        owner_id: str,
    ) -> Sequence[CandidateProposal]:
        ...

    def check_provenance(
        self,
        *,
        content: str,
        authoritative_evidence: str,
        supporting_memories: Sequence[SearchHit],
        allow_repair: bool,
    ) -> ProvenanceAssessment:
        ...

    def classify_relation(
        self,
        *,
        candidate: SemanticCandidate,
        neighbors: Sequence[SearchHit],
    ) -> RelationDecision:
        ...

    def consolidate(
        self,
        *,
        candidate: SemanticCandidate,
        target: SearchHit,
        relation: SemanticRelation,
    ) -> ConsolidationProposal:
        ...


@dataclass(frozen=True)
class PlannedTransition:
    candidate: SemanticCandidate
    decision: RelationDecision
    provenance: ProvenanceCheck
    final_content: str
    change_reason: str | None

    def to_dict(self) -> dict:
        return {
            "candidate": {
                "content": self.candidate.content,
                "memory_type": self.candidate.memory_type,
                "scope": self.candidate.scope,
                "confidence": self.candidate.confidence,
                "evidence_uuid": self.candidate.evidence_uuid,
                "evidence_quote": self.candidate.evidence_quote,
            },
            "decision": {
                "relation": SemanticRelation(self.decision.relation).value,
                "target_memory_uuid": self.decision.target_memory_uuid,
                "confidence": self.decision.confidence,
                "explanation": self.decision.explanation,
            },
            "provenance": {
                "checked_content": self.provenance.checked_content,
                "claim_statuses": [ClaimStatus(s).value for s in self.provenance.claim_statuses],
            },
            "final_content": self.final_content,
            "change_reason": self.change_reason,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "PlannedTransition":
        candidate = payload["candidate"]
        decision = payload["decision"]
        provenance = payload["provenance"]
        return cls(
            candidate=SemanticCandidate(
                content=str(candidate["content"]),
                memory_type=str(candidate["memory_type"]),
                scope=str(candidate["scope"]),
                confidence=float(candidate["confidence"]),
                evidence_uuid=str(candidate["evidence_uuid"]),
                evidence_quote=str(candidate["evidence_quote"]),
            ),
            decision=RelationDecision(
                relation=SemanticRelation(str(decision["relation"])),
                target_memory_uuid=(
                    str(decision["target_memory_uuid"])
                    if decision.get("target_memory_uuid") is not None
                    else None
                ),
                confidence=float(decision["confidence"]),
                explanation=decision.get("explanation"),
            ),
            provenance=ProvenanceCheck(
                checked_content=str(provenance["checked_content"]),
                claim_statuses=tuple(ClaimStatus(s) for s in provenance["claim_statuses"]),
            ),
            final_content=str(payload["final_content"]),
            change_reason=payload.get("change_reason"),
        )


@dataclass(frozen=True)
class SemanticJobPlan:
    transitions: tuple[PlannedTransition, ...]
    rejections: tuple[dict, ...]

    def to_json(self) -> str:
        return json.dumps(
            {
                "transitions": [t.to_dict() for t in self.transitions],
                "rejections": list(self.rejections),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> "SemanticJobPlan":
        payload = json.loads(value)
        return cls(
            transitions=tuple(PlannedTransition.from_dict(x) for x in payload.get("transitions", [])),
            rejections=tuple(dict(x) for x in payload.get("rejections", [])),
        )


@dataclass(frozen=True)
class WorkerRunResult:
    job_uuid: str | None
    status: str
    committed: tuple[TransitionResult, ...] = ()
    rejections: tuple[dict, ...] = ()
    error: str | None = None


class SemanticWorker:
    """Lease-based, crash-replay-safe semantic consolidation controller."""

    def __init__(
        self,
        service: BrainMemoryService,
        reasoner: SemanticReasoner,
        *,
        worker_id: str,
        max_candidates: int = 4,
        neighbor_limit: int = 8,
        lease_seconds: int = 120,
        max_attempts: int = 5,
    ):
        self.service = service
        self.reasoner = reasoner
        self.worker_id = str(worker_id or "").strip()
        if not self.worker_id:
            raise ValueError("worker_id is required")
        self.max_candidates = max(1, min(int(max_candidates), 8))
        self.neighbor_limit = max(1, min(int(neighbor_limit), 20))
        self.lease_seconds = max(5, min(int(lease_seconds), 3600))
        self.max_attempts = max(1, min(int(max_attempts), 20))

    def run_once(self) -> WorkerRunResult:
        lease = self.service.claim_semantic_job(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if lease is None:
            return WorkerRunResult(None, "idle")

        job_uuid = str(lease["job_uuid"])
        lease_token = str(lease["lease_token"])
        try:
            context = self.service.semantic_job_context(
                job_uuid=job_uuid,
                lease_token=lease_token,
            )
            existing_plan = context.get("plan_json")
            if existing_plan:
                plan = SemanticJobPlan.from_json(str(existing_plan))
            else:
                plan = self._build_plan(context)
                self.service.save_semantic_job_plan(
                    job_uuid=job_uuid,
                    lease_token=lease_token,
                    plan_json=plan.to_json(),
                )

            committed: list[TransitionResult] = []
            for index, transition in enumerate(plan.transitions):
                result = self.service.commit_semantic_candidate(
                    owner_id=str(context["owner_id"]),
                    candidate=transition.candidate,
                    decision=transition.decision,
                    provenance=transition.provenance,
                    idempotency_key=f"semantic-job:{job_uuid}:{index}",
                    final_content=transition.final_content,
                    change_reason=transition.change_reason,
                )
                committed.append(result)

            result_json = json.dumps(
                {
                    "committed": [
                        {
                            "action": result.action.value,
                            "memory_uuid": result.memory_uuid,
                            "revision_no": result.revision_no,
                            "state_check_uuid": result.state_check_uuid,
                            "changed": result.changed,
                            "conflict": result.conflict,
                        }
                        for result in committed
                    ],
                    "rejections": list(plan.rejections),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            self.service.complete_semantic_job(
                job_uuid=job_uuid,
                lease_token=lease_token,
                result_json=result_json,
            )
            return WorkerRunResult(
                job_uuid,
                "done",
                tuple(committed),
                plan.rejections,
            )
        except Exception as exc:
            status = self.service.retry_semantic_job(
                job_uuid=job_uuid,
                lease_token=lease_token,
                error=f"{type(exc).__name__}: {exc}",
                max_attempts=self.max_attempts,
            )
            return WorkerRunResult(job_uuid, status, error=f"{type(exc).__name__}: {exc}")

    def _build_plan(self, context: dict) -> SemanticJobPlan:
        owner_id = str(context["owner_id"])
        evidence_uuid = str(context["evidence_uuid"])
        evidence_text = str(context["raw_text"])

        proposals = list(
            self.reasoner.propose_candidates(
                evidence_text=evidence_text,
                evidence_uuid=evidence_uuid,
                owner_id=owner_id,
            )
        )[: self.max_candidates]

        transitions: list[PlannedTransition] = []
        rejections: list[dict] = []

        for index, proposal in enumerate(proposals):
            # Semantic rejection is represented explicitly by ``None``.
            # Processing/model/validation exceptions are NOT semantic decisions:
            # let them escape so run_once() releases the whole job to retry.
            transition = self._plan_candidate(
                owner_id=owner_id,
                evidence_uuid=evidence_uuid,
                evidence_text=evidence_text,
                proposal=proposal,
            )
            if transition is None:
                rejections.append({"candidate_index": index, "reason": "not durable/grounded"})
            else:
                transitions.append(transition)

        return SemanticJobPlan(tuple(transitions), tuple(rejections))

    def _plan_candidate(
        self,
        *,
        owner_id: str,
        evidence_uuid: str,
        evidence_text: str,
        proposal: CandidateProposal,
    ) -> PlannedTransition | None:
        content = self._require_candidate_text(proposal.content)
        quote = str(proposal.evidence_quote or "").strip()
        if not quote or quote not in evidence_text:
            raise ValueError("candidate evidence_quote is not a literal span of authoritative evidence")

        memory_type = str(proposal.memory_type or "other").strip().casefold()
        if memory_type not in _ALLOWED_MEMORY_TYPES:
            memory_type = "other"
        scope = str(proposal.scope or "unspecified").strip() or "unspecified"
        confidence = self._clamp01(proposal.confidence)

        first = self.reasoner.check_provenance(
            content=content,
            authoritative_evidence=evidence_text,
            supporting_memories=(),
            allow_repair=True,
        )
        statuses = tuple(ClaimStatus(s) for s in first.claim_statuses)
        if not statuses:
            raise ValueError("provenance verifier returned no claim statuses")

        if claims_are_grounded(statuses):
            grounded_content = content
            grounded_check = ProvenanceCheck(content, statuses)
        else:
            repaired = str(first.repaired_content or "").strip()
            if not repaired:
                return None
            repaired = self._require_candidate_text(repaired)
            second = self.reasoner.check_provenance(
                content=repaired,
                authoritative_evidence=evidence_text,
                supporting_memories=(),
                allow_repair=False,
            )
            second_statuses = tuple(ClaimStatus(s) for s in second.claim_statuses)
            if not second_statuses or not claims_are_grounded(second_statuses):
                return None
            grounded_content = repaired
            grounded_check = ProvenanceCheck(repaired, second_statuses)

        candidate = SemanticCandidate(
            content=grounded_content,
            memory_type=memory_type,
            scope=scope,
            confidence=confidence,
            evidence_uuid=evidence_uuid,
            evidence_quote=quote,
        )

        neighbors = tuple(
            hit
            for hit in self.service.search(
                owner_id=owner_id,
                query=grounded_content,
                limit=self.neighbor_limit,
                include_episodes=False,
            )
            if hit.kind == "semantic"
        )
        allowed_targets = {hit.uuid for hit in neighbors}
        decision = self.reasoner.classify_relation(candidate=candidate, neighbors=neighbors)
        relation = SemanticRelation(decision.relation)
        target_uuid = decision.target_memory_uuid
        if target_uuid is not None and str(target_uuid) not in allowed_targets:
            raise ValueError("relation target is outside the owner-scoped retrieved candidate set")

        decision = RelationDecision(
            relation=relation,
            target_memory_uuid=(str(target_uuid) if target_uuid is not None else None),
            confidence=self._clamp01(decision.confidence),
            explanation=decision.explanation,
        )

        action = derive_persistence_action(
            relation,
            target_memory_uuid=decision.target_memory_uuid,
        )

        final_content = grounded_content
        final_candidate = candidate
        final_provenance = grounded_check
        change_reason = decision.explanation

        if action is PersistenceAction.UPDATE:
            target = next(
                (hit for hit in neighbors if hit.uuid == decision.target_memory_uuid),
                None,
            )
            if target is None:
                # Python's transition mapping normally converts missing-target
                # UPDATE-like relations to CONFLICT, so this branch should be
                # unreachable unless a future mapping changes.
                raise ValueError("update action requires a retrieved target")

            merged = self.reasoner.consolidate(
                candidate=candidate,
                target=target,
                relation=relation,
            )
            final_content = self._require_candidate_text(merged.content)
            final_type = self._preferred_type(merged.memory_type, candidate.memory_type, target)
            final_scope = self._preferred_scope(merged.scope, candidate.scope, target)
            final_confidence = (
                self._clamp01(merged.confidence)
                if merged.confidence is not None
                else min(candidate.confidence, float(target.metadata.get("confidence", candidate.confidence)))
            )
            final_candidate = SemanticCandidate(
                content=candidate.content,
                memory_type=final_type,
                scope=final_scope,
                confidence=final_confidence,
                evidence_uuid=candidate.evidence_uuid,
                evidence_quote=candidate.evidence_quote,
            )
            final_check = self.reasoner.check_provenance(
                content=final_content,
                authoritative_evidence=evidence_text,
                supporting_memories=(target,),
                allow_repair=False,
            )
            final_statuses = tuple(ClaimStatus(s) for s in final_check.claim_statuses)
            if not final_statuses or not claims_are_grounded(final_statuses):
                return None
            final_provenance = ProvenanceCheck(final_content, final_statuses)
            change_reason = merged.change_reason or decision.explanation

        return PlannedTransition(
            candidate=final_candidate,
            decision=decision,
            provenance=final_provenance,
            final_content=final_content,
            change_reason=change_reason,
        )

    @staticmethod
    def _preferred_type(proposed: str | None, candidate: str, target: SearchHit) -> str:
        proposed_value = str(proposed or "").strip().casefold()
        if proposed_value in _ALLOWED_MEMORY_TYPES and proposed_value != "other":
            return proposed_value
        if candidate in _ALLOWED_MEMORY_TYPES and candidate != "other":
            return candidate
        target_type = str(target.metadata.get("memory_type") or "other").strip().casefold()
        return target_type if target_type in _ALLOWED_MEMORY_TYPES else "other"

    @staticmethod
    def _preferred_scope(proposed: str | None, candidate: str, target: SearchHit) -> str:
        proposed_value = str(proposed or "").strip()
        if proposed_value and proposed_value != "unspecified":
            return proposed_value
        if candidate and candidate != "unspecified":
            return candidate
        target_scope = str(target.metadata.get("scope") or "unspecified").strip()
        return target_scope or "unspecified"

    @staticmethod
    def _require_candidate_text(value: str) -> str:
        text = " ".join(str(value or "").split()).strip()
        if len(text) < 5:
            raise ValueError("semantic candidate is too short")
        if len(text) > 16000:
            raise ValueError("semantic candidate exceeds the runaway size limit")
        return text

    @staticmethod
    def _clamp01(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

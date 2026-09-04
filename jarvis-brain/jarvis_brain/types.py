from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable


class SourceKind(StrEnum):
    USER_MESSAGE = "USER_MESSAGE"
    EXPLICIT_USER_MEMORY = "EXPLICIT_USER_MEMORY"
    IMPORT = "IMPORT"
    TOOL_RESULT = "TOOL_RESULT"
    SYSTEM_MIGRATION = "SYSTEM_MIGRATION"


class EpisodeStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    ARCHIVED = "archived"


class MemoryStatus(StrEnum):
    CURRENT = "current"
    FORGOTTEN = "forgotten"


class SemanticRelation(StrEnum):
    NOVEL = "NOVEL"
    MATCH = "MATCH"
    EXTENSION = "EXTENSION"
    STATE_CHANGE = "STATE_CHANGE"
    CONTRADICTION = "CONTRADICTION"
    CONTEXT_VARIANT = "CONTEXT_VARIANT"
    UNCERTAIN = "UNCERTAIN"


class PersistenceAction(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"


class ClaimStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    SUPPORTED_PARAPHRASE = "SUPPORTED_PARAPHRASE"
    STALE_OR_OVERRIDDEN = "STALE_OR_OVERRIDDEN"
    UNSUPPORTED = "UNSUPPORTED"
    CONTRADICTED = "CONTRADICTED"


BLOCKING_CLAIM_STATUSES = frozenset(
    {
        ClaimStatus.STALE_OR_OVERRIDDEN,
        ClaimStatus.UNSUPPORTED,
        ClaimStatus.CONTRADICTED,
    }
)


def claims_are_grounded(statuses: Iterable[ClaimStatus | str]) -> bool:
    """Python derives aggregate provenance truth from per-claim statuses."""
    normalized = [ClaimStatus(status) for status in statuses]
    return bool(normalized) and not any(status in BLOCKING_CLAIM_STATUSES for status in normalized)


@dataclass(frozen=True)
class ProvenanceCheck:
    """Verification result for the exact final semantic text being committed.

    A later verifier/model may classify individual claims, but Python verifies
    that the verdict applies to the exact content being written and derives the
    aggregate grounded/not-grounded decision itself.
    """

    checked_content: str
    claim_statuses: tuple[ClaimStatus, ...]


@dataclass(frozen=True)
class SemanticCandidate:
    content: str
    memory_type: str
    scope: str
    confidence: float
    evidence_uuid: str
    evidence_quote: str


@dataclass(frozen=True)
class RelationDecision:
    relation: SemanticRelation
    target_memory_uuid: str | None
    confidence: float
    explanation: str | None = None


@dataclass(frozen=True)
class TransitionResult:
    action: PersistenceAction
    memory_uuid: str | None
    revision_no: int | None
    state_check_uuid: str
    changed: bool
    conflict: bool = False


@dataclass(frozen=True)
class SearchHit:
    kind: str
    uuid: str
    text: str
    score: float
    metadata: dict[str, Any]

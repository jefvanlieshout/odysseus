from __future__ import annotations

from .types import PersistenceAction, SemanticRelation


def derive_persistence_action(
    relation: SemanticRelation | str,
    *,
    target_memory_uuid: str | None,
) -> PersistenceAction:
    """Python-owned mapping from semantic relationship to persistence action.

    The model is never allowed to choose CREATE/UPDATE/DUPLICATE/CONFLICT.
    """
    rel = SemanticRelation(relation)
    has_target = bool(str(target_memory_uuid or "").strip())

    if rel is SemanticRelation.MATCH:
        return PersistenceAction.DUPLICATE if has_target else PersistenceAction.CONFLICT
    if rel in {SemanticRelation.EXTENSION, SemanticRelation.STATE_CHANGE}:
        return PersistenceAction.UPDATE if has_target else PersistenceAction.CONFLICT
    if rel in {SemanticRelation.NOVEL, SemanticRelation.CONTEXT_VARIANT}:
        return PersistenceAction.CREATE
    return PersistenceAction.CONFLICT


def relation_edge_type(relation: SemanticRelation | str) -> str:
    rel = SemanticRelation(relation)
    return {
        SemanticRelation.STATE_CHANGE: "SUPERSEDES",
        SemanticRelation.EXTENSION: "EXTENDS",
        SemanticRelation.MATCH: "REINFORCES",
        SemanticRelation.CONTEXT_VARIANT: "CONTEXTUAL_VARIANT",
        SemanticRelation.CONTRADICTION: "CONTRADICTS",
        SemanticRelation.UNCERTAIN: "RELATED_TO",
        SemanticRelation.NOVEL: "RELATED_TO",
    }[rel]

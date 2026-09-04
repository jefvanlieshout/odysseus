from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .semantic_worker import (
    CandidateProposal,
    ConsolidationProposal,
    ProvenanceAssessment,
)
from .types import ClaimStatus, RelationDecision, SearchHit, SemanticCandidate, SemanticRelation


logger = logging.getLogger(__name__)
_STRUCTURED_RETRY_CEILING = 4096


class StructuredReasonerError(RuntimeError):
    """The model route or structured response failed closed."""


@dataclass(frozen=True)
class StructuredReasonerConfig:
    chat_url: str
    model: str
    api_key: str = ""
    extra_headers: Mapping[str, str] | None = None
    timeout_seconds: float = 60.0
    max_tokens: int = 900
    temperature: float = 0.0
    reasoning_effort: str | None = "medium"

    @classmethod
    def from_env(cls) -> "StructuredReasonerConfig":
        headers: dict[str, str] = {}
        raw_headers = os.environ.get("BRAIN_LLM_HEADERS_JSON", "").strip()
        if raw_headers:
            try:
                parsed = json.loads(raw_headers)
            except json.JSONDecodeError as exc:
                raise StructuredReasonerError("BRAIN_LLM_HEADERS_JSON is not valid JSON") from exc
            if not isinstance(parsed, dict):
                raise StructuredReasonerError("BRAIN_LLM_HEADERS_JSON must be a JSON object")
            headers = {str(k): str(v) for k, v in parsed.items()}
        try:
            timeout = float(os.environ.get("BRAIN_LLM_TIMEOUT_SECONDS", "60") or "60")
        except ValueError:
            timeout = 60.0
        try:
            max_tokens = int(os.environ.get("BRAIN_LLM_MAX_TOKENS", "900") or "900")
        except ValueError:
            max_tokens = 900
        raw_effort = os.environ.get("BRAIN_LLM_REASONING_EFFORT", "medium").strip().casefold()
        reasoning_effort = (
            None
            if raw_effort in {"", "none", "off", "disabled"}
            else raw_effort
        )
        return cls(
            chat_url=os.environ.get("BRAIN_LLM_URL", "").strip(),
            model=os.environ.get("BRAIN_LLM_MODEL", "").strip(),
            api_key=os.environ.get("BRAIN_LLM_API_KEY", "").strip(),
            extra_headers=headers,
            timeout_seconds=min(300.0, max(1.0, timeout)),
            max_tokens=min(4096, max(128, max_tokens)),
            temperature=0.0,
            reasoning_effort=reasoning_effort,
        )

    def validate(self) -> None:
        if not self.chat_url:
            raise StructuredReasonerError("BRAIN_LLM_URL is required")
        parts = urlsplit(self.chat_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise StructuredReasonerError("BRAIN_LLM_URL must be an absolute http(s) URL")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise StructuredReasonerError("BRAIN_LLM_URL must not contain credentials, query, or fragment")
        if not self.model:
            raise StructuredReasonerError("BRAIN_LLM_MODEL is required")
        if not 64 <= int(self.max_tokens) <= _STRUCTURED_RETRY_CEILING:
            raise StructuredReasonerError(
                f"max_tokens must be between 64 and {_STRUCTURED_RETRY_CEILING}"
            )
        if self.reasoning_effort is not None and self.reasoning_effort not in {"low", "medium", "xhigh"}:
            raise StructuredReasonerError(
                "BRAIN_LLM_REASONING_EFFORT must be low, medium, xhigh, or disabled"
            )


_MEMORY_TYPES = ["preference", "fact", "project", "constraint", "relationship", "other"]
_RELATIONS = [relation.value for relation in SemanticRelation]
_CLAIM_STATUSES = [status.value for status in ClaimStatus]


CANDIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "memory_type": {"type": "string", "enum": _MEMORY_TYPES},
                    "scope": {"type": "string", "minLength": 1, "maxLength": 200},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "evidence_quote": {"type": "string", "minLength": 1, "maxLength": 500},
                },
                "required": ["content", "memory_type", "scope", "confidence", "evidence_quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}

PROVENANCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claim_statuses": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": {"type": "string", "enum": _CLAIM_STATUSES},
        },
        "repaired_content": {"type": "string", "maxLength": 1000},
    },
    "required": ["claim_statuses", "repaired_content"],
    "additionalProperties": False,
}

RELATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relation": {"type": "string", "enum": _RELATIONS},
        "target_memory_uuid": {"type": "string", "maxLength": 64},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "explanation": {"type": "string", "maxLength": 600},
    },
    "required": ["relation", "target_memory_uuid", "confidence", "explanation"],
    "additionalProperties": False,
}

CONSOLIDATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "minLength": 1, "maxLength": 1200},
        "memory_type": {"type": "string", "enum": [*_MEMORY_TYPES, "__KEEP__"]},
        "scope": {"type": "string", "maxLength": 200},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "change_reason": {"type": "string", "maxLength": 600},
    },
    "required": ["content", "memory_type", "scope", "confidence", "change_reason"],
    "additionalProperties": False,
}


class OpenAIJsonReasoner:
    """Schema-constrained SemanticReasoner for OpenAI-compatible chat endpoints.

    The model never returns persistence actions.  It may only propose candidate
    content, claim-level provenance statuses, semantic relations, and merged
    text.  Python's SemanticWorker remains the authority over transitions.
    """

    def __init__(self, config: StructuredReasonerConfig):
        config.validate()
        self.config = config

    @staticmethod
    def _require_exact_keys(payload: dict[str, Any], required: set[str], *, context: str) -> None:
        if set(payload) != required:
            raise StructuredReasonerError(
                f"{context} returned unexpected keys: expected {sorted(required)}, got {sorted(payload)}"
            )

    @staticmethod
    def _response_diagnostics(envelope: Any, *, operation: str) -> str:
        """Return metadata-only diagnostics; never include prompts or reasoning text."""
        choice = None
        message = None
        finish_reason = None
        try:
            choices = envelope.get("choices") if isinstance(envelope, dict) else None
            choice = choices[0] if isinstance(choices, list) and choices else None
            if isinstance(choice, dict):
                finish_reason = choice.get("finish_reason")
                message = choice.get("message")
        except Exception:
            choice = None
            message = None

        content = message.get("content") if isinstance(message, dict) else None
        reasoning = None
        if isinstance(message, dict):
            reasoning = message.get("reasoning_content")
            if reasoning is None:
                reasoning = message.get("reasoning")

        usage = envelope.get("usage") if isinstance(envelope, dict) else None
        completion_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
        details = usage.get("completion_tokens_details") if isinstance(usage, dict) else None
        reasoning_tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None

        return (
            f"operation={operation} "
            f"finish_reason={finish_reason!r} "
            f"content_chars={len(content) if isinstance(content, str) else 0} "
            f"reasoning_chars={len(reasoning) if isinstance(reasoning, str) else 0} "
            f"completion_tokens={completion_tokens!r} "
            f"reasoning_tokens={reasoning_tokens!r}"
        )

    @staticmethod
    def _structured_token_budgets(base_tokens: int) -> tuple[int, ...]:
        """Bounded retry ladder for reasoning models truncated before JSON.

        The first call keeps the configured normal budget.  Retries happen only
        when the endpoint explicitly reports finish_reason=length before a valid
        structured object exists.  Earlier semantic-worker stages therefore stay
        intact: candidate/provenance/relation/consolidation retries are local to
        the exact failed model operation.
        """
        base = max(64, min(_STRUCTURED_RETRY_CEILING, int(base_tokens)))
        budgets = [base]
        for multiplier in (2, 4):
            candidate = min(_STRUCTURED_RETRY_CEILING, base * multiplier)
            if candidate > budgets[-1]:
                budgets.append(candidate)
        if budgets[-1] < _STRUCTURED_RETRY_CEILING:
            budgets.append(_STRUCTURED_RETRY_CEILING)
        return tuple(budgets)

    def _call(self, *, operation: str, schema: dict[str, Any], system: str, user: str) -> dict[str, Any]:
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": f"jarvis_brain_{operation}",
                "strict": True,
                "schema": schema,
            },
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Cache-Control": "no-store",
            "User-Agent": "JarvisBrainStructuredReasoner/0.4.1",
            "Connection": "close",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        for key, value in dict(self.config.extra_headers or {}).items():
            if key.casefold() in {"content-length", "host"}:
                continue
            headers[key] = value

        budgets = self._structured_token_budgets(self.config.max_tokens)
        attempted_budgets: list[int] = []

        for attempt, max_tokens in enumerate(budgets, start=1):
            attempted_budgets.append(max_tokens)
            body = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": self.config.temperature,
                "max_tokens": max_tokens,
                "stream": False,
                "response_format": response_format,
            }
            if self.config.reasoning_effort is not None:
                body["reasoning_effort"] = self.config.reasoning_effort

            request = Request(
                self.config.chat_url,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers=headers,
            )
            try:
                with urlopen(request, timeout=self.config.timeout_seconds) as response:
                    raw = response.read(4 * 1024 * 1024)
                    status = int(getattr(response, "status", 200))
            except HTTPError as exc:
                try:
                    detail = exc.read(8192).decode("utf-8", errors="replace")
                finally:
                    exc.close()
                raise StructuredReasonerError(f"LLM HTTP {exc.code}: {detail[:1000]}") from None
            except URLError as exc:
                raise StructuredReasonerError(f"LLM connection error: {exc.reason}") from exc
            except TimeoutError as exc:
                raise StructuredReasonerError("LLM request timed out") from exc

            if not 200 <= status < 300:
                raise StructuredReasonerError(f"LLM returned HTTP {status}")
            try:
                envelope = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise StructuredReasonerError("LLM returned invalid response JSON") from exc

            diagnostics = self._response_diagnostics(envelope, operation=operation)
            choice = None
            message = None
            finish_reason = None
            try:
                choices = envelope.get("choices") if isinstance(envelope, dict) else None
                choice = choices[0] if isinstance(choices, list) and choices else None
                if isinstance(choice, dict):
                    finish_reason = choice.get("finish_reason")
                    message = choice.get("message")
            except Exception:
                choice = None
                message = None

            content = message.get("content") if isinstance(message, dict) else None
            truncated = str(finish_reason or "").casefold() == "length"
            can_retry = truncated and attempt < len(budgets)

            if not isinstance(content, str) or not content.strip():
                if can_retry:
                    next_budget = budgets[attempt]
                    logger.warning(
                        "[brain-reasoner] event=structured_retry "
                        "operation=%s attempt=%s max_tokens=%s next_max_tokens=%s %s",
                        operation, attempt, max_tokens, next_budget, diagnostics,
                    )
                    continue
                raise StructuredReasonerError(
                    "LLM structured content is empty "
                    f"({diagnostics} attempts={attempt} budgets={attempted_budgets})"
                )

            try:
                result = json.loads(content)
            except json.JSONDecodeError as exc:
                if can_retry:
                    next_budget = budgets[attempt]
                    logger.warning(
                        "[brain-reasoner] event=structured_retry "
                        "operation=%s attempt=%s max_tokens=%s next_max_tokens=%s "
                        "reason=truncated_json %s",
                        operation, attempt, max_tokens, next_budget, diagnostics,
                    )
                    continue
                raise StructuredReasonerError(
                    "LLM structured content is not valid JSON "
                    f"({diagnostics} attempts={attempt} budgets={attempted_budgets})"
                ) from exc

            if not isinstance(result, dict):
                raise StructuredReasonerError("LLM structured content must be a JSON object")

            if attempt > 1:
                logger.info(
                    "[brain-reasoner] event=structured_recovered "
                    "operation=%s attempts=%s final_max_tokens=%s budgets=%s",
                    operation, attempt, max_tokens, attempted_budgets,
                )
            return result

        raise StructuredReasonerError(
            f"LLM structured operation exhausted retry ladder: operation={operation} "
            f"budgets={attempted_budgets}"
        )

    def propose_candidates(
        self,
        *,
        evidence_text: str,
        evidence_uuid: str,
        owner_id: str,
    ) -> Sequence[CandidateProposal]:
        payload = self._call(
            operation="candidate_proposals",
            schema=CANDIDATE_SCHEMA,
            system=(
                "You extract durable SEMANTIC user memory candidates from one authoritative user message. "
                "A semantic memory should normally remain useful and true across many future conversations. "
                "Good candidates include stable preferences, durable facts, long-lived project goals or architecture "
                "decisions, constraints, and relationships. "
                "Do NOT save greetings, questions, commands, conversational filler, temporary feelings/health states, "
                "or transient operational/project status such as something currently running, stopped, fixed, broken, "
                "being tested, deployed, or 'finally working'. Those are episodic observations unless the message "
                "also explicitly states a durable goal, architecture decision, preference, constraint, or ownership fact. "
                "Do not infer a durable project identity or user ownership merely because a named system/component appears "
                "inside a transient status remark. "
                "Every evidence_quote MUST be an exact contiguous substring copied from EVIDENCE. "
                "If there is no durable semantic memory, return an empty candidates array. Never invent details."
            ),
            user=(
                f"OWNER_ID: {owner_id}\nEVIDENCE_UUID: {evidence_uuid}\n"
                f"EVIDENCE:\n{evidence_text}"
            ),
        )
        self._require_exact_keys(payload, {"candidates"}, context="candidate proposer")
        items = payload["candidates"]
        if not isinstance(items, list) or len(items) > 4:
            raise StructuredReasonerError("candidate proposer returned invalid candidates array")
        proposals: list[CandidateProposal] = []
        required = {"content", "memory_type", "scope", "confidence", "evidence_quote"}
        for item in items:
            if not isinstance(item, dict):
                raise StructuredReasonerError("candidate item must be an object")
            self._require_exact_keys(item, required, context="candidate item")
            memory_type = str(item["memory_type"])
            if memory_type not in _MEMORY_TYPES:
                raise StructuredReasonerError(f"unsupported memory_type: {memory_type}")
            try:
                confidence = float(item["confidence"])
            except (TypeError, ValueError) as exc:
                raise StructuredReasonerError("candidate confidence must be numeric") from exc
            if not 0.0 <= confidence <= 1.0:
                raise StructuredReasonerError("candidate confidence is outside [0,1]")
            proposals.append(CandidateProposal(
                content=str(item["content"]).strip(),
                memory_type=memory_type,
                scope=str(item["scope"]).strip() or "unspecified",
                confidence=confidence,
                evidence_quote=str(item["evidence_quote"]),
            ))
        return tuple(proposals)

    def check_provenance(
        self,
        *,
        content: str,
        authoritative_evidence: str,
        supporting_memories: Sequence[SearchHit],
        allow_repair: bool,
    ) -> ProvenanceAssessment:
        support = [
            {"uuid": hit.uuid, "text": hit.text, "metadata": hit.metadata}
            for hit in supporting_memories
        ]
        payload = self._call(
            operation="provenance",
            schema=PROVENANCE_SCHEMA,
            system=(
                "You are a strict claim-level provenance verifier. Classify every material claim in CANDIDATE "
                "using only AUTHORITATIVE_EVIDENCE plus SUPPORTING_MEMORY where supplied. Allowed statuses are "
                "SUPPORTED, SUPPORTED_PARAPHRASE, STALE_OR_OVERRIDDEN, UNSUPPORTED, CONTRADICTED. "
                "Do not output an aggregate grounded boolean. repaired_content must be an empty string unless "
                "ALLOW_REPAIR is true AND a minimally edited, fully supportable version can be produced. "
                "Never add new claims during repair."
            ),
            user=(
                f"ALLOW_REPAIR: {'true' if allow_repair else 'false'}\n"
                f"CANDIDATE:\n{content}\n\n"
                f"AUTHORITATIVE_EVIDENCE:\n{authoritative_evidence}\n\n"
                f"SUPPORTING_MEMORY_JSON:\n{json.dumps(support, ensure_ascii=False, sort_keys=True)}"
            ),
        )
        self._require_exact_keys(payload, {"claim_statuses", "repaired_content"}, context="provenance verifier")
        raw_statuses = payload["claim_statuses"]
        if not isinstance(raw_statuses, list) or not raw_statuses:
            raise StructuredReasonerError("provenance verifier returned no claim statuses")
        try:
            statuses = tuple(ClaimStatus(str(value)) for value in raw_statuses)
        except ValueError as exc:
            raise StructuredReasonerError("provenance verifier returned an invalid claim status") from exc
        repaired = str(payload["repaired_content"] or "").strip()
        if not allow_repair and repaired:
            raise StructuredReasonerError("verify-only provenance call returned repaired_content")
        return ProvenanceAssessment(statuses, repaired or None)

    def classify_relation(
        self,
        *,
        candidate: SemanticCandidate,
        neighbors: Sequence[SearchHit],
    ) -> RelationDecision:
        neighbor_payload = [
            {
                "uuid": hit.uuid,
                "text": hit.text,
                "memory_type": hit.metadata.get("memory_type"),
                "scope": hit.metadata.get("scope"),
                "status": hit.metadata.get("status"),
            }
            for hit in neighbors
        ]
        payload = self._call(
            operation="semantic_relation",
            schema=RELATION_SCHEMA,
            system=(
                "Classify the semantic relationship between CANDIDATE and the supplied owner-scoped memory "
                "NEIGHBORS. Return exactly one semantic relation using these definitions. "
                "NOVEL: no neighbor represents the same durable subject/state. "
                "MATCH: the candidate and target express the same durable proposition with only wording/style "
                "differences; MATCH requires that the candidate adds NO explicit durable qualifier, condition, "
                "reason, exception, boundary, scope detail, parameter, or other useful fact. "
                "EXTENSION: the candidate is compatible with a target and adds at least one explicit durable detail "
                "that belongs in the same memory, such as a qualifier, condition, reason, exception, boundary, scope "
                "detail, parameter, or other useful fact. Do NOT collapse an explicit user-stated detail into MATCH "
                "merely because it seems implied, obvious, typical, or inferable from the target. "
                "STATE_CHANGE: the candidate explicitly replaces or changes the prior state/preference/fact for the "
                "same subject/context, especially with temporal language such as now, changed, switched, no longer, "
                "used to, or instead. "
                "CONTRADICTION: candidate and target cannot both be true in the same context and there is no clear "
                "temporal replacement signal. "
                "CONTEXT_VARIANT: candidate is related to a target but applies to a genuinely distinct context where "
                "both memories should remain separately useful rather than be merged. "
                "UNCERTAIN: evidence is insufficient to choose one of the above confidently. "
                "Decision priority for a relevant target: explicit temporal replacement -> STATE_CHANGE; incompatible "
                "same-context claims without temporal replacement -> CONTRADICTION; compatible explicit added durable "
                "detail that belongs in the same memory -> EXTENSION; same proposition with no added durable detail -> "
                "MATCH; related but independently useful context -> CONTEXT_VARIANT. "
                "target_memory_uuid must be exactly one UUID from NEIGHBORS when the relation targets an existing "
                "memory; otherwise return an empty string. "
                "You MUST NOT choose CREATE, UPDATE, DUPLICATE, CONFLICT, or any database action."
            ),
            user=(
                "CANDIDATE_JSON:\n"
                + json.dumps({
                    "content": candidate.content,
                    "memory_type": candidate.memory_type,
                    "scope": candidate.scope,
                    "confidence": candidate.confidence,
                    "evidence_quote": candidate.evidence_quote,
                }, ensure_ascii=False, sort_keys=True)
                + "\nNEIGHBORS_JSON:\n"
                + json.dumps(neighbor_payload, ensure_ascii=False, sort_keys=True)
            ),
        )
        self._require_exact_keys(
            payload,
            {"relation", "target_memory_uuid", "confidence", "explanation"},
            context="relation classifier",
        )
        if any(key in payload for key in ("action", "persistence_action", "database_action")):
            raise StructuredReasonerError("relation classifier attempted to return a persistence action")
        try:
            relation = SemanticRelation(str(payload["relation"]))
        except ValueError as exc:
            raise StructuredReasonerError("relation classifier returned an invalid relation") from exc
        target = str(payload["target_memory_uuid"] or "").strip() or None
        try:
            confidence = float(payload["confidence"])
        except (TypeError, ValueError) as exc:
            raise StructuredReasonerError("relation confidence must be numeric") from exc
        if not 0.0 <= confidence <= 1.0:
            raise StructuredReasonerError("relation confidence is outside [0,1]")
        return RelationDecision(
            relation=relation,
            target_memory_uuid=target,
            confidence=confidence,
            explanation=str(payload["explanation"] or "").strip() or None,
        )

    def consolidate(
        self,
        *,
        candidate: SemanticCandidate,
        target: SearchHit,
        relation: SemanticRelation,
    ) -> ConsolidationProposal:
        payload = self._call(
            operation="consolidation",
            schema=CONSOLIDATION_SCHEMA,
            system=(
                "Produce one concise current semantic memory by consolidating EXISTING_MEMORY with CANDIDATE "
                "for an UPDATE-like relationship. Preserve supported useful context, replace stale state when "
                "appropriate, and do not add unsupported facts. This output is still subject to a separate final "
                "provenance verifier. memory_type='__KEEP__' means retain the controller-selected type; an empty "
                "scope means retain the controller-selected scope."
            ),
            user=(
                f"RELATION: {SemanticRelation(relation).value}\n"
                "CANDIDATE_JSON:\n"
                + json.dumps({
                    "content": candidate.content,
                    "memory_type": candidate.memory_type,
                    "scope": candidate.scope,
                    "confidence": candidate.confidence,
                }, ensure_ascii=False, sort_keys=True)
                + "\nEXISTING_MEMORY_JSON:\n"
                + json.dumps({
                    "uuid": target.uuid,
                    "text": target.text,
                    "metadata": target.metadata,
                }, ensure_ascii=False, sort_keys=True)
            ),
        )
        self._require_exact_keys(
            payload,
            {"content", "memory_type", "scope", "confidence", "change_reason"},
            context="consolidator",
        )
        memory_type = str(payload["memory_type"])
        if memory_type not in [*_MEMORY_TYPES, "__KEEP__"]:
            raise StructuredReasonerError("consolidator returned invalid memory_type")
        try:
            confidence = float(payload["confidence"])
        except (TypeError, ValueError) as exc:
            raise StructuredReasonerError("consolidation confidence must be numeric") from exc
        if not 0.0 <= confidence <= 1.0:
            raise StructuredReasonerError("consolidation confidence is outside [0,1]")
        return ConsolidationProposal(
            content=str(payload["content"]).strip(),
            memory_type=None if memory_type == "__KEEP__" else memory_type,
            scope=str(payload["scope"] or "").strip() or None,
            confidence=confidence,
            change_reason=str(payload["change_reason"] or "").strip() or None,
        )

"""Candidate providers for assistant tool visibility.

Providers produce *signals*. They never mutate a shared tool set and never
grant permission. ToolBroker remains the final visibility boundary within the
controller-supplied permitted catalog.

This keeps selection explainable:
- core/recovery tools;
- explicit controller context;
- semantic/context candidates from Odysseus;
- small typed-domain cold-start anchors;
- short verified capability leases from real tool events.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Mapping, Sequence

from assistant.fork.tool_broker import ToolCandidate
from assistant.fork.tool_catalog import (
    ToolRecord,
    anchor_names_for_capabilities,
)


@dataclass(frozen=True, slots=True)
class CandidatePlan:
    candidates: tuple[ToolCandidate, ...]
    evidence: tuple[str, ...]
    budget: int
    suppressed_cross_domain: tuple[str, ...] = ()


def _domain_caps(capabilities: Iterable[str]) -> set[str]:
    return {
        str(cap)
        for cap in capabilities
        if str(cap).startswith("domain:")
    }


def _record_domain_caps(record: ToolRecord | None) -> set[str]:
    if record is None:
        return set()
    return {
        cap
        for cap in record.capabilities
        if cap.startswith("domain:")
    }


def _record_read_only(record: ToolRecord | None) -> bool:
    if record is None or record.contract is None:
        return False
    return bool(record.contract.read_only)


def _record_server_id(record: ToolRecord | None) -> str:
    if record is None or record.contract is None:
        return ""
    return str(record.contract.server_id or "")


def _query_relevance_score(record: ToolRecord | None, text: str) -> float:
    """Small lexical/operation score inside an already-authorized domain.

    This does not discover tools; it only breaks ties between candidates that
    are already relevant by provider/domain. Resource words are stronger than
    generic domain membership, so an inbox request prefers email search/read
    over list_folders/list_identities.
    """
    if record is None or record.contract is None:
        return 0.0
    query_tokens = {
        token for token in re.findall(r"[a-z0-9]+", str(text or "").casefold())
        if token
    }
    if not query_tokens:
        return 0.0
    contract = record.contract
    resource_tokens = {
        token for token in re.findall(r"[a-z0-9]+", contract.resource_kind.casefold())
        if token
    }
    # Simple singular aliases keep emails/messages/folders useful without a
    # language model or a second semantic index inside the controller.
    expanded_query = set(query_tokens)
    for token in tuple(query_tokens):
        if token.endswith("s") and len(token) > 3:
            expanded_query.add(token[:-1])
    if expanded_query & {"mail", "mailbox", "inbox", "message"}:
        expanded_query.add("email")
    if expanded_query & {"alias", "address", "sender"}:
        expanded_query.add("identity")

    score = 0.0
    resource_match = bool(resource_tokens & expanded_query)
    if resource_match:
        score += 4.0
    if contract.verb and contract.verb in expanded_query:
        score += 3.0

    recency_words = {"latest", "newest", "recent", "first", "last", "top"}
    content_words = {"read", "show", "summarize", "summarise", "summary", "check", "open"}
    if resource_match and expanded_query & recency_words and contract.verb in {"search", "list", "find", "query"}:
        score += 2.0
    if resource_match and expanded_query & content_words and contract.verb in {"read", "get", "fetch"}:
        score += 1.5
    return score


def explicitly_named_candidate_tools(
    text: str,
    candidate_names: Iterable[str],
) -> set[str]:
    """Return already-retrieved tools explicitly named by the user.

    This is a precision signal layered on top of semantic retrieval. It never
    discovers or grants a new tool. Names are normalized only across ordinary
    separators, so ``api_call`` matches "api call" while ``manage_tasks`` does
    not match the generic word "tasks".
    """
    normalized_text = " " + re.sub(
        r"[^a-z0-9]+",
        " ",
        str(text or "").casefold(),
    ).strip() + " "
    if normalized_text == "  ":
        return set()

    matched: set[str] = set()
    for raw_name in candidate_names:
        name = str(raw_name or "").strip()
        if not name:
            continue
        leaf = name.split("__", 2)[-1] if name.startswith("mcp__") else name
        phrase = re.sub(
            r"[^a-z0-9]+",
            " ",
            leaf.casefold(),
        ).strip()
        if len(phrase) < 3:
            continue
        if f" {phrase} " in normalized_text:
            matched.add(name)
    return matched


def _signal(
    name: str,
    tier: int,
    score: float,
    reason: str,
) -> ToolCandidate:
    return ToolCandidate(
        name=str(name),
        tier=int(tier),
        score=float(score),
        reason=str(reason),
    )


def visibility_budget(
    *,
    current_count: int,
    forced_count: int,
    core_count: int,
    explicit_domains: bool,
    override: int | None = None,
) -> int:
    """Return a conservative schema budget with discovery as the escape hatch."""
    if override is not None:
        return max(1, int(override))

    # v0.2.6: discovery is mature enough that normal prompts can optimize
    # for precision instead of keeping a broad recall-heavy schema surface.
    base = 16 if explicit_domains else 20
    required = core_count + forced_count
    adaptive = min(20, max(10, current_count + 4))
    # Explicit/core context remains protected. Only an unusually large forced
    # footprint may push beyond the ordinary 20-schema ceiling.
    ceiling = max(20, required)
    return min(ceiling, max(required, base, adaptive))


def build_candidate_plan(
    *,
    records: Mapping[str, ToolRecord],
    current_names: Iterable[str],
    forced_names: Iterable[str],
    core_names: Iterable[str],
    suggested_capabilities: Iterable[str],
    evidence_names: Sequence[str],
    max_visible: int | None = None,
    named_provider_ids: Iterable[str] = (),
    read_only_only: bool = False,
    query_text: str = "",
) -> CandidatePlan:
    """Compose scored visibility candidates from independent providers."""
    current = {str(name) for name in current_names if str(name)}
    forced = {str(name) for name in forced_names if str(name)}
    core = {str(name) for name in core_names if str(name)}
    suggested = {str(cap) for cap in suggested_capabilities if str(cap)}
    suggested_domains = _domain_caps(suggested)
    named_providers = {str(item) for item in named_provider_ids if str(item)}

    out: list[ToolCandidate] = []
    suppressed_cross_domain: set[str] = set()

    # Stable recovery surface.
    for name in sorted(core):
        record = records.get(name)
        if read_only_only and record is not None and not _record_read_only(record):
            continue
        out.append(_signal(name, 100, 0.0, "core-visible"))

    # Explicit route/context state wins over all relevance heuristics, except an
    # explicit read-only user constraint which is a controller restriction.
    for name in sorted(forced):
        record = records.get(name)
        if read_only_only and record is not None and not _record_read_only(record):
            continue
        out.append(_signal(name, 95, 0.0, "explicit-context"))

    dependency_producers: set[str] = set()
    provider_served_domains: set[str] = set()
    for target_name in current | forced:
        target = records.get(target_name)
        if target is not None and target.contract is not None:
            dependency_producers.update(target.contract.producer_tools)

    # A user-named connected provider is strong routing context. Surface the
    # provider tools that actually match the request resource, plus prerequisite
    # producers. This avoids flooding a Fastmail inbox request with unrelated
    # identities/folders/calendar siblings while still recovering a producer
    # that semantic retrieval missed. Visibility is not execution authority.
    if named_providers:
        for record in records.values():
            if _record_server_id(record) not in named_providers:
                continue
            if read_only_only and not _record_read_only(record):
                continue
            record_domains = _record_domain_caps(record)
            if suggested_domains and not (record_domains & suggested_domains):
                continue
            relevance = _query_relevance_score(record, query_text)
            if relevance <= 0 and record.name not in dependency_producers:
                continue
            provider_served_domains.update(
                record_domains & suggested_domains
                if suggested_domains else record_domains
            )
            out.append(_signal(
                record.name, 85, relevance, "explicit-provider-domain"
            ))

    # If a retrieved tool has schema-required identifier inputs, promote the
    # catalog tools that can produce those prerequisites. This is generic
    # dependency routing: e.g. search_email/list_emails before read_email, or a
    # list operation before a get-by-id operation.
    for target_name in sorted(current | forced):
        target = records.get(target_name)
        if target is None or target.contract is None:
            continue
        for producer_name in target.contract.producer_tools:
            producer = records.get(producer_name)
            if producer is None:
                continue
            if read_only_only and not _record_read_only(producer):
                continue
            out.append(_signal(
                producer_name, 89, _query_relevance_score(producer, query_text),
                "dependency-producer"
            ))

    # Verified execution creates a short capability lease.  On an explicitly
    # typed new topic, only history sharing that topic survives.  On a vague
    # follow-up, recent evidence itself plus small domain anchors remain useful.
    lease_anchor_capabilities: set[str] = set()
    for name in evidence_names:
        record = records.get(str(name))
        record_domains = _record_domain_caps(record)
        if suggested_domains and not (record_domains & suggested_domains):
            continue
        if read_only_only and record is not None and not _record_read_only(record):
            continue

        if record is not None:
            out.append(_signal(record.name, 88, 0.0, "verified-capability-lease"))
            # Reminder MCPs have a tighter sibling capability than the broad
            # notes/calendar/tasks domain. Prefer it so "cancel that reminder"
            # does not also lease every notes/calendar tool.
            if "family:reminders" in record.capabilities:
                lease_anchor_capabilities.add("family:reminders")
            else:
                lease_anchor_capabilities.update(
                    record_domains & suggested_domains
                    if suggested_domains
                    else record_domains
                )
        elif not suggested_domains:
            # Unknown/custom recent tools are sticky only on untyped follow-ups.
            out.append(_signal(str(name), 86, 0.0, "verified-tool-lease"))

    if lease_anchor_capabilities:
        for name in anchor_names_for_capabilities(
            lease_anchor_capabilities,
            records,
        ):
            out.append(_signal(name, 84, 0.0, "lease-domain-anchor"))

    # Existing Odysseus retrieval/context output is a candidate provider, not
    # final authority.  Typed domains softly demote *known unrelated* domain
    # candidates, letting the prompt budget remove obvious cross-topic noise.
    for name in sorted(current):
        if name in forced:
            continue
        record = records.get(name)
        record_domains = _record_domain_caps(record)
        if read_only_only and record is not None and not _record_read_only(record):
            suppressed_cross_domain.add(name)
            continue
        if (
            named_providers
            and provider_served_domains
            and _record_server_id(record) not in named_providers
            and bool(record_domains & provider_served_domains)
        ):
            # The user named a concrete provider and that provider exposes a
            # useful tool for this domain. Native/other-provider duplicates are
            # hidden rather than merely demoted, preventing accidental fallback
            # to a different mailbox/API. Forced controller context still wins
            # because forced names bypass this retrieval loop.
            suppressed_cross_domain.add(name)
            continue
        if suggested_domains:
            if record_domains & suggested_domains:
                _query_score = _query_relevance_score(record, query_text)
                if (
                    named_providers
                    and _record_server_id(record) in named_providers
                    and _query_score <= 0
                    and name not in dependency_producers
                ):
                    suppressed_cross_domain.add(name)
                    continue
                if named_providers and _record_server_id(record) in named_providers:
                    tier = 90
                    reason = "retrieval-provider-match"
                elif named_providers:
                    # Keep same-domain native/other-provider tools as fallback,
                    # but let explicitly named provider tools win the budget.
                    tier = 62
                    reason = "retrieval-provider-fallback"
                else:
                    tier = 74
                    reason = "retrieval-domain-match"
            elif record_domains:
                # v0.2.6: a tool classified into a different known domain is
                # noise on an explicitly typed turn. Multi-domain requests are
                # represented by multiple suggested domains; discover_tools is
                # the recovery path for genuine misses.
                suppressed_cross_domain.add(name)
                continue
            else:
                tier = 54
                reason = "retrieval-unclassified"
        else:
            tier = 70
            reason = "retrieval-context"
        out.append(_signal(
            name, tier, 1.0 + _query_relevance_score(record, query_text), reason
        ))

    # Typed intent adds only a small set of cold-start anchors, NOT the whole
    # family/domain.  Less-common tools are reachable through semantic retrieval
    # or discover_tools.
    for name in anchor_names_for_capabilities(suggested_domains, records):
        record = records.get(name)
        if read_only_only and record is not None and not _record_read_only(record):
            continue
        record_domains = _record_domain_caps(record)
        if (
            named_providers
            and provider_served_domains
            and _record_server_id(record) not in named_providers
            and bool(record_domains & provider_served_domains)
        ):
            continue
        tier = 58 if named_providers else 66
        reason = "typed-domain-provider-fallback" if named_providers else "typed-domain-anchor"
        out.append(_signal(name, tier, 0.0, reason))

    budget = visibility_budget(
        current_count=len(current),
        forced_count=len(forced),
        core_count=len(core),
        explicit_domains=bool(suggested_domains),
        override=max_visible,
    )
    return CandidatePlan(
        candidates=tuple(out),
        evidence=tuple(str(name) for name in evidence_names if str(name)),
        budget=budget,
        suppressed_cross_domain=tuple(sorted(suppressed_cross_domain)),
    )

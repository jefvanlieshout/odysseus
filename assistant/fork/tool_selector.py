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
) -> CandidatePlan:
    """Compose scored visibility candidates from independent providers."""
    current = {str(name) for name in current_names if str(name)}
    forced = {str(name) for name in forced_names if str(name)}
    core = {str(name) for name in core_names if str(name)}
    suggested = {str(cap) for cap in suggested_capabilities if str(cap)}
    suggested_domains = _domain_caps(suggested)

    out: list[ToolCandidate] = []
    suppressed_cross_domain: set[str] = set()

    # Stable recovery surface.
    for name in sorted(core):
        out.append(_signal(name, 100, 0.0, "core-visible"))

    # Explicit route/context state wins over all relevance heuristics.
    for name in sorted(forced):
        out.append(_signal(name, 95, 0.0, "explicit-context"))

    # Verified execution creates a short capability lease.  On an explicitly
    # typed new topic, only history sharing that topic survives.  On a vague
    # follow-up, recent evidence itself plus small domain anchors remain useful.
    lease_anchor_capabilities: set[str] = set()
    for name in evidence_names:
        record = records.get(str(name))
        record_domains = _record_domain_caps(record)
        if suggested_domains and not (record_domains & suggested_domains):
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
        record = records.get(name)
        record_domains = _record_domain_caps(record)
        if suggested_domains:
            if record_domains & suggested_domains:
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
        out.append(_signal(name, tier, 1.0, reason))

    # Typed intent adds only a small set of cold-start anchors, NOT the whole
    # family/domain.  Less-common tools are reachable through semantic retrieval
    # or discover_tools.
    for name in anchor_names_for_capabilities(suggested_domains, records):
        out.append(_signal(name, 66, 0.0, "typed-domain-anchor"))

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

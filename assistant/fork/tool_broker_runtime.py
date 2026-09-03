"""Runtime sticky tool visibility for the assistant fork.

This module only influences which already-available tool schemas stay visible
for natural follow-up turns. It never executes tools, grants permission,
bypasses approvals, or treats model prose as evidence that an action happened.

Evidence comes exclusively from persisted Odysseus ``tool_events``. This gives
us a small, authoritative conversation-state bridge while the old RAG/keyword
selector remains the cold-start selector during the migration to ToolBroker.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping, Sequence


from assistant.fork.tool_catalog import (
    build_runtime_registry,
    build_tool_catalog,
    capabilities_for_name as _catalog_capabilities_for_name,
    connected_mcp_names as _catalog_connected_mcp_names,
    domain_capabilities as _catalog_domain_capabilities,
    record_for_runtime_name,
)
from assistant.fork.tool_selector import build_candidate_plan

_MCP_QUALIFIED_RE = re.compile(r"\bmcp__[A-Za-z0-9_-]+__[A-Za-z0-9_-]+\b")


@dataclass(frozen=True, slots=True)
class StickyVisibilityResult:
    tools: set[str] | None
    added: tuple[str, ...]
    evidence: tuple[str, ...]


def _resolved_tool_event_name(event: Mapping[str, Any]) -> str:
    """Resolve the concrete tool name from one persisted tool event."""
    tool = str(event.get("tool") or "").strip()
    if tool and tool != "mcp":
        return tool
    for key in ("desc", "command", "output"):
        value = str(event.get(key) or "")
        match = _MCP_QUALIFIED_RE.search(value)
        if match:
            return match.group(0)
    return tool


def recent_authoritative_tool_names(
    messages: Sequence[Mapping[str, Any]],
    *,
    previous_user_turns: int = 2,
    max_events: int = 8,
) -> tuple[str, ...]:
    """Return recent executed tool names from persisted message metadata.

    The newest user turn is skipped. We then retain at most the previous two
    conversational turns, so a capability is sticky long enough for references
    such as ``move the first one`` but not forever after the topic changes.
    """
    if previous_user_turns < 1 or max_events < 1:
        return ()

    out: list[str] = []
    seen: set[str] = set()
    user_turns_seen = 0
    skipped_latest_user = False

    for message in reversed(messages or []):
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or "")
        if role == "user":
            if not skipped_latest_user:
                skipped_latest_user = True
                continue
            user_turns_seen += 1
            if user_turns_seen >= previous_user_turns:
                break

        metadata = message.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        events = metadata.get("tool_events")
        if not isinstance(events, list):
            continue
        for event in reversed(events):
            if not isinstance(event, Mapping):
                continue
            name = _resolved_tool_event_name(event)
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(name)
            if len(out) >= max_events:
                return tuple(out)
    return tuple(out)


def _mcp_server_prefix(name: str) -> str | None:
    if not name.startswith("mcp__"):
        return None
    parts = name.split("__", 2)
    if len(parts) != 3 or not parts[1]:
        return None
    return f"mcp__{parts[1]}__"


def _connected_mcp_names(mcp_mgr: Any) -> set[str]:
    return set(_catalog_connected_mcp_names(mcp_mgr))


def sticky_tools_from_history(
    messages: Sequence[Mapping[str, Any]],
    *,
    mcp_mgr: Any = None,
    previous_user_turns: int = 2,
    suggested_capabilities: Iterable[str] = (),
    domain_members: Mapping[str, Iterable[str]] | None = None,
) -> tuple[set[str], tuple[str, ...]]:
    """Return the tools held by short verified capability leases.

    Compatibility helper for existing tests/callers. Live selection uses the
    same provider plan inside ``preview_final_tool_visibility``.
    """
    evidence = recent_authoritative_tool_names(
        messages,
        previous_user_turns=previous_user_turns,
    )
    if not evidence:
        return set(), ()

    records = build_tool_catalog(
        mcp_mgr=mcp_mgr,
        domain_members=domain_members,
    )
    for name in evidence:
        if name not in records:
            records[name] = record_for_runtime_name(
                name,
                domain_members=domain_members,
            )

    plan = build_candidate_plan(
        records=records,
        current_names=(),
        forced_names=(),
        core_names=(),
        suggested_capabilities=suggested_capabilities,
        evidence_names=evidence,
        max_visible=64,
    )
    sticky = {
        candidate.name
        for candidate in plan.candidates
        if candidate.reason in {
            "verified-capability-lease",
            "verified-tool-lease",
            "lease-domain-anchor",
        }
    }
    return sticky, evidence


def apply_sticky_tool_visibility(
    *,
    current: set[str] | None,
    messages: Sequence[Mapping[str, Any]],
    disabled_tools: Iterable[str] = (),
    mcp_mgr: Any = None,
) -> StickyVisibilityResult:
    """Merge short-lived sticky follow-up tools into selector output.

    ``disabled_tools`` is only a visibility filter. Existing Odysseus
    execution-time authorization and approval gates remain final authority.
    """
    sticky, evidence = sticky_tools_from_history(messages, mcp_mgr=mcp_mgr)
    if not sticky:
        return StickyVisibilityResult(
            tools=None if current is None else set(current),
            added=(),
            evidence=evidence,
        )

    disabled = {str(name) for name in disabled_tools}
    sticky.difference_update(disabled)

    if current is None:
        try:
            from src.tool_index import ALWAYS_AVAILABLE
            merged = set(ALWAYS_AVAILABLE)
        except Exception:
            merged = set()
    else:
        merged = set(current)

    before = set(merged)
    merged.update(sticky)
    return StickyVisibilityResult(
        tools=merged,
        added=tuple(sorted(merged - before)),
        evidence=evidence,
    )

# v0.2.3 broker shadow visibility
@dataclass(frozen=True, slots=True)
class BrokerVisibilityPreview:
    """Final ToolBroker visibility plus explainability metadata."""

    tools: set[str]
    added: tuple[str, ...]
    removed: tuple[str, ...]
    evidence: tuple[str, ...]
    reasons: Mapping[str, str]
    budget: int | None = None


# v0.2.3 typed cold-start capability recovery
def _broker_capabilities_for_name(
    name: str,
    *,
    domain_members: Mapping[str, Iterable[str]] | None = None,
) -> frozenset[str]:
    return _catalog_capabilities_for_name(
        name,
        domain_members=domain_members,
    )


def broker_capabilities_for_domains(domains: Iterable[str]) -> tuple[str, ...]:
    """Translate typed controller domains into ToolCatalog capabilities."""
    return _catalog_domain_capabilities(domains)


def preview_final_tool_visibility(
    *,
    current: set[str] | None,
    messages: Sequence[Mapping[str, Any]],
    disabled_tools: Iterable[str] = (),
    mcp_mgr: Any = None,
    forced_names: Iterable[str] = (),
    suggested_capabilities: Iterable[str] = (),
    max_visible: int | None = None,
    domain_members: Mapping[str, Iterable[str]] | None = None,
) -> BrokerVisibilityPreview:
    """Select final visibility from independent candidate providers.

    Odysseus supplies tool facts and initial retrieval/context candidates.
    ToolCatalog normalizes the installed/connected universe. Candidate providers
    emit relevance signals. ToolBroker alone enforces the final visibility set
    inside that controller-owned permitted universe.
    """
    from assistant.fork.tool_broker import ToolBroker, ToolDescriptor
    from src.tool_index import ALWAYS_AVAILABLE

    disabled = {str(name) for name in disabled_tools if name}
    current_set = {
        str(name)
        for name in (current or ())
        if name and str(name) not in disabled
    }
    forced = {
        str(name)
        for name in forced_names
        if name and str(name) not in disabled
    }
    evidence = recent_authoritative_tool_names(messages)

    records = build_tool_catalog(
        mcp_mgr=mcp_mgr,
        disabled_tools=disabled,
        domain_members=domain_members,
    )

    # Caller-provided/custom runtime tools must remain selectable even when an
    # older Odysseus metadata surface has not learned about them yet. They get
    # conservative metadata; execution/security still decide authority.
    required_runtime_names = (
        current_set
        | forced
        | set(evidence)
        | set(ALWAYS_AVAILABLE)
        | {"discover_tools"}
    )
    for name in required_runtime_names:
        if name and name not in disabled and name not in records:
            records[name] = record_for_runtime_name(
                name,
                domain_members=domain_members,
            )

    permitted = set(records)
    if not permitted:
        return BrokerVisibilityPreview(
            tools=set(),
            added=(),
            removed=tuple(sorted(current_set)),
            evidence=evidence,
            reasons={},
        )

    descriptors = [
        ToolDescriptor(
            name=record.name,
            capabilities=record.capabilities,
            core_visible=(
                record.name in ALWAYS_AVAILABLE
                or record.name == "discover_tools"
            ),
            source=record.source,
            description=record.description,
        )
        for record in records.values()
    ]

    plan = build_candidate_plan(
        records=records,
        current_names=current_set,
        forced_names=forced,
        core_names=set(ALWAYS_AVAILABLE) | {"discover_tools"},
        suggested_capabilities=suggested_capabilities,
        evidence_names=evidence,
        max_visible=max_visible,
    )

    broker = ToolBroker(descriptors, max_visible=plan.budget)
    selection = broker.select_candidates(
        permitted_names=permitted,
        candidates=plan.candidates,
    )
    proposed = set(selection.visible)

    return BrokerVisibilityPreview(
        tools=proposed,
        added=tuple(sorted(proposed - current_set)),
        removed=tuple(sorted(current_set - proposed)),
        evidence=evidence,
        reasons=dict(selection.reasons),
    )


# v0.2.3 model-adapter visibility restriction
def restrict_tool_visibility(
    current: set[str] | None,
    supported: Iterable[str],
) -> set[str] | None:
    """Allow a model adapter to REMOVE Broker-visible tools, never add them."""
    if current is None:
        return None
    return set(current) & {str(name) for name in supported if name}

# v0.2.4 real discover_tools runtime

_DISCOVERY_GENERIC_TOOLS = frozenset({
    "ask_user",
    "bash",
    "python",
    "app_api",
    "pipeline",
    "manage_memory",
    "manage_mcp",
    "manage_endpoints",
    "manage_settings",
})

_DISCOVERY_STOPWORDS = frozenset({
    "a", "an", "and", "any", "are", "can", "capability", "do", "find",
    "for", "from", "have", "i", "in", "inspect", "locally", "manage",
    "need", "of", "or", "please", "that", "the", "their", "them", "this",
    "to", "tool", "tools", "use", "using", "want", "with",
})


def _discovery_stem(token: str) -> str:
    token = str(token or "").lower()
    if len(token) > 5 and token.endswith("ing"):
        token = token[:-3]
    elif len(token) > 4 and token.endswith("ed"):
        token = token[:-2]
    elif len(token) > 4 and token.endswith("es"):
        token = token[:-2]
    elif len(token) > 3 and token.endswith("s"):
        token = token[:-1]
    return token


def _discovery_tokens(text: str) -> tuple[str, ...]:
    tokens = []
    for raw in re.findall(r"[a-z0-9]+", (text or "").lower()):
        stemmed = _discovery_stem(raw)
        if len(stemmed) < 2 or stemmed in _DISCOVERY_STOPWORDS:
            continue
        tokens.append(stemmed)
    return tuple(tokens)


def _discovery_name_score(name: str, query: str) -> int:
    """Deterministic lexical relevance between a tool name and capability query."""
    query_tokens = set(_discovery_tokens(query))
    if not query_tokens:
        return 0

    normalized_name = re.sub(
        r"^mcp__[^_]+__", "",
        str(name or "").lower(),
    )
    name_tokens = {
        _discovery_stem(token)
        for token in re.findall(r"[a-z0-9]+", normalized_name)
        if token
    }
    return len(query_tokens & name_tokens)


def _discovery_description_map(
    *,
    mcp_mgr: Any = None,
) -> dict[str, str]:
    """Use the normalized ToolCatalog descriptions for discovery precision."""
    records = build_tool_catalog(mcp_mgr=mcp_mgr)
    return {
        name: record.description
        for name, record in records.items()
        if record.description
    }


def _rank_discovery_candidates(
    query: str,
    candidates: Iterable[str],
    *,
    descriptions: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Rerank semantic discovery candidates toward specific capability tools.

    Semantic retrieval remains the recall layer. This function is the precision
    layer: exact tool-name matches matter most, description matches help, and
    generic escape hatches are mildly demoted when they have no concrete lexical
    evidence for the requested capability.
    """
    ordered = []
    seen = set()
    for raw_name in candidates:
        name = str(raw_name or "").strip()
        if not name or name in seen or name == "discover_tools":
            continue
        seen.add(name)
        ordered.append(name)

    if not ordered:
        return ()

    query_tokens = set(_discovery_tokens(query))
    desc_map = dict(descriptions or {})
    semantic_span = len(ordered)

    scored = []
    for semantic_rank, name in enumerate(ordered):
        name_hits = _discovery_name_score(name, query)
        description_tokens = set(_discovery_tokens(desc_map.get(name, "")))
        description_hits = len(query_tokens & description_tokens)

        # Preserve semantic retrieval as a meaningful signal, but make specific
        # capability evidence dominate it.
        semantic_score = semantic_span - semantic_rank
        score = (
            name_hits * 30
            + min(description_hits, 8) * 4
            + semantic_score
        )

        # Generic/meta tools are useful escape hatches, but they should not outrank
        # a concrete purpose-built tool solely because their embedding is broad.
        if (
            name in _DISCOVERY_GENERIC_TOOLS
            and name_hits == 0
            and description_hits <= 1
        ):
            score -= 18

        scored.append(
            (
                score,
                name_hits,
                description_hits,
                -semantic_rank,
                name,
            )
        )

    scored.sort(reverse=True)
    return tuple(item[-1] for item in scored)


def discover_runtime_tools(
    *,
    query: str,
    disabled_tools: Iterable[str] = (),
    mcp_mgr: Any = None,
    max_results: int = 8,
) -> tuple[str, ...]:
    """Discover controller-permitted tools for an explicit recovery request.

    This is visibility discovery only. It does not execute, enable, connect, or
    grant permission to any tool. Results are constrained to the controller's
    installed/connected universe minus the current disabled set.

    ToolIndex semantic retrieval supplies recall. A local deterministic reranker
    then favors concrete tool names/descriptions over broad generic helpers.
    """
    query = str(query or "").strip()
    if not query:
        return ()

    try:
        max_results = max(1, min(int(max_results), 12))
    except (TypeError, ValueError):
        max_results = 8

    disabled = {str(name) for name in disabled_tools if name}
    runtime_registry = build_runtime_registry(
        mcp_mgr=mcp_mgr,
        disabled_tools=disabled,
    )
    permitted = set(runtime_registry)
    permitted.discard("discover_tools")

    if not permitted:
        return ()

    semantic_candidates: list[str] = []
    seen: set[str] = set()

    def add_candidate(name: Any) -> None:
        candidate = str(name or "").strip()
        if (
            not candidate
            or candidate == "discover_tools"
            or candidate in disabled
            or candidate not in permitted
            or candidate in seen
        ):
            return
        seen.add(candidate)
        semantic_candidates.append(candidate)

    # Pull a deliberately wider pool than we return. Previously we accepted the
    # first max_results semantic hits verbatim, which let broad tools fill every
    # slot before specificity had any chance to matter.
    try:
        from src.tool_index import get_tool_index

        tool_idx = get_tool_index()
        if tool_idx is not None:
            if mcp_mgr is not None:
                try:
                    tool_idx.index_mcp_tools(mcp_mgr, {})
                except Exception:
                    pass
            retrieved = tool_idx.get_tools_for_query(
                query,
                max(max_results * 4, 24),
            )
            for name in retrieved or ():
                add_candidate(name)
    except Exception:
        pass

    # Guarantee that strong literal capability matches are eligible even when
    # embeddings miss them entirely.
    literal_matches = sorted(
        (
            (_discovery_name_score(name, query), name)
            for name in permitted
        ),
        key=lambda item: (-item[0], item[1]),
    )
    for score, name in literal_matches:
        if score <= 0:
            break
        add_candidate(name)

    ranked = _rank_discovery_candidates(
        query,
        semantic_candidates,
        descriptions=_discovery_description_map(mcp_mgr=mcp_mgr),
    )

    # A semantic outage can leave the pool empty. Preserve the old deterministic
    # fallback rather than failing discovery entirely.
    if not ranked:
        ranked = tuple(
            name
            for score, name in literal_matches
            if score > 0
        )

    return tuple(ranked[:max_results])

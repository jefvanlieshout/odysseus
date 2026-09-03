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


_BUILTIN_FAMILIES: Mapping[str, frozenset[str]] = {
    "calendar": frozenset({"manage_calendar"}),
    "notes_tasks": frozenset({"manage_notes", "manage_tasks"}),
    "email": frozenset({
        "list_email_accounts", "list_emails", "read_email",
        "scan_email_unsubscribes", "unsubscribe_email", "send_email",
        "reply_to_email", "bulk_email", "archive_email", "delete_email",
        "mark_email_read", "resolve_contact", "ui_control",
    }),
    "contacts": frozenset({"resolve_contact", "manage_contact"}),
    "sessions": frozenset({
        "create_session", "list_sessions", "manage_session",
        "send_to_session", "search_chats",
    }),
    "integrations": frozenset({"api_call"}),
    "research": frozenset({"trigger_research", "manage_research"}),
    "cookbook": frozenset({
        "download_model", "serve_model", "serve_preset", "list_serve_presets",
        "list_served_models", "stop_served_model", "tail_serve_output",
        "list_downloads", "cancel_download", "search_hf_models",
        "list_cached_models", "list_cookbook_servers", "adopt_served_model",
    }),
}

_TOOL_TO_FAMILY = {
    tool: family
    for family, tools in _BUILTIN_FAMILIES.items()
    for tool in tools
}

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
    names: set[str] = set()
    if mcp_mgr is None:
        return names
    try:
        for tool in mcp_mgr.get_all_tools():
            if not isinstance(tool, Mapping) or tool.get("is_disabled"):
                continue
            qualified = str(tool.get("qualified_name") or "").strip()
            if qualified:
                names.add(qualified)
    except Exception:
        # Visibility enrichment is optional. Failure here must never break the
        # agent loop or weaken the controller's existing permission checks.
        return set()
    return names


def sticky_tools_from_history(
    messages: Sequence[Mapping[str, Any]],
    *,
    mcp_mgr: Any = None,
    previous_user_turns: int = 2,
) -> tuple[set[str], tuple[str, ...]]:
    """Expand recent authoritative tool use into follow-up visibility."""
    evidence = recent_authoritative_tool_names(
        messages,
        previous_user_turns=previous_user_turns,
    )
    if not evidence:
        return set(), ()

    sticky: set[str] = set()
    connected_mcp = _connected_mcp_names(mcp_mgr)

    for name in evidence:
        family = _TOOL_TO_FAMILY.get(name)
        if family:
            sticky.update(_BUILTIN_FAMILIES[family])
            continue

        prefix = _mcp_server_prefix(name)
        if prefix:
            # One real call on an MCP server keeps sibling tools visible for
            # natural follow-ups such as ``cancel it`` after create_reminder.
            siblings = {tool for tool in connected_mcp if tool.startswith(prefix)}
            sticky.update(siblings or {name})
            continue

        # Unknown/custom tools are sticky only as themselves. Do not invent a
        # capability relation we cannot prove.
        sticky.add(name)

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
    """Behavior-free preview of the ToolBroker's proposed final visible set."""

    tools: set[str]
    added: tuple[str, ...]
    removed: tuple[str, ...]
    evidence: tuple[str, ...]
    reasons: Mapping[str, str]


# v0.2.3 typed cold-start capability recovery
def _broker_capabilities_for_name(name: str) -> frozenset[str]:
    """Return conservative typed capabilities for one concrete tool.

    These capabilities classify tools, not user prose. They let the
    controller's already-typed intent recover a permitted tool when semantic
    retrieval misses it, without reintroducing keyword routing in the Broker.
    """
    caps = {f"tool:{name}"}

    family = _TOOL_TO_FAMILY.get(name)
    if family:
        caps.add(f"family:{family}")

    prefix = _mcp_server_prefix(name)
    if prefix:
        caps.add(f"mcp-server:{prefix}")

    if "reminder" in name.lower():
        caps.add("family:reminders")

    return frozenset(caps)


_BROKER_DOMAIN_CAPABILITIES: Mapping[str, frozenset[str]] = {
    "notes_calendar_tasks": frozenset({
        "family:calendar",
        "family:notes_tasks",
        "family:reminders",
    }),
    "email": frozenset({"family:email"}),
    "contacts": frozenset({"family:contacts"}),
    "cookbook": frozenset({"family:cookbook"}),
    "sessions": frozenset({"family:sessions"}),
    "integrations": frozenset({"family:integrations"}),
}


def broker_capabilities_for_domains(domains: Iterable[str]) -> tuple[str, ...]:
    """Translate controller/router domains into typed Broker suggestions."""
    caps: set[str] = set()
    for domain in domains:
        caps.update(_BROKER_DOMAIN_CAPABILITIES.get(str(domain), ()))
    return tuple(sorted(caps))


def preview_final_tool_visibility(
    *,
    current: set[str] | None,
    messages: Sequence[Mapping[str, Any]],
    disabled_tools: Iterable[str] = (),
    mcp_mgr: Any = None,
    forced_names: Iterable[str] = (),
    suggested_capabilities: Iterable[str] = (),
    max_visible: int | None = None,
) -> BrokerVisibilityPreview:
    """Compute the v0.2.3 Broker proposal without changing live visibility."""

    from assistant.fork.tool_broker import (
        ConversationToolState,
        ToolBroker,
        ToolDescriptor,
    )
    from src.tool_index import ALWAYS_AVAILABLE
    from src.tool_policy import known_tool_names

    disabled = {str(name) for name in disabled_tools if name}
    connected_mcp = _connected_mcp_names(mcp_mgr)

    permitted = set(known_tool_names())
    permitted.update(connected_mcp)
    permitted.difference_update(disabled)

    current_set = set(current or ())
    permitted.update(name for name in current_set if name not in disabled)

    forced = {str(name) for name in forced_names if name}
    permitted.update(name for name in forced if name not in disabled)

    if not permitted:
        return BrokerVisibilityPreview(
            tools=set(),
            added=(),
            removed=tuple(sorted(current_set)),
            evidence=(),
            reasons={},
        )

    descriptors = [
        ToolDescriptor(
            name=name,
            capabilities=_broker_capabilities_for_name(name),
            core_visible=(name in ALWAYS_AVAILABLE or name == "discover_tools"),
            source="mcp" if name.startswith("mcp__") else "builtin",
        )
        for name in sorted(permitted)
    ]

    state = ConversationToolState()
    evidence = recent_authoritative_tool_names(messages)
    for name in evidence:
        state.activate(*_broker_capabilities_for_name(name))

    suggested = {str(cap) for cap in suggested_capabilities if cap}

    semantic_scores = {
        name: 1.0
        for name in current_set
        if name in permitted
    }

    # v0.2.3 preserve-candidates shadow tuning
    # The first production finalizer must not silently drop legacy-selected
    # candidates merely to hit an arbitrary prompt-size target. Preserve every
    # current candidate plus core/forced/authoritative-sticky recovery tools.
    # Prompt-budget ranking can become a separate, measured migration later.
    sticky_preview, _ = sticky_tools_from_history(messages, mcp_mgr=mcp_mgr)
    sticky_preview.difference_update(disabled)

    suggested_names = {
        descriptor.name
        for descriptor in descriptors
        if descriptor.capabilities & suggested
    }

    candidate_names = (
        current_set
        | forced
        | set(ALWAYS_AVAILABLE)
        | {"discover_tools"}
        | sticky_preview
        | suggested_names
    )
    candidate_names.intersection_update(permitted)
    selection_limit = (
        int(max_visible)
        if max_visible is not None
        else max(1, len(candidate_names))
    )

    broker = ToolBroker(descriptors, max_visible=selection_limit)
    selection = broker.select(
        permitted_names=permitted,
        state=state,
        suggested_capabilities=tuple(sorted(suggested)),
        semantic_scores=semantic_scores,
        forced_names=tuple(sorted(forced)),
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


def _discovery_description_map() -> dict[str, str]:
    descriptions: dict[str, str] = {}
    try:
        from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
        descriptions.update({
            str(name): str(description or "")
            for name, description in BUILTIN_TOOL_DESCRIPTIONS.items()
        })
    except Exception:
        pass

    # Native schemas sometimes carry newer/more precise wording than the ToolIndex
    # registry. Merge them so discovery ranking follows the actual callable surface.
    try:
        from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
        for schema in FUNCTION_TOOL_SCHEMAS:
            fn = schema.get("function") if isinstance(schema, dict) else None
            if not isinstance(fn, dict):
                continue
            name = str(fn.get("name") or "").strip()
            description = str(fn.get("description") or "").strip()
            if name and description:
                descriptions[name] = (
                    descriptions.get(name, "") + " " + description
                ).strip()
    except Exception:
        pass
    return descriptions


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

    from src.tool_policy import known_tool_names

    disabled = {str(name) for name in disabled_tools if name}
    permitted = set(known_tool_names())
    permitted.update(_connected_mcp_names(mcp_mgr))
    permitted.discard("discover_tools")
    permitted.difference_update(disabled)

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
        descriptions=_discovery_description_map(),
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

"""Normalized ToolCatalog adapter for the assistant fork.

Odysseus owns tool implementations, schemas, security metadata, retrieval text,
and dynamic MCP connections.  This module *adapts* those upstream authoring
surfaces into one read-only catalog for assistant orchestration.

It deliberately does not execute tools, grant permissions, change approval
rules, or become another source of truth for Odysseus tool definitions.

The only assistant-owned metadata here is selection policy:
- small cold-start anchors per intent domain;
- dynamic MCP domain adapters (email/browser/reminders).

Everything else is derived best-effort from Odysseus at runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from assistant.fork.tool_contracts import (
    ToolContract,
    build_contract,
    contract_prompt_hint,
    infer_domains,
    link_contract_dependencies,
)


# Small orchestration policy, NOT a duplicate list of every tool in a domain.
# These are fallback anchors when semantic retrieval misses. Discovery remains
# the escape hatch for less-common tools.
DOMAIN_ANCHORS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "web": ("web_search", "web_fetch"),
    "email": (
        "list_email_accounts",
        "list_emails",
        "read_email",
        "resolve_contact",
        "manage_contact",
    ),
    "contacts": ("resolve_contact", "manage_contact"),
    "cookbook": (
        "list_served_models",
        "list_cached_models",
        "tail_serve_output",
        "serve_model",
    ),
    "notes_calendar_tasks": (
        "manage_notes",
        "manage_calendar",
        "manage_tasks",
        # Dynamic reminder MCP bare names:
        "assistant_create_reminder",
        "assistant_list_reminders",
        "assistant_cancel_reminder",
    ),
    "ui": ("ui_control",),
    "sessions": ("list_sessions", "manage_session", "search_chats"),
    "settings": ("manage_settings", "manage_mcp", "manage_endpoints"),
    "integrations": ("api_call",),
    # Dynamic builtin-browser MCP bare names:
    "browser": (
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_fill_form",
        "browser_type",
        "browser_press_key",
        "browser_tabs",
    ),
})

CAPABILITY_ANCHORS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "family:reminders": (
        "assistant_create_reminder",
        "assistant_list_reminders",
        "assistant_cancel_reminder",
    ),
})


_DYNAMIC_ONLY_ANCHORS = frozenset({
    "assistant_create_reminder",
    "assistant_list_reminders",
    "assistant_cancel_reminder",
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_fill_form",
    "browser_type",
    "browser_press_key",
    "browser_tabs",
})


@dataclass(frozen=True, slots=True)
class ToolRecord:
    name: str
    source: str
    sources: frozenset[str]
    capabilities: frozenset[str]
    description: str = ""
    schema: Mapping[str, Any] | None = None
    effects: frozenset[str] = frozenset()
    security_known: bool = False
    provider: str = ""
    server_id: str = ""
    bare_name: str = ""
    output_schema: Mapping[str, Any] | None = None
    contract: ToolContract | None = None


@dataclass(frozen=True, slots=True)
class ToolCatalogAudit:
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    source_counts: Mapping[str, int]
    tool_count: int

    @property
    def ok(self) -> bool:
        return not self.errors


def _schema_name(schema: Any) -> str:
    if not isinstance(schema, Mapping):
        return ""
    fn = schema.get("function")
    if isinstance(fn, Mapping):
        return str(fn.get("name") or "").strip()
    return str(schema.get("name") or "").strip()


def _schema_description(schema: Any) -> str:
    if not isinstance(schema, Mapping):
        return ""
    fn = schema.get("function")
    if isinstance(fn, Mapping):
        return str(fn.get("description") or "").strip()
    return str(schema.get("description") or "").strip()


def _mcp_server_id(name: str) -> str:
    if not str(name).startswith("mcp__"):
        return ""
    parts = str(name).split("__", 2)
    return parts[1] if len(parts) == 3 else ""


def mcp_server_prefix(name: str) -> str | None:
    server_id = _mcp_server_id(name)
    return f"mcp__{server_id}__" if server_id else None


def bare_tool_name(name: str) -> str:
    concrete = str(name or "").strip()
    if not concrete.startswith("mcp__"):
        return concrete
    parts = concrete.split("__", 2)
    return parts[2] if len(parts) == 3 else concrete


def domain_capabilities(domains: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({
        f"domain:{str(domain).strip()}"
        for domain in domains
        if str(domain).strip()
    }))


def connected_mcp_tools(mcp_mgr: Any) -> tuple[Mapping[str, Any], ...]:
    if mcp_mgr is None:
        return ()
    try:
        return tuple(
            tool
            for tool in (mcp_mgr.get_all_tools() or ())
            if isinstance(tool, Mapping) and not tool.get("is_disabled")
        )
    except Exception:
        return ()


def connected_mcp_names(mcp_mgr: Any) -> frozenset[str]:
    return frozenset(
        str(tool.get("qualified_name") or "").strip()
        for tool in connected_mcp_tools(mcp_mgr)
        if str(tool.get("qualified_name") or "").strip()
    )


def _dynamic_capabilities(name: str) -> set[str]:
    caps: set[str] = set()
    concrete = str(name or "").strip()
    server_id = _mcp_server_id(concrete)
    bare = bare_tool_name(concrete)

    if server_id:
        caps.add(f"mcp-server:mcp__{server_id}__")
    if server_id == "email":
        caps.add("domain:email")
    if server_id == "builtin_browser":
        caps.add("domain:browser")
    if "reminder" in bare.casefold():
        caps.update({"domain:notes_calendar_tasks", "family:reminders"})
    return caps


def _normalize_domain_members(
    domain_members: Mapping[str, Iterable[str]] | None,
) -> dict[str, frozenset[str]]:
    if not domain_members:
        return {}
    return {
        str(domain): frozenset(
            str(item)
            for item in names
            if str(item).strip()
        )
        for domain, names in domain_members.items()
    }


def _domain_memberships(
    name: str,
    domain_members: Mapping[str, Iterable[str]] | None,
) -> set[str]:
    if not domain_members:
        return set()
    concrete = str(name or "").strip()
    return {
        f"domain:{str(domain)}"
        for domain, names in domain_members.items()
        if concrete in names
    }


def _collect_upstream_sources() -> tuple[
    dict[str, set[str]],
    dict[str, Mapping[str, Any]],
    dict[str, str],
    dict[str, frozenset[str]],
    set[str],
]:
    """Best-effort adapter over the currently installed Odysseus surfaces."""
    source_names: dict[str, set[str]] = {}
    schemas: dict[str, Mapping[str, Any]] = {}
    descriptions: dict[str, str] = {}
    effects: dict[str, frozenset[str]] = {}
    security_known: set[str] = set()

    try:
        from src.tool_policy import known_tool_names
        source_names["policy"] = {
            str(name) for name in known_tool_names() if str(name).strip()
        }
    except Exception:
        source_names["policy"] = set()

    try:
        from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
        names: set[str] = set()
        for schema in FUNCTION_TOOL_SCHEMAS:
            name = _schema_name(schema)
            if not name:
                continue
            names.add(name)
            schemas[name] = schema
            desc = _schema_description(schema)
            if desc:
                descriptions[name] = desc
        source_names["schemas"] = names
    except Exception:
        source_names["schemas"] = set()

    try:
        from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
        source_names["retrieval"] = {
            str(name) for name in BUILTIN_TOOL_DESCRIPTIONS if str(name).strip()
        }
        for name, description in BUILTIN_TOOL_DESCRIPTIONS.items():
            concrete = str(name).strip()
            text = str(description or "").strip()
            if concrete and text:
                # Retrieval text is intentionally richer; prefer it.
                descriptions[concrete] = text
    except Exception:
        source_names["retrieval"] = set()

    try:
        from src.tool_capabilities import TOOL_CAPABILITIES
        names: set[str] = set()
        for name, capability in TOOL_CAPABILITIES.items():
            concrete = str(name).strip()
            if not concrete:
                continue
            names.add(concrete)
            security_known.add(concrete)
            raw_effects = getattr(capability, "effects", ()) or ()
            effects[concrete] = frozenset(
                str(getattr(effect, "value", effect))
                for effect in raw_effects
            )
        source_names["security"] = names
    except Exception:
        source_names["security"] = set()

    # Upstream is actively migrating execution into TOOL_HANDLERS.  Older
    # revisions may import heavier dependencies or still dispatch some native
    # tools elsewhere, so this is an informative source, not the sole authority.
    try:
        from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
        source_names["handlers"] = {
            str(name) for name in TOOL_HANDLERS if str(name).strip()
        }
        source_names["tags"] = {
            str(name) for name in TOOL_TAGS if str(name).strip()
        }
    except Exception:
        source_names["handlers"] = set()
        try:
            from src.agent_tools import TOOL_TAGS
            source_names["tags"] = {
                str(name) for name in TOOL_TAGS if str(name).strip()
            }
        except Exception:
            source_names["tags"] = set()

    return source_names, schemas, descriptions, effects, security_known


def _record(
    name: str,
    *,
    source: str,
    sources: Iterable[str] = (),
    description: str = "",
    schema: Mapping[str, Any] | None = None,
    output_schema: Mapping[str, Any] | None = None,
    effects: Iterable[str] = (),
    security_known: bool = False,
    domain_members: Mapping[str, Iterable[str]] | None = None,
    provider: str = "",
    server_id: str = "",
    mcp_read_only: bool | None = None,
    provider_domains: Iterable[str] = (),
) -> ToolRecord:
    concrete = str(name or "").strip()
    bare = bare_tool_name(concrete)
    caps = {f"tool:{concrete}"}
    caps.update(_domain_memberships(concrete, domain_members))
    caps.update(_dynamic_capabilities(concrete))

    # Native Odysseus domain membership already has an authoritative source:
    # ``domain_members`` supplied by the controller plus the small legacy
    # dynamic capability map above.  Do not widen native capabilities merely
    # from a descriptive tool name (e.g. ``manage_calendar``), because that
    # changes verified capability-lease semantics.  External MCP tools do not
    # have that upstream map, so infer their domains from provider metadata,
    # leaf names and descriptions.
    inferred_domains = (
        infer_domains(
            bare,
            description,
            provider_domains=provider_domains,
        )
        if source == "mcp"
        else frozenset()
    )
    caps.update(f"domain:{domain}" for domain in inferred_domains)
    domains = {
        cap.split(":", 1)[1]
        for cap in caps
        if cap.startswith("domain:")
    }
    normalized_effects = frozenset(str(item) for item in effects if item)
    contract = build_contract(
        name=concrete,
        bare_name=bare,
        source=source,
        description=description,
        schema=schema,
        output_schema=output_schema,
        effects=normalized_effects,
        security_known=security_known,
        provider=provider,
        server_id=server_id,
        mcp_read_only=mcp_read_only,
        domains=domains,
    )
    return ToolRecord(
        name=concrete,
        source=source,
        sources=frozenset(str(item) for item in sources if item),
        capabilities=frozenset(caps),
        description=str(description or ""),
        schema=schema,
        effects=normalized_effects,
        security_known=bool(security_known),
        provider=str(provider or ""),
        server_id=str(server_id or ""),
        bare_name=bare,
        output_schema=output_schema,
        contract=contract,
    )


def build_tool_catalog(
    *,
    mcp_mgr: Any = None,
    disabled_tools: Iterable[str] = (),
    domain_members: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, ToolRecord]:
    """Compile one normalized selection catalog from Odysseus + connected MCP."""
    disabled = {str(name) for name in disabled_tools if name}
    normalized_domains = _normalize_domain_members(domain_members)
    source_names, schemas, descriptions, effects, security_known = (
        _collect_upstream_sources()
    )

    all_native: set[str] = {"discover_tools"}
    for names in source_names.values():
        all_native.update(names)

    records: dict[str, ToolRecord] = {}
    for name in sorted(all_native):
        if not name or name in disabled:
            continue
        memberships = {
            source for source, names in source_names.items() if name in names
        }
        records[name] = _record(
            name,
            source="builtin",
            sources=memberships or {"assistant-recovery"},
            description=descriptions.get(name, ""),
            schema=schemas.get(name),
            effects=effects.get(name, ()),
            security_known=name in security_known,
            domain_members=normalized_domains,
        )

    # Remote MCP metadata is normalized in two passes. Strongly-named tools
    # establish provider domains first; ambiguous siblings such as
    # list_folders/list_identities may then inherit that provider context.
    mcp_tools = connected_mcp_tools(mcp_mgr)
    provider_domains: dict[str, set[str]] = {}
    for tool in mcp_tools:
        server_id = str(tool.get("server_id") or _mcp_server_id(
            str(tool.get("qualified_name") or "")
        )).strip()
        bare = str(tool.get("name") or bare_tool_name(
            str(tool.get("qualified_name") or "")
        )).strip()
        description = str(tool.get("description") or "").strip()
        provider_domains.setdefault(server_id, set()).update(
            infer_domains(bare, description)
        )

    for tool in mcp_tools:
        name = str(tool.get("qualified_name") or "").strip()
        if not name or name in disabled:
            continue
        server_id = str(tool.get("server_id") or _mcp_server_id(name)).strip()
        provider = str(tool.get("server_name") or server_id).strip()
        description = str(tool.get("description") or "").strip()
        schema = tool.get("input_schema") or tool.get("inputSchema")
        output_schema = tool.get("output_schema") or tool.get("outputSchema")
        mcp_read_only = tool.get("read_only")
        records[name] = _record(
            name,
            source="mcp",
            sources={"mcp"},
            description=description,
            schema=schema if isinstance(schema, Mapping) else None,
            output_schema=(
                output_schema if isinstance(output_schema, Mapping) else None
            ),
            # MCP read/write classification comes from provider annotations
            # when available and the manager's fail-high fallback otherwise.
            security_known=mcp_read_only is not None,
            domain_members=normalized_domains,
            provider=provider,
            server_id=server_id,
            mcp_read_only=(
                bool(mcp_read_only) if mcp_read_only is not None else None
            ),
            provider_domains=provider_domains.get(server_id, ()),
        )

    # Once every tool is normalized, attach provider-local producer links
    # (search/list -> read/get identifier prerequisites).
    contracts = link_contract_dependencies({
        name: record.contract
        for name, record in records.items()
        if record.contract is not None
    })
    for name, contract in contracts.items():
        record = records.get(name)
        if record is not None:
            records[name] = replace(record, contract=contract)

    return records


def record_for_runtime_name(
    name: str,
    *,
    domain_members: Mapping[str, Iterable[str]] | None = None,
) -> ToolRecord:
    """Create conservative metadata for a caller-supplied/custom runtime name."""
    concrete = str(name or "").strip()
    return _record(
        concrete,
        source="runtime",
        sources={"runtime"},
        domain_members=domain_members,
    )


def native_tool_names() -> frozenset[str]:
    return frozenset(
        name
        for name, record in build_tool_catalog().items()
        if record.source != "mcp"
    )


def capabilities_for_name(
    name: str,
    *,
    domain_members: Mapping[str, Iterable[str]] | None = None,
) -> frozenset[str]:
    return record_for_runtime_name(
        name,
        domain_members=domain_members,
    ).capabilities


def _anchor_matches(
    record: ToolRecord,
    anchor: str,
) -> bool:
    return record.name == anchor or bare_tool_name(record.name) == anchor


def anchor_names_for_capabilities(
    capabilities: Iterable[str],
    records: Mapping[str, ToolRecord],
) -> tuple[str, ...]:
    """Resolve small fallback anchors against the actual runtime catalog.

    Exact/native names win. MCP-qualified aliases are used only when an exact
    native anchor is unavailable, preventing duplicate native+MCP email schemas.
    """
    requested = {str(cap) for cap in capabilities if str(cap)}
    anchor_groups: list[tuple[str, ...]] = []

    for capability in sorted(requested):
        if capability.startswith("domain:"):
            domain = capability.split(":", 1)[1]
            anchor_groups.append(DOMAIN_ANCHORS.get(domain, ()))
        anchor_groups.append(CAPABILITY_ANCHORS.get(capability, ()))

    out: list[str] = []
    seen: set[str] = set()

    for anchors in anchor_groups:
        for anchor in anchors:
            exact = records.get(anchor)
            if exact is not None:
                if exact.name not in seen:
                    seen.add(exact.name)
                    out.append(exact.name)
                continue
            dynamic = sorted(
                record.name
                for record in records.values()
                if record.source == "mcp" and _anchor_matches(record, anchor)
            )
            for name in dynamic:
                if name not in seen:
                    seen.add(name)
                    out.append(name)
    return tuple(out)


def enrich_tool_schemas_with_contracts(
    schemas: Sequence[Mapping[str, Any]],
    records: Mapping[str, ToolRecord],
) -> list[dict[str, Any]]:
    """Append compact contract hints without mutating upstream schema objects."""
    enriched: list[dict[str, Any]] = []
    for schema in schemas or ():
        if not isinstance(schema, Mapping):
            continue
        outer = dict(schema)
        fn = outer.get("function")
        if not isinstance(fn, Mapping):
            enriched.append(outer)
            continue
        fn_copy = dict(fn)
        name = str(fn_copy.get("name") or "").strip()
        record = records.get(name)
        hint = contract_prompt_hint(record.contract) if record and record.contract else ""
        if hint:
            desc = str(fn_copy.get("description") or "").rstrip()
            if hint.strip() not in desc:
                fn_copy["description"] = (desc + hint).strip()
        outer["function"] = fn_copy
        enriched.append(outer)
    return enriched


def audit_tool_catalog(
    *,
    mcp_mgr: Any = None,
    domain_members: Mapping[str, Iterable[str]] | None = None,
) -> ToolCatalogAudit:
    source_names, schemas, descriptions, _effects, security_known = (
        _collect_upstream_sources()
    )
    records = build_tool_catalog(
        mcp_mgr=mcp_mgr,
        domain_members=domain_members,
    )
    errors: list[str] = []
    warnings: list[str] = []

    native_source_union: set[str] = {"discover_tools"}
    for names in source_names.values():
        native_source_union.update(names)

    # The caller-provided Odysseus domain map is treated as upstream metadata.
    # Older Odysseus revisions still have direct-dispatch tools that may not be
    # represented in every registry surface, so surface drift as a warning.
    if domain_members:
        for domain, names in domain_members.items():
            for raw_name in names:
                name = str(raw_name)
                if name and name not in native_source_union:
                    warnings.append(
                        f"domain {domain!r} references tool {name!r} absent "
                        "from the normalized upstream metadata surfaces"
                    )

    # Our small anchor overlay must either resolve to a native source or be
    # explicitly dynamic-only (MCP reminders/browser).
    for domain, anchors in DOMAIN_ANCHORS.items():
        for anchor in anchors:
            if anchor in _DYNAMIC_ONLY_ANCHORS:
                continue
            if anchor not in native_source_union:
                errors.append(
                    f"assistant anchor {domain!r}/{anchor!r} is absent from "
                    "all Odysseus native metadata sources"
                )
    for capability, anchors in CAPABILITY_ANCHORS.items():
        for anchor in anchors:
            if anchor in _DYNAMIC_ONLY_ANCHORS:
                continue
            if anchor not in native_source_union:
                errors.append(
                    f"assistant anchor {capability!r}/{anchor!r} is absent from "
                    "all Odysseus native metadata sources"
                )

    # Warn, don't fail, on upstream metadata incompleteness. Unknown security
    # already fails high in Odysseus, and retrieval can recover through schema
    # or discovery; the audit makes drift visible without weakening runtime.
    important = set()
    if domain_members:
        for names in domain_members.values():
            important.update(str(name) for name in names)
    for anchors in DOMAIN_ANCHORS.values():
        important.update(anchor for anchor in anchors if anchor not in _DYNAMIC_ONLY_ANCHORS)

    for name in sorted(important & native_source_union):
        if name not in descriptions and name not in schemas:
            warnings.append(f"{name!r} has no schema/retrieval description")
        if source_names.get("security") and name not in security_known:
            warnings.append(f"{name!r} has no explicit ToolCapabilities entry")

    counts = MappingProxyType({
        source: len(names)
        for source, names in sorted(source_names.items())
    })
    return ToolCatalogAudit(
        errors=tuple(errors),
        warnings=tuple(warnings),
        source_counts=counts,
        tool_count=len(records),
    )


# Compatibility name used by the v0.2.5 migration and older fork tests.
def build_runtime_registry(
    *,
    mcp_mgr: Any = None,
    disabled_tools: Iterable[str] = (),
    domain_members: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, ToolRecord]:
    return build_tool_catalog(
        mcp_mgr=mcp_mgr,
        disabled_tools=disabled_tools,
        domain_members=domain_members,
    )


if __name__ == "__main__":
    audit = audit_tool_catalog()
    print(f"ToolCatalog: {audit.tool_count} tools")
    print("sources:")
    for source, count in audit.source_counts.items():
        print(f"  {source}: {count}")
    if audit.warnings:
        print("warnings:")
        for warning in audit.warnings:
            print(f"  - {warning}")
    if audit.errors:
        print("errors:")
        for error in audit.errors:
            print(f"  - {error}")
        raise SystemExit(1)
    print("audit: PASS")

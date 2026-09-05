"""Normalized tool contracts for assistant orchestration.

Tool contracts describe *how to use* already-authorized tools.  They do not
execute tools, grant permission, or weaken Odysseus approval/security gates.

The contract layer intentionally combines three sources:
- provider/native schema metadata (required inputs, descriptions);
- deterministic effect metadata from Odysseus / MCP annotations;
- small generic conventions (search/list tools produce identifiers consumed by
  read/get tools in the same resource family).

This gives the ToolBroker enough structure to prefer useful next steps without
hard-coding individual integrations such as Fastmail.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping


# Effects that can mutate state or execute arbitrary code. Network egress by
# itself is *not* mutating: web/MCP reads legitimately need network access.
_MUTATING_EFFECTS = frozenset({
    "write_workspace",
    "write_private",
    "execute_code",
    "external_side_effect",
    "ui_side_effect",
    "admin_change",
    "destructive",
})

_READONLY_VERBS = frozenset({
    "list", "get", "read", "search", "fetch", "query", "find", "describe",
    "show", "view", "lookup", "count", "status", "info", "inspect", "summarize",
    "summarise", "summary",
})
_DISCOVERY_VERBS = frozenset({"list", "search", "find", "query", "lookup"})
_MUTATING_VERBS = frozenset({
    "add", "append", "archive", "cancel", "create", "delete", "edit", "mark",
    "move", "post", "remove", "reply", "send", "set", "start", "stop", "update",
    "write", "unsubscribe",
})

# These are orchestration domains already understood by the fork. Keep this
# deliberately small and semantic; caller-provided domain_members remain the
# stronger source for native tools.
_DOMAIN_TERMS: Mapping[str, frozenset[str]] = MappingProxyType({
    "email": frozenset({
        "email", "emails", "mail", "mailbox", "inbox", "thread", "threads",
        "message", "messages",
    }),
    "contacts": frozenset({"contact", "contacts", "addressbook", "carddav"}),
    "notes_calendar_tasks": frozenset({
        "calendar", "calendars", "event", "events", "note", "notes",
        "reminder", "reminders", "task", "tasks",
    }),
    "browser": frozenset({
        "browser", "navigate", "snapshot", "screenshot", "tab", "tabs", "page",
    }),
    "web": frozenset({"web", "url", "website"}),
    "sessions": frozenset({"session", "sessions", "chat", "chats"}),
})

# Ambiguous resource words can inherit a provider's strong domain. Fastmail is
# the motivating example: list_folders/list_identities are email operations,
# while list_calendars remains explicitly calendar-domain.
_PROVIDER_DOMAIN_INHERITANCE: Mapping[str, frozenset[str]] = MappingProxyType({
    "email": frozenset({"folder", "folders", "identity", "identities", "label", "labels"}),
    "notes_calendar_tasks": frozenset({"folder", "folders"}),
})

_READONLY_REQUEST_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bread[ -]?only\b",
        r"\bdo\s+not\s+(?:change|modify|write|update)\s+anything\b",
        r"\bdon['’]?t\s+(?:change|modify|write|update)\s+anything\b",
        r"\bwithout\s+(?:changing|modifying|writing|updating)\s+anything\b",
        r"\b(?:just|only)\s+(?:read|inspect|look|show|check|search|list|view)\b",
    )
)


# Provider query languages are usage metadata, not routing authority.  Keep
# provider-specific syntax in one small adapter table so MCP selection remains
# generic while the model receives the grammar required by the chosen tool.
_PROVIDER_TOOL_USAGE_HINTS: Mapping[tuple[str, str], tuple[str, ...]] = MappingProxyType({
    ("fastmail", "search_email"): (
        "Fastmail folder filters use in:<folder>, e.g. in:inbox; never write 'in inbox'",
        "use Fastmail operators such as from:, to:, subject:, date:, before:, after:, and is:unread only when the user requested those filters",
    ),
})

_FASTMAIL_SYSTEM_FOLDER_WORDS = frozenset({
    "inbox", "sent", "draft", "drafts", "trash", "spam", "archive",
})


@dataclass(frozen=True, slots=True)
class ToolContract:
    """Controller-facing normalized usage contract for one concrete tool."""

    name: str
    bare_name: str
    source: str
    provider: str = ""
    server_id: str = ""
    domains: frozenset[str] = frozenset()
    effects: frozenset[str] = frozenset()
    effect_known: bool = False
    read_only: bool = False
    required_inputs: tuple[str, ...] = ()
    produced_outputs: frozenset[str] = frozenset()
    resource_kind: str = ""
    verb: str = ""
    producer_tools: tuple[str, ...] = ()
    usage_hints: tuple[str, ...] = ()

    @property
    def mutating(self) -> bool:
        return not self.read_only


def _tokens(value: str) -> tuple[str, ...]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or ""))
    return tuple(
        token for token in re.split(r"[^a-z0-9]+", text.casefold()) if token
    )


def _singular(token: str) -> str:
    token = str(token or "").casefold()
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("ses") and len(token) > 4:
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        return token[:-1]
    return token


def _schema_parameters(schema: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(schema, Mapping):
        return {}
    function = schema.get("function")
    if isinstance(function, Mapping):
        params = function.get("parameters")
        return params if isinstance(params, Mapping) else {}
    return schema


def required_schema_inputs(schema: Mapping[str, Any] | None) -> tuple[str, ...]:
    params = _schema_parameters(schema)
    required = params.get("required") if isinstance(params, Mapping) else None
    if not isinstance(required, (list, tuple, set)):
        return ()
    return tuple(str(item) for item in required if str(item).strip())


def _output_fields(output_schema: Mapping[str, Any] | None) -> set[str]:
    if not isinstance(output_schema, Mapping):
        return set()
    props = output_schema.get("properties")
    if not isinstance(props, Mapping):
        return set()
    return {str(name) for name in props if str(name).strip()}


def _verb_and_resource(bare_name: str) -> tuple[str, str]:
    parts = list(_tokens(bare_name))
    if not parts:
        return "", ""
    verb = parts[0]
    # Common assistant prefixes are not semantic verbs.
    if verb in {"assistant", "tool", "mcp"} and len(parts) > 1:
        parts = parts[1:]
        verb = parts[0]
    resource_parts = parts[1:]
    if not resource_parts:
        return verb, ""
    # Strip lightweight operation modifiers but retain compound resources like
    # email_account. The last semantic noun is usually the useful dependency key.
    resource = "_".join(_singular(token) for token in resource_parts)
    return verb, resource


def infer_domains(
    bare_name: str,
    description: str = "",
    *,
    provider_domains: Iterable[str] = (),
) -> frozenset[str]:
    name_tokens = set(_tokens(bare_name))
    description_tokens = set(_tokens(description))
    all_tokens = name_tokens | description_tokens
    domains = {
        domain
        for domain, terms in _DOMAIN_TERMS.items()
        if all_tokens & terms
    }

    inherited = {str(domain) for domain in provider_domains if str(domain)}
    # Provider inheritance is only a fallback for otherwise-unclassified tools.
    # A tool whose own name/description already says "mailbox" or "calendar"
    # must not become multi-domain merely because its server exposes both.
    if inherited and not domains:
        for domain in inherited:
            ambiguous = _PROVIDER_DOMAIN_INHERITANCE.get(domain, frozenset())
            if name_tokens & ambiguous:
                domains.add(domain)
    return frozenset(domains)


def _infer_read_only_from_name(bare_name: str) -> bool:
    verb, _resource = _verb_and_resource(bare_name)
    if verb in _READONLY_VERBS:
        return True
    if verb in _MUTATING_VERBS:
        return False
    return False  # fail-high for unknown external tools


def _normalize_effects(effects: Iterable[str]) -> frozenset[str]:
    return frozenset(str(item) for item in effects if str(item))


def _infer_produced_outputs(
    bare_name: str,
    output_schema: Mapping[str, Any] | None,
) -> frozenset[str]:
    out = {_canonical_input_name(name) for name in _output_fields(output_schema)}
    verb, resource = _verb_and_resource(bare_name)
    if verb in _DISCOVERY_VERBS and resource:
        out.add(f"{resource}_id")
        # Email search/list results commonly surface both message/email and
        # thread identifiers. This is a resource convention, not provider logic.
        if resource in {"email", "emails", "message", "messages"}:
            out.update({"email_id", "message_id", "thread_id"})
    return frozenset(item for item in out if item)


def _canonical_input_name(name: str, resource_kind: str = "") -> str:
    tokens = list(_tokens(name))
    if not tokens:
        return ""
    normalized = "_".join(_singular(token) for token in tokens)
    if normalized in {"id", "ids"} and resource_kind:
        return f"{resource_kind}_id"
    return normalized


def _provider_usage_hints(provider: str, bare_name: str) -> tuple[str, ...]:
    provider_key = " ".join(_tokens(provider))
    bare_key = str(bare_name or "").casefold()
    hints: list[str] = []
    for (provider_token, tool_name), values in _PROVIDER_TOOL_USAGE_HINTS.items():
        if provider_token in provider_key.split() and bare_key == tool_name:
            hints.extend(values)
    return tuple(dict.fromkeys(hints))


def _validate_provider_query(
    contract: ToolContract,
    args: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Reject known malformed provider query syntax before a remote call.

    This is intentionally narrow: provider grammars are not guessed globally.
    We only validate syntax with a documented provider adapter, and otherwise
    leave the query untouched.
    """
    provider_key = " ".join(_tokens(contract.provider))
    if "fastmail" not in provider_key.split() or contract.bare_name.casefold() != "search_email":
        return None

    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return None

    # Fastmail's documented folder operator is ``in:<folder>``.  The exact bug
    # that motivated this guard was ``in inbox``: Fastmail treated those as
    # ordinary search words and returned unrelated/older messages.  Limit the
    # rejection to well-known system folders so legitimate prose searches such
    # as "meeting in Brussels" remain valid.
    bad = re.search(
        r"(?:^|\s)in\s+(inbox|sent|drafts?|trash|spam|archive)(?=\s|$)",
        query,
        flags=re.IGNORECASE,
    )
    if bad and bad.group(1).casefold() in _FASTMAIL_SYSTEM_FOLDER_WORDS:
        folder = bad.group(1).casefold()
        return {
            "error": (
                "Fastmail folder search syntax is invalid: use "
                f"'in:{folder}' instead of 'in {folder}'."
            ),
            "exit_code": 1,
            "blocked": True,
            "policy": "tool_contract_query_syntax",
            "argument": "query",
            "suggested_query": re.sub(
                rf"(?i)(?:^|(?<=\s))in\s+{re.escape(bad.group(1))}(?=\s|$)",
                f"in:{folder}",
                query,
                count=1,
            ),
        }
    return None


def build_contract(
    *,
    name: str,
    bare_name: str,
    source: str,
    description: str = "",
    schema: Mapping[str, Any] | None = None,
    output_schema: Mapping[str, Any] | None = None,
    effects: Iterable[str] = (),
    security_known: bool = False,
    provider: str = "",
    server_id: str = "",
    mcp_read_only: bool | None = None,
    domains: Iterable[str] = (),
) -> ToolContract:
    normalized_effects = _normalize_effects(effects)
    verb, resource = _verb_and_resource(bare_name)
    explicit_domains = {str(domain) for domain in domains if str(domain)}

    if source == "mcp":
        read_only = bool(mcp_read_only) if mcp_read_only is not None else _infer_read_only_from_name(bare_name)
        effect_known = mcp_read_only is not None
        if not normalized_effects:
            normalized_effects = frozenset({"read_private", "network_egress"}) if read_only else frozenset({"external_side_effect", "network_egress"})
    else:
        effect_known = bool(security_known)
        if security_known:
            read_only = not bool(normalized_effects & _MUTATING_EFFECTS)
        else:
            read_only = _infer_read_only_from_name(bare_name)

    required = required_schema_inputs(schema)
    produced = _infer_produced_outputs(bare_name, output_schema)
    return ToolContract(
        name=str(name),
        bare_name=str(bare_name),
        source=str(source),
        provider=str(provider or ""),
        server_id=str(server_id or ""),
        domains=frozenset(explicit_domains),
        effects=normalized_effects,
        effect_known=effect_known,
        read_only=read_only,
        required_inputs=required,
        produced_outputs=produced,
        resource_kind=resource,
        verb=verb,
        usage_hints=_provider_usage_hints(provider, bare_name),
    )


def explicit_read_only_request(text: str) -> bool:
    """True only for an explicit user constraint against mutation."""
    value = str(text or "")
    return any(pattern.search(value) for pattern in _READONLY_REQUEST_PATTERNS)


def _provider_key(value: str) -> str:
    return " ".join(_tokens(value))


def explicitly_named_provider_ids(
    text: str,
    contracts: Iterable[ToolContract],
) -> frozenset[str]:
    """Resolve explicitly named connected MCP providers from catalog metadata."""
    haystack = " " + " ".join(_tokens(text)) + " "
    matched: set[str] = set()
    seen: set[tuple[str, str]] = set()
    for contract in contracts:
        if contract.source != "mcp" or not contract.server_id or not contract.provider:
            continue
        key = (contract.server_id, contract.provider)
        if key in seen:
            continue
        seen.add(key)
        phrase = _provider_key(contract.provider)
        if len(phrase) >= 3 and f" {phrase} " in haystack:
            matched.add(contract.server_id)
    return frozenset(matched)


def link_contract_dependencies(
    contracts: Mapping[str, ToolContract],
) -> dict[str, ToolContract]:
    """Attach provider-local producer hints to tools with identifier inputs."""
    out: dict[str, ToolContract] = {}
    values = tuple(contracts.values())
    for target in values:
        required = {
            _canonical_input_name(name, target.resource_kind)
            for name in target.required_inputs
        }
        id_requirements = {name for name in required if name.endswith("_id") or name == "id"}
        producers: list[tuple[int, str]] = []
        if id_requirements:
            for candidate in values:
                if candidate.name == target.name or not candidate.read_only:
                    continue
                if candidate.verb not in _DISCOVERY_VERBS:
                    continue
                # Prefer same MCP provider. For native tools, stay native unless
                # there is no provider identity to constrain.
                if target.server_id:
                    if candidate.server_id != target.server_id:
                        continue
                elif candidate.server_id:
                    continue
                target_domains = set(target.domains)
                candidate_domains = set(candidate.domains)
                if target_domains and candidate_domains and not (target_domains & candidate_domains):
                    continue

                produced = set(candidate.produced_outputs)
                exact = bool(id_requirements & produced)
                same_resource = bool(
                    target.resource_kind
                    and candidate.resource_kind == target.resource_kind
                )
                # search_email may also produce thread_id, captured above by
                # inferred outputs; same-resource fallback covers generic list/read.
                if not exact and not same_resource:
                    continue
                score = (2 if exact else 1) + (2 if candidate.server_id == target.server_id and target.server_id else 0)
                producers.append((score, candidate.name))

        ordered = tuple(
            name for _score, name in sorted(producers, key=lambda item: (-item[0], item[1]))[:4]
        )
        out[target.name] = replace(target, producer_tools=ordered)
    return out


def contract_prompt_hint(contract: ToolContract) -> str:
    """Compact model-facing usage hint; execution enforcement remains separate."""
    parts: list[str] = []
    if contract.read_only:
        parts.append("read-only")
    elif contract.effect_known or contract.source == "mcp":
        parts.append("state-changing")

    if contract.usage_hints:
        parts.extend(contract.usage_hints)

    if contract.producer_tools:
        producer_names = []
        for name in contract.producer_tools[:3]:
            # Same-provider MCP names are easier for the model to read as leaves;
            # schema visibility still exposes the concrete qualified function.
            leaf = name.split("__", 2)[-1] if name.startswith("mcp__") else name
            if leaf not in producer_names:
                producer_names.append(leaf)
        parts.append(
            "if an identifier is missing, first use " + ", ".join(producer_names)
            + "; never invent IDs"
        )
    if not parts:
        return ""
    return " Tool contract: " + "; ".join(parts) + "."


def validate_contract_arguments(
    contract: ToolContract,
    content: str,
) -> dict[str, Any] | None:
    """Validate schema-required JSON args before any backend side effect.

    Returns a structured controller error when required fields are missing.
    Non-JSON legacy tool payloads are left to their existing parsers.
    """
    raw = str(content or "").strip()
    if not raw.startswith("{"):
        return None
    try:
        args = json.loads(raw)
    except Exception:
        return None
    if not isinstance(args, dict):
        return None
    provider_error = _validate_provider_query(contract, args)
    if provider_error is not None:
        return provider_error
    if not contract.required_inputs:
        return None
    missing = [
        name for name in contract.required_inputs
        if name not in args or args.get(name) in (None, "", [], {})
    ]
    if not missing:
        return None
    return {
        "error": (
            f"Tool '{contract.bare_name}' is not ready: missing required input(s): "
            + ", ".join(missing)
        ),
        "exit_code": 1,
        "blocked": True,
        "policy": "tool_contract_prerequisite",
        "missing": missing,
        "producer_tools": list(contract.producer_tools),
    }

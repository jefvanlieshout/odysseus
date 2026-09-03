"""Selection-quality helpers for the assistant fork.

These helpers improve relevance and convergence without becoming another source
of tool authority. Odysseus still classifies intent and tool effects; the fork
only refines how those verified signals are used by ToolBroker.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping


_CACHEABLE_READ_EFFECTS = frozenset({
    "read_public",
    "read_workspace",
    "read_private",
})

_VOLATILE_READ_TOOLS = frozenset({
    "list_downloads",
    "list_served_models",
    "tail_serve_output",
})


def refine_intent_for_selection(
    full_intent: Mapping[str, Any] | None,
    latest_only_intent: Mapping[str, Any] | None,
    latest_text: str,
) -> dict[str, Any]:
    """Drop inherited continuation context when the latest turn is explicit."""
    result = dict(full_intent or {})
    latest = dict(latest_only_intent or {})

    full_domains = {
        str(domain)
        for domain in (result.get("domains") or ())
        if str(domain)
    }
    latest_domains = {
        str(domain)
        for domain in (latest.get("domains") or ())
        if str(domain)
    }

    if not bool(result.get("continuation")):
        return result
    if not latest_domains or bool(latest.get("low_signal")):
        return result

    latest_text = str(latest_text or "").strip()
    if len(latest_text) < 4:
        return result

    full_query = str(result.get("retrieval_query") or "").strip()
    latest_query = str(latest.get("retrieval_query") or latest_text).strip()
    inherited_query = bool(
        full_query
        and latest_query
        and full_query != latest_query
        and ("\n" in full_query or latest_query not in full_query)
    )

    if latest_domains == full_domains and not inherited_query:
        return result

    result["domains"] = set(latest_domains)
    result["continuation"] = False
    result["retrieval_query"] = latest_query or latest_text
    result["selection_refined"] = True
    result["selection_refine_reason"] = "latest-explicit-topic"
    return result


def canonical_tool_call_signature(tool_name: Any, content: Any) -> str:
    """Return a stable signature for one concrete tool call."""
    name = str(tool_name or "").strip()

    if isinstance(content, Mapping):
        normalized: Any = dict(content)
    else:
        raw = str(content or "").strip()
        try:
            normalized = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            normalized = re.sub(r"\s+", " ", raw)

    if isinstance(normalized, (dict, list)):
        payload = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    else:
        payload = str(normalized)
    return f"{name}:{payload}"


def action_is_cacheable_read(tool_name: Any, content: Any) -> bool:
    """Whether an identical successful call may be reused within one turn.

    Unknown tools fail closed. Anything with write, execution, egress,
    destructive, UI, admin, or user-interaction effects is not cacheable.
    """
    concrete = str(tool_name or "").strip()
    if (
        concrete in _VOLATILE_READ_TOOLS
        or concrete.startswith("mcp__builtin_browser__")
    ):
        return False

    try:
        from src.tool_capabilities import capabilities_for_action

        capabilities = capabilities_for_action(tool_name, content)
    except Exception:
        return False

    if not getattr(capabilities, "known", False):
        return False

    effects = {
        str(getattr(effect, "value", effect))
        for effect in (getattr(capabilities, "effects", ()) or ())
    }
    return bool(effects) and effects <= _CACHEABLE_READ_EFFECTS

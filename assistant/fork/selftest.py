#!/usr/bin/env python3
"""No-dependency contract tests for the assistant fork."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from assistant.fork.execution_context import ExecutionContext
from assistant.fork.tool_broker import (
    ConversationToolState,
    ToolBroker,
    ToolDescriptor,
    ToolRisk,
)
from assistant.fork.tool_broker_runtime import (
    apply_sticky_tool_visibility,
    recent_authoritative_tool_names,
    sticky_tools_from_history,
)


class _FakeMcpManager:
    def get_all_tools(self):
        return [
            {"qualified_name": "mcp__assistant_reminders__assistant_create_reminder"},
            {"qualified_name": "mcp__assistant_reminders__assistant_list_reminders"},
            {"qualified_name": "mcp__assistant_reminders__assistant_cancel_reminder"},
            {"qualified_name": "mcp__email__list_emails"},
            {"qualified_name": "mcp__email__read_email"},
            {"qualified_name": "mcp__builtin_browser__browser_wait_for"},
            {"qualified_name": "mcp__builtin_browser__browser_navigate"},
            {"qualified_name": "mcp__builtin_browser__browser_snapshot"},
            {"qualified_name": "mcp__builtin_browser__browser_click"},
            {"qualified_name": "mcp__builtin_browser__browser_fill_form"},
            {"qualified_name": "mcp__builtin_browser__browser_type"},
            {"qualified_name": "mcp__builtin_browser__browser_press_key"},
            {"qualified_name": "mcp__builtin_browser__browser_tabs"},
            {"qualified_name": "mcp__other__unrelated"},
        ]


def _tool_history(tool: str) -> list[dict]:
    return [
        {"role": "user", "content": "first request"},
        {
            "role": "assistant",
            "content": "done",
            "metadata": {"tool_events": [{"tool": tool, "output": "ok"}]},
        },
        {"role": "user", "content": "follow-up"},
    ]


def main() -> None:
    tools = [
        ToolDescriptor("discover_tools", frozenset({"tools.discovery"}), core_visible=True),
        ToolDescriptor("calendar_read", frozenset({"calendar.read"})),
        ToolDescriptor("calendar_write", frozenset({"calendar.write"}), risk=ToolRisk.MUTATING),
        ToolDescriptor("proxmox_read", frozenset({"proxmox.read"})),
        ToolDescriptor("proxmox_destroy", frozenset({"proxmox.control"}), risk=ToolRisk.DESTRUCTIVE),
    ]
    broker = ToolBroker(tools, max_visible=4)
    state = ConversationToolState()

    # Permission boundary: semantic/router hints cannot surface a denied tool.
    sel = broker.select(
        permitted_names={"discover_tools", "calendar_read"},
        state=state,
        suggested_capabilities=["proxmox.control", "calendar.read"],
        semantic_scores={"proxmox_destroy": 999.0, "calendar_read": 0.2},
    )
    assert "proxmox_destroy" not in sel.visible
    assert "calendar_read" in sel.visible
    assert "discover_tools" in sel.visible

    # Typed sticky state survives a vague follow-up without matching a magic word.
    state.activate("calendar.read", "calendar.write")
    sel = broker.select(
        permitted_names={"discover_tools", "calendar_read", "calendar_write", "proxmox_read"},
        state=state,
    )
    assert "calendar_read" in sel.visible and "calendar_write" in sel.visible

    # Discovery can recover a permitted capability omitted by auto selection.
    found = broker.discover(
        permitted_names={"discover_tools", "proxmox_read"},
        capabilities={"proxmox.read"},
    )
    assert found == ("proxmox_read",)

    # Runtime state comes from authoritative persisted tool events, not prose.
    messages = _tool_history("manage_calendar")
    assert recent_authoritative_tool_names(messages) == ("manage_calendar",)
    sticky, evidence = sticky_tools_from_history(messages)
    assert evidence == ("manage_calendar",)
    assert sticky == {"manage_calendar"}

    # Disabled tools remain invisible even when recent history suggests them.
    merged = apply_sticky_tool_visibility(
        current={"ask_user"},
        messages=messages,
        disabled_tools={"manage_calendar"},
    )
    assert merged.tools == {"ask_user"}
    assert not merged.added

    # MCP follow-ups keep siblings from the same server, not unrelated servers.
    mcp_messages = [
        {"role": "user", "content": "remind me"},
        {
            "role": "assistant",
            "content": "done",
            "metadata": {
                "tool_events": [{
                    "tool": "mcp",
                    "desc": "Called mcp__assistant_reminders__assistant_create_reminder",
                    "output": "created",
                }]
            },
        },
        {"role": "user", "content": "cancel it"},
    ]
    sticky, _ = sticky_tools_from_history(mcp_messages, mcp_mgr=_FakeMcpManager())
    assert sticky == {
        "mcp__assistant_reminders__assistant_create_reminder",
        "mcp__assistant_reminders__assistant_list_reminders",
        "mcp__assistant_reminders__assistant_cancel_reminder",
    }

    # Sticky evidence expires after the configured conversational window.
    stale = [
        {"role": "user", "content": "calendar"},
        {"role": "assistant", "metadata": {"tool_events": [{"tool": "manage_calendar"}]}},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "normal answer"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "normal answer"},
        {"role": "user", "content": "new topic"},
    ]
    assert recent_authoritative_tool_names(stale, previous_user_turns=2) == ()

    # v0.2.3 shadow finalizer: Broker may rank/trim candidates but cannot
    # surface disabled or unrelated tools, and verified MCP history can recover
    # siblings from the same server.
    from assistant.fork.tool_broker_runtime import preview_final_tool_visibility

    preview = preview_final_tool_visibility(
        current={
            "ask_user",
            "manage_memory",
            "update_plan",
            "mcp__assistant_reminders__assistant_create_reminder",
        },
        messages=mcp_messages,
        mcp_mgr=_FakeMcpManager(),
        max_visible=8,
    )
    assert "mcp__assistant_reminders__assistant_create_reminder" in preview.tools
    assert "mcp__assistant_reminders__assistant_list_reminders" in preview.tools
    assert "mcp__assistant_reminders__assistant_cancel_reminder" in preview.tools
    assert "mcp__other__unrelated" not in preview.tools

    # v0.2.5 deliberate prompt budget: an arbitrarily wide legacy candidate
    # set is trimmed instead of blindly preserving every schema. Core recovery
    # remains visible and omitted tools are recoverable through discover_tools.
    wide_current = {
        "ask_user", "manage_memory", "update_plan",
        *(f"shadow_custom_{idx}" for idx in range(30)),
    }
    preview_wide = preview_final_tool_visibility(
        current=wide_current,
        messages=[],
    )
    assert {
        "ask_user", "discover_tools", "manage_memory", "update_plan",
    } <= preview_wide.tools
    assert len(preview_wide.tools) <= 24
    assert preview_wide.removed

    preview_disabled = preview_final_tool_visibility(
        current={"ask_user", "manage_calendar"},
        messages=messages,
        disabled_tools={"manage_calendar"},
        max_visible=8,
    )
    assert "manage_calendar" not in preview_disabled.tools

    preview_forced = preview_final_tool_visibility(
        current={"ask_user", "manage_memory", "update_plan"},
        messages=[],
        forced_names={"web_search"},
        # v0.2.4 adds discover_tools as a fifth core/recovery candidate.
        max_visible=5,
    )
    assert preview_forced.tools == {
        "ask_user", "discover_tools", "manage_memory", "update_plan", "web_search",
    }

    # Typed cold-start recovery: a controller-classified reminder domain must
    # recover the connected reminder MCP family even when semantic selection
    # omitted it entirely.
    from assistant.fork.tool_broker_runtime import broker_capabilities_for_domains

    reminder_caps = broker_capabilities_for_domains({"notes_calendar_tasks"})
    preview_cold_reminder = preview_final_tool_visibility(
        current={"ask_user", "manage_memory", "update_plan"},
        messages=[],
        mcp_mgr=_FakeMcpManager(),
        suggested_capabilities=reminder_caps,
        max_visible=16,
    )
    assert "mcp__assistant_reminders__assistant_create_reminder" in preview_cold_reminder.tools
    assert "mcp__assistant_reminders__assistant_list_reminders" in preview_cold_reminder.tools
    assert "mcp__assistant_reminders__assistant_cancel_reminder" in preview_cold_reminder.tools
    assert "mcp__other__unrelated" not in preview_cold_reminder.tools

    preview_cold_disabled = preview_final_tool_visibility(
        current={"ask_user", "manage_memory", "update_plan"},
        messages=[],
        mcp_mgr=_FakeMcpManager(),
        disabled_tools={"mcp__assistant_reminders__assistant_create_reminder"},
        suggested_capabilities=reminder_caps,
        max_visible=16,
    )
    assert "mcp__assistant_reminders__assistant_create_reminder" not in preview_cold_disabled.tools

    # Model adapters are allowed to narrow Broker output but can never add a
    # tool that the Broker did not expose.
    from assistant.fork.tool_broker_runtime import restrict_tool_visibility
    broker_visible = {"ask_user", "manage_memory", "manage_notes"}
    adapted = restrict_tool_visibility(
        broker_visible,
        {"ask_user", "manage_notes", "manage_calendar"},
    )
    assert adapted == {"ask_user", "manage_notes"}
    assert "manage_calendar" not in adapted


    # v0.2.5 ToolCatalog architecture: Odysseus owns tool facts; the fork
    # adapts them and owns only orchestration hints/candidate ranking.
    from assistant.fork.tool_catalog import (
        anchor_names_for_capabilities,
        audit_tool_catalog,
        build_tool_catalog,
    )
    from assistant.fork.tool_selector import build_candidate_plan

    _test_domain_members = {
        "web": {"web_search", "web_fetch"},
        "email": {
            "list_email_accounts", "list_emails", "read_email",
            "scan_email_unsubscribes", "unsubscribe_email", "send_email",
            "reply_to_email", "bulk_email", "archive_email", "delete_email",
            "mark_email_read", "resolve_contact", "manage_contact",
        },
        "contacts": {"resolve_contact", "manage_contact"},
        "cookbook": {
            "list_served_models", "list_cached_models",
            "tail_serve_output", "serve_model", "list_models",
        },
        "notes_calendar_tasks": {
            "manage_notes", "manage_calendar", "manage_tasks",
        },
        "ui": {"ui_control"},
        "sessions": {
            "list_sessions", "manage_session", "search_chats",
        },
        "settings": {
            "manage_settings", "manage_mcp", "manage_endpoints",
        },
        "integrations": {"api_call"},
    }

    catalog_audit = audit_tool_catalog(
        domain_members=_test_domain_members,
    )
    assert catalog_audit.ok, catalog_audit.errors

    catalog = build_tool_catalog(
        mcp_mgr=_FakeMcpManager(),
        domain_members=_test_domain_members,
    )
    assert "tail_serve_output" in catalog
    assert {
        "domain:email",
        "domain:contacts",
    } <= catalog["resolve_contact"].capabilities
    assert "domain:email" in catalog["mcp__email__list_emails"].capabilities
    assert "domain:browser" in catalog[
        "mcp__builtin_browser__browser_wait_for"
    ].capabilities
    assert "family:reminders" in catalog[
        "mcp__assistant_reminders__assistant_create_reminder"
    ].capabilities

    email_anchors = set(anchor_names_for_capabilities(
        {"domain:email"},
        catalog,
    ))
    assert {"list_emails", "read_email", "resolve_contact"} <= email_anchors
    assert "mcp__email__list_emails" not in email_anchors

    # Candidate providers do not gain authority: the Broker still rejects a
    # high-priority candidate outside the permitted controller set.
    from assistant.fork.tool_broker import ToolCandidate

    boundary_broker = ToolBroker(
        [
            ToolDescriptor(
                "discover_tools",
                frozenset({"tool:discover_tools"}),
                core_visible=True,
            ),
            ToolDescriptor(
                "allowed",
                frozenset({"tool:allowed"}),
            ),
            ToolDescriptor(
                "denied",
                frozenset({"tool:denied"}),
            ),
        ],
        max_visible=3,
    )
    boundary = boundary_broker.select_candidates(
        permitted_names={"discover_tools", "allowed"},
        candidates=[
            ToolCandidate("denied", 999, 999.0, "malicious-provider"),
            ToolCandidate("allowed", 10, 0.0, "test"),
            ToolCandidate("discover_tools", 100, 0.0, "core-visible"),
        ],
    )
    assert "denied" not in boundary.visible
    assert "allowed" in boundary.visible

    # Explicit topic switches suppress unrelated verified-history leases.
    previous_email = _tool_history("mcp__email__list_emails")
    cookbook_caps = broker_capabilities_for_domains({"cookbook"})
    cookbook_after_email = preview_final_tool_visibility(
        current={
            "ask_user", "manage_memory", "update_plan",
            "list_served_models",
        },
        messages=previous_email,
        mcp_mgr=_FakeMcpManager(),
        suggested_capabilities=cookbook_caps,
        domain_members=_test_domain_members,
        max_visible=10,
    )
    assert "list_served_models" in cookbook_after_email.tools
    assert "mcp__email__list_emails" not in cookbook_after_email.tools

    # A stray semantic browser candidate no longer expands the entire browser
    # MCP server. Typed notes/reminder intent outranks that cross-domain noise.
    reminder_caps = broker_capabilities_for_domains(
        {"notes_calendar_tasks"}
    )
    reminder_with_browser_noise = preview_final_tool_visibility(
        current={
            "ask_user", "manage_memory", "update_plan",
            "mcp__builtin_browser__browser_wait_for",
        },
        messages=[],
        mcp_mgr=_FakeMcpManager(),
        suggested_capabilities=reminder_caps,
        domain_members=_test_domain_members,
        max_visible=10,
    )
    assert "mcp__assistant_reminders__assistant_create_reminder" in (
        reminder_with_browser_noise.tools
    )
    assert "mcp__assistant_reminders__assistant_cancel_reminder" in (
        reminder_with_browser_noise.tools
    )
    assert "mcp__builtin_browser__browser_wait_for" not in (
        reminder_with_browser_noise.tools
    )

    # Verified reminder history leases only the tight reminder sibling group,
    # not every notes/calendar/task tool and not old email/Cookbook families.
    reminder_history = _tool_history(
        "mcp__assistant_reminders__assistant_create_reminder"
    )
    reminder_sticky, reminder_evidence = sticky_tools_from_history(
        reminder_history,
        mcp_mgr=_FakeMcpManager(),
        suggested_capabilities=reminder_caps,
        domain_members=_test_domain_members,
    )
    assert reminder_evidence == (
        "mcp__assistant_reminders__assistant_create_reminder",
    )
    assert reminder_sticky == {
        "mcp__assistant_reminders__assistant_create_reminder",
        "mcp__assistant_reminders__assistant_list_reminders",
        "mcp__assistant_reminders__assistant_cancel_reminder",
    }

    # The agent loop now delegates generic web/UI/settings/browser relevance to
    # ToolCatalog/Broker; only real document/file/workspace context keeps direct
    # selector mutation.
    agent_source = (ROOT / "src" / "agent_loop.py").read_text(encoding="utf-8")
    assert "v0.2.5 ToolCatalog selector boundary" in agent_source
    assert "_expand_browser_mcp_tools(_relevant_tools, mcp_mgr)" not in agent_source
    assert "_relevant_tools.update(WEB_TOOL_NAMES)" not in agent_source
    assert '_relevant_tools.add("ui_control")' not in agent_source
    assert "domain_members=_DOMAIN_TOOL_MAP" in agent_source

    # v0.2.4 discovery fallback is deterministic.
    from assistant.fork.tool_broker_runtime import _discovery_name_score

    assert _discovery_name_score(
        "mcp__assistant_reminders__assistant_create_reminder",
        "create a reminder",
    ) > 0
    assert _discovery_name_score(
        "proxmox_get_guest_status",
        "read proxmox guest status",
    ) >= 2
    assert _discovery_name_score("send_email", "proxmox guest status") == 0


    # v0.2.4 discovery precision: purpose-built tools outrank generic escape
    # hatches even if semantic retrieval originally placed the generic tools first.
    from assistant.fork.tool_broker_runtime import _rank_discovery_candidates

    ranked_models = _rank_discovery_candidates(
        "inspect locally served AI models and running model server status",
        [
            "app_api",
            "manage_memory",
            "bash",
            "list_cached_models",
            "stop_served_model",
            "list_served_models",
            "ask_user",
        ],
        descriptions={
            "list_served_models": "List running model servers and their status.",
            "list_cached_models": "List cached AI models.",
            "stop_served_model": "Stop a running model server.",
            "bash": "Run shell commands on the server.",
            "app_api": "Generic internal API loopback.",
            "ask_user": "Ask the user a question.",
            "manage_memory": "Manage persistent user memories.",
        },
    )
    assert ranked_models[0] == "list_served_models"
    assert ranked_models.index("list_cached_models") < ranked_models.index("bash")
    assert ranked_models.index("stop_served_model") < ranked_models.index("ask_user")

    ranked_reminders = _rank_discovery_candidates(
        "create cancel and list reminders",
        [
            "ask_user",
            "mcp__assistant_reminders__assistant_cancel_reminder",
            "manage_memory",
            "mcp__assistant_reminders__assistant_list_reminders",
            "mcp__assistant_reminders__assistant_create_reminder",
        ],
        descriptions={},
    )
    assert ranked_reminders[:3] == (
        "mcp__assistant_reminders__assistant_cancel_reminder",
        "mcp__assistant_reminders__assistant_list_reminders",
        "mcp__assistant_reminders__assistant_create_reminder",
    )

    ranked_email = _rank_discovery_candidates(
        "read search and list email",
        [
            "app_api",
            "read_email",
            "manage_mcp",
            "list_emails",
            "search_emails",
        ],
        descriptions={},
    )
    assert set(ranked_email[:3]) == {
        "read_email",
        "list_emails",
        "search_emails",
    }

    # v0.2.5 schema-emission authority contract. Once Broker/model routing
    # supplies a concrete set, no downstream admin/schema layer may add tools.
    agent_source = (ROOT / "src" / "agent_loop.py").read_text(encoding="utf-8")
    assert "if route_relevant_tools is not None:" in agent_source
    assert "schema_names |= _ADMIN_TOOLS" not in agent_source
    assert (
        'if schema.get("function", {}).get("name") in schema_names'
        in agent_source
    )

    # v0.2.6 selection quality: known cross-domain retrieval is pruned, not
    # merely demoted until a generous budget happens to admit it.
    quality_reminder = preview_final_tool_visibility(
        current={
            "ask_user", "manage_memory", "update_plan", "manage_notes",
            "list_served_models", "manage_mcp", "reply_to_email",
            "mcp__builtin_browser__browser_wait_for",
            "mcp__assistant_reminders__assistant_create_reminder",
            "mcp__assistant_reminders__assistant_list_reminders",
            "mcp__assistant_reminders__assistant_cancel_reminder",
        },
        messages=[],
        mcp_mgr=_FakeMcpManager(),
        suggested_capabilities=reminder_caps,
        domain_members=_test_domain_members,
    )
    assert {
        "list_served_models",
        "manage_mcp",
        "reply_to_email",
        "mcp__builtin_browser__browser_wait_for",
    }.isdisjoint(quality_reminder.tools)
    assert {
        "mcp__assistant_reminders__assistant_create_reminder",
        "mcp__assistant_reminders__assistant_list_reminders",
        "mcp__assistant_reminders__assistant_cancel_reminder",
    } <= quality_reminder.tools
    assert quality_reminder.budget is not None
    assert quality_reminder.budget <= 20
    assert quality_reminder.diagnostics
    assert quality_reminder.diagnostics["cross_domain_suppressed"] >= 4

    # The old v0.2.5 preview forgot to propagate its real budget.
    quality_wide = preview_final_tool_visibility(
        current=wide_current,
        messages=[],
    )
    assert quality_wide.budget is not None
    assert quality_wide.budget <= 20
    assert quality_wide.diagnostics["selected_total"] == len(
        quality_wide.tools
    )

    # Explicit latest-turn topics reset inherited continuation context, while
    # genuinely vague follow-ups retain the conversational interpretation.
    from assistant.fork.selector_quality import (
        action_is_cacheable_read,
        canonical_tool_call_signature,
        refine_intent_for_selection,
    )

    refined = refine_intent_for_selection(
        {
            "continuation": True,
            "low_signal": False,
            "domains": {"cookbook", "notes_calendar_tasks"},
            "retrieval_query": (
                "inspect failed model serve\n"
                "actually cancel that reminder"
            ),
        },
        {
            "continuation": False,
            "low_signal": False,
            "domains": {"cookbook"},
            "retrieval_query": "inspect failed model serve",
        },
        "inspect failed model serve",
    )
    assert refined["continuation"] is False
    assert refined["domains"] == {"cookbook"}
    assert refined["retrieval_query"] == "inspect failed model serve"

    vague = refine_intent_for_selection(
        {
            "continuation": True,
            "low_signal": True,
            "domains": {"notes_calendar_tasks"},
            "retrieval_query": "cancel it\nremind me tomorrow",
        },
        {
            "continuation": True,
            "low_signal": True,
            "domains": set(),
            "retrieval_query": "cancel it",
        },
        "cancel it",
    )
    assert vague["continuation"] is True
    assert vague["domains"] == {"notes_calendar_tasks"}

    # Duplicate-call suppression only applies to deterministic/cacheable reads.
    assert canonical_tool_call_signature(
        "list_emails",
        '{"max_results":3,"unread_only":false}',
    ) == canonical_tool_call_signature(
        "list_emails",
        '{"unread_only": false, "max_results": 3}',
    )
    assert action_is_cacheable_read(
        "list_emails",
        '{"max_results":3}',
    )
    assert action_is_cacheable_read(
        "manage_calendar",
        '{"action":"list_events"}',
    )
    assert not action_is_cacheable_read(
        "manage_calendar",
        '{"action":"create_event"}',
    )
    assert not action_is_cacheable_read(
        "delete_email",
        '{"uid":"1"}',
    )

    # Explicit runtime context survives typed-domain pruning.
    explicit_file_context = preview_final_tool_visibility(
        current={
            "ask_user", "manage_memory", "update_plan",
            "grep", "list_served_models",
        },
        messages=[],
        forced_names={"grep"},
        suggested_capabilities=reminder_caps,
        domain_members=_test_domain_members,
    )
    assert "grep" in explicit_file_context.tools
    assert "list_served_models" not in explicit_file_context.tools

    agent_source = (ROOT / "src" / "agent_loop.py").read_text(
        encoding="utf-8"
    )
    assert "[tool-quality] intent reset" in agent_source
    assert "suppressed duplicate successful read batch" in agent_source
    assert "diagnostics=%s" in agent_source
    assert (
        "_broker_explicit_context_tools.update(_uploaded_context_tools)"
        in agent_source
    )
    assert '_broker_explicit_context_tools.add("manage_skills")' in agent_source

    chat_source = (ROOT / "routes" / "chat_routes.py").read_text(
        encoding="utf-8"
    )
    assert "if _search_enabled and _explicit_web_intent:" in chat_source
    assert '_tool_intent.category == "web"' in chat_source
    assert (
        "latest|current|today|news|weather|forecast|rate"
        not in chat_source
    )

    # list_models now has Cookbook provenance, so an explicit web turn drops
    # it as known cross-domain noise instead of keeping it unclassified.
    web_quality = preview_final_tool_visibility(
        current={
            "ask_user", "manage_memory", "update_plan",
            "web_search", "web_fetch", "list_models",
        },
        messages=[],
        suggested_capabilities=broker_capabilities_for_domains({"web"}),
        domain_members=_test_domain_members,
    )
    assert {"web_search", "web_fetch"} <= web_quality.tools
    assert "list_models" not in web_quality.tools
    assert (
        "if _search_enabled:\n"
        "                        _forced_tools = set(WEB_TOOL_NAMES)"
        not in chat_source
    )

    # Agent identity is provenance only; child delegation is explicit.
    ctx = ExecutionContext(agent_id="main", source="telegram", user_id="user-1", session_id="s1")
    child = ctx.child("homelab")
    assert child.agent_id == "homelab"
    assert child.delegated_by == "main"
    assert child.source == "telegram"
    assert child.run_id != ctx.run_id

    print("✓ assistant fork contracts: PASS")
    print("  - visibility cannot bypass controller permissions")
    print("  - typed sticky capability state works without keyword matching")
    print("  - persisted tool events drive runtime sticky visibility")
    print("  - disabled tools remain hidden")
    print("  - MCP sibling tools survive natural follow-ups")
    print("  - sticky evidence expires after a short conversation window")
    print("  - discover-tools recovery path works")
    print("  - ToolCatalog adapts Odysseus metadata instead of duplicating it")
    print("  - candidate providers are signals; ToolBroker remains authority")
    print("  - verified capability leases do not bleed across topic switches")
    print("  - visibility has a bounded prompt budget + discovery escape hatch")
    print("  - schema emission cannot expand Broker/model-route visibility")
    print("  - typed turns hard-prune known cross-domain retrieval noise")
    print("  - explicit latest topics reset false continuation inheritance")
    print("  - repeated successful reads converge without backend re-execution")
    print("  - selector telemetry reports real budgets and suppression counts")
    print("  - execution context is sub-agent-ready provenance")


if __name__ == "__main__":
    main()

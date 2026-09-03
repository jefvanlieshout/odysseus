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

    # Shadow-mode safety regression: until we have real ranking scores,
    # an arbitrarily wide legacy candidate set must be preserved intact rather
    # than alphabetically truncated at 24 tools.
    wide_current = {
        "ask_user", "manage_memory", "update_plan",
        *(f"shadow_custom_{idx}" for idx in range(30)),
    }
    preview_wide = preview_final_tool_visibility(
        current=wide_current,
        messages=[],
    )
    assert preview_wide.tools == wide_current
    assert not preview_wide.removed

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
        max_visible=4,
    )
    assert preview_forced.tools == {
        "ask_user", "manage_memory", "update_plan", "web_search",
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
    print("  - execution context is sub-agent-ready provenance")


if __name__ == "__main__":
    main()

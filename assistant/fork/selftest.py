#!/usr/bin/env python3
"""No-dependency contract tests for the fork foundation."""
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

    # Sticky context survives a vague follow-up without matching a magic word.
    state.activate("calendar.read", "calendar.write")
    sel = broker.select(
        permitted_names={"discover_tools", "calendar_read", "calendar_write", "proxmox_read"},
        state=state,
    )
    assert "calendar_read" in sel.visible and "calendar_write" in sel.visible

    # Discovery can recover a permitted capability omitted by the automatic selector.
    found = broker.discover(
        permitted_names={"discover_tools", "proxmox_read"},
        capabilities={"proxmox.read"},
    )
    assert found == ("proxmox_read",)

    # Agent identity is provenance only; child delegation is explicit.
    ctx = ExecutionContext(agent_id="main", source="telegram", user_id="user-1", session_id="s1")
    child = ctx.child("homelab")
    assert child.agent_id == "homelab"
    assert child.delegated_by == "main"
    assert child.source == "telegram"
    assert child.run_id != ctx.run_id

    print("✓ assistant fork contracts: PASS")
    print("  - visibility cannot bypass controller permissions")
    print("  - sticky capability state works without keyword matching")
    print("  - discover-tools recovery path works")
    print("  - execution context is sub-agent-ready provenance")


if __name__ == "__main__":
    main()

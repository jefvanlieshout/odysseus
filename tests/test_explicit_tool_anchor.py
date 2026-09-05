from assistant.fork.tool_catalog import ToolRecord
from assistant.fork.tool_selector import (
    build_candidate_plan,
    explicitly_named_candidate_tools,
)


def _record(name: str, domain: str) -> ToolRecord:
    return ToolRecord(
        name=name,
        source="test",
        sources=frozenset({"test"}),
        capabilities=frozenset({f"domain:{domain}"}),
    )


def test_explicit_proxmox_name_survives_tasks_domain():
    candidates = {"proxmox", "manage_tasks", "list_served_models"}
    named = explicitly_named_candidate_tools(
        (
            "Gwen, check my Proxmox server. Give me running guests, "
            "storage usage, and recent failed tasks."
        ),
        candidates,
    )
    assert named == {"proxmox"}

    records = {
        "proxmox": _record("proxmox", "integrations"),
        "manage_tasks": _record("manage_tasks", "notes_calendar_tasks"),
        "list_served_models": _record("list_served_models", "cookbook"),
    }
    plan = build_candidate_plan(
        records=records,
        current_names=candidates,
        forced_names=named,
        core_names=(),
        suggested_capabilities={"domain:notes_calendar_tasks"},
        evidence_names=(),
    )

    reasons = {
        (candidate.name, candidate.reason)
        for candidate in plan.candidates
    }
    assert ("proxmox", "explicit-context") in reasons
    assert "proxmox" not in plan.suppressed_cross_domain
    assert "list_served_models" in plan.suppressed_cross_domain


def test_generic_tasks_does_not_fake_explicit_manage_tasks_name():
    assert explicitly_named_candidate_tools(
        "Show me my tasks for today.",
        {"manage_tasks", "proxmox"},
    ) == set()


def test_separator_normalization_for_explicit_tool_name():
    assert explicitly_named_candidate_tools(
        "Use api call for this integration.",
        {"api_call", "manage_tasks"},
    ) == {"api_call"}

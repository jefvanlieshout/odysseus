from src.agent_loop import _classify_agent_request
from src.tools.proxmox import _task_status_bucket


def _classify(text: str):
    return _classify_agent_request(
        [{"role": "user", "content": text}],
        text,
    )


def test_scoped_proxmox_tasks_do_not_seed_personal_task_domain():
    result = _classify(
        "Atlas, check my Proxmox server. Give me a health overview of the node, "
        "running and stopped guests, storage usage, and any recent failed tasks."
    )
    assert "notes_calendar_tasks" not in result["domains"]


def test_personal_tasks_still_seed_personal_task_domain():
    result = _classify("Show me my tasks for today.")
    assert "notes_calendar_tasks" in result["domains"]


def test_explicit_personal_task_about_proxmox_still_counts():
    result = _classify("Create a task to check my Proxmox server tomorrow.")
    assert "notes_calendar_tasks" in result["domains"]


def test_proxmox_task_status_buckets_keep_warnings_separate():
    assert _task_status_bucket({"status": "OK"}) == "ok"
    assert _task_status_bucket({"status": "WARNINGS: 1"}) == "warning"
    assert _task_status_bucket({"status": "ERROR: startup failed"}) == "failed"
    assert _task_status_bucket({}) == "unknown"

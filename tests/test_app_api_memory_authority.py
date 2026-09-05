"""Regression contract: app_api must not bypass memory_backend authority."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_app_api_blocks_native_memory_prefix():
    text = (ROOT / "src/tools/system.py").read_text(encoding="utf-8")
    assert '"/api/memory"' in text
    assert "APP_API_NATIVE_MEMORY_AUTHORITY_V1" in text
    assert 'path.startswith(("/api/memory", "/api/codex/memory"))' in text
    assert "Use manage_memory" in text


def test_agent_descriptions_do_not_advertise_native_memory_via_app_api():
    agent = (ROOT / "src/agent_loop.py").read_text(encoding="utf-8")
    index = (ROOT / "src/tool_index.py").read_text(encoding="utf-8")
    schema = (ROOT / "src/tool_schemas.py").read_text(encoding="utf-8")
    assert "NEVER use `app_api` for `/api/memory*`" in agent
    assert "Native /api/memory* and /api/codex/memory* routes are deliberately blocked" in index
    assert "Native /api/memory* and /api/codex/memory* routes are blocked" in schema

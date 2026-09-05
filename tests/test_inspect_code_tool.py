from __future__ import annotations

import asyncio
import json

import src.tools.code_inspection as code_inspection


def _call(payload):
    return asyncio.run(code_inspection.do_inspect_code(json.dumps(payload), owner="tester"))


def test_read_and_search_are_repo_confined(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "demo.py").write_text("alpha\nneedle here\nomega\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=never\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "brain.db").write_text("not really a db", encoding="utf-8")
    monkeypatch.setenv("ODYSSEUS_SELF_CODE_ROOT", str(tmp_path))

    read = _call({"action": "read", "path": "src/demo.py"})
    assert read["exit_code"] == 0
    assert "needle here" in read["content"]

    search = _call({"action": "search", "query": "needle"})
    assert search["exit_code"] == 0
    assert search["matches"][0]["path"] == "src/demo.py"

    blocked = _call({"action": "read", "path": ".env"})
    assert blocked["exit_code"] == 1

    escaped = _call({"action": "read", "path": "../outside.txt"})
    assert escaped["exit_code"] == 1


def test_tree_hides_private_runtime_paths(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "demo.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "x.log").write_text("secret-ish\n", encoding="utf-8")
    monkeypatch.setenv("ODYSSEUS_SELF_CODE_ROOT", str(tmp_path))

    result = _call({"action": "tree", "depth": 2})
    joined = "\n".join(result["entries"])
    assert "src/demo.py" in joined
    assert ".git" not in joined
    assert "logs" not in joined


def test_native_inspect_code_call_converts():
    from src.agent_tools import function_call_to_tool_block
    block = function_call_to_tool_block("inspect_code", '{"action":"status"}')
    assert block is not None
    assert block.tool_type == "inspect_code"
    assert json.loads(block.content) == {"action": "status"}


def test_self_code_intent_is_narrow():
    from src.agent_loop import _looks_like_self_code_request
    assert _looks_like_self_code_request("Gwen, inspect your own source code")
    assert _looks_like_self_code_request("run inspect code for me")
    assert _looks_like_self_code_request("what commit is your own code on?")
    assert _looks_like_self_code_request("show me Odysseus implementation")
    assert not _looks_like_self_code_request("inspect my Proxmox config")
    assert not _looks_like_self_code_request("read this Python project")

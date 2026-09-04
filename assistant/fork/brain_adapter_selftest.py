#!/usr/bin/env python3
"""Contract tests for the Odysseus -> Brain shadow adapter."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from assistant.fork.brain_adapter import (
    BrainAdapterConfig,
    BrainMemoryAdapter,
    ShadowCaptureResult,
    shadow_persisted_message,
)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        self.requests = []
        self.reply_status = 200
        self.reply_payload = {"ok": True}
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length)
        body = json.loads(raw.decode("utf-8"))
        self.server.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "body": body,
            }
        )
        payload = json.dumps(self.server.reply_payload).encode("utf-8")
        self.send_response(self.server.reply_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _start_server():
    server = _Server(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _config(server, mode="shadow"):
    host, port = server.server_address
    return BrainAdapterConfig(
        mode=mode,
        base_url=f"http://{host}:{port}",
        api_key="k" * 40,
        timeout_seconds=1.0,
        default_owner="local-shadow",
    )


def main() -> None:
    server, thread = _start_server()
    try:
        adapter = BrainMemoryAdapter(_config(server))

        # User messages are observations: Brain creates transcript + evidence +
        # episode + semantic job atomically.
        result = adapter.capture_persisted_message(
            owner=None,
            session_id="session-1",
            message_id="db-user-1",
            role="user",
            content="I prefer root-cause Linux fixes.",
            occurred_at="2026-09-04T12:00:00Z",
            metadata={"source": "chat"},
        )
        assert result.delivered
        request = server.requests[-1]
        assert request["path"] == "/v1/capture/observation"
        assert request["authorization"] == "Bearer " + ("k" * 40)
        assert "?" not in request["path"]
        assert request["body"]["owner_id"] == "local-shadow"
        assert request["body"]["external_source_ref"] == "db-user-1"
        assert request["body"]["session_id"] == "session-1"
        assert request["body"]["source_kind"] == "USER_MESSAGE"
        assert request["body"]["raw_text"] == "I prefer root-cause Linux fixes."

        # Assistant/tool metadata is preserved but is transcript-only.
        tool_events = [{"tool": "manage_calendar", "output": "created event"}]
        result = adapter.capture_persisted_message(
            owner="jef",
            session_id="session-1",
            message_id="db-assistant-1",
            role="assistant",
            content="Done.",
            occurred_at="2026-09-04T12:00:01Z",
            metadata={"tool_events": tool_events},
        )
        assert result.delivered
        request = server.requests[-1]
        assert request["path"] == "/v1/capture/message"
        assert request["body"]["owner_id"] == "jef"
        assert request["body"]["external_session_ref"] == "session-1"
        assert request["body"]["external_message_ref"] == "db-assistant-1"
        assert request["body"]["metadata"]["tool_events"] == tool_events

        # System/tool roles use the same transcript-only boundary.
        for role in ("system", "tool"):
            result = adapter.capture_persisted_message(
                owner="jef",
                session_id="session-1",
                message_id=f"db-{role}-1",
                role=role,
                content=f"{role} content",
            )
            assert result.delivered
            assert server.requests[-1]["path"] == "/v1/capture/message"
            assert server.requests[-1]["body"]["role"] == role

        # Disabled mode performs no network I/O.
        before = len(server.requests)
        off = BrainMemoryAdapter(_config(server, mode="off"))
        result = off.capture_persisted_message(
            owner="jef",
            session_id="s",
            message_id="m",
            role="user",
            content="hello",
        )
        assert not result.attempted and not result.delivered
        assert len(server.requests) == before

        # Misconfiguration and unsupported modes fail closed before I/O.
        bad = BrainMemoryAdapter(
            BrainAdapterConfig(
                mode="shadow",
                base_url=f"http://127.0.0.1:{server.server_address[1]}",
                api_key="short",
            )
        )
        result = bad.capture_persisted_message(
            owner="jef", session_id="s", message_id="m2", role="user", content="hello"
        )
        assert not result.attempted and "32" in (result.error or "")
        assert len(server.requests) == before

        unsupported = BrainMemoryAdapter(
            BrainAdapterConfig(
                mode="primary",
                base_url=f"http://127.0.0.1:{server.server_address[1]}",
                api_key="x" * 40,
            )
        )
        result = unsupported.capture_persisted_message(
            owner="jef", session_id="s", message_id="m3", role="user", content="hello"
        )
        assert not result.attempted and "unsupported" in (result.error or "")
        assert len(server.requests) == before

        # Empty/unknown-role messages are never fabricated into Brain content.
        for role, content in (("assistant", ""), ("developer", "hello")):
            result = adapter.capture_persisted_message(
                owner="jef",
                session_id="s",
                message_id=f"skip-{role}",
                role=role,
                content=content,
            )
            assert not result.attempted
        assert len(server.requests) == before

        # HTTP failures are telemetry, never exceptions; HTTPError is closed.
        server.reply_status = 409
        server.reply_payload = {"ok": False, "error": "idempotency conflict"}
        result = adapter.capture_persisted_message(
            owner="jef",
            session_id="session-1",
            message_id="db-user-conflict",
            role="user",
            content="changed replay",
        )
        assert result.attempted and not result.delivered
        assert result.status_code == 409
        assert "idempotency" in (result.error or "")

        # Environment helper stays off by default.
        with patch.dict(os.environ, {}, clear=True):
            result = shadow_persisted_message(
                owner="jef",
                session_id="s",
                message_id="off-default",
                role="user",
                content="hello",
            )
            assert not result.attempted

        # The native hook must live after db.close(), so Brain I/O never holds
        # the authoritative Odysseus database session open.
        source = (ROOT / "core" / "session_manager.py").read_text(encoding="utf-8")
        start = source.index("    def _persist_message(self, session_id: str, message: ChatMessage):")
        end = source.index("    def truncate_messages(", start)
        body = source[start:end]
        assert "brain_shadow_payload = None" in body
        assert "shadow_persisted_message" in body
        assert "message.metadata['_db_id'] = msg_id" in body
        assert body.rindex("db.close()") < body.index("shadow_persisted_message")
        assert body.index("db.commit()") < body.index("shadow_persisted_message")

        print("Brain shadow adapter contracts: PASS")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


if __name__ == "__main__":
    main()

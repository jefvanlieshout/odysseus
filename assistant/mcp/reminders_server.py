#!/usr/bin/env python3
"""MCP bridge exposing assistant-events reminders to Odysseus/Qwen.

This process intentionally owns no scheduler state. It is a thin, structured
adapter: MCP tool call -> authenticated assistant-events HTTP request.
Python in assistant-events remains authoritative for persistence and delivery.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


SERVER_NAME = "assistant-reminders"
EVENTS_URL = os.getenv("ASSISTANT_EVENTS_URL", "http://assistant-events:8780").rstrip("/")
EVENTS_API_KEY = os.getenv("ASSISTANT_EVENTS_API_KEY", "").strip()
AGENT_ID = os.getenv("ASSISTANT_AGENT_ID", "main").strip() or "main"
HTTP_TIMEOUT_SECONDS = max(1.0, float(os.getenv("ASSISTANT_EVENTS_TIMEOUT_SECONDS", "10")))

server = Server(SERVER_NAME)


def _json_text(data: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, sort_keys=True))]


def _request_json(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | list[Any]:
    if not EVENTS_API_KEY:
        raise RuntimeError("ASSISTANT_EVENTS_API_KEY is not configured")

    body = None
    headers = {
        "Authorization": f"Bearer {EVENTS_API_KEY}",
        "Accept": "application/json",
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(f"{EVENTS_URL}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw)
        except json.JSONDecodeError:
            detail = raw or exc.reason
        raise RuntimeError(f"assistant-events HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"assistant-events is unreachable at {EVENTS_URL}: {exc.reason}") from exc


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="assistant_create_reminder",
            description=(
                "Preferred reminder tool for Jef. Use this when Jef asks to be reminded later. "
                "The reminder is persisted by the assistant event service and delivered through "
                "the configured notification channel (currently Telegram). For relative requests "
                "such as 'in 10 minutes', set delay_seconds. For a specific clock/date, set due_at "
                "to a timezone-aware ISO-8601 datetime. Do not guess an ambiguous time; ask Jef. "
                "For an existing verified system condition, condition_fingerprint + only_if_active "
                "can make the reminder fire only if that condition is still active."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "minLength": 1,
                        "description": "What Jef should be reminded about.",
                    },
                    "title": {
                        "type": "string",
                        "default": "Reminder",
                        "description": "Short notification title.",
                    },
                    "delay_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 31536000,
                        "description": "Relative delay in seconds. Use exactly one of delay_seconds or due_at.",
                    },
                    "due_at": {
                        "type": "string",
                        "description": "Timezone-aware ISO-8601 datetime. Use exactly one of due_at or delay_seconds.",
                    },
                    "condition_fingerprint": {
                        "type": "string",
                        "description": "Optional verified event-condition fingerprint for a conditional reminder.",
                    },
                    "only_if_active": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, skip the reminder if condition_fingerprint is no longer active.",
                    },
                },
                "required": ["message"],
            },
        ),
        Tool(
            name="assistant_list_reminders",
            description=(
                "List recent reminders created through the assistant notification system, including "
                "scheduled, delivered, skipped, failed, and cancelled reminders."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 20,
                    }
                },
            },
        ),
        Tool(
            name="assistant_cancel_reminder",
            description=(
                "Cancel one still-scheduled assistant reminder by reminder_id. Use only when Jef "
                "asks to cancel/stop a reminder or clearly identifies a scheduled reminder to remove."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "reminder_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Reminder UUID returned by assistant_create_reminder/list_reminders.",
                    }
                },
                "required": ["reminder_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    arguments = dict(arguments or {})
    try:
        if name == "assistant_create_reminder":
            message = str(arguments.get("message", "")).strip()
            if not message:
                return _json_text({"ok": False, "error": "message is required"})

            delay_seconds = arguments.get("delay_seconds")
            due_at = arguments.get("due_at")
            if (delay_seconds is None) == (due_at is None):
                return _json_text({
                    "ok": False,
                    "error": "provide exactly one of delay_seconds or due_at",
                })

            payload: dict[str, Any] = {
                "title": str(arguments.get("title") or "Reminder").strip() or "Reminder",
                "message": message,
                "actor_id": "user",
                "agent_id": AGENT_ID,
                "only_if_active": bool(arguments.get("only_if_active", False)),
            }
            if delay_seconds is not None:
                payload["delay_seconds"] = int(delay_seconds)
            else:
                payload["due_at"] = str(due_at).strip()

            condition = str(arguments.get("condition_fingerprint") or "").strip()
            if condition:
                payload["condition_fingerprint"] = condition

            result = _request_json("POST", "/reminders", payload)
            return _json_text({"ok": True, "action": "created", "reminder": result})

        if name == "assistant_list_reminders":
            limit = int(arguments.get("limit", 20))
            limit = max(1, min(limit, 100))
            result = _request_json("GET", f"/reminders?limit={limit}")
            return _json_text({"ok": True, "reminders": result})

        if name == "assistant_cancel_reminder":
            reminder_id = str(arguments.get("reminder_id", "")).strip()
            if not reminder_id:
                return _json_text({"ok": False, "error": "reminder_id is required"})
            result = _request_json("POST", f"/reminders/{reminder_id}/cancel")
            return _json_text({"ok": True, "action": "cancelled", "result": result})

        return _json_text({"ok": False, "error": f"unknown tool: {name}"})
    except (TypeError, ValueError) as exc:
        return _json_text({"ok": False, "error": f"invalid arguments: {exc}"})
    except Exception as exc:
        return _json_text({"ok": False, "error": str(exc)})


async def run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())

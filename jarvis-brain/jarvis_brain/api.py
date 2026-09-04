from __future__ import annotations

import hmac
import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .service import BrainError, BrainMemoryService, IdempotencyConflict, OwnershipError
from .types import SourceKind

MAX_REQUEST_BYTES = 1024 * 1024


class BrainAPIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, service: BrainMemoryService, api_key: str):
        api_key = str(api_key or "")
        if len(api_key) < 32:
            raise ValueError("Brain API key must be at least 32 characters")
        self.service = service
        self.api_key = api_key
        super().__init__(server_address, BrainAPIHandler)


class BrainAPIHandler(BaseHTTPRequestHandler):
    server: BrainAPIServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        return

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else ""
        return bool(supplied) and hmac.compare_digest(supplied, self.server.api_key)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
        return False

    def _read_json(self) -> dict:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("request body too large")
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type.casefold():
            raise ValueError("Content-Type must be application/json")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError("invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            self._json(HTTPStatus.OK, self.server.service.health())
            return
        if not self._require_auth():
            return
        if parsed.path == "/v1/status":
            owner = (parse_qs(parsed.query).get("owner_id") or [None])[0]
            self._json(HTTPStatus.OK, {
                "ok": True,
                "counts": self.server.service.store.counts(owner),
            })
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        try:
            body = self._read_json()
            path = urlsplit(self.path).path
            if path == "/v1/capture/message":
                result = self.server.service.capture_message(
                    owner_id=body.get("owner_id"),
                    external_session_ref=body.get("external_session_ref"),
                    external_message_ref=body.get("external_message_ref"),
                    role=body.get("role"),
                    content=body.get("content"),
                    occurred_at=body.get("occurred_at"),
                    metadata=body.get("metadata"),
                )
                self._json(HTTPStatus.OK, {"ok": True, **result})
                return
            if path == "/v1/capture/observation":
                result = self.server.service.capture_observation(
                    owner_id=body.get("owner_id"),
                    raw_text=body.get("raw_text"),
                    external_source_ref=body.get("external_source_ref"),
                    session_id=body.get("session_id"),
                    source_kind=body.get("source_kind", SourceKind.USER_MESSAGE.value),
                    occurred_at=body.get("occurred_at"),
                    metadata=body.get("metadata"),
                )
                self._json(HTTPStatus.OK, {"ok": True, **result})
                return
            if path == "/v1/search":
                hits = self.server.service.search(
                    owner_id=body.get("owner_id"),
                    query=body.get("query"),
                    limit=body.get("limit", 10),
                    include_episodes=body.get("include_episodes", True),
                )
                self._json(HTTPStatus.OK, {
                    "ok": True,
                    "hits": [
                        {
                            "kind": h.kind, "uuid": h.uuid, "text": h.text,
                            "score": h.score, "metadata": h.metadata,
                        }
                        for h in hits
                    ],
                })
                return
            if path == "/v1/rebuild-index":
                result = self.server.service.rebuild_vector_index(owner_id=body.get("owner_id"))
                self._json(HTTPStatus.OK, {"ok": True, **result})
                return
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
        except OwnershipError:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
        except IdempotencyConflict as exc:
            self._json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
        except (ValueError, TypeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except BrainError as exc:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(exc)})
        except Exception:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "internal error"})


def start_server_in_thread(service: BrainMemoryService, api_key: str, host: str = "127.0.0.1", port: int = 0):
    server = BrainAPIServer((host, port), service, api_key)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread

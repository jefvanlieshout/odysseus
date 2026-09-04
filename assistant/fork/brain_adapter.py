"""Native Odysseus -> Jarvis Brain shadow adapter.

Shadow mode is deliberately one-way and non-authoritative:

1. Odysseus persists its normal chat row first.
2. This adapter mirrors that exact persisted message to Brain.
3. Any Brain failure is reported as telemetry only and MUST NOT undo, alter,
   or block the already-committed Odysseus state.

No memory reads or Brain-derived context are exposed here.  That boundary is
intentional for assistant-v0.3.0 shadow validation.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_ALLOWED_ROLES = frozenset({"user", "assistant", "system", "tool"})
_ALLOWED_MODES = frozenset({"off", "shadow"})


@dataclass(frozen=True)
class BrainAdapterConfig:
    mode: str = "off"
    base_url: str = ""
    api_key: str = ""
    timeout_seconds: float = 0.5
    default_owner: str = "local"

    @classmethod
    def from_env(cls) -> "BrainAdapterConfig":
        raw_timeout = os.environ.get("ASSISTANT_BRAIN_TIMEOUT_SECONDS", "0.5")
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            timeout = 0.5
        timeout = min(5.0, max(0.05, timeout))
        return cls(
            mode=str(os.environ.get("ASSISTANT_BRAIN_MODE", "off") or "off").strip().casefold(),
            base_url=str(os.environ.get("JARVIS_BRAIN_URL", "") or "").strip().rstrip("/"),
            api_key=str(os.environ.get("JARVIS_BRAIN_API_KEY", "") or ""),
            timeout_seconds=timeout,
            default_owner=str(os.environ.get("ASSISTANT_BRAIN_DEFAULT_OWNER", "local") or "local").strip(),
        )

    def validation_error(self) -> str | None:
        if self.mode not in _ALLOWED_MODES:
            return f"unsupported Brain mode: {self.mode!r}"
        if self.mode == "off":
            return None
        if not self.default_owner:
            return "ASSISTANT_BRAIN_DEFAULT_OWNER must not be empty"
        if not self.base_url:
            return "JARVIS_BRAIN_URL is required in shadow mode"
        parts = urlsplit(self.base_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            return "JARVIS_BRAIN_URL must be an absolute http(s) URL"
        if parts.username or parts.password or parts.query or parts.fragment:
            return "JARVIS_BRAIN_URL must not contain credentials, query, or fragment"
        if len(self.api_key) < 32:
            return "JARVIS_BRAIN_API_KEY must contain at least 32 characters"
        return None


@dataclass(frozen=True)
class ShadowCaptureResult:
    attempted: bool
    delivered: bool
    endpoint: str | None = None
    status_code: int | None = None
    error: str | None = None


class BrainMemoryAdapter:
    """Small stdlib-only client for the Brain sidecar's shadow endpoints."""

    def __init__(self, config: BrainAdapterConfig):
        self.config = config

    @staticmethod
    def _owner(owner: str | None, default_owner: str) -> str:
        value = str(owner or "").strip()
        return value or default_owner

    def _post_json(self, endpoint: str, payload: Mapping[str, Any]) -> ShadowCaptureResult:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        request = Request(
            self.config.base_url + endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Cache-Control": "no-store",
                "User-Agent": "Odysseus-BrainShadow/0.3.0",
                "Connection": "close",
            },
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read(1024 * 1024)
                status = int(getattr(response, "status", 200))
            if status < 200 or status >= 300:
                return ShadowCaptureResult(True, False, endpoint, status, f"HTTP {status}")
            try:
                decoded = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                return ShadowCaptureResult(True, False, endpoint, status, "Brain returned invalid JSON")
            if isinstance(decoded, dict) and decoded.get("ok") is False:
                return ShadowCaptureResult(
                    True, False, endpoint, status, str(decoded.get("error") or "Brain rejected request")
                )
            return ShadowCaptureResult(True, True, endpoint, status, None)
        except HTTPError as exc:
            try:
                raw = exc.read(4096)
                try:
                    decoded = json.loads(raw.decode("utf-8")) if raw else {}
                    detail = decoded.get("error") if isinstance(decoded, dict) else None
                except Exception:
                    detail = None
                return ShadowCaptureResult(
                    True,
                    False,
                    endpoint,
                    int(exc.code),
                    str(detail or f"HTTP {exc.code}"),
                )
            finally:
                exc.close()
        except URLError as exc:
            return ShadowCaptureResult(True, False, endpoint, None, f"connection error: {exc.reason}")
        except TimeoutError:
            return ShadowCaptureResult(True, False, endpoint, None, "timeout")
        except Exception as exc:
            return ShadowCaptureResult(
                True, False, endpoint, None, f"{type(exc).__name__}: {exc}"
            )

    def capture_persisted_message(
        self,
        *,
        owner: str | None,
        session_id: str,
        message_id: str,
        role: str,
        content: str,
        occurred_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ShadowCaptureResult:
        """Mirror an already-committed Odysseus message to Brain.

        A user message enters the Brain as an observation because the Brain
        service atomically creates its transcript row, evidence, episode, and
        pending semantic job. Other roles are transcript-only and therefore
        cannot accidentally become claims about the user.
        """
        if self.config.mode == "off":
            return ShadowCaptureResult(False, False, None, None, "disabled")

        error = self.config.validation_error()
        if error:
            return ShadowCaptureResult(False, False, None, None, error)

        role = str(role or "").strip().casefold()
        if role not in _ALLOWED_ROLES:
            return ShadowCaptureResult(False, False, None, None, f"unsupported role: {role!r}")

        session_id = str(session_id or "").strip()
        message_id = str(message_id or "").strip()
        if not session_id or not message_id:
            return ShadowCaptureResult(False, False, None, None, "missing persisted message identity")

        # Brain v0.3.0 currently requires non-empty text. Do not fabricate
        # content in shadow mode; retain native Odysseus as the source of truth
        # and surface this as a skipped mirror for telemetry.
        content = str(content if content is not None else "")
        if not content.strip():
            return ShadowCaptureResult(False, False, None, None, "empty persisted content")

        owner_id = self._owner(owner, self.config.default_owner)
        clean_metadata = dict(metadata or {})

        if role == "user":
            return self._post_json(
                "/v1/capture/observation",
                {
                    "owner_id": owner_id,
                    "raw_text": content,
                    "external_source_ref": message_id,
                    "session_id": session_id,
                    "source_kind": "USER_MESSAGE",
                    "occurred_at": occurred_at,
                    "metadata": clean_metadata,
                },
            )

        return self._post_json(
            "/v1/capture/message",
            {
                "owner_id": owner_id,
                "external_session_ref": session_id,
                "external_message_ref": message_id,
                "role": role,
                "content": content,
                "occurred_at": occurred_at,
                "metadata": clean_metadata,
            },
        )


def shadow_persisted_message(**kwargs) -> ShadowCaptureResult:
    """Environment-configured one-shot shadow mirror.

    This helper intentionally creates no global authoritative state.  It is
    cheap while disabled and makes environment changes visible on the next
    persisted message during shadow experiments.
    """
    adapter = BrainMemoryAdapter(BrainAdapterConfig.from_env())
    result = adapter.capture_persisted_message(**kwargs)

    if result.attempted and result.delivered:
        logger.debug(
            "[brain-shadow] delivered message_id=%s endpoint=%s",
            kwargs.get("message_id"),
            result.endpoint,
        )
    elif result.attempted:
        logger.warning(
            "[brain-shadow] delivery failed message_id=%s endpoint=%s status=%s error=%s",
            kwargs.get("message_id"),
            result.endpoint,
            result.status_code,
            result.error,
        )
    elif result.error not in {None, "disabled"}:
        logger.warning(
            "[brain-shadow] skipped message_id=%s error=%s",
            kwargs.get("message_id"),
            result.error,
        )
    return result

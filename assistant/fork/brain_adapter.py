"""Native Odysseus -> Jarvis Brain shadow adapter.

Shadow mode is deliberately one-way and non-authoritative:

1. Odysseus persists its normal chat row first.
2. This adapter mirrors that exact persisted message to Brain.
3. Any Brain failure is reported as telemetry only and MUST NOT undo, alter,
   or block the already-committed Odysseus state.

v0.4.0 adds one bounded read path for ephemeral Brain recall context.
Odysseus remains authoritative for chat persistence; recall is reference data
only and must fail open without changing native conversation state.
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
_ALLOWED_MODES = frozenset({"off", "shadow", "recall"})


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    value = str(raw).strip().casefold()
    if value in {"1", "true", "yes", "on", "enabled"}:
        return True
    if value in {"0", "false", "no", "off", "disabled"}:
        return False
    logger.warning("Invalid %s=%r; using default=%s", name, raw, default)
    return default


@dataclass(frozen=True)
class BrainAdapterConfig:
    mode: str = "off"
    base_url: str = ""
    api_key: str = ""
    timeout_seconds: float = 0.5
    recall_timeout_seconds: float = 2.0
    recall_max_items: int = 6
    recall_max_chars: int = 2800
    default_owner: str = "local"
    capture_enabled: bool = True
    recall_enabled: bool = True

    @classmethod
    def from_env(cls) -> "BrainAdapterConfig":
        try:
            timeout = float(
                os.environ.get(
                    "ASSISTANT_BRAIN_TIMEOUT_SECONDS", "0.5"
                )
            )
        except (TypeError, ValueError):
            timeout = 0.5
        try:
            recall_timeout = float(
                os.environ.get(
                    "ASSISTANT_BRAIN_RECALL_TIMEOUT_SECONDS", "2.0"
                )
            )
        except (TypeError, ValueError):
            recall_timeout = 2.0
        try:
            recall_max_items = int(
                os.environ.get(
                    "ASSISTANT_BRAIN_RECALL_MAX_ITEMS", "6"
                ) or "6"
            )
        except (TypeError, ValueError):
            recall_max_items = 6
        try:
            recall_max_chars = int(
                os.environ.get(
                    "ASSISTANT_BRAIN_RECALL_MAX_CHARS", "2800"
                ) or "2800"
            )
        except (TypeError, ValueError):
            recall_max_chars = 2800

        return cls(
            mode=str(
                os.environ.get("ASSISTANT_BRAIN_MODE", "off") or "off"
            ).strip().casefold(),
            capture_enabled=_env_flag("ASSISTANT_BRAIN_CAPTURE_ENABLED", True),
            recall_enabled=_env_flag("ASSISTANT_BRAIN_RECALL_ENABLED", True),
            base_url=str(
                os.environ.get("JARVIS_BRAIN_URL", "") or ""
            ).strip().rstrip("/"),
            api_key=str(
                os.environ.get("JARVIS_BRAIN_API_KEY", "") or ""
            ),
            timeout_seconds=min(5.0, max(0.05, timeout)),
            recall_timeout_seconds=min(
                5.0, max(0.10, recall_timeout)
            ),
            recall_max_items=max(1, min(recall_max_items, 8)),
            recall_max_chars=max(512, min(recall_max_chars, 8000)),
            default_owner=str(
                os.environ.get(
                    "ASSISTANT_BRAIN_DEFAULT_OWNER", "local"
                ) or "local"
            ).strip(),
        )

    def validation_error(self) -> str | None:
        if self.mode not in _ALLOWED_MODES:
            return f"unsupported Brain mode: {self.mode!r}"
        active_capture = self.capture_enabled and self.mode in {"shadow", "recall"}
        active_recall = self.recall_enabled and self.mode == "recall"
        if self.mode == "off" or not (active_capture or active_recall):
            return None
        if not self.default_owner:
            return "ASSISTANT_BRAIN_DEFAULT_OWNER must not be empty"
        if not self.base_url:
            return "JARVIS_BRAIN_URL is required when Brain is enabled"
        parts = urlsplit(self.base_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            return "JARVIS_BRAIN_URL must be an absolute http(s) URL"
        if (
            parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            return (
                "JARVIS_BRAIN_URL must not contain credentials, "
                "query, or fragment"
            )
        if len(self.api_key) < 32:
            return (
                "JARVIS_BRAIN_API_KEY must contain at least "
                "32 characters"
            )
        return None


@dataclass(frozen=True)
class ShadowCaptureResult:
    attempted: bool
    delivered: bool
    endpoint: str | None = None
    status_code: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class BrainRecallResult:
    attempted: bool
    delivered: bool
    packet: Mapping[str, Any] | None = None
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

    def _post_json_payload(
        self,
        endpoint: str,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> tuple[ShadowCaptureResult, dict[str, Any] | None]:
        body = json.dumps(
            payload, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        request = Request(
            self.config.base_url + endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Cache-Control": "no-store",
                "User-Agent": "Odysseus-BrainRecall/0.4.1",
                "Connection": "close",
            },
        )
        try:
            with urlopen(
                request, timeout=timeout_seconds
            ) as response:
                raw = response.read(1024 * 1024)
                status = int(
                    getattr(response, "status", 200)
                )
            if not 200 <= status < 300:
                return (
                    ShadowCaptureResult(
                        True, False, endpoint, status,
                        f"HTTP {status}"
                    ),
                    None,
                )
            try:
                decoded = (
                    json.loads(raw.decode("utf-8"))
                    if raw else {}
                )
            except Exception:
                return (
                    ShadowCaptureResult(
                        True, False, endpoint, status,
                        "Brain returned invalid JSON"
                    ),
                    None,
                )
            if not isinstance(decoded, dict):
                return (
                    ShadowCaptureResult(
                        True, False, endpoint, status,
                        "Brain returned non-object JSON"
                    ),
                    None,
                )
            if decoded.get("ok") is False:
                return (
                    ShadowCaptureResult(
                        True, False, endpoint, status,
                        str(
                            decoded.get("error")
                            or "Brain rejected request"
                        ),
                    ),
                    decoded,
                )
            return (
                ShadowCaptureResult(
                    True, True, endpoint, status, None
                ),
                decoded,
            )
        except HTTPError as exc:
            try:
                raw = exc.read(4096)
                try:
                    decoded = (
                        json.loads(raw.decode("utf-8"))
                        if raw else {}
                    )
                    detail = (
                        decoded.get("error")
                        if isinstance(decoded, dict)
                        else None
                    )
                except Exception:
                    detail = None
                return (
                    ShadowCaptureResult(
                        True, False, endpoint, int(exc.code),
                        str(detail or f"HTTP {exc.code}")
                    ),
                    None,
                )
            finally:
                exc.close()
        except URLError as exc:
            return (
                ShadowCaptureResult(
                    True, False, endpoint, None,
                    f"connection error: {exc.reason}"
                ),
                None,
            )
        except TimeoutError:
            return (
                ShadowCaptureResult(
                    True, False, endpoint, None, "timeout"
                ),
                None,
            )
        except Exception as exc:
            return (
                ShadowCaptureResult(
                    True, False, endpoint, None,
                    f"{type(exc).__name__}: {exc}"
                ),
                None,
            )

    def recall_for_prompt(
        self,
        *,
        owner: str | None,
        query: str,
        exclude_external_source_refs: (
            list[str] | tuple[str, ...]
        ) = (),
    ) -> BrainRecallResult:
        # Fetch bounded reference context for one live user turn.
        if self.config.mode != "recall" or not self.config.recall_enabled:
            return BrainRecallResult(
                False, False, None, None, None, "disabled"
            )

        error = self.config.validation_error()
        if error:
            return BrainRecallResult(
                False, False, None, None, None, error
            )

        query = str(query or "").strip()
        if not query:
            return BrainRecallResult(
                False, False, None, None, None, "empty query"
            )

        owner_id = self._owner(
            owner, self.config.default_owner
        )
        excluded = [
            str(value).strip()
            for value in exclude_external_source_refs
            if str(value or "").strip()
        ][:16]

        request_result, decoded = self._post_json_payload(
            "/v1/recall",
            {
                "owner_id": owner_id,
                "query": query,
                "candidate_limit": 16,
                "max_items": self.config.recall_max_items,
                "max_chars": self.config.recall_max_chars,
                "include_episodes": True,
                "exclude_external_source_refs": excluded,
            },
            timeout_seconds=self.config.recall_timeout_seconds,
        )
        if not request_result.delivered or decoded is None:
            return BrainRecallResult(
                request_result.attempted,
                False,
                None,
                request_result.endpoint,
                request_result.status_code,
                request_result.error,
            )

        context = decoded.get("context")
        selected = decoded.get("selected")
        if not isinstance(context, str) or not isinstance(
            selected, list
        ):
            return BrainRecallResult(
                True, False, None, "/v1/recall",
                request_result.status_code,
                "Brain recall response has invalid shape",
            )
        if len(context) > self.config.recall_max_chars:
            return BrainRecallResult(
                True, False, None, "/v1/recall",
                request_result.status_code,
                "Brain recall response exceeded context budget",
            )

        packet = {
            "candidate_count": int(
                decoded.get("candidate_count") or 0
            ),
            "eligible_semantic_count": int(
                decoded.get(
                    "eligible_semantic_count"
                ) or 0
            ),
            "eligible_episode_count": int(
                decoded.get(
                    "eligible_episode_count"
                ) or 0
            ),
            "selected_count": int(
                decoded.get("selected_count") or 0
            ),
            "selection_mode": str(
                decoded.get("selection_mode") or "none"
            ),
            "selected": selected,
            "context": context,
            "context_chars": int(
                decoded.get("context_chars") or 0
            ),
            "budget_chars": int(
                decoded.get("budget_chars")
                or self.config.recall_max_chars
            ),
            "vector_candidate_count": int(
                decoded.get(
                    "vector_candidate_count"
                ) or 0
            ),
        }
        return BrainRecallResult(
            True, True, packet, "/v1/recall",
            request_result.status_code, None
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
        if self.config.mode == "off" or not self.config.capture_enabled:
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



def brain_recall_for_prompt(
    *,
    owner: str | None,
    session_id: str,
    query: str,
    exclude_external_source_refs: (
        list[str] | tuple[str, ...]
    ) = (),
) -> BrainRecallResult:
    # Environment-configured, fail-open recall helper.
    adapter = BrainMemoryAdapter(
        BrainAdapterConfig.from_env()
    )
    owner_id = adapter._owner(
        owner, adapter.config.default_owner
    )
    logger.debug(
        "[brain-recall] event=brain_recall_started "
        "owner=%s session_id=%s",
        owner_id, session_id,
    )
    result = adapter.recall_for_prompt(
        owner=owner,
        query=query,
        exclude_external_source_refs=(
            exclude_external_source_refs
        ),
    )

    if result.delivered and result.packet is not None:
        packet = result.packet
        selected = packet.get("selected") or []
        ids = [
            str(item.get("uuid"))
            for item in selected
            if isinstance(item, dict) and item.get("uuid")
        ]
        logger.debug(
            "[brain-recall] event=brain_recall_candidates "
            "owner=%s session_id=%s candidates=%s "
            "semantic=%s episodic=%s vector_candidates=%s",
            owner_id,
            session_id,
            packet.get("candidate_count", 0),
            packet.get("eligible_semantic_count", 0),
            packet.get("eligible_episode_count", 0),
            packet.get("vector_candidate_count", 0),
        )
        logger.info(
            "[brain-recall] event=brain_recall_selected "
            "owner=%s session_id=%s selected=%s mode=%s "
            "ids=%s chars=%s",
            owner_id,
            session_id,
            packet.get("selected_count", 0),
            packet.get("selection_mode", "none"),
            ids,
            packet.get("context_chars", 0),
        )
    elif result.attempted:
        logger.warning(
            "[brain-recall] event=brain_recall_error "
            "owner=%s session_id=%s status=%s error=%s",
            owner_id, session_id,
            result.status_code, result.error,
        )
    elif result.error not in {
        None, "disabled", "empty query"
    }:
        logger.warning(
            "[brain-recall] event=brain_recall_skipped "
            "owner=%s session_id=%s error=%s",
            owner_id, session_id, result.error,
        )
    return result

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

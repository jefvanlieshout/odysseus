from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from jarvis_brain import BrainMemoryService, build_vector_index_from_env
from jarvis_brain.llm_reasoner import OpenAIJsonReasoner, StructuredReasonerConfig
from jarvis_brain.semantic_worker import SemanticWorker


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.environ.get(name)
    value = default if raw is None or not raw.strip() else float(raw)
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    value = default if raw is None or not raw.strip() else int(raw)
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class WorkerDaemonConfig:
    db_path: str
    llm_url: str
    llm_model: str
    llm_headers: dict[str, str]
    llm_timeout_seconds: float
    llm_max_tokens: int
    llm_temperature: float
    llm_reasoning_effort: str | None
    llm_ready_url: str
    llm_ready_timeout_seconds: float
    llm_unavailable_poll_seconds: float
    poll_seconds: float
    error_backoff_seconds: float
    lease_seconds: int
    max_consecutive_jobs: int
    worker_id: str

    @classmethod
    def from_env(cls) -> "WorkerDaemonConfig":
        raw_headers = os.environ.get("BRAIN_LLM_HEADERS_JSON", "").strip()
        headers: dict[str, str] = {}
        if raw_headers:
            parsed = json.loads(raw_headers)
            if not isinstance(parsed, dict):
                raise ValueError("BRAIN_LLM_HEADERS_JSON must be a JSON object")
            headers = {str(key): str(value) for key, value in parsed.items()}

        llm_url = os.environ.get(
            "BRAIN_LLM_URL",
            "http://127.0.0.1:8000/v1/chat/completions",
        ).strip()
        parts = urlsplit(llm_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("BRAIN_LLM_URL must be an absolute http(s) URL")

        model = os.environ.get("BRAIN_LLM_MODEL", "Qwen/Qwen3.8-27B").strip()
        if not model:
            raise ValueError("BRAIN_LLM_MODEL may not be empty")

        default_ready_url = _default_ready_url(llm_url)
        ready_url = os.environ.get("BRAIN_LLM_READY_URL", default_ready_url).strip()
        ready_parts = urlsplit(ready_url)
        if ready_parts.scheme not in {"http", "https"} or not ready_parts.netloc:
            raise ValueError("BRAIN_LLM_READY_URL must be an absolute http(s) URL")

        default_worker_id = f"{socket.gethostname()}:{os.getpid()}"

        raw_effort = os.environ.get("BRAIN_LLM_REASONING_EFFORT", "medium").strip().casefold()
        if raw_effort in {"", "none", "off", "disabled"}:
            reasoning_effort = None
        elif raw_effort in {"low", "medium", "xhigh"}:
            reasoning_effort = raw_effort
        else:
            raise ValueError(
                "BRAIN_LLM_REASONING_EFFORT must be low, medium, xhigh, or disabled"
            )

        return cls(
            db_path=os.environ.get("BRAIN_DB_PATH", "/data/brain.db"),
            llm_url=llm_url,
            llm_model=model,
            llm_headers=headers,
            llm_timeout_seconds=_env_float(
                "BRAIN_LLM_TIMEOUT_SECONDS", 120.0, minimum=1.0
            ),
            llm_max_tokens=_env_int("BRAIN_LLM_MAX_TOKENS", 900, minimum=64),
            llm_temperature=_env_float(
                "BRAIN_LLM_TEMPERATURE", 0.0, minimum=0.0
            ),
            llm_reasoning_effort=reasoning_effort,
            llm_ready_url=ready_url,
            llm_ready_timeout_seconds=_env_float(
                "BRAIN_LLM_READY_TIMEOUT_SECONDS", 2.0, minimum=0.2
            ),
            llm_unavailable_poll_seconds=_env_float(
                "BRAIN_LLM_UNAVAILABLE_POLL_SECONDS", 10.0, minimum=1.0
            ),
            poll_seconds=_env_float(
                "BRAIN_WORKER_POLL_SECONDS", 2.0, minimum=0.1
            ),
            error_backoff_seconds=_env_float(
                "BRAIN_WORKER_ERROR_BACKOFF_SECONDS", 5.0, minimum=0.5
            ),
            lease_seconds=_env_int(
                "BRAIN_WORKER_LEASE_SECONDS", 300, minimum=30
            ),
            max_consecutive_jobs=_env_int(
                "BRAIN_WORKER_MAX_CONSECUTIVE_JOBS", 8, minimum=1
            ),
            worker_id=os.environ.get(
                "BRAIN_WORKER_ID", default_worker_id
            ).strip() or default_worker_id,
        )




def _default_ready_url(chat_url: str) -> str:
    parts = urlsplit(chat_url)
    path = parts.path or ""
    suffix = "/v1/chat/completions"
    if path.endswith(suffix):
        path = path[: -len(suffix)] + "/v1/models"
    else:
        path = "/v1/models"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _model_id_matches(expected: str, actual: str) -> bool:
    expected = str(expected or "").strip().casefold()
    actual = str(actual or "").strip().casefold()
    if not expected or not actual:
        return False
    if expected == actual:
        return True
    expected_base = expected.rsplit("/", 1)[-1]
    actual_base = actual.rsplit("/", 1)[-1]
    return expected_base == actual_base


def _probe_llm(config: WorkerDaemonConfig) -> tuple[bool, str]:
    # Readiness only: no inference, no job claim, no state mutation.
    headers = {
        "Accept": "application/json",
        "Cache-Control": "no-store",
        "User-Agent": "JarvisBrainReadiness/0.3.0",
        "Connection": "close",
    }
    for key, value in config.llm_headers.items():
        if key.casefold() not in {"host", "content-length"}:
            headers[key] = value

    request = Request(config.llm_ready_url, method="GET", headers=headers)
    try:
        with urlopen(request, timeout=config.llm_ready_timeout_seconds) as response:
            status = int(getattr(response, "status", 200))
            raw = response.read(1024 * 1024)
    except HTTPError as exc:
        try:
            exc.read(1024)
        finally:
            exc.close()
        return False, f"HTTP {exc.code}"
    except (URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"{type(exc).__name__}: {reason}"

    if not 200 <= status < 300:
        return False, f"HTTP {status}"

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return False, f"invalid readiness JSON: {type(exc).__name__}"

    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return False, "readiness response has no model list"

    model_ids = [
        str(item.get("id") or "")
        for item in items
        if isinstance(item, dict)
    ]
    if not any(_model_id_matches(config.llm_model, item) for item in model_ids):
        return False, f"expected model not served; available={model_ids[:8]!r}"

    return True, "ready"


def _emit(event: str, **fields: Any) -> None:
    payload = {
        "component": "jarvis-brain-semantic-worker",
        "event": event,
        "time": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        **fields,
    }
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        flush=True,
    )


def _result_payload(result: Any) -> dict[str, Any]:
    committed = []
    for item in tuple(getattr(result, "committed", ()) or ()):
        action = getattr(item, "action", None)
        committed.append(
            {
                "action": getattr(action, "value", str(action)),
                "memory_uuid": getattr(item, "memory_uuid", None),
                "revision_no": getattr(item, "revision_no", None),
                "changed": getattr(item, "changed", None),
                "conflict": getattr(item, "conflict", None),
            }
        )
    return {
        "job_uuid": getattr(result, "job_uuid", None),
        "status": getattr(result, "status", None),
        "committed": committed,
        "rejections": list(getattr(result, "rejections", ()) or ()),
        "error": getattr(result, "error", None),
    }


def build_worker(config: WorkerDaemonConfig) -> SemanticWorker:
    vector = build_vector_index_from_env()
    if getattr(vector, "healthy", False) is not True:
        raise RuntimeError("Brain vector index is not healthy")

    service = BrainMemoryService(config.db_path, vector_index=vector)
    reasoner = OpenAIJsonReasoner(
        StructuredReasonerConfig(
            chat_url=config.llm_url,
            model=config.llm_model,
            extra_headers=config.llm_headers,
            timeout_seconds=config.llm_timeout_seconds,
            max_tokens=config.llm_max_tokens,
            temperature=config.llm_temperature,
            reasoning_effort=config.llm_reasoning_effort,
        )
    )
    return SemanticWorker(
        service,
        reasoner,
        worker_id=config.worker_id,
        lease_seconds=config.lease_seconds,
    )


def _check(config: WorkerDaemonConfig) -> None:
    vector = build_vector_index_from_env()
    if getattr(vector, "healthy", False) is not True:
        raise RuntimeError("Brain vector index is not healthy")
    service = BrainMemoryService(config.db_path, vector_index=vector)
    with service.store.read() as db:
        schema_version = int(
            db.execute(
                "SELECT value FROM brain_meta WHERE key='schema_version'"
            ).fetchone()[0]
        )
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    if schema_version != 3:
        raise RuntimeError(f"expected Brain schema v3, got v{schema_version}")
    if integrity != "ok":
        raise RuntimeError(f"SQLite integrity_check failed: {integrity}")
    _emit(
        "check_ok",
        schema_version=schema_version,
        vector_backend=type(vector).__name__,
        model=config.llm_model,
        llm_host=urlsplit(config.llm_url).hostname,
        reasoning_effort=config.llm_reasoning_effort,
        llm_ready_url=config.llm_ready_url,
    )


def run_daemon(config: WorkerDaemonConfig, *, once: bool = False) -> int:
    stop = threading.Event()

    def request_stop(signum: int, _frame: Any) -> None:
        _emit("stop_requested", signal=signum)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    worker = build_worker(config)
    _emit(
        "started",
        worker_id=config.worker_id,
        db_path=config.db_path,
        model=config.llm_model,
        llm_host=urlsplit(config.llm_url).hostname,
        reasoning_effort=config.llm_reasoning_effort,
        llm_ready_url=config.llm_ready_url,
        poll_seconds=config.poll_seconds,
        lease_seconds=config.lease_seconds,
        max_consecutive_jobs=config.max_consecutive_jobs,
    )

    consecutive_errors = 0
    jobs_since_yield = 0
    llm_was_ready: bool | None = None

    while not stop.is_set():
        try:
            llm_ready, llm_detail = _probe_llm(config)
            if not llm_ready:
                if llm_was_ready is not False:
                    _emit(
                        "llm_unavailable",
                        ready_url=config.llm_ready_url,
                        detail=llm_detail,
                    )
                llm_was_ready = False
                consecutive_errors = 0
                jobs_since_yield = 0
                if once:
                    _emit("once_deferred", reason="llm_unavailable", detail=llm_detail)
                    return 2
                stop.wait(config.llm_unavailable_poll_seconds)
                continue

            if llm_was_ready is False:
                _emit(
                    "llm_ready",
                    ready_url=config.llm_ready_url,
                    model=config.llm_model,
                )
            llm_was_ready = True

            result = worker.run_once()
            payload = _result_payload(result)
            job_uuid = payload["job_uuid"]

            if not job_uuid:
                consecutive_errors = 0
                jobs_since_yield = 0
                if once:
                    _emit("once_complete", result=payload)
                    return 0
                stop.wait(config.poll_seconds)
                continue

            _emit("job_result", **payload)
            jobs_since_yield += 1

            if payload["error"]:
                consecutive_errors += 1
                delay = min(
                    60.0,
                    config.error_backoff_seconds
                    * (2 ** min(consecutive_errors - 1, 4)),
                )
                _emit(
                    "job_error_backoff",
                    job_uuid=job_uuid,
                    delay_seconds=delay,
                )
                if once:
                    return 1
                stop.wait(delay)
            else:
                consecutive_errors = 0

            if once:
                return 0 if not payload["error"] else 1

            if jobs_since_yield >= config.max_consecutive_jobs:
                jobs_since_yield = 0
                stop.wait(min(config.poll_seconds, 1.0))

        except Exception as exc:
            consecutive_errors += 1
            delay = min(
                60.0,
                config.error_backoff_seconds
                * (2 ** min(consecutive_errors - 1, 4)),
            )
            _emit(
                "loop_exception",
                error_type=type(exc).__name__,
                error=str(exc),
                traceback="".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                )[-5000:],
                delay_seconds=delay,
            )
            if once:
                return 1
            stop.wait(delay)

    _emit("stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Autonomous Jarvis Brain semantic worker"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="process at most one available semantic job and exit",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify schema/vector/config without calling the LLM or claiming a job",
    )
    parser.add_argument(
        "--probe-llm",
        action="store_true",
        help="probe the configured /v1/models route without inference or job mutation",
    )
    args = parser.parse_args(argv)

    config = WorkerDaemonConfig.from_env()
    if args.check:
        _check(config)
        return 0
    if args.probe_llm:
        ready, detail = _probe_llm(config)
        _emit(
            "llm_probe",
            ready=ready,
            detail=detail,
            ready_url=config.llm_ready_url,
            model=config.llm_model,
        )
        return 0 if ready else 2
    return run_daemon(config, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())

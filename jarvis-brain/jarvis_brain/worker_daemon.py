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
from urllib.parse import urlsplit

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
        poll_seconds=config.poll_seconds,
        lease_seconds=config.lease_seconds,
        max_consecutive_jobs=config.max_consecutive_jobs,
    )

    consecutive_errors = 0
    jobs_since_yield = 0

    while not stop.is_set():
        try:
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
    args = parser.parse_args(argv)

    config = WorkerDaemonConfig.from_env()
    if args.check:
        _check(config)
        return 0
    return run_daemon(config, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from jarvis_brain.worker_daemon import WorkerDaemonConfig, _result_payload


class _Action:
    value = "UPDATE"


class _Commit:
    action = _Action()
    memory_uuid = "memory-1"
    revision_no = 2
    changed = True
    conflict = False


class _Result:
    job_uuid = "job-1"
    status = "done"
    committed = (_Commit(),)
    rejections = ()
    error = None


class WorkerDaemonTests(unittest.TestCase):
    def test_defaults_are_production_sane(self):
        keys = {
            "BRAIN_LLM_URL",
            "BRAIN_LLM_MODEL",
            "BRAIN_LLM_HEADERS_JSON",
            "BRAIN_LLM_TIMEOUT_SECONDS",
            "BRAIN_LLM_MAX_TOKENS",
            "BRAIN_LLM_TEMPERATURE",
            "BRAIN_LLM_REASONING_EFFORT",
            "BRAIN_WORKER_POLL_SECONDS",
            "BRAIN_WORKER_ERROR_BACKOFF_SECONDS",
            "BRAIN_WORKER_LEASE_SECONDS",
            "BRAIN_WORKER_MAX_CONSECUTIVE_JOBS",
            "BRAIN_WORKER_ID",
        }
        env = {key: value for key, value in os.environ.items() if key not in keys}
        with patch.dict(os.environ, env, clear=True):
            config = WorkerDaemonConfig.from_env()

        self.assertEqual(
            config.llm_url,
            "http://127.0.0.1:8000/v1/chat/completions",
        )
        self.assertEqual(config.llm_model, "Qwen/Qwen3.8-27B")
        self.assertEqual(config.lease_seconds, 300)
        self.assertEqual(config.poll_seconds, 2.0)
        self.assertEqual(config.llm_headers, {})
        self.assertEqual(config.llm_reasoning_effort, "medium")

    def test_headers_are_parsed_without_changing_types_at_call_boundary(self):
        with patch.dict(
            os.environ,
            {
                "BRAIN_LLM_HEADERS_JSON": json.dumps(
                    {"Authorization": "Bearer secret", "X-Test": 123}
                )
            },
            clear=False,
        ):
            config = WorkerDaemonConfig.from_env()
        self.assertEqual(
            config.llm_headers,
            {"Authorization": "Bearer secret", "X-Test": "123"},
        )

    def test_reasoning_effort_can_be_disabled_but_invalid_values_fail(self):
        with patch.dict(
            os.environ,
            {"BRAIN_LLM_REASONING_EFFORT": "off"},
            clear=False,
        ):
            self.assertIsNone(WorkerDaemonConfig.from_env().llm_reasoning_effort)

        with patch.dict(
            os.environ,
            {"BRAIN_LLM_REASONING_EFFORT": "high"},
            clear=False,
        ):
            with self.assertRaises(ValueError):
                WorkerDaemonConfig.from_env()

    def test_result_logging_keeps_python_owned_action(self):
        payload = _result_payload(_Result())
        self.assertEqual(payload["job_uuid"], "job-1")
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["committed"][0]["action"], "UPDATE")
        self.assertTrue(payload["committed"][0]["changed"])
        self.assertIsNone(payload["error"])


if __name__ == "__main__":
    unittest.main()

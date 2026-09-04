from __future__ import annotations

import json
import os
import unittest
from unittest.mock import Mock, patch

from jarvis_brain.worker_daemon import (
    WorkerDaemonConfig,
    _default_ready_url,
    _model_id_matches,
    _probe_llm,
    _result_payload,
    run_daemon,
)


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
            "BRAIN_LLM_READY_URL",
            "BRAIN_LLM_READY_TIMEOUT_SECONDS",
            "BRAIN_LLM_UNAVAILABLE_POLL_SECONDS",
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
        self.assertEqual(config.llm_ready_url, "http://127.0.0.1:8000/v1/models")
        self.assertEqual(config.llm_ready_timeout_seconds, 2.0)
        self.assertEqual(config.llm_unavailable_poll_seconds, 10.0)

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

    def test_ready_url_is_derived_from_chat_route(self):
        self.assertEqual(
            _default_ready_url("http://127.0.0.1:8000/v1/chat/completions"),
            "http://127.0.0.1:8000/v1/models",
        )

    def test_model_match_accepts_logical_id_and_llamacpp_gguf_artifact(self):
        self.assertTrue(_model_id_matches("Qwen/Qwen3.8-27B", "Qwen/Qwen3.8-27B"))
        self.assertTrue(_model_id_matches("Qwen/Qwen3.8-27B", "Qwen3.8-27B"))
        self.assertTrue(_model_id_matches(
            "Qwen/Qwen3.8-27B",
            "/app/.cache/huggingface/hub/models--Qwen--Qwen3.8-27B/"
            "snapshots/abc/Qwen3.8-27B-Q4_K_M.gguf",
        ))
        self.assertTrue(_model_id_matches(
            "Qwen/Qwen3.8-27B",
            "/models/Qwen3.8-27B-IQ4_XS.gguf",
        ))
        self.assertFalse(_model_id_matches(
            "Qwen/Qwen3.8-27B",
            "/models/Qwen3.8-14B-Q4_K_M.gguf",
        ))
        self.assertFalse(_model_id_matches("Qwen/Qwen3.8-27B", "other/model"))

    def test_probe_llm_requires_expected_model(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return json.dumps({
                    "data": [{"id": "Qwen/Qwen3.8-27B"}]
                }).encode()

        config = WorkerDaemonConfig.from_env()
        with patch("jarvis_brain.worker_daemon.urlopen", return_value=Response()):
            ready, detail = _probe_llm(config)
        self.assertTrue(ready)
        self.assertEqual(detail, "ready")

        class Wrong(Response):
            def read(self, limit):
                return json.dumps({"data": [{"id": "other/model"}]}).encode()

        with patch("jarvis_brain.worker_daemon.urlopen", return_value=Wrong()):
            ready, detail = _probe_llm(config)
        self.assertFalse(ready)
        self.assertIn("expected model not served", detail)

    def test_unavailable_llm_does_not_claim_or_mutate_a_job(self):
        config = WorkerDaemonConfig.from_env()
        fake_worker = Mock()
        with (
            patch("jarvis_brain.worker_daemon.build_worker", return_value=fake_worker),
            patch("jarvis_brain.worker_daemon._probe_llm", return_value=(False, "connection refused")),
        ):
            rc = run_daemon(config, once=True)

        self.assertEqual(rc, 2)
        fake_worker.run_once.assert_not_called()

    def test_result_logging_keeps_python_owned_action(self):
        payload = _result_payload(_Result())
        self.assertEqual(payload["job_uuid"], "job-1")
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["committed"][0]["action"], "UPDATE")
        self.assertTrue(payload["committed"][0]["changed"])
        self.assertIsNone(payload["error"])


if __name__ == "__main__":
    unittest.main()

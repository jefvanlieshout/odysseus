from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jarvis_brain import (
    ClaimStatus,
    OpenAIJsonReasoner,
    RelationDecision,
    SearchHit,
    SemanticCandidate,
    SemanticRelation,
    StructuredReasonerConfig,
    StructuredReasonerError,
)
from jarvis_brain.llm_reasoner import (
    CANDIDATE_SCHEMA,
    CONSOLIDATION_SCHEMA,
    PROVENANCE_SCHEMA,
    RELATION_SCHEMA,
)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        self.replies = []
        self.requests = []
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or "0")
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.requests.append({
            "path": self.path,
            "headers": dict(self.headers.items()),
            "body": body,
        })
        status, structured = self.server.replies.pop(0)
        if isinstance(structured, bytes):
            raw = structured
        else:
            envelope = {
                "choices": [{"message": {"role": "assistant", "content": structured}}]
            }
            raw = json.dumps(envelope).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _start():
    server = _Server(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class ReasonerTests(unittest.TestCase):
    def setUp(self):
        self.server, self.thread = _start()
        host, port = self.server.server_address
        self.reasoner = OpenAIJsonReasoner(StructuredReasonerConfig(
            chat_url=f"http://{host}:{port}/v1/chat/completions",
            model="Qwen/Qwen3.8-27B",
            api_key="secret",
            timeout_seconds=2.0,
        ))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def reply(self, payload, status=200):
        self.server.replies.append((status, json.dumps(payload)))

    def test_schemas_are_strict_and_relation_has_no_action(self):
        for schema in (CANDIDATE_SCHEMA, PROVENANCE_SCHEMA, RELATION_SCHEMA, CONSOLIDATION_SCHEMA):
            self.assertFalse(schema.get("additionalProperties", True))
        self.assertNotIn("action", RELATION_SCHEMA["properties"])
        self.assertNotIn("persistence_action", RELATION_SCHEMA["properties"])

    def test_candidate_call_uses_json_schema_and_exact_quote(self):
        self.reply({"candidates": [{
            "content": "The user prefers root-cause Linux fixes.",
            "memory_type": "preference",
            "scope": "linux",
            "confidence": 0.95,
            "evidence_quote": "root-cause Linux fixes",
        }]})
        result = self.reasoner.propose_candidates(
            evidence_text="I prefer root-cause Linux fixes.",
            evidence_uuid="e-1",
            owner_id="jef",
        )
        self.assertEqual(result[0].memory_type, "preference")
        request = self.server.requests[-1]
        rf = request["body"]["response_format"]
        self.assertEqual(rf["type"], "json_schema")
        self.assertTrue(rf["json_schema"]["strict"])
        self.assertEqual(request["body"]["temperature"], 0.0)
        self.assertFalse(request["body"]["stream"])
        self.assertEqual(request["headers"].get("Authorization"), "Bearer secret")

    def test_empty_candidate_array_is_valid(self):
        self.reply({"candidates": []})
        self.assertEqual(
            tuple(self.reasoner.propose_candidates(
                evidence_text="hello qwen", evidence_uuid="e", owner_id="jef"
            )),
            (),
        )

    def test_candidate_unknown_key_fails_closed(self):
        self.reply({"candidates": [{
            "content": "x", "memory_type": "fact", "scope": "x",
            "confidence": 0.5, "evidence_quote": "x", "action": "CREATE",
        }]})
        with self.assertRaises(StructuredReasonerError):
            self.reasoner.propose_candidates(evidence_text="x", evidence_uuid="e", owner_id="j")

    def test_provenance_and_verify_only_repair_guard(self):
        self.reply({
            "claim_statuses": ["SUPPORTED_PARAPHRASE"],
            "repaired_content": "",
        })
        result = self.reasoner.check_provenance(
            content="The user prefers Linux.",
            authoritative_evidence="I prefer Linux.",
            supporting_memories=(),
            allow_repair=False,
        )
        self.assertEqual(result.claim_statuses, (ClaimStatus.SUPPORTED_PARAPHRASE,))

        self.reply({
            "claim_statuses": ["UNSUPPORTED"],
            "repaired_content": "The user prefers Linux.",
        })
        with self.assertRaises(StructuredReasonerError):
            self.reasoner.check_provenance(
                content="x", authoritative_evidence="I prefer Linux.",
                supporting_memories=(), allow_repair=False,
            )

    def test_relation_never_accepts_action_key(self):
        self.reply({
            "relation": "NOVEL",
            "target_memory_uuid": "",
            "confidence": 0.9,
            "explanation": "new",
            "action": "CREATE",
        })
        with self.assertRaises(StructuredReasonerError):
            self.reasoner.classify_relation(
                candidate=SemanticCandidate("x", "fact", "x", 0.9, "e", "x"),
                neighbors=(),
            )

    def test_relation_returns_semantic_relation_only(self):
        target = "11111111-1111-4111-8111-111111111111"
        self.reply({
            "relation": "STATE_CHANGE",
            "target_memory_uuid": target,
            "confidence": 0.98,
            "explanation": "new state",
        })
        result = self.reasoner.classify_relation(
            candidate=SemanticCandidate("uses zsh", "fact", "linux", 0.9, "e", "zsh"),
            neighbors=(SearchHit("semantic", target, "uses fish", 1.0, {}),),
        )
        self.assertEqual(result.relation, SemanticRelation.STATE_CHANGE)
        self.assertEqual(result.target_memory_uuid, target)

    def test_consolidation_keep_sentinel_maps_to_none(self):
        self.reply({
            "content": "The user currently uses zsh.",
            "memory_type": "__KEEP__",
            "scope": "",
            "confidence": 0.9,
            "change_reason": "shell changed",
        })
        result = self.reasoner.consolidate(
            candidate=SemanticCandidate("uses zsh", "fact", "linux", 0.9, "e", "zsh"),
            target=SearchHit("semantic", "m", "uses fish", 1.0, {}),
            relation=SemanticRelation.STATE_CHANGE,
        )
        self.assertIsNone(result.memory_type)
        self.assertIsNone(result.scope)

    def test_http_error_and_invalid_json_fail_closed(self):
        self.server.replies.append((500, b'{"error":"boom"}'))
        with self.assertRaises(StructuredReasonerError):
            self.reasoner.propose_candidates(evidence_text="x", evidence_uuid="e", owner_id="j")

        self.server.replies.append((200, b'not-json'))
        with self.assertRaises(StructuredReasonerError):
            self.reasoner.propose_candidates(evidence_text="x", evidence_uuid="e", owner_id="j")

    def test_config_rejects_credentials_in_url(self):
        with self.assertRaises(StructuredReasonerError):
            OpenAIJsonReasoner(StructuredReasonerConfig(
                chat_url="http://user:pass@localhost:8080/v1/chat/completions",
                model="qwen",
            ))


if __name__ == "__main__":
    unittest.main()

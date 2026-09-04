from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from jarvis_brain import (
    BrainMemoryService,
    ClaimStatus,
    ProvenanceCheck,
    RelationDecision,
    SemanticCandidate,
    SemanticRelation,
    SourceKind,
)
from jarvis_brain.api import start_server_in_thread
from jarvis_brain.vector_chroma import ChromaConfig, ChromaVectorIndex


class FakeEmbedder:
    model_name = "fake-multilingual"

    def _vec(self, text):
        # deterministic 3d test embedding
        t = text.casefold()
        return [
            float("fish" in t or "shell" in t),
            float("gpu" in t or "v100" in t),
            float(len(t) % 11) / 10.0,
        ]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


class FakeCollection:
    def __init__(self, name, metadata=None):
        self.name = name
        self.metadata = dict(metadata or {})
        self.items = {}

    def count(self):
        return len(self.items)

    def upsert(self, *, ids, embeddings, documents, metadatas):
        for i, item_id in enumerate(ids):
            self.items[item_id] = {
                "embedding": list(embeddings[i]),
                "document": documents[i],
                "metadata": dict(metadatas[i]),
            }

    def get(self, ids=None, where=None, include=None, limit=None, offset=None):
        rows = []
        for item_id, row in self.items.items():
            if ids is not None and item_id not in ids:
                continue
            if where and any(row["metadata"].get(k) != v for k, v in where.items()):
                continue
            rows.append((item_id, row))
        start = int(offset or 0)
        stop = None if limit is None else start + int(limit)
        rows = rows[start:stop]
        return {
            "ids": [x[0] for x in rows],
            "metadatas": [x[1]["metadata"] for x in rows],
        }

    def delete(self, *, ids):
        for item_id in list(ids):
            self.items.pop(item_id, None)

    def query(self, *, query_embeddings, n_results, where, include):
        q = query_embeddings[0]
        rows = []
        for item_id, row in self.items.items():
            if any(row["metadata"].get(k) != v for k, v in where.items()):
                continue
            emb = row["embedding"]
            # Cheap cosine-ish test distance. Equal vectors => 0.
            dot = sum(a*b for a,b in zip(q, emb))
            nq = sum(a*a for a in q) ** 0.5
            ne = sum(a*a for a in emb) ** 0.5
            sim = dot/(nq*ne) if nq and ne else 0.0
            distance = 1.0 - max(-1.0, min(1.0, sim))
            rows.append((distance, item_id, row))
        rows.sort()
        rows = rows[:n_results]
        return {
            "ids": [[r[1] for r in rows]],
            "distances": [[r[0] for r in rows]],
            "metadatas": [[r[2]["metadata"] for r in rows]],
        }


class FakeChromaClient:
    def __init__(self):
        self.collections = {}
        self.heartbeats = 0

    def heartbeat(self):
        self.heartbeats += 1
        return 123

    def get_or_create_collection(self, name, metadata=None, embedding_function=None, configuration=None):
        return self.collections.setdefault(name, FakeCollection(name, metadata))


def http_json(url, *, key=None, payload=None):
    headers = {}
    data = None
    method = "GET"
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
        method = "POST"
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=2) as res:
            return res.status, json.loads(res.read().decode())
    except HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        finally:
            exc.close()


class VectorSidecarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = FakeChromaClient()
        self.vector = ChromaVectorIndex(
            client=self.client,
            embedder=FakeEmbedder(),
            config=ChromaConfig(
                semantic_collection="test_semantic",
                episodic_collection="test_episodic",
                embedding_model="fake-multilingual",
                embedding_contract="fake-embedding-contract-v1",
            ),
        )
        self.brain = BrainMemoryService(Path(self.tmp.name)/"brain.db", vector_index=self.vector)

    def tearDown(self):
        self.tmp.cleanup()

    def capture(self, owner="jef", ref="m1", text="I use fish shell."):
        return self.brain.capture_observation(
            owner_id=owner,
            raw_text=text,
            external_source_ref=ref,
            session_id="s1",
            source_kind=SourceKind.USER_MESSAGE,
        )

    def create_memory(self, owner="jef", ref="m1", text="I use fish shell."):
        obs = self.capture(owner, ref, text)
        candidate = SemanticCandidate(
            content="The user uses fish shell.",
            memory_type="fact",
            scope="linux",
            confidence=0.95,
            evidence_uuid=obs["evidence_uuid"],
            evidence_quote="fish shell",
        )
        result = self.brain.commit_semantic_candidate(
            owner_id=owner,
            candidate=candidate,
            decision=RelationDecision(SemanticRelation.NOVEL, None, 0.99),
            provenance=ProvenanceCheck(candidate.content, (ClaimStatus.SUPPORTED_PARAPHRASE,)),
            idempotency_key=f"semantic-{owner}-{ref}",
        )
        return obs, result

    def test_chroma_collections_are_separate(self):
        self.assertIn("test_semantic", self.client.collections)
        self.assertIn("test_episodic", self.client.collections)
        self.assertIsNot(self.client.collections["test_semantic"], self.client.collections["test_episodic"])

    def test_chroma_records_carry_owner_and_kind(self):
        obs, result = self.create_memory()
        semantic = self.client.collections["test_semantic"].items[result.memory_uuid]
        episode = self.client.collections["test_episodic"].items[obs["episode_uuid"]]
        self.assertEqual(semantic["metadata"]["owner_id"], "jef")
        self.assertEqual(semantic["metadata"]["kind"], "semantic")
        self.assertEqual(episode["metadata"]["owner_id"], "jef")
        self.assertEqual(episode["metadata"]["kind"], "episode")

    def test_vector_search_is_owner_filtered(self):
        self.create_memory("jef", "j1", "I use fish shell.")
        self.create_memory("other", "o1", "I use fish shell too.")
        rows = self.vector.search(owner_id="jef", query="fish shell", kinds=("semantic",), limit=10)
        self.assertTrue(rows)
        ids = {row[1] for row in rows}
        other_ids = set(self.client.collections["test_semantic"].items) - ids
        self.assertTrue(other_ids)

    def test_delete_refuses_cross_owner_uuid(self):
        _obs, result = self.create_memory("jef", "j1")
        self.vector.delete(owner_id="other", kind="semantic", uuid=result.memory_uuid)
        self.assertIn(result.memory_uuid, self.client.collections["test_semantic"].items)

    def test_rebuild_is_from_sqlite_truth(self):
        obs, result = self.create_memory()
        sem = self.client.collections["test_semantic"]
        epi = self.client.collections["test_episodic"]
        sem.items["invented"] = {
            "embedding": [1,0,0], "document": "invented",
            "metadata": {"owner_id":"jef","kind":"semantic"},
        }
        epi.items.clear()
        summary = self.brain.rebuild_vector_index(owner_id="jef")
        self.assertEqual(summary, {"owners":1, "semantic":1, "episodes":1})
        self.assertNotIn("invented", sem.items)
        self.assertIn(result.memory_uuid, sem.items)
        self.assertIn(obs["episode_uuid"], epi.items)

    def test_forgotten_semantic_is_not_rebuilt(self):
        _obs, result = self.create_memory()
        control = self.brain.capture_observation(
            owner_id="jef", raw_text="Forget that.",
            external_source_ref="control", session_id="s1",
            source_kind=SourceKind.EXPLICIT_USER_MEMORY,
        )
        self.brain.forget_memory(
            owner_id="jef", memory_uuid=result.memory_uuid,
            evidence_uuid=control["evidence_uuid"],
            idempotency_key="forget",
        )
        self.brain.rebuild_vector_index(owner_id="jef")
        self.assertNotIn(result.memory_uuid, self.client.collections["test_semantic"].items)

    def test_hybrid_search_uses_real_vector(self):
        _obs, result = self.create_memory()
        hits = self.brain.search(
            owner_id="jef",
            query="fish shell",
            include_episodes=False,
        )
        self.assertTrue(any(h.uuid == result.memory_uuid for h in hits))
        with self.brain.store.read() as db:
            row = db.execute(
                "SELECT vector_used FROM recall_events WHERE owner_id='jef' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(int(row[0]), 1)

    def test_health_reports_vector_backend(self):
        health = self.brain.health()
        self.assertEqual(health["vector"], "healthy")
        self.assertEqual(health["vector_backend"], "ChromaVectorIndex")
        self.assertEqual(health["phase"], "semantic-worker-core")
        self.assertTrue(health["semantic_worker_core"])


    def test_collection_contract_mismatch_fails_closed(self):
        client = FakeChromaClient()
        client.collections["bad_sem"] = FakeCollection(
            "bad_sem",
            {
                "jarvis_brain_kind": "semantic",
                "jarvis_brain_embedding_model": "fake-multilingual",
                "jarvis_brain_embedding_contract": "old-contract",
            },
        )
        with self.assertRaisesRegex(RuntimeError, "incompatible jarvis_brain_embedding_contract"):
            ChromaVectorIndex(
                client=client,
                embedder=FakeEmbedder(),
                config=ChromaConfig(
                    semantic_collection="bad_sem",
                    episodic_collection="good_epi",
                    embedding_model="fake-multilingual",
                    embedding_contract="new-contract",
                ),
            )

    def test_health_reports_embedding_contract_identity(self):
        health = self.brain.health()
        identity = health["vector_identity"]
        self.assertEqual(identity["embedding_model"], "fake-multilingual")
        self.assertEqual(identity["embedding_contract"], "fake-embedding-contract-v1")
        self.assertEqual(identity["fastembed_version"], "0.8.0")
        self.assertEqual(identity["chromadb_version"], "1.5.9")

    def test_api_requires_long_key(self):
        with self.assertRaises(ValueError):
            start_server_in_thread(self.brain, "too-short")

    def test_health_is_public_but_status_is_authenticated(self):
        key = "x"*40
        server, thread = start_server_in_thread(self.brain, key)
        try:
            host, port = server.server_address
            status, body = http_json(f"http://{host}:{port}/health")
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            status, _ = http_json(f"http://{host}:{port}/v1/status?owner_id=jef")
            self.assertEqual(status, 401)
            status, body = http_json(f"http://{host}:{port}/v1/status?owner_id=jef", key=key)
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_api_capture_and_search(self):
        key = "k"*40
        server, thread = start_server_in_thread(self.brain, key)
        try:
            host, port = server.server_address
            base = f"http://{host}:{port}"
            status, body = http_json(
                base+"/v1/capture/observation", key=key,
                payload={
                    "owner_id":"jef", "raw_text":"I use fish shell.",
                    "external_source_ref":"api-m1", "session_id":"api-s1",
                    "source_kind":"USER_MESSAGE",
                },
            )
            self.assertEqual(status, 200)
            self.assertTrue(body["created"])
            status, body = http_json(
                base+"/v1/search", key=key,
                payload={"owner_id":"jef","query":"fish shell","limit":5},
            )
            self.assertEqual(status, 200)
            self.assertTrue(body["hits"])
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_api_replay_conflict_is_409(self):
        key = "q"*40
        server, thread = start_server_in_thread(self.brain, key)
        try:
            host, port = server.server_address
            base = f"http://{host}:{port}"
            payload = {
                "owner_id":"jef", "raw_text":"original",
                "external_source_ref":"same", "session_id":"s",
                "source_kind":"USER_MESSAGE",
            }
            self.assertEqual(http_json(base+"/v1/capture/observation", key=key, payload=payload)[0], 200)
            payload["raw_text"] = "changed"
            status, _ = http_json(base+"/v1/capture/observation", key=key, payload=payload)
            self.assertEqual(status, 409)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_api_does_not_accept_key_in_query_string(self):
        key = "z"*40
        server, thread = start_server_in_thread(self.brain, key)
        try:
            host, port = server.server_address
            status, _ = http_json(f"http://{host}:{port}/v1/status?owner_id=jef&api_key={key}")
            self.assertEqual(status, 401)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_api_bad_json_is_400(self):
        key = "a"*40
        server, thread = start_server_in_thread(self.brain, key)
        try:
            host, port = server.server_address
            req = Request(
                f"http://{host}:{port}/v1/search",
                data=b"{broken",
                headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},
                method="POST",
            )
            try:
                urlopen(req, timeout=2)
                self.fail("expected HTTPError")
            except HTTPError as exc:
                try:
                    self.assertEqual(exc.code, 400)
                finally:
                    exc.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

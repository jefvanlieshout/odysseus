from __future__ import annotations

import os
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as package_version
from typing import Protocol, Sequence


SEMANTIC_COLLECTION = "jarvis_brain_semantic_v1"
EPISODIC_COLLECTION = "jarvis_brain_episodic_v1"
DEFAULT_FASTEMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
PINNED_FASTEMBED_VERSION = "0.8.0"
PINNED_CHROMADB_VERSION = "1.5.9"
DEFAULT_EMBEDDING_CONTRACT = "fastembed-0.8.0:paraphrase-multilingual-MiniLM-L12-v2:mean-pooling:384d:v1"


class EmbeddingProvider(Protocol):
    @property
    def model_name(self) -> str: ...
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedProvider:
    """Small local embedding provider used only for derived vector indexing."""

    def __init__(self, model_name: str = DEFAULT_FASTEMBED_MODEL, cache_dir: str | None = None):
        model_name = str(model_name or "").strip() or DEFAULT_FASTEMBED_MODEL
        from fastembed import TextEmbedding

        try:
            installed = package_version("fastembed")
        except PackageNotFoundError as exc:
            raise RuntimeError("fastembed is not installed") from exc
        if installed != PINNED_FASTEMBED_VERSION:
            raise RuntimeError(
                f"fastembed runtime mismatch: expected {PINNED_FASTEMBED_VERSION}, found {installed}"
            )

        self._model_name = model_name
        self._cache_dir = cache_dir or os.environ.get("BRAIN_EMBED_CACHE") or "/data/fastembed-cache"
        self._model = TextEmbedding(model_name=model_name, cache_dir=self._cache_dir)

    @property
    def model_name(self) -> str:
        return self._model_name

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        values = list(self._model.embed(list(texts)))
        return [[float(v) for v in row] for row in values]

    def embed_query(self, text: str) -> list[float]:
        rows = self.embed_documents([text])
        if not rows:
            raise RuntimeError("embedding provider returned no query vector")
        return rows[0]


@dataclass(frozen=True)
class ChromaConfig:
    host: str = "chromadb"
    port: int = 8000
    ssl: bool = False
    semantic_collection: str = SEMANTIC_COLLECTION
    episodic_collection: str = EPISODIC_COLLECTION
    embedding_model: str = DEFAULT_FASTEMBED_MODEL
    embedding_contract: str = DEFAULT_EMBEDDING_CONTRACT

    @classmethod
    def from_env(cls) -> "ChromaConfig":
        return cls(
            host=os.environ.get("CHROMA_HOST", "chromadb"),
            port=int(os.environ.get("CHROMA_PORT", "8000")),
            ssl=os.environ.get("CHROMA_SSL", "false").strip().casefold() in {"1", "true", "yes", "on"},
            semantic_collection=os.environ.get("BRAIN_CHROMA_SEMANTIC_COLLECTION", SEMANTIC_COLLECTION),
            episodic_collection=os.environ.get("BRAIN_CHROMA_EPISODIC_COLLECTION", EPISODIC_COLLECTION),
            embedding_model=os.environ.get("BRAIN_EMBED_MODEL", DEFAULT_FASTEMBED_MODEL),
            embedding_contract=os.environ.get("BRAIN_EMBED_CONTRACT", DEFAULT_EMBEDDING_CONTRACT),
        )


class ChromaVectorIndex:
    """FastEmbed + Chroma adapter.

    Chroma is candidate-only derived state. Every search hit must still be
    resolved through owner-scoped SQLite by BrainMemoryService.
    """

    def __init__(self, *, client, embedder: EmbeddingProvider, config: ChromaConfig | None = None):
        self.client = client
        self.embedder = embedder
        self.config = config or ChromaConfig(embedding_model=embedder.model_name)
        self._collections = {
            "semantic": self._get_or_create(self.config.semantic_collection, "semantic"),
            "episode": self._get_or_create(self.config.episodic_collection, "episode"),
        }

    @classmethod
    def from_http(cls, config: ChromaConfig | None = None) -> "ChromaVectorIndex":
        config = config or ChromaConfig.from_env()
        import chromadb

        try:
            installed = package_version("chromadb")
        except PackageNotFoundError as exc:
            raise RuntimeError("chromadb is not installed") from exc
        if installed != PINNED_CHROMADB_VERSION:
            raise RuntimeError(
                f"chromadb runtime mismatch: expected {PINNED_CHROMADB_VERSION}, found {installed}"
            )

        client = chromadb.HttpClient(host=config.host, port=config.port, ssl=config.ssl)
        embedder = FastEmbedProvider(config.embedding_model)
        return cls(client=client, embedder=embedder, config=config)

    def _get_or_create(self, name: str, kind: str):
        metadata = {
            "jarvis_brain_kind": kind,
            "jarvis_brain_embedding_model": self.embedder.model_name,
            "jarvis_brain_embedding_contract": self.config.embedding_contract,
        }
        try:
            collection = self.client.get_or_create_collection(
                name=name,
                metadata=metadata,
                embedding_function=None,
                configuration={"hnsw": {"space": "cosine"}},
            )
        except TypeError:
            # Compatibility with older Chroma Python clients that predate the
            # 1.x ``configuration`` argument.
            legacy_metadata = dict(metadata)
            legacy_metadata["hnsw:space"] = "cosine"
            collection = self.client.get_or_create_collection(
                name=name,
                metadata=legacy_metadata,
                embedding_function=None,
            )

        actual = dict(getattr(collection, "metadata", None) or {})
        for key in (
            "jarvis_brain_kind",
            "jarvis_brain_embedding_model",
            "jarvis_brain_embedding_contract",
        ):
            if actual.get(key) != metadata.get(key):
                raise RuntimeError(
                    f"Chroma collection {name!r} has incompatible {key}: "
                    f"expected {metadata.get(key)!r}, found {actual.get(key)!r}"
                )
        return collection

    @property
    def identity(self) -> dict:
        return {
            "embedding_model": self.embedder.model_name,
            "embedding_contract": self.config.embedding_contract,
            "fastembed_version": PINNED_FASTEMBED_VERSION,
            "chromadb_version": PINNED_CHROMADB_VERSION,
            "semantic_collection": self.config.semantic_collection,
            "episodic_collection": self.config.episodic_collection,
        }

    @property
    def healthy(self) -> bool:
        try:
            heartbeat = getattr(self.client, "heartbeat", None)
            if callable(heartbeat):
                heartbeat()
            for collection in self._collections.values():
                collection.count()
            return True
        except Exception:
            return False

    def _collection(self, kind: str):
        if kind not in self._collections:
            raise ValueError(f"unsupported vector kind: {kind}")
        return self._collections[kind]

    def upsert(self, *, owner_id: str, kind: str, uuid: str, text: str) -> None:
        owner_id = str(owner_id or "").strip()
        uuid = str(uuid or "").strip()
        text = str(text or "").strip()
        if not owner_id or not uuid or not text:
            raise ValueError("owner_id, uuid and text are required")
        embedding = self.embedder.embed_documents([text])[0]
        self._collection(kind).upsert(
            ids=[uuid],
            embeddings=[embedding],
            documents=[text],
            metadatas=[{
                "owner_id": owner_id,
                "kind": kind,
                "embedding_model": self.embedder.model_name,
                "embedding_contract": self.config.embedding_contract,
            }],
        )

    def delete(self, *, owner_id: str, kind: str, uuid: str) -> None:
        collection = self._collection(kind)
        current = collection.get(ids=[uuid], include=["metadatas"])
        ids = list(current.get("ids") or [])
        metas = list(current.get("metadatas") or [])
        for idx, item_id in enumerate(ids):
            meta = metas[idx] if idx < len(metas) else {}
            if item_id == uuid and (meta or {}).get("owner_id") == owner_id:
                collection.delete(ids=[uuid])
                return

    def clear_owner(self, *, owner_id: str, kinds: Sequence[str]) -> None:
        for kind in kinds:
            collection = self._collection(kind)
            while True:
                current = collection.get(
                    where={"owner_id": owner_id},
                    include=["metadatas"],
                    limit=500,
                    offset=0,
                )
                ids = list(current.get("ids") or [])
                if not ids:
                    break
                collection.delete(ids=ids)

    def search(self, *, owner_id: str, query: str, kinds: Sequence[str], limit: int) -> list[tuple[str, str, float]]:
        query = str(query or "").strip()
        if not query:
            return []
        query_embedding = self.embedder.embed_query(query)
        rows: list[tuple[str, str, float]] = []
        per_kind = max(1, int(limit))
        for kind in kinds:
            collection = self._collection(kind)
            count = int(collection.count())
            if count <= 0:
                continue
            result = collection.query(
                query_embeddings=[query_embedding],
                n_results=min(per_kind, count),
                where={"owner_id": owner_id},
                include=["distances", "metadatas"],
            )
            ids = (result.get("ids") or [[]])[0]
            distances = (result.get("distances") or [[]])[0]
            metas = (result.get("metadatas") or [[]])[0]
            for idx, item_uuid in enumerate(ids):
                meta = metas[idx] if idx < len(metas) else {}
                if (meta or {}).get("owner_id") != owner_id:
                    continue
                if (meta or {}).get("kind") not in {None, kind}:
                    continue
                distance = float(distances[idx]) if idx < len(distances) else 1.0
                similarity = max(0.0, min(1.0, 1.0 - distance))
                rows.append((kind, str(item_uuid), similarity))
        rows.sort(key=lambda row: (row[2], row[1]), reverse=True)
        return rows[:max(1, int(limit))]

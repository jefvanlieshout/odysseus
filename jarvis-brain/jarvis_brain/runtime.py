from __future__ import annotations

import os

from .retrieval import NullVectorIndex
from .vector_chroma import ChromaConfig, ChromaVectorIndex


def build_vector_index_from_env():
    backend = os.environ.get("BRAIN_VECTOR_BACKEND", "chroma").strip().casefold()
    if backend in {"", "none", "off", "disabled", "null"}:
        return NullVectorIndex("vector backend disabled")

    if backend != "chroma":
        return NullVectorIndex(f"unsupported vector backend: {backend}")

    try:
        return ChromaVectorIndex.from_http(ChromaConfig.from_env())
    except Exception as exc:
        return NullVectorIndex(f"chroma unavailable: {type(exc).__name__}: {exc}")

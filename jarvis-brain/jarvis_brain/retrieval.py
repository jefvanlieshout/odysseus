from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Protocol, Sequence

from .types import SearchHit

_TOKEN_RE = re.compile(r"[\w.+-]{2,}", re.UNICODE)
_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "what", "when",
    "where", "which", "you", "your", "have", "has", "had", "are", "was",
    "were", "about", "into", "our",
}


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((text or "").casefold()) if t not in _STOP]


def lexical_score(query: str, text: str) -> float:
    q = tokenize(query)
    if not q:
        return 0.0
    t = tokenize(text)
    if not t:
        return 0.0
    counts = Counter(t)
    unique_q = list(dict.fromkeys(q))
    matched = sum(1 for token in unique_q if token in counts)
    if not matched:
        return 0.0
    coverage = matched / len(unique_q)
    frequency = sum(min(counts[token], 3) for token in unique_q) / max(len(t), 1)
    phrase = 0.75 if query.casefold().strip() in text.casefold() else 0.0
    return round(coverage * 7.0 + math.log1p(frequency * 30.0) + phrase, 6)


class VectorIndex(Protocol):
    @property
    def healthy(self) -> bool: ...

    def search(self, *, owner_id: str, query: str, kinds: Sequence[str], limit: int) -> list[tuple[str, str, float]]:
        """Return authoritative-record UUID candidates and similarity scores."""
        ...

    def upsert(self, *, owner_id: str, kind: str, uuid: str, text: str) -> None: ...
    def delete(self, *, owner_id: str, kind: str, uuid: str) -> None: ...
    def clear_owner(self, *, owner_id: str, kinds: Sequence[str]) -> None: ...


@dataclass
class NullVectorIndex:
    reason: str = "vector index not configured"

    @property
    def healthy(self) -> bool:
        return False

    def search(self, *, owner_id: str, query: str, kinds: Sequence[str], limit: int) -> list[tuple[str, str, float]]:
        return []

    def upsert(self, *, owner_id: str, kind: str, uuid: str, text: str) -> None:
        return None

    def delete(self, *, owner_id: str, kind: str, uuid: str) -> None:
        return None

    def clear_owner(self, *, owner_id: str, kinds: Sequence[str]) -> None:
        return None

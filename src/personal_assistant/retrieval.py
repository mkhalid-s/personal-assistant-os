from __future__ import annotations

import math
import re
import zlib
from collections import Counter
from typing import Protocol, runtime_checkable


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def lexical_score(query: str, text: str) -> float:
    q = tokenize(query)
    t = tokenize(text)
    if not q or not t:
        return 0.0
    tc = Counter(t)
    return sum(tc.get(tok, 0) for tok in q) / max(len(q), 1)


def embed_text(text: str, dims: int = 64) -> list[float]:
    vec = [0.0] * dims
    tokens = tokenize(text)
    if not tokens:
        return vec
    for token in tokens:
        # zlib.crc32 is stable across processes; builtin hash() is salted per-process
        # (PYTHONHASHSEED), which made retrieval/analogy scores non-deterministic.
        vec[zlib.crc32(token.encode("utf-8")) % dims] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"vector dimension mismatch: {len(a)} vs {len(b)}")
    return sum(x * y for x, y in zip(a, b, strict=False))


# ---------------------------------------------------------------------------
# Embedding backend — pluggable seam
#
# Default: _HashBackend (wraps the existing embed_text() — identical output,
# zero behavior change). Register a real backend with set_embedding_backend()
# to get semantic embeddings. hybrid_score() picks up the change automatically.
# ---------------------------------------------------------------------------

@runtime_checkable
class EmbeddingBackend(Protocol):
    """Minimal protocol for an embedding backend."""

    @property
    def dims(self) -> int: ...

    def embed(self, text: str) -> list[float]: ...


class _HashBackend:
    """Fallback backend using the zlib-crc32 hashing trick. No external deps."""

    dims: int = 64

    def embed(self, text: str) -> list[float]:
        return embed_text(text, dims=self.dims)


_backend: EmbeddingBackend = _HashBackend()


def set_embedding_backend(backend: EmbeddingBackend) -> None:
    """Register a real embedding backend. Call once at startup."""
    global _backend
    _backend = backend


def get_embedding_backend() -> EmbeddingBackend:
    """Return the currently registered backend."""
    return _backend


def is_semantic_backend() -> bool:
    """True when a non-hash backend is registered (useful for logging/doctor checks)."""
    return not isinstance(_backend, _HashBackend)


def hybrid_score(
    query: str,
    text: str,
    *,
    lexical_w: float = 0.45,
    semantic_w: float = 0.55,
) -> float:
    """Combine lexical and semantic scores. Weights default to values calibrated
    for the hash backend; callers using a real embedding backend should pass
    lexical_w=0.30, semantic_w=0.70 for better results."""
    b = get_embedding_backend()
    lex = lexical_score(query, text)
    sem = cosine_similarity(b.embed(query), b.embed(text))
    return lexical_w * lex + semantic_w * sem

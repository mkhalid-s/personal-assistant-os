from __future__ import annotations

import math
import re
import zlib
from collections import Counter


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
    return sum(x * y for x, y in zip(a, b))


def hybrid_score(query: str, text: str) -> float:
    lex = lexical_score(query, text)
    sem = cosine_similarity(embed_text(query), embed_text(text))
    return 0.45 * lex + 0.55 * sem

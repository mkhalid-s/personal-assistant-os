"""Embedding backend implementations for MYOS retrieval.

Usage:
    from personal_assistant.embedding_backends import load_best_available
    load_best_available(conn)   # call once at startup, after initialize_schema

The module registers the best available backend on the module-level seam in
retrieval.py. All callers of hybrid_score() / get_embedding_backend() pick up
the change automatically — no further wiring needed.

Install the embed extra for real semantic embeddings:
    pip install personal-assistant-os[embed]
Without it the hash fallback remains active and the doctor check reports a
warning (non-fatal — the system works, just with hash-based similarity).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .db import append_event
from .retrieval import (
    EmbeddingBackend,
    _HashBackend,
    embed_text,
    get_embedding_backend,
    is_semantic_backend,
    set_embedding_backend,
)

_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
_FASTEMBED_DIMS = 384


class FastEmbedBackend:
    """EmbeddingBackend backed by fastembed (ONNX, no torch required).

    Install: pip install personal-assistant-os[embed]
    Model:   BAAI/bge-small-en-v1.5 (133 MB, downloaded on first use,
             cached at ~/.cache/fastembed/)
    """

    def __init__(self, model: Any) -> None:
        self._model = model
        self.dims: int = _FASTEMBED_DIMS

    def embed(self, text: str) -> list[float]:
        # fastembed.TextEmbedding.embed() yields numpy arrays; convert to list.
        result = list(self._model.embed([text or " "]))
        vec = result[0]
        try:
            return [float(v) for v in vec]
        except TypeError:
            return list(vec.tolist())


def load_best_available(conn: sqlite3.Connection) -> EmbeddingBackend:
    """Register the best available embedding backend and return it.

    Tries fastembed first. Falls back to the hash backend when the optional
    dep is absent. Logs which backend was selected via observability so the
    choice is visible in event_log without adding any stdout noise.
    """
    try:
        from fastembed import TextEmbedding  # type: ignore[import-untyped]

        model = TextEmbedding(_FASTEMBED_MODEL)
        backend: EmbeddingBackend = FastEmbedBackend(model)
        set_embedding_backend(backend)
        append_event(
            conn,
            "embedding_backend_loaded",
            "system",
            0,
            json.dumps({"backend": "fastembed", "model": _FASTEMBED_MODEL, "dims": _FASTEMBED_DIMS}),
        )
        return backend
    except ImportError:
        append_event(
            conn,
            "embedding_backend_loaded",
            "system",
            0,
            json.dumps({"backend": "hash", "reason": "fastembed not installed"}),
        )
        return get_embedding_backend()  # _HashBackend already registered


def embedding_doctor_check() -> tuple[bool, str]:
    """Return (ok, detail) for the myos doctor embedding check.

    ok=True when a real semantic backend is active.
    ok=False (warning, non-fatal) when the hash fallback is in use.
    """
    if is_semantic_backend():
        b = get_embedding_backend()
        return True, f"fastembed active — model={_FASTEMBED_MODEL} dims={b.dims}"
    return (
        False,
        (
            "hash fallback active — semantic retrieval is non-meaningful. "
            "Run: pip install personal-assistant-os[embed]"
        ),
    )


def embed_and_cache(
    conn: sqlite3.Connection,
    source_type: str,
    source_id: str,
    content: str,
) -> bool:
    """Compute and persist an embedding for (source_type, source_id).

    Returns True if a new or updated embedding was written, False if the
    cached embedding is already current (content_hash matches).

    Skips silently when the hash backend is active — hash embeddings are
    cheap to recompute on demand and not worth persisting (they carry no
    semantic signal and would waste 64 × 8 bytes per chunk for nothing).
    """
    if not is_semantic_backend():
        return False

    content_hash = hashlib.sha256((content or "").encode("utf-8")).hexdigest()
    existing = conn.execute(
        "SELECT content_hash FROM embedding_cache WHERE source_type=? AND source_id=?",
        (source_type, str(source_id)),
    ).fetchone()
    if existing and existing["content_hash"] == content_hash:
        return False  # already current

    b = get_embedding_backend()
    vec = b.embed(content or " ")
    blob = json.dumps(vec, separators=(",", ":"))
    conn.execute(
        """
        INSERT INTO embedding_cache (source_type, source_id, content_hash, model_name, embedding_blob, dims)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_type, source_id) DO UPDATE SET
            content_hash   = excluded.content_hash,
            model_name     = excluded.model_name,
            embedding_blob = excluded.embedding_blob,
            dims           = excluded.dims,
            created_at     = CURRENT_TIMESTAMP
        """,
        (source_type, str(source_id), content_hash, _FASTEMBED_MODEL, blob, b.dims),
    )
    return True


def load_cached_embedding(
    conn: sqlite3.Connection,
    source_type: str,
    source_id: str,
) -> list[float] | None:
    """Load a persisted embedding vector. Returns None on cache miss."""
    row = conn.execute(
        "SELECT embedding_blob FROM embedding_cache WHERE source_type=? AND source_id=?",
        (source_type, str(source_id)),
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["embedding_blob"])
    except (TypeError, ValueError):
        return None


def load_cached_embeddings_bulk(
    conn: sqlite3.Connection,
    keys: list[tuple[str, str]],
) -> dict[tuple[str, str], list[float]]:
    """Load multiple embeddings in one query. Returns {(source_type, source_id): vec}."""
    if not keys:
        return {}
    placeholders = ",".join("(?,?)" for _ in keys)
    flat = [v for pair in keys for v in pair]
    rows = conn.execute(
        f"SELECT source_type, source_id, embedding_blob FROM embedding_cache "  # noqa: S608
        f"WHERE (source_type, source_id) IN (VALUES {placeholders})",
        flat,
    ).fetchall()
    result: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        try:
            result[(row["source_type"], row["source_id"])] = json.loads(row["embedding_blob"])
        except (TypeError, ValueError):
            pass
    return result

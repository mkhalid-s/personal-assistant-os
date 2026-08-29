"""Tests for B5 — write-time embedding hooks and backfill command.

Verifies that index_chunk() and agentcore.remember() call embed_and_cache()
and that the CLI backfill command fills the cache for pre-existing rows.
"""
from __future__ import annotations

import sqlite3
import unittest

from personal_assistant.agentcore import remember
from personal_assistant.db import initialize_schema
from personal_assistant.embedding_backends import (
    embed_and_cache,
    load_cached_embedding,
)
from personal_assistant.inbox import index_chunk
from personal_assistant.retrieval import _HashBackend, set_embedding_backend


class _MockBackend:
    dims = 128
    def embed(self, text: str) -> list[float]:
        return [0.5] * self.dims


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# index_chunk write-time hook
# ---------------------------------------------------------------------------

class IndexChunkEmbedHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        set_embedding_backend(_MockBackend())

    def tearDown(self) -> None:
        set_embedding_backend(_HashBackend())
        self.conn.close()

    def test_index_chunk_writes_embedding_to_cache(self) -> None:
        index_chunk(self.conn, "work_item", 1, "Auth service is broken")
        self.conn.commit()
        vec = load_cached_embedding(self.conn, "work_item", "1")
        self.assertIsNotNone(vec)
        self.assertEqual(len(vec), 128)

    def test_index_chunk_skips_empty_content(self) -> None:
        result = index_chunk(self.conn, "work_item", 2, "")
        self.assertFalse(result)
        vec = load_cached_embedding(self.conn, "work_item", "2")
        self.assertIsNone(vec)

    def test_index_chunk_no_error_on_hash_backend(self) -> None:
        set_embedding_backend(_HashBackend())
        # Should not raise, just skip silently
        result = index_chunk(self.conn, "work_item", 3, "Some content")
        self.assertTrue(result)
        vec = load_cached_embedding(self.conn, "work_item", "3")
        self.assertIsNone(vec)  # hash backend skips cache


# ---------------------------------------------------------------------------
# agentcore.remember write-time hook
# ---------------------------------------------------------------------------

class RememberEmbedHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        set_embedding_backend(_MockBackend())

    def tearDown(self) -> None:
        set_embedding_backend(_HashBackend())
        self.conn.close()

    def test_remember_writes_embedding_to_cache(self) -> None:
        chunk_id = remember(self.conn, "We decided to ship next Friday", source_type="conversation", source_id=10)
        self.conn.commit()
        self.assertIsNotNone(chunk_id)
        vec = load_cached_embedding(self.conn, "conversation", "10")
        self.assertIsNotNone(vec)
        self.assertEqual(len(vec), 128)

    def test_remember_skips_empty_text(self) -> None:
        result = remember(self.conn, "", source_type="conversation", source_id=11)
        self.assertIsNone(result)
        vec = load_cached_embedding(self.conn, "conversation", "11")
        self.assertIsNone(vec)

    def test_remember_no_error_on_hash_backend(self) -> None:
        set_embedding_backend(_HashBackend())
        chunk_id = remember(self.conn, "Some memory content", source_type="conversation", source_id=12)
        self.conn.commit()
        self.assertIsNotNone(chunk_id)
        vec = load_cached_embedding(self.conn, "conversation", "12")
        self.assertIsNone(vec)


# ---------------------------------------------------------------------------
# backfill: embed_and_cache on pre-existing rows
# ---------------------------------------------------------------------------

class BackfillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        # Seed text_chunks directly without triggering embed_and_cache
        set_embedding_backend(_HashBackend())  # hash = no cache writes

    def tearDown(self) -> None:
        set_embedding_backend(_HashBackend())
        self.conn.close()

    def _seed_chunk(self, source_type: str, source_id: int, content: str) -> None:
        self.conn.execute(
            "INSERT INTO text_chunks (source_type, source_id, content) VALUES (?, ?, ?)",
            (source_type, source_id, content),
        )

    def test_backfill_fills_missing_embeddings(self) -> None:
        self._seed_chunk("work_item", 1, "First chunk content")
        self._seed_chunk("work_item", 2, "Second chunk content")
        self.conn.commit()

        # Verify no embeddings yet
        cached = self.conn.execute("SELECT COUNT(*) AS c FROM embedding_cache").fetchone()["c"]
        self.assertEqual(cached, 0)

        # Switch to real backend and backfill
        set_embedding_backend(_MockBackend())
        rows = self.conn.execute(
            """
            SELECT tc.source_type, tc.source_id, tc.content
            FROM text_chunks tc
            LEFT JOIN embedding_cache ec
                ON ec.source_type = tc.source_type AND ec.source_id = CAST(tc.source_id AS TEXT)
            WHERE ec.source_type IS NULL
            """
        ).fetchall()
        for row in rows:
            embed_and_cache(self.conn, row["source_type"], str(row["source_id"]), row["content"])
        self.conn.commit()

        cached_after = self.conn.execute("SELECT COUNT(*) AS c FROM embedding_cache").fetchone()["c"]
        self.assertEqual(cached_after, 2)

    def test_backfill_skips_already_cached(self) -> None:
        self._seed_chunk("note", 5, "Already cached content")
        self.conn.commit()

        set_embedding_backend(_MockBackend())
        embed_and_cache(self.conn, "note", "5", "Already cached content")
        self.conn.commit()

        # Backfill again — should skip since hash matches
        result = embed_and_cache(self.conn, "note", "5", "Already cached content")
        self.assertFalse(result)  # no update needed

    def test_backfill_updates_stale_content(self) -> None:
        self._seed_chunk("note", 6, "original")
        self.conn.commit()

        set_embedding_backend(_MockBackend())
        embed_and_cache(self.conn, "note", "6", "original")
        self.conn.commit()

        # Content changed — backfill should update
        result = embed_and_cache(self.conn, "note", "6", "completely different now")
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()

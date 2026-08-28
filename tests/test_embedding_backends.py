"""Tests for embedding_backends.py — B3 of P4 real-embeddings plan.

All tests use mock backends; fastembed is NOT required to run the suite.
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from personal_assistant.db import initialize_schema
from personal_assistant.embedding_backends import (
    embed_and_cache,
    embedding_doctor_check,
    load_cached_embedding,
    load_cached_embeddings_bulk,
    load_best_available,
)
from personal_assistant.retrieval import (
    _HashBackend,
    get_embedding_backend,
    is_semantic_backend,
    set_embedding_backend,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


class _MockBackend:
    """Minimal EmbeddingBackend for testing — returns fixed-dim zero vector."""
    dims = 384
    def embed(self, text: str) -> list[float]:
        return [0.1] * self.dims


# ---------------------------------------------------------------------------
# load_best_available
# ---------------------------------------------------------------------------

class LoadBestAvailableTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        set_embedding_backend(_HashBackend())

    def tearDown(self) -> None:
        set_embedding_backend(_HashBackend())
        self.conn.close()

    def test_returns_hash_backend_when_fastembed_not_installed(self) -> None:
        with patch.dict("sys.modules", {"fastembed": None}):
            backend = load_best_available(self.conn)
        self.assertIsInstance(backend, _HashBackend)
        self.assertFalse(is_semantic_backend())

    def test_registers_fastembed_backend_when_available(self) -> None:
        mock_model = MagicMock()
        mock_model.embed.return_value = [[0.1] * 384]
        mock_fastembed = MagicMock()
        mock_fastembed.TextEmbedding.return_value = mock_model

        with patch.dict("sys.modules", {"fastembed": mock_fastembed}):
            backend = load_best_available(self.conn)

        self.assertTrue(is_semantic_backend())
        self.assertEqual(backend.dims, 384)

    def test_logs_backend_choice_to_event_log(self) -> None:
        with patch.dict("sys.modules", {"fastembed": None}):
            load_best_available(self.conn)
        events = self.conn.execute(
            "SELECT payload FROM event_log WHERE event_type='embedding_backend_loaded'"
        ).fetchall()
        self.assertGreater(len(events), 0)
        data = json.loads(events[0]["payload"])
        self.assertEqual(data["backend"], "hash")

    def test_import_error_leaves_hash_backend_registered(self) -> None:
        with patch.dict("sys.modules", {"fastembed": None}):
            load_best_available(self.conn)
        self.assertIsInstance(get_embedding_backend(), _HashBackend)


# ---------------------------------------------------------------------------
# embedding_doctor_check
# ---------------------------------------------------------------------------

class EmbeddingDoctorCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        set_embedding_backend(_HashBackend())

    def tearDown(self) -> None:
        set_embedding_backend(_HashBackend())

    def test_hash_backend_returns_not_ok(self) -> None:
        ok, detail = embedding_doctor_check()
        self.assertFalse(ok)
        self.assertIn("hash fallback", detail)
        self.assertIn("pip install", detail)

    def test_real_backend_returns_ok(self) -> None:
        set_embedding_backend(_MockBackend())
        ok, detail = embedding_doctor_check()
        self.assertTrue(ok)
        self.assertIn("fastembed", detail)


# ---------------------------------------------------------------------------
# embed_and_cache
# ---------------------------------------------------------------------------

class EmbedAndCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        set_embedding_backend(_MockBackend())

    def tearDown(self) -> None:
        set_embedding_backend(_HashBackend())
        self.conn.close()

    def test_writes_embedding_to_cache(self) -> None:
        result = embed_and_cache(self.conn, "work_item", "42", "Auth is broken")
        self.conn.commit()
        self.assertTrue(result)
        row = self.conn.execute(
            "SELECT * FROM embedding_cache WHERE source_type='work_item' AND source_id='42'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["dims"], 384)

    def test_skips_when_content_hash_matches(self) -> None:
        embed_and_cache(self.conn, "work_item", "42", "same content")
        self.conn.commit()
        result = embed_and_cache(self.conn, "work_item", "42", "same content")
        self.assertFalse(result)  # no update needed

    def test_updates_when_content_changes(self) -> None:
        embed_and_cache(self.conn, "work_item", "42", "original content")
        self.conn.commit()
        result = embed_and_cache(self.conn, "work_item", "42", "changed content")
        self.assertTrue(result)

    def test_skips_silently_on_hash_backend(self) -> None:
        set_embedding_backend(_HashBackend())
        result = embed_and_cache(self.conn, "work_item", "99", "some content")
        self.assertFalse(result)
        row = self.conn.execute(
            "SELECT 1 FROM embedding_cache WHERE source_id='99'"
        ).fetchone()
        self.assertIsNone(row)

    def test_stores_correct_content_hash(self) -> None:
        import hashlib
        content = "The build is blocked"
        embed_and_cache(self.conn, "note", "1", content)
        self.conn.commit()
        row = self.conn.execute(
            "SELECT content_hash FROM embedding_cache WHERE source_id='1'"
        ).fetchone()
        expected = hashlib.sha256(content.encode()).hexdigest()
        self.assertEqual(row["content_hash"], expected)

    def test_upsert_replaces_old_embedding(self) -> None:
        embed_and_cache(self.conn, "note", "5", "first")
        self.conn.commit()
        embed_and_cache(self.conn, "note", "5", "second — different")
        self.conn.commit()
        count = self.conn.execute(
            "SELECT COUNT(*) as c FROM embedding_cache WHERE source_type='note' AND source_id='5'"
        ).fetchone()["c"]
        self.assertEqual(count, 1)


# ---------------------------------------------------------------------------
# load_cached_embedding / load_cached_embeddings_bulk
# ---------------------------------------------------------------------------

class LoadCachedEmbeddingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        set_embedding_backend(_MockBackend())

    def tearDown(self) -> None:
        set_embedding_backend(_HashBackend())
        self.conn.close()

    def test_returns_none_on_cache_miss(self) -> None:
        self.assertIsNone(load_cached_embedding(self.conn, "work_item", "99"))

    def test_returns_vector_on_cache_hit(self) -> None:
        embed_and_cache(self.conn, "work_item", "1", "content")
        self.conn.commit()
        vec = load_cached_embedding(self.conn, "work_item", "1")
        self.assertIsNotNone(vec)
        self.assertEqual(len(vec), 384)

    def test_bulk_load_returns_all_hits(self) -> None:
        embed_and_cache(self.conn, "work_item", "10", "first")
        embed_and_cache(self.conn, "work_item", "11", "second")
        self.conn.commit()
        result = load_cached_embeddings_bulk(
            self.conn, [("work_item", "10"), ("work_item", "11"), ("work_item", "99")]
        )
        self.assertIn(("work_item", "10"), result)
        self.assertIn(("work_item", "11"), result)
        self.assertNotIn(("work_item", "99"), result)

    def test_bulk_load_empty_keys_returns_empty(self) -> None:
        self.assertEqual(load_cached_embeddings_bulk(self.conn, []), {})


if __name__ == "__main__":
    unittest.main()

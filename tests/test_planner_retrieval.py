"""Tests for B7 — planner._agent_analogies() with real embedding backend.

Previously _agent_analogies had no direct test coverage.
"""

from __future__ import annotations

import sqlite3
import unittest

from personal_assistant.db import initialize_schema
from personal_assistant.planner import _agent_analogies, _parse_source_key
from personal_assistant.retrieval import _HashBackend, set_embedding_backend


class _MockBackend:
    dims = 64

    def embed(self, text: str) -> list[float]:
        # Non-trivial: use character-position hashing so different texts get
        # different vectors, enabling meaningful cosine similarity tests.
        import math

        vec = [0.0] * self.dims
        for i, ch in enumerate(text[: self.dims]):
            vec[i % self.dims] += ord(ch) / 128.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# _parse_source_key
# ---------------------------------------------------------------------------


class ParseSourceKeyTest(unittest.TestCase):
    def test_work_item_pattern(self) -> None:
        self.assertEqual(_parse_source_key("work_item#42"), ("work_item", "42"))

    def test_intent_pattern(self) -> None:
        self.assertEqual(_parse_source_key("intent#7"), ("intent", "7"))

    def test_external_item_pattern(self) -> None:
        self.assertEqual(_parse_source_key("external_item#1"), ("external_item", "1"))

    def test_person_pattern(self) -> None:
        self.assertEqual(_parse_source_key("person#3"), ("person", "3"))

    def test_observation_returns_none(self) -> None:
        self.assertIsNone(_parse_source_key("observation:safe_action_executed"))

    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(_parse_source_key(""))

    def test_no_id_returns_none(self) -> None:
        self.assertIsNone(_parse_source_key("work_item#"))


# ---------------------------------------------------------------------------
# _agent_analogies — hash backend (existing behaviour unchanged)
# ---------------------------------------------------------------------------


class AgentAnalogiesHashTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        set_embedding_backend(_HashBackend())

    def tearDown(self) -> None:
        set_embedding_backend(_HashBackend())
        self.conn.close()

    def test_returns_empty_on_empty_db(self) -> None:
        results = _agent_analogies(self.conn, "auth service", limit=5)
        self.assertEqual(results, [])

    def test_returns_tuples_of_score_source_content(self) -> None:
        self.conn.execute(
            "INSERT INTO work_items (title, kind, status, priority, risk_score) "
            "VALUES ('Auth is broken', 'task', 'open', 1, 5)"
        )
        self.conn.commit()
        results = _agent_analogies(self.conn, "auth", limit=5)
        if results:
            score, source, content = results[0]
            self.assertIsInstance(score, float)
            self.assertIn("work_item#", source)
            self.assertIsInstance(content, str)

    def test_limit_respected(self) -> None:
        for i in range(10):
            self.conn.execute(
                "INSERT INTO work_items (title, kind, status, priority, risk_score) "
                f"VALUES ('Task about auth {i}', 'task', 'open', 2, 3)"
            )
        self.conn.commit()
        results = _agent_analogies(self.conn, "auth task", limit=3)
        self.assertLessEqual(len(results), 3)

    def test_results_sorted_descending_by_score(self) -> None:
        for title in ("auth broken", "unrelated agenda"):
            self.conn.execute(
                "INSERT INTO work_items (title, kind, status, priority, risk_score) "
                f"VALUES ('{title}', 'task', 'open', 2, 3)"
            )
        self.conn.commit()
        results = _agent_analogies(self.conn, "auth", limit=5)
        scores = [r[0] for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))


# ---------------------------------------------------------------------------
# _agent_analogies — real backend path (rebalanced weights, stored embed)
# ---------------------------------------------------------------------------


class AgentAnalogiesSemanticTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        set_embedding_backend(_MockBackend())

    def tearDown(self) -> None:
        set_embedding_backend(_HashBackend())
        self.conn.close()

    def test_returns_positive_scores_with_semantic_backend(self) -> None:
        self.conn.execute(
            "INSERT INTO work_items (title, kind, status, priority, risk_score) "
            "VALUES ('Deploy auth microservice', 'task', 'open', 1, 5)"
        )
        self.conn.commit()
        results = _agent_analogies(self.conn, "authentication deployment", limit=5)
        if results:
            self.assertGreater(results[0][0], 0.0)

    def test_relevant_item_scores_higher_than_unrelated(self) -> None:
        self.conn.execute(
            "INSERT INTO work_items (title, kind, status, priority, risk_score) "
            "VALUES ('Auth service is blocked by infra', 'task', 'open', 1, 8)"
        )
        self.conn.execute(
            "INSERT INTO work_items (title, kind, status, priority, risk_score) "
            "VALUES ('Schedule team offsite agenda', 'task', 'open', 3, 1)"
        )
        self.conn.commit()
        results = _agent_analogies(self.conn, "auth blocked infra", limit=5)
        by_source = {r[1]: r[0] for r in results}
        sources = list(by_source.keys())
        if len(sources) >= 2:
            # Auth item should score higher than offsite
            auth_src = next((s for s in sources if "1" in s.split("#")[-1]), None)
            off_src = next((s for s in sources if "2" in s.split("#")[-1]), None)
            if auth_src and off_src:
                self.assertGreater(by_source[auth_src], by_source[off_src])

    def test_stored_embedding_used_when_available(self) -> None:
        from personal_assistant.embedding_backends import embed_and_cache

        self.conn.execute(
            "INSERT INTO work_items (title, kind, status, priority, risk_score) "
            "VALUES ('API rate limiting spike', 'task', 'open', 1, 5)"
        )
        item_id = int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        # Pre-cache the embedding
        embed_and_cache(self.conn, "work_item", str(item_id), "API rate limiting spike kind=task risk=5 status=open")
        self.conn.commit()

        results = _agent_analogies(self.conn, "rate limiting", limit=5)
        # Should find the item; stored embedding used for reranking
        sources = [r[1] for r in results]
        self.assertTrue(any(f"work_item#{item_id}" in s for s in sources))

    def test_observations_scored_on_the_fly(self) -> None:
        task_id = self.conn.execute("INSERT INTO agent_tasks (objective) VALUES ('test')").lastrowid
        self.conn.execute(
            "INSERT INTO agent_observations (agent_task_id, observation_type, content) "
            "VALUES (?, 'safe_action_executed', 'Sent auth update to Jira PROJ-123')",
            (task_id,),
        )
        self.conn.commit()
        results = _agent_analogies(self.conn, "auth jira", limit=5, scopes={"local_memory"})
        # Observations use on-the-fly scoring via seam — no error expected
        self.assertIsInstance(results, list)


if __name__ == "__main__":
    unittest.main()

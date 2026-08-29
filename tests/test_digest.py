from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from personal_assistant.db import initialize_schema
from personal_assistant.digest import list_digests, maybe_generate_and_record, record_digest


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


def _seed_task(conn: sqlite3.Connection, objective: str = "test goal") -> int:
    task_id = conn.execute("INSERT INTO agent_tasks (objective) VALUES (?)", (objective,)).lastrowid
    conn.commit()
    return task_id


def _seed_observations(conn: sqlite3.Connection, task_id: int, n: int = 3) -> None:
    for i in range(n):
        conn.execute(
            "INSERT INTO agent_observations (agent_task_id, observation_type, content) VALUES (?,?,?)",
            (task_id, "safe_action_executed", f"Action {i} completed successfully"),
        )
    conn.commit()


class RecordDigestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_writes_to_assistant_digests(self) -> None:
        task_id = _seed_task(self.conn)
        did = record_digest(self.conn, task_id, "Test title", "Auth service was blocked.")
        self.conn.commit()
        self.assertIsNotNone(did)
        row = self.conn.execute("SELECT * FROM assistant_digests WHERE id=?", (did,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "Test title")
        self.assertEqual(row["source_type"], "autonomy_task")
        self.assertEqual(row["source_id"], task_id)

    def test_indexes_in_text_chunks(self) -> None:
        task_id = _seed_task(self.conn)
        record_digest(self.conn, task_id, "Digest", "Deployed auth service successfully.")
        self.conn.commit()
        chunk = self.conn.execute("SELECT content FROM text_chunks WHERE source_type='digest'").fetchone()
        self.assertIsNotNone(chunk)
        self.assertIn("auth", chunk["content"].lower())

    def test_returns_none_on_empty_body(self) -> None:
        task_id = _seed_task(self.conn)
        result = record_digest(self.conn, task_id, "Title", "")
        self.assertIsNone(result)

    def test_applies_privacy_filter(self) -> None:
        task_id = _seed_task(self.conn)
        did = record_digest(self.conn, task_id, "Digest", "Key sk-ABCDEFGHIJKLMNOPQRSTUV was used")
        self.conn.commit()
        row = self.conn.execute("SELECT body FROM assistant_digests WHERE id=?", (did,)).fetchone()
        self.assertNotIn("sk-ABCDEFGHIJKLMNOPQRSTUV", row["body"])
        self.assertIn("[REDACTED_SECRET]", row["body"])

    def test_custom_source_type(self) -> None:
        task_id = _seed_task(self.conn)
        did = record_digest(self.conn, task_id, "T", "Body.", source_type="factory_run")
        self.conn.commit()
        row = self.conn.execute("SELECT source_type FROM assistant_digests WHERE id=?", (did,)).fetchone()
        self.assertEqual(row["source_type"], "factory_run")


class MaybeGenerateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_returns_none_when_no_observations(self) -> None:
        task_id = _seed_task(self.conn)
        result = maybe_generate_and_record(self.conn, task_id, "goal", backend_name="claude")
        self.assertIsNone(result)

    def test_returns_none_when_backend_unavailable(self) -> None:
        task_id = _seed_task(self.conn)
        _seed_observations(self.conn, task_id)
        mock_backend = unittest.mock.MagicMock()
        mock_backend.available.return_value = (False, "not configured")
        with patch("personal_assistant.providers.get_backend", return_value=mock_backend):
            result = maybe_generate_and_record(self.conn, task_id, "goal")
        self.assertIsNone(result)

    def test_returns_digest_id_on_success(self) -> None:
        task_id = _seed_task(self.conn)
        _seed_observations(self.conn, task_id)
        mock_backend = unittest.mock.MagicMock()
        mock_backend.available.return_value = (True, "ok")
        mock_backend.reason.return_value = {"reply": "The agent executed the auth check successfully."}
        with patch("personal_assistant.providers.get_backend", return_value=mock_backend):
            result = maybe_generate_and_record(self.conn, task_id, "auth check")
        self.conn.commit()
        self.assertIsNotNone(result)
        row = self.conn.execute("SELECT body FROM assistant_digests WHERE id=?", (result,)).fetchone()
        self.assertIn("auth", row["body"].lower())

    def test_never_raises_on_provider_error(self) -> None:
        task_id = _seed_task(self.conn)
        _seed_observations(self.conn, task_id)
        with patch("personal_assistant.providers.get_backend", side_effect=RuntimeError("exploded")):
            result = maybe_generate_and_record(self.conn, task_id, "goal")
        self.assertIsNone(result)


class ListDigestsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_returns_empty_when_none(self) -> None:
        self.assertEqual(list_digests(self.conn), [])

    def test_returns_digests_newest_first(self) -> None:
        task_id = _seed_task(self.conn)
        record_digest(self.conn, task_id, "First", "Body A")
        record_digest(self.conn, task_id, "Second", "Body B")
        self.conn.commit()
        digests = list_digests(self.conn)
        self.assertEqual(digests[0]["title"], "Second")

    def test_limit_respected(self) -> None:
        task_id = _seed_task(self.conn)
        for i in range(5):
            record_digest(self.conn, task_id, f"Digest {i}", f"Body {i}")
        self.conn.commit()
        self.assertEqual(len(list_digests(self.conn, limit=3)), 3)

    def test_source_type_filter(self) -> None:
        task_id = _seed_task(self.conn)
        record_digest(self.conn, task_id, "Factory", "Body", source_type="factory_run")
        record_digest(self.conn, task_id, "Auto", "Body", source_type="autonomy_task")
        self.conn.commit()
        factory_digests = list_digests(self.conn, source_type="factory_run")
        self.assertEqual(len(factory_digests), 1)
        self.assertEqual(factory_digests[0]["source_type"], "factory_run")


if __name__ == "__main__":
    unittest.main()

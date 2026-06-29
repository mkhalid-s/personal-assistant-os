from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from personal_assistant.db import initialize_schema
from personal_assistant.graph import connect_work_items, upsert_node
from personal_assistant.retrieval import hybrid_score


class RetrievalGraphTest(unittest.TestCase):
    def test_hybrid_score(self) -> None:
        self.assertGreater(hybrid_score("auth risk", "auth dependency risk"), 0.2)

    def test_graph_connect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "assistant.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            initialize_schema(conn)
            upsert_node(conn, "work_item", 1, "Item 1")
            upsert_node(conn, "work_item", 2, "Item 2")
            connect_work_items(conn, 1, 2, "blocks")
            conn.commit()
            count = conn.execute("SELECT COUNT(*) AS c FROM knowledge_edges").fetchone()["c"]
            conn.close()
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()

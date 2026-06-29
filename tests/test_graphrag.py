from __future__ import annotations

import sqlite3
import unittest

from personal_assistant import graphrag
from personal_assistant.db import initialize_schema
from personal_assistant.graph import connect_work_items
from personal_assistant.inbox import ensure_work_item_node, index_chunk


class GraphRAGDesignTest(unittest.TestCase):
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        initialize_schema(conn)
        return conn

    def _work_item(self, conn: sqlite3.Connection, title: str) -> int:
        conn.execute(
            """
            INSERT INTO work_items (title, kind, status, priority, risk_score)
            VALUES (?, 'task', 'open', 2, 10)
            """,
            (title,),
        )
        item_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        ensure_work_item_node(conn, item_id, title)
        index_chunk(conn, "work_item", item_id, title)
        return item_id

    def test_retrieve_returns_citations_without_new_graph_storage(self) -> None:
        conn = self._conn()
        try:
            item_id = self._work_item(conn, "Customer escalation dashboard needs daily visibility")
            conn.commit()

            hits = graphrag.retrieve(conn, "customer dashboard visibility", limit=3)
            self.assertGreaterEqual(len(hits), 1)
            self.assertEqual(hits[0]["citation"], f"work_item#{item_id}")
            self.assertEqual(hits[0]["graph_path"], [])
            self.assertIn("direct hybrid retrieval", hits[0]["reason"])

            tables = {
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            self.assertIn("knowledge_nodes", tables)
            self.assertIn("knowledge_edges", tables)
            self.assertNotIn("entities", tables)
            self.assertNotIn("claims", tables)
            self.assertNotIn("retrieval_runs", tables)
        finally:
            conn.close()

    def test_retrieve_expands_linked_work_items_with_explained_path(self) -> None:
        conn = self._conn()
        try:
            source_id = self._work_item(conn, "Launch dashboard tracks customer escalations")
            related_id = self._work_item(conn, "Backend ingestion job supplies upstream metrics")
            unrelated_id = self._work_item(conn, "Prepare weekly team agenda")
            connect_work_items(conn, source_id, related_id, "depends_on", 0.8)
            conn.commit()

            hits = graphrag.retrieve(conn, "customer escalation dashboard", limit=5)
            by_citation = {hit["citation"]: hit for hit in hits}

            self.assertIn(f"work_item#{source_id}", by_citation)
            self.assertIn(f"work_item#{related_id}", by_citation)
            self.assertNotIn(f"work_item#{unrelated_id}", by_citation)

            expanded = by_citation[f"work_item#{related_id}"]
            self.assertIn("graph expansion", expanded["reason"])
            self.assertEqual(
                expanded["graph_path"],
                [f"work_item#{source_id}", "depends_on:0.80", f"work_item#{related_id}"],
            )
            self.assertLess(expanded["score"], by_citation[f"work_item#{source_id}"]["score"])
        finally:
            conn.close()

    def test_retrieve_is_deterministic_for_same_query_and_graph(self) -> None:
        conn = self._conn()
        try:
            source_id = self._work_item(conn, "Risk review for production dashboard")
            related_id = self._work_item(conn, "Mitigation plan for production dashboard dependency")
            connect_work_items(conn, source_id, related_id, "mitigated_by", 1.0)
            conn.commit()

            first = graphrag.retrieve(conn, "production dashboard risk", limit=5)
            second = graphrag.retrieve(conn, "production dashboard risk", limit=5)

            self.assertEqual(first, second)
            self.assertEqual(first[0]["citation"], f"work_item#{source_id}")
            self.assertTrue(any(hit["graph_path"] for hit in first))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

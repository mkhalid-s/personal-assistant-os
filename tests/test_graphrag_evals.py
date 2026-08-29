from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from typing import Any

from personal_assistant import graphrag
from personal_assistant.db import initialize_schema
from personal_assistant.graph import connect_work_items
from personal_assistant.inbox import ensure_work_item_node, index_chunk

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "graphrag_eval_cases.json"


class GraphRAGEvalFixtureTest(unittest.TestCase):
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

    def _load_cases(self) -> list[dict[str, Any]]:
        return json.loads(FIXTURE_PATH.read_text())

    def _recall_at_k(
        self,
        conn: sqlite3.Connection,
        case: dict,
        ids_by_key: dict[str, int],
        k: int = 5,
    ) -> float:
        """Return recall@k for a single eval case."""
        hits = graphrag.retrieve(conn, case["query"], limit=k)
        citations = {hit["citation"] for hit in hits}
        expected = {f"work_item#{ids_by_key[key]}" for key in case["expected_citations"]}
        found = len(expected & citations)
        return found / max(len(expected), 1)

    def test_recall_at_5_meets_threshold(self) -> None:
        """Aggregate recall@5 across all eval cases must be >= 0.75.

        This is an advisory CI gate (set continue-on-error in ci.yml until
        validated against a real embedding backend). The threshold is
        achievable with FTS5 candidate selection + hash backend for these
        graph-expansion cases.
        """
        cases = self._load_cases()
        recalls: list[float] = []
        for case in cases:
            conn = self._conn()
            try:
                ids_by_key = {item["key"]: self._work_item(conn, item["title"]) for item in case["work_items"]}
                for link in case["links"]:
                    connect_work_items(
                        conn,
                        ids_by_key[link["from"]],
                        ids_by_key[link["to"]],
                        link["relation"],
                        float(link["weight"]),
                    )
                conn.commit()
                recalls.append(self._recall_at_k(conn, case, ids_by_key, k=5))
            finally:
                conn.close()

        mean_recall = sum(recalls) / max(len(recalls), 1)
        self.assertGreaterEqual(
            mean_recall,
            0.75,
            f"Mean recall@5 = {mean_recall:.2f} across {len(recalls)} cases "
            f"(per-case: {[f'{r:.2f}' for r in recalls]})",
        )

    def test_fixture_cases_retrieve_expected_sources_and_graph_paths(self) -> None:
        cases = self._load_cases()
        self.assertGreaterEqual(len(cases), 3)

        for case in cases:
            with self.subTest(case=case["name"]):
                conn = self._conn()
                try:
                    ids_by_key = {item["key"]: self._work_item(conn, item["title"]) for item in case["work_items"]}
                    for link in case["links"]:
                        connect_work_items(
                            conn,
                            ids_by_key[link["from"]],
                            ids_by_key[link["to"]],
                            link["relation"],
                            float(link["weight"]),
                        )
                    conn.commit()

                    hits = graphrag.retrieve(conn, case["query"], limit=5)
                    citations = {hit["citation"] for hit in hits}
                    for key in case["expected_citations"]:
                        self.assertIn(f"work_item#{ids_by_key[key]}", citations)

                    graph_paths = {tuple(hit["graph_path"]) for hit in hits if hit["graph_path"]}
                    for path in case["expected_graph_paths"]:
                        expected = tuple(
                            f"work_item#{ids_by_key[part]}" if part in ids_by_key else part for part in path
                        )
                        self.assertIn(expected, graph_paths)
                finally:
                    conn.close()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sqlite3
import unittest

from personal_assistant import claims, entities, graphrag, relationships
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

    def test_retrieve_returns_citations_with_claim_storage_available(self) -> None:
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
            self.assertIn("entities", tables)
            self.assertIn("retrieval_runs", tables)
            self.assertIn("retrieval_run_sources", tables)
            self.assertIn("claims", tables)
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

    def test_recorded_retrieval_run_persists_sources_and_paths(self) -> None:
        conn = self._conn()
        try:
            source_id = self._work_item(conn, "Launch dashboard tracks customer escalations")
            related_id = self._work_item(conn, "Backend ingestion job supplies upstream metrics")
            connect_work_items(conn, source_id, related_id, "depends_on", 0.8)
            conn.commit()

            hits = graphrag.retrieve(
                conn,
                "customer escalation dashboard",
                limit=5,
                record_run=True,
                mode="test_graph",
            )

            run_id = hits[0]["retrieval_run_id"]
            run = conn.execute(
                "SELECT query, mode, selected_count FROM retrieval_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            self.assertEqual(run["query"], "customer escalation dashboard")
            self.assertEqual(run["mode"], "test_graph")
            self.assertEqual(run["selected_count"], len(hits))

            rows = conn.execute(
                """
                SELECT rank, citation, reason, graph_path_json
                FROM retrieval_run_sources
                WHERE retrieval_run_id = ?
                ORDER BY rank ASC
                """,
                (run_id,),
            ).fetchall()
            self.assertEqual(len(rows), len(hits))
            self.assertEqual(rows[0]["citation"], f"work_item#{source_id}")
            self.assertTrue(
                any(
                    row["citation"] == f"work_item#{related_id}"
                    and "graph expansion" in row["reason"]
                    and f"work_item#{source_id}" in row["graph_path_json"]
                    for row in rows
                )
            )
        finally:
            conn.close()

    def test_entity_relationship_expansion_adds_related_sources(self) -> None:
        conn = self._conn()
        try:
            source_id = self._work_item(conn, "Project Atlas needs launch visibility")
            related_id = self._work_item(conn, "Service Billing API supplies upstream metrics")
            relationships.record_relationships(
                conn,
                "Project Atlas depends on Service Billing API.",
                source_type="note",
                source_id="relationship-fixture",
            )
            entities.record_entities(
                conn,
                "Project Atlas needs launch visibility",
                source_type="work_item",
                source_id=source_id,
            )
            entities.record_entities(
                conn,
                "Service Billing API supplies upstream metrics",
                source_type="work_item",
                source_id=related_id,
            )
            conn.commit()

            hits = graphrag.retrieve(conn, "Project Atlas launch", limit=5)
            by_citation = {hit["citation"]: hit for hit in hits}

            self.assertIn(f"work_item#{source_id}", by_citation)
            self.assertIn(f"work_item#{related_id}", by_citation)
            self.assertIn("entity outbound relationship expansion", by_citation[f"work_item#{related_id}"]["reason"])
            self.assertTrue(by_citation[f"work_item#{related_id}"]["graph_path"])
        finally:
            conn.close()

    def test_entity_relationship_expansion_works_inbound(self) -> None:
        conn = self._conn()
        try:
            project_id = self._work_item(conn, "Project Atlas needs launch visibility")
            service_id = self._work_item(conn, "Service Billing API supplies upstream metrics")
            relationships.record_relationships(
                conn,
                "Project Atlas depends on Service Billing API.",
                source_type="note",
                source_id="relationship-fixture",
            )
            entities.record_entities(
                conn,
                "Project Atlas needs launch visibility",
                source_type="work_item",
                source_id=project_id,
            )
            entities.record_entities(
                conn,
                "Service Billing API supplies upstream metrics",
                source_type="work_item",
                source_id=service_id,
            )
            conn.commit()

            hits = graphrag.retrieve(conn, "Service Billing API metrics", limit=5)
            by_citation = {hit["citation"]: hit for hit in hits}

            self.assertIn(f"work_item#{service_id}", by_citation)
            self.assertIn(f"work_item#{project_id}", by_citation)
            project_hit = by_citation[f"work_item#{project_id}"]
            self.assertIn("entity inbound relationship expansion", project_hit["reason"])
            self.assertIn("inbound:depends_on", project_hit["graph_path"])
        finally:
            conn.close()

    def test_claims_can_be_persisted_and_listed(self) -> None:
        conn = self._conn()
        try:
            recorded = claims.record_claims(
                conn,
                "Project Atlas requires Service Billing API. Random fragment without claim.",
                source_type="work_item",
                source_id=7,
            )
            conn.commit()

            self.assertEqual(len(recorded), 1)
            rows = claims.list_claims(conn)
            self.assertEqual(rows[0]["claim_text"], "Project Atlas requires Service Billing API")
            self.assertEqual(rows[0]["source_type"], "work_item")
            self.assertEqual(rows[0]["source_id"], "7")
        finally:
            conn.close()

    def test_multihop_graph_and_claim_backed_retrieval(self) -> None:
        conn = self._conn()
        try:
            source_id = self._work_item(conn, "Project Atlas launch needs dependency review")
            middle_id = self._work_item(conn, "Gateway integration handoff record")
            target_id = self._work_item(conn, "Billing metrics confirm downstream stability")
            connect_work_items(conn, source_id, middle_id, "depends_on", 0.9)
            connect_work_items(conn, middle_id, target_id, "validated_by", 0.9)
            conn.commit()

            hits = graphrag.retrieve(conn, "Project Atlas launch dependency", limit=6, graph_hops=2)
            by_citation = {hit["citation"]: hit for hit in hits}
            self.assertIn(f"work_item#{target_id}", by_citation)
            target = by_citation[f"work_item#{target_id}"]
            self.assertTrue(target["graph_path"])
            self.assertIn("multi-hop graph expansion", target["reason"])
            self.assertTrue(
                any(
                    "multi-hop graph expansion" in hit["reason"]
                    and f"work_item#{source_id}" in hit["graph_path"]
                    for hit in hits
                )
            )

            claims.record_claims(
                conn,
                "Gateway integration requires dependency review.",
                source_type="work_item",
                source_id=middle_id,
            )
            conn.commit()
            claim_hits = graphrag.retrieve(conn, "Gateway integration requires dependency review", limit=3, graph_hops=1)
            self.assertTrue(any("claim-backed retrieval" in hit["reason"] for hit in claim_hits))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

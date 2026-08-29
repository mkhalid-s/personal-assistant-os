from __future__ import annotations

import sqlite3
import unittest

from personal_assistant.catalog import add_service, get_service_context, list_services, remove_service
from personal_assistant.db import initialize_schema


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


class AddServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_creates_knowledge_node(self) -> None:
        add_service(self.conn, "auth-service", owner="platform")
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM knowledge_nodes WHERE node_type='service' AND label='auth-service'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_indexes_text_chunk(self) -> None:
        add_service(self.conn, "payments", description="Payment gateway")
        self.conn.commit()
        chunk = self.conn.execute(
            "SELECT content FROM text_chunks WHERE source_type='service'"
        ).fetchone()
        self.assertIsNotNone(chunk)
        self.assertIn("payments", chunk["content"].lower())

    def test_creates_dependency_edges(self) -> None:
        add_service(self.conn, "api-gateway", deps=["auth-service", "rate-limiter"])
        self.conn.commit()
        edges = self.conn.execute(
            """
            SELECT n.label FROM knowledge_edges e
            JOIN knowledge_nodes n ON n.id = e.to_node_id
            WHERE e.relation = 'depends_on'
            ORDER BY n.label ASC
            """
        ).fetchall()
        self.assertEqual([e["label"] for e in edges], ["auth-service", "rate-limiter"])

    def test_idempotent_add(self) -> None:
        add_service(self.conn, "auth-service", owner="platform")
        add_service(self.conn, "auth-service", owner="platform")
        self.conn.commit()
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM knowledge_nodes WHERE node_type='service' AND label='auth-service'"
        ).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_empty_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            add_service(self.conn, "")

    def test_empty_deps_ignored(self) -> None:
        add_service(self.conn, "simple-service", deps=["", "  "])
        self.conn.commit()
        edges = self.conn.execute("SELECT COUNT(*) AS c FROM knowledge_edges").fetchone()["c"]
        self.assertEqual(edges, 0)


class RemoveServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_removes_node_and_edges(self) -> None:
        add_service(self.conn, "auth-service", deps=["postgres"])
        self.conn.commit()
        found = remove_service(self.conn, "auth-service")
        self.conn.commit()
        self.assertTrue(found)
        row = self.conn.execute(
            "SELECT 1 FROM knowledge_nodes WHERE node_type='service' AND label='auth-service'"
        ).fetchone()
        self.assertIsNone(row)

    def test_returns_false_on_missing(self) -> None:
        self.assertFalse(remove_service(self.conn, "does-not-exist"))

    def test_removes_text_chunk(self) -> None:
        add_service(self.conn, "svc-to-delete")
        self.conn.commit()
        remove_service(self.conn, "svc-to-delete")
        self.conn.commit()
        row = self.conn.execute(
            "SELECT 1 FROM text_chunks WHERE source_type='service'"
        ).fetchone()
        self.assertIsNone(row)


class ListServicesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_empty_catalog_returns_empty(self) -> None:
        self.assertEqual(list_services(self.conn), [])

    def test_lists_all_services_alphabetically(self) -> None:
        add_service(self.conn, "zebra")
        add_service(self.conn, "alpha")
        self.conn.commit()
        names = [s["name"] for s in list_services(self.conn)]
        self.assertEqual(names, ["alpha", "zebra"])

    def test_includes_deps(self) -> None:
        add_service(self.conn, "api", deps=["db", "cache"])
        self.conn.commit()
        services = list_services(self.conn)
        api = next(s for s in services if s["name"] == "api")
        self.assertIn("db", api["deps"])
        self.assertIn("cache", api["deps"])


class GetServiceContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_empty_catalog_returns_empty_string(self) -> None:
        self.assertEqual(get_service_context(self.conn, "auth"), "")

    def test_returns_relevant_service(self) -> None:
        add_service(self.conn, "auth-service", description="Handles OAuth2 authentication")
        add_service(self.conn, "billing-service", description="Manages subscription billing")
        self.conn.commit()
        ctx = get_service_context(self.conn, "authentication token expired")
        self.assertIn("auth", ctx.lower())

    def test_includes_dep_info(self) -> None:
        add_service(self.conn, "api-gateway", deps=["auth-service"])
        self.conn.commit()
        ctx = get_service_context(self.conn, "api gateway")
        self.assertIn("auth-service", ctx)

    def test_respects_limit(self) -> None:
        for i in range(10):
            add_service(self.conn, f"service-{i}")
        self.conn.commit()
        ctx = get_service_context(self.conn, "service", limit=3)
        lines = [l for l in ctx.split("\n") if l.strip().startswith("-")]
        self.assertLessEqual(len(lines), 3)

    def test_starts_with_header(self) -> None:
        add_service(self.conn, "auth-service")
        self.conn.commit()
        ctx = get_service_context(self.conn, "auth")
        self.assertTrue(ctx.startswith("Relevant services:"))


if __name__ == "__main__":
    unittest.main()

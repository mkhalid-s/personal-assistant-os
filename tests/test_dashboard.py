"""Tests for dashboard.py — query layer, HTML render, and GET dispatcher."""

from __future__ import annotations

import json
import sqlite3
import unittest

from personal_assistant.dashboard import (
    GRAPH_JSON_SCHEMA,
    _query_approvals,
    _query_audit_event_types,
    _query_audit_trail,
    _query_graph_export,
    _query_graph_summary,
    _query_intents,
    dashboard_http_response,
    export_graph_json,
    parse_audit_query,
    render_dashboard_html,
)
from personal_assistant.db import initialize_schema
from personal_assistant.graph import upsert_node


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


class DashboardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.addCleanup(self.conn.close)

    def _make_intent(self, objective: str, *, status: str = "open", priority: int = 2) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO intents (objective, context, constraints_json, success_criteria, priority, status)
            VALUES (?, '', '[]', '', ?, ?)
            """,
            (objective, priority, status),
        )
        return int(cur.lastrowid)

    def _make_task(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO agent_tasks (objective, status, priority) VALUES (?, 'active', 2)",
            ("test objective",),
        )
        return int(cur.lastrowid)

    def _make_action(
        self,
        task_id: int,
        *,
        status: str = "proposed",
        requires_approval: int = 1,
        title: str = "Test action",
        payload: str = "{}",
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval)
            VALUES (?, 'create_inbox_item', ?, ?, ?, ?)
            """,
            (task_id, title, payload, status, requires_approval),
        )
        return int(cur.lastrowid)


class QueryIntentsTest(DashboardTestCase):
    def test_empty_returns_empty_list(self) -> None:
        self.assertEqual(_query_intents(self.conn), [])

    def test_returns_open_and_active_with_counts(self) -> None:
        open_id = self._make_intent("Ship dashboard", status="open", priority=1)
        self._make_intent("Active goal", status="active", priority=2)
        self._make_intent("Closed goal", status="done", priority=1)
        self.conn.execute(
            "INSERT INTO intent_risks (intent_id, risk, status) VALUES (?, 'slip', 'open')",
            (open_id,),
        )
        self.conn.execute(
            "INSERT INTO intent_risks (intent_id, risk, status) VALUES (?, 'old', 'closed')",
            (open_id,),
        )
        self.conn.execute(
            """INSERT INTO intent_evidence (intent_id, source_type, content)
               VALUES (?, 'note', 'proof')""",
            (open_id,),
        )
        self.conn.commit()
        rows = _query_intents(self.conn)
        self.assertEqual(len(rows), 2)
        by_obj = {r["objective"]: r for r in rows}
        self.assertEqual(by_obj["Ship dashboard"]["open_risks"], 1)
        self.assertEqual(by_obj["Ship dashboard"]["evidence_count"], 1)
        self.assertEqual(by_obj["Active goal"]["open_risks"], 0)
        self.assertNotIn("Closed goal", by_obj)

    def test_orders_by_priority_and_is_capped(self) -> None:
        for i in range(25):
            self._make_intent(f"intent {i}", priority=3)
        self.conn.commit()
        rows = _query_intents(self.conn, limit=5)
        self.assertEqual(len(rows), 5)

    def test_returns_plain_dicts_with_required_keys(self) -> None:
        self._make_intent("keys")
        self.conn.commit()
        row = _query_intents(self.conn)[0]
        self.assertIsInstance(row, dict)
        for key in ("id", "objective", "status", "priority", "created_at", "open_risks", "evidence_count"):
            self.assertIn(key, row)


class QueryAuditTrailTest(DashboardTestCase):
    def test_empty_page(self) -> None:
        result = _query_audit_trail(self.conn)
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["page"], 1)
        self.assertEqual(result["pages"], 1)

    def test_paging_is_bounded(self) -> None:
        for i in range(30):
            self.conn.execute(
                "INSERT INTO event_log (event_type, entity_type, entity_id, payload) VALUES (?, 'x', ?, '{}')",
                ("alpha" if i < 12 else "beta", i),
            )
        self.conn.commit()
        page1 = _query_audit_trail(self.conn, page=1, page_size=10)
        page2 = _query_audit_trail(self.conn, page=2, page_size=10)
        self.assertEqual(len(page1["rows"]), 10)
        self.assertEqual(len(page2["rows"]), 10)
        self.assertEqual(page1["total"], 30)
        self.assertEqual(page1["pages"], 3)
        self.assertNotEqual(page1["rows"][0]["id"], page2["rows"][0]["id"])

    def test_event_type_filter(self) -> None:
        self.conn.execute("INSERT INTO event_log (event_type, entity_type, payload) VALUES ('alpha', 'x', '{}')")
        self.conn.execute("INSERT INTO event_log (event_type, entity_type, payload) VALUES ('beta', 'x', '{}')")
        self.conn.commit()
        result = _query_audit_trail(self.conn, event_type="alpha")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["rows"][0]["event_type"], "alpha")

    def test_page_size_clamped(self) -> None:
        result = _query_audit_trail(self.conn, page_size=10_000)
        self.assertLessEqual(result["page_size"], 100)

    def test_overshoot_page_clamps_and_still_returns_rows(self) -> None:
        for _ in range(12):
            self.conn.execute(
                "INSERT INTO event_log (event_type, entity_type, payload) VALUES ('alpha', 'x', '{}')",
            )
        self.conn.commit()
        result = _query_audit_trail(self.conn, page=99, page_size=5)
        self.assertEqual(result["page"], 3)
        self.assertEqual(result["pages"], 3)
        self.assertEqual(len(result["rows"]), 2)

    def test_event_types_bounded(self) -> None:
        for i in range(12):
            self.conn.execute(
                "INSERT INTO event_log (event_type, entity_type, payload) VALUES (?, 'x', '{}')",
                (f"t{i}",),
            )
        self.conn.commit()
        types = _query_audit_event_types(self.conn, limit=5)
        self.assertEqual(len(types), 5)

    def test_parse_audit_query(self) -> None:
        page, event_type, page_size = parse_audit_query({"page": ["2"], "event_type": [" loop_end "]})
        self.assertEqual(page, 2)
        self.assertEqual(event_type, "loop_end")
        self.assertEqual(page_size, 25)


class QueryApprovalsTest(DashboardTestCase):
    def test_empty(self) -> None:
        self.assertEqual(_query_approvals(self.conn), [])

    def test_returns_proposed_and_approved_only(self) -> None:
        task_id = self._make_task()
        self._make_action(task_id, status="proposed", payload='{"target":"inbox"}')
        self._make_action(task_id, status="approved")
        self._make_action(task_id, status="executed")
        self._make_action(task_id, status="proposed", requires_approval=0)
        self.conn.commit()
        rows = _query_approvals(self.conn)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["payload_json"], '{"target":"inbox"}')

    def test_capped(self) -> None:
        task_id = self._make_task()
        for _ in range(12):
            self._make_action(task_id)
        self.conn.commit()
        self.assertEqual(len(_query_approvals(self.conn, limit=5)), 5)


class QueryGraphTest(DashboardTestCase):
    def test_empty_summary(self) -> None:
        summary = _query_graph_summary(self.conn)
        self.assertEqual(summary["node_count"], 0)
        self.assertEqual(summary["edge_count"], 0)
        self.assertEqual(summary["top_types"], [])

    def test_export_is_bounded_and_counts_types(self) -> None:
        a = upsert_node(self.conn, "work_item", 1, "alpha")
        b = upsert_node(self.conn, "work_item", 2, "beta")
        upsert_node(self.conn, "person", 1, "Ada")
        self.conn.execute(
            """INSERT INTO knowledge_edges (from_node_id, to_node_id, relation, weight, source)
               VALUES (?, ?, 'blocks', 1.0, 'manual')""",
            (a, b),
        )
        self.conn.commit()
        summary = _query_graph_summary(self.conn)
        self.assertEqual(summary["node_count"], 3)
        self.assertEqual(summary["edge_count"], 1)
        types = {row["node_type"]: row["n"] for row in summary["top_types"]}
        self.assertEqual(types["work_item"], 2)
        exported = _query_graph_export(self.conn)
        self.assertEqual(exported["schema"], GRAPH_JSON_SCHEMA)
        self.assertEqual(len(exported["nodes"]), 3)
        self.assertEqual(len(exported["edges"]), 1)
        payload = json.loads(export_graph_json(self.conn))
        self.assertEqual(payload["schema"], GRAPH_JSON_SCHEMA)

    def test_export_respects_node_limit(self) -> None:
        for i in range(8):
            upsert_node(self.conn, "note", i, f"n{i}")
        self.conn.commit()
        exported = _query_graph_export(self.conn, node_limit=3)
        self.assertEqual(len(exported["nodes"]), 3)
        self.assertTrue(exported["truncated"])


class RenderDashboardTest(DashboardTestCase):
    def _seed(self) -> None:
        intent_id = self._make_intent("<script>alert(1)</script>", priority=1)
        self.conn.execute(
            "INSERT INTO intent_evidence (intent_id, source_type, content) VALUES (?, 'note', 'ev')",
            (intent_id,),
        )
        task_id = self._make_task()
        self._make_action(task_id, title="Need review", payload='{"path":"<b>x</b>","note":"ok"}')
        self.conn.execute(
            "INSERT INTO event_log (event_type, entity_type, payload) VALUES ('intent_created', 'intent', '{}')"
        )
        upsert_node(self.conn, "work_item", 9, "node-nine")
        self.conn.commit()

    def test_render_includes_new_sections_and_escapes(self) -> None:
        self._seed()
        html = render_dashboard_html(self.conn)
        self.assertIn("id='intents'", html)
        self.assertIn("id='approvals'", html)
        self.assertIn("id='audit'", html)
        self.assertIn("id='graph'", html)
        self.assertIn("Risk Watch", html)
        self.assertIn("Need review", html)
        self.assertIn("intent_created", html)
        self.assertIn("work_item (1)", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)

    def test_audit_filter_in_render(self) -> None:
        self.conn.execute("INSERT INTO event_log (event_type, entity_type, payload) VALUES ('alpha', 'x', '{}')")
        self.conn.execute("INSERT INTO event_log (event_type, entity_type, payload) VALUES ('beta', 'x', '{}')")
        self.conn.commit()
        html = render_dashboard_html(self.conn, audit_event_type="beta")
        self.assertIn("value='beta'", html)
        self.assertIn(">beta<", html)

    def test_http_dispatcher_token_and_graph_json(self) -> None:
        upsert_node(self.conn, "person", 1, "Ada")
        self.conn.commit()
        token = "secret-token-value"
        unauthorized = dashboard_http_response(self.conn, path="/", query={}, token=token, supplied_token="nope")
        self.assertEqual(unauthorized[0], 401)
        html = dashboard_http_response(
            self.conn,
            path="/",
            query={"token": [token], "page": ["1"]},
            token=token,
            supplied_token=token,
        )
        self.assertEqual(html[0], 200)
        self.assertIn("text/html", html[1])
        self.assertIn(b"Graph context", html[2])
        graph = dashboard_http_response(
            self.conn,
            path="/graph.json",
            query={"token": [token]},
            token=token,
            supplied_token=token,
        )
        self.assertEqual(graph[0], 200)
        self.assertIn("application/json", graph[1])
        payload = json.loads(graph[2].decode("utf-8"))
        self.assertEqual(payload["schema"], GRAPH_JSON_SCHEMA)
        self.assertEqual(payload["counts"]["node_count"], 1)


if __name__ == "__main__":
    unittest.main()

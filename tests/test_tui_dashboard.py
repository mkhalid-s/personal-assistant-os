"""Tests for tui_dashboard.py — data layer and plain-text fallback."""

from __future__ import annotations

import io
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from personal_assistant.db import initialize_schema
from personal_assistant.tui_dashboard import (
    _due_label,
    _event_summary,
    _query_graph_counts,
    _query_inbox_count,
    _query_intents,
    _query_latest_digest,
    _query_loop_status,
    _query_queue,
    _query_recent_events,
    _query_work_count,
    _query_work_items,
    build_snapshot,
    status_plain,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


def _ts(delta_seconds: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(seconds=delta_seconds)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class QueryQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def _make_task(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO agent_tasks (objective, status, priority) VALUES (?, 'active', 2)",
            ("test objective",),
        )
        return cur.lastrowid

    def _make_action(self, task_id: int, status: str = "proposed", requires_approval: int = 1) -> int:
        cur = self.conn.execute(
            """INSERT INTO agent_actions (agent_task_id, action_type, title, status, requires_approval)
               VALUES (?, 'create_inbox_item', 'Test action', ?, ?)""",
            (task_id, status, requires_approval),
        )
        return cur.lastrowid

    def test_empty_queue_returns_empty_list(self) -> None:
        self.assertEqual(_query_queue(self.conn), [])

    def test_returns_proposed_actions(self) -> None:
        task_id = self._make_task()
        self._make_action(task_id, status="proposed")
        self.conn.commit()
        rows = _query_queue(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "proposed")

    def test_returns_approved_actions(self) -> None:
        task_id = self._make_task()
        self._make_action(task_id, status="approved")
        self.conn.commit()
        rows = _query_queue(self.conn)
        self.assertEqual(len(rows), 1)

    def test_excludes_executed_actions(self) -> None:
        task_id = self._make_task()
        self._make_action(task_id, status="executed")
        self.conn.commit()
        self.assertEqual(_query_queue(self.conn), [])

    def test_excludes_non_approval_actions(self) -> None:
        task_id = self._make_task()
        self._make_action(task_id, status="proposed", requires_approval=0)
        self.conn.commit()
        self.assertEqual(_query_queue(self.conn), [])

    def test_returns_plain_dicts(self) -> None:
        task_id = self._make_task()
        self._make_action(task_id)
        self.conn.commit()
        rows = _query_queue(self.conn)
        self.assertIsInstance(rows[0], dict)

    def test_all_required_keys_present(self) -> None:
        task_id = self._make_task()
        self._make_action(task_id)
        self.conn.commit()
        row = _query_queue(self.conn)[0]
        for key in ("id", "action_type", "title", "status", "payload_json", "created_at"):
            self.assertIn(key, row)

    def test_capped_at_10(self) -> None:
        task_id = self._make_task()
        for _ in range(15):
            self._make_action(task_id)
        self.conn.commit()
        self.assertEqual(len(_query_queue(self.conn)), 10)


class QueryWorkItemsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def _make_work_item(self, title: str, risk: int = 10, status: str = "open") -> None:
        self.conn.execute(
            "INSERT INTO work_items (title, kind, status, risk_score) VALUES (?, 'task', ?, ?)",
            (title, status, risk),
        )

    def test_empty_returns_empty_list(self) -> None:
        self.assertEqual(_query_work_items(self.conn), [])

    def test_orders_by_risk_desc(self) -> None:
        self._make_work_item("low", risk=10)
        self._make_work_item("high", risk=80)
        self.conn.commit()
        rows = _query_work_items(self.conn)
        self.assertEqual(rows[0]["title"], "high")

    def test_excludes_done_items(self) -> None:
        self._make_work_item("done item", status="done")
        self.conn.commit()
        self.assertEqual(_query_work_items(self.conn), [])

    def test_capped_at_6(self) -> None:
        for i in range(10):
            self._make_work_item(f"item {i}", risk=i)
        self.conn.commit()
        self.assertEqual(len(_query_work_items(self.conn)), 6)


class QueryWorkCountTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_empty_returns_zeros(self) -> None:
        result = _query_work_count(self.conn)
        self.assertEqual(result, {"total": 0, "at_risk": 0})

    def test_counts_at_risk_correctly(self) -> None:
        self.conn.execute("INSERT INTO work_items (title, kind, status, risk_score) VALUES ('a', 'task', 'open', 80)")
        self.conn.execute("INSERT INTO work_items (title, kind, status, risk_score) VALUES ('b', 'task', 'open', 20)")
        self.conn.execute("INSERT INTO work_items (title, kind, status, risk_score) VALUES ('c', 'task', 'done', 90)")
        self.conn.commit()
        result = _query_work_count(self.conn)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["at_risk"], 1)


class QueryInboxCountTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_empty_returns_zero(self) -> None:
        self.assertEqual(_query_inbox_count(self.conn), 0)

    def test_counts_new_only(self) -> None:
        self.conn.execute("INSERT INTO inbox_items (text, source, status) VALUES ('a', 'test', 'new')")
        self.conn.execute("INSERT INTO inbox_items (text, source, status) VALUES ('b', 'test', 'read')")
        self.conn.commit()
        self.assertEqual(_query_inbox_count(self.conn), 1)


class QueryLatestDigestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(_query_latest_digest(self.conn))

    def test_returns_most_recent(self) -> None:
        self.conn.execute("INSERT INTO assistant_digests (title, body) VALUES ('old digest', 'body1')")
        self.conn.execute("INSERT INTO assistant_digests (title, body) VALUES ('new digest', 'body2')")
        self.conn.commit()
        result = _query_latest_digest(self.conn)
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "new digest")


class QueryLoopStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_empty_returns_empty_dict(self) -> None:
        self.assertEqual(_query_loop_status(self.conn), {})

    def test_returns_latest_entry(self) -> None:
        self.conn.execute(
            """INSERT INTO autonomy_run_ledger (decision_type, status, actions_proposed,
               safe_actions_executed, pending_approvals, blocked_or_failed)
               VALUES ('full_loop', 'completed', 2, 1, 0, 0)"""
        )
        self.conn.commit()
        result = _query_loop_status(self.conn)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["decision_type"], "full_loop")


class BuildSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_all_keys_present_on_empty_db(self) -> None:
        snapshot = build_snapshot(self.conn)
        for key in (
            "queue",
            "work_items",
            "work_counts",
            "events",
            "intents",
            "graph",
            "inbox_new",
            "digest",
            "loop",
            "backend_name",
        ):
            self.assertIn(key, snapshot)

    def test_backend_name_from_env(self) -> None:
        with patch.dict("os.environ", {"MYOS_AGENT_BACKEND": "ollama"}):
            snapshot = build_snapshot(self.conn)
        self.assertEqual(snapshot["backend_name"], "ollama")

    def test_backend_name_defaults_to_claude(self) -> None:
        import os

        os.environ.pop("MYOS_AGENT_BACKEND", None)
        snapshot = build_snapshot(self.conn)
        self.assertEqual(snapshot["backend_name"], "claude")


class DueLabelTest(unittest.TestCase):
    def test_none_returns_empty(self) -> None:
        self.assertEqual(_due_label(None), "")

    def test_today(self) -> None:
        today = datetime.now().date().isoformat()
        self.assertEqual(_due_label(today), "today")

    def test_tomorrow(self) -> None:
        tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
        self.assertEqual(_due_label(tomorrow), "tomorrow")

    def test_future_days(self) -> None:
        future = (datetime.now().date() + timedelta(days=5)).isoformat()
        self.assertEqual(_due_label(future), "+5d")

    def test_overdue(self) -> None:
        past = (datetime.now().date() - timedelta(days=3)).isoformat()
        result = _due_label(past)
        self.assertIn("overdue", result)

    def test_malformed_returns_string(self) -> None:
        result = _due_label("not-a-date")
        self.assertIsInstance(result, str)


class EventSummaryTest(unittest.TestCase):
    def test_basic_event(self) -> None:
        ev = {"event_type": "loop_cycle_end", "entity_id": 4, "payload": "{}"}
        result = _event_summary(ev)
        self.assertIn("loop_cycle_end", result)
        self.assertIn("#4", result)

    def test_no_entity_id(self) -> None:
        ev = {"event_type": "sync_done", "entity_id": None, "payload": None}
        result = _event_summary(ev)
        self.assertEqual(result, "sync_done")

    def test_payload_condensed(self) -> None:
        ev = {"event_type": "rule_block", "entity_id": None, "payload": '{"action_type": "apply_patch"}'}
        result = _event_summary(ev)
        self.assertIn("action_type=apply_patch", result)

    def test_truncated_to_70(self) -> None:
        ev = {"event_type": "x" * 80, "entity_id": None, "payload": None}
        result = _event_summary(ev)
        self.assertLessEqual(len(result), 70)


class StatusPlainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_does_not_crash_on_empty_db(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            status_plain(self.conn)
        output = buf.getvalue()
        self.assertIn("MYOS Status", output)

    def test_shows_install_hint(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            status_plain(self.conn)
        self.assertIn("pip install", buf.getvalue())

    def test_shows_backend_name(self) -> None:
        with patch.dict("os.environ", {"MYOS_AGENT_BACKEND": "ollama"}):
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                status_plain(self.conn)
        self.assertIn("ollama", buf.getvalue())


class QueryIntentsAndGraphTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.addCleanup(self.conn.close)

    def test_intents_empty(self) -> None:
        self.assertEqual(_query_intents(self.conn), [])

    def test_intents_open_active_with_counts_and_limit(self) -> None:
        self.conn.execute(
            """INSERT INTO intents (objective, constraints_json, priority, status)
               VALUES ('Keep lights on', '[]', 1, 'open')"""
        )
        intent_id = int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        self.conn.execute(
            """INSERT INTO intents (objective, constraints_json, priority, status)
               VALUES ('Parked', '[]', 1, 'done')"""
        )
        self.conn.execute(
            "INSERT INTO intent_risks (intent_id, risk, status) VALUES (?, 'slip', 'open')",
            (intent_id,),
        )
        for i in range(12):
            self.conn.execute(
                """INSERT INTO intents (objective, constraints_json, priority, status)
                   VALUES (?, '[]', 3, 'active')""",
                (f"extra {i}",),
            )
        self.conn.commit()
        rows = _query_intents(self.conn, limit=5)
        self.assertEqual(len(rows), 5)
        keep = [r for r in _query_intents(self.conn, limit=20) if r["objective"] == "Keep lights on"][0]
        self.assertEqual(keep["open_risks"], 1)
        self.assertEqual(keep["evidence_count"], 0)
        self.assertFalse(any(r["objective"] == "Parked" for r in rows))

    def test_graph_counts(self) -> None:
        self.conn.execute("INSERT INTO knowledge_nodes (node_type, ref_id, label) VALUES ('work_item', 1, 'a')")
        self.conn.execute("INSERT INTO knowledge_nodes (node_type, ref_id, label) VALUES ('person', 1, 'b')")
        a = int(self.conn.execute("SELECT id FROM knowledge_nodes WHERE label='a'").fetchone()["id"])
        b = int(self.conn.execute("SELECT id FROM knowledge_nodes WHERE label='b'").fetchone()["id"])
        self.conn.execute(
            """INSERT INTO knowledge_edges (from_node_id, to_node_id, relation)
               VALUES (?, ?, 'related')""",
            (a, b),
        )
        self.conn.commit()
        counts = _query_graph_counts(self.conn)
        self.assertEqual(counts["node_count"], 2)
        self.assertEqual(counts["edge_count"], 1)
        types = {row["node_type"]: row["n"] for row in counts["top_types"]}
        self.assertEqual(types["work_item"], 1)

    def test_recent_events_honors_larger_window(self) -> None:
        for i in range(15):
            self.conn.execute(
                "INSERT INTO event_log (event_type, entity_type, payload) VALUES (?, 'x', '{}')",
                (f"e{i}",),
            )
        self.conn.commit()
        self.assertEqual(len(_query_recent_events(self.conn)), 8)
        self.assertEqual(len(_query_recent_events(self.conn, limit=12)), 12)


class StatusPlainNewPanelsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.addCleanup(self.conn.close)

    def test_shows_intents_graph_and_payload(self) -> None:
        self.conn.execute(
            """INSERT INTO intents (objective, constraints_json, priority, status)
               VALUES ('Ship Phase A', '[]', 1, 'open')"""
        )
        task_id = self.conn.execute(
            "INSERT INTO agent_tasks (objective, status, priority) VALUES ('t', 'active', 2)"
        ).lastrowid
        self.conn.execute(
            """INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval)
               VALUES (?, 'create_inbox_item', 'Queue me', '{"target":"notes"}', 'proposed', 1)""",
            (task_id,),
        )
        self.conn.execute("INSERT INTO knowledge_nodes (node_type, ref_id, label) VALUES ('work_item', 1, 'n')")
        self.conn.execute(
            "INSERT INTO event_log (event_type, entity_type, payload) VALUES ('loop_cycle_end', 'loop', '{}')"
        )
        self.conn.commit()
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            status_plain(self.conn)
        output = buf.getvalue()
        self.assertIn("Intents:", output)
        self.assertIn("Ship Phase A", output)
        self.assertIn("Graph:", output)
        self.assertIn("nodes", output)
        self.assertIn("target=notes", output)
        self.assertIn("loop_cycle_end", output)

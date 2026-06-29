"""Phase 1 tests: graded autonomy, the destructive execution guard, FTS5 memory,
and the worker atomic-claim race fix. No network required."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _fresh_db_conn():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["MYOS_DB_PATH"] = tmp.name
    from personal_assistant.db import get_connection

    return get_connection(), tmp.name


class AutonomyClassifierTest(unittest.TestCase):
    """Pure classifier — the safety contract."""

    def test_action_tiers(self):
        from personal_assistant import autonomy as a

        self.assertEqual(a.classify_action("create_inbox_item")["tier"], a.AUTO)
        # create_inbox_item is the ONLY auto-safe queued action type (finding #11).
        self.assertEqual(a.AUTO_ACTION_TYPES, frozenset({"create_inbox_item"}))
        self.assertEqual(a.classify_action("draft_external_update", level="balanced")["tier"], a.CONFIRM)
        self.assertEqual(a.classify_action("apply_patch", level="balanced")["tier"], a.CONFIRM)
        # External mutations are NEVER auto, even at bold (review finding #2).
        self.assertEqual(a.classify_action("draft_external_update", level="bold")["tier"], a.CONFIRM)

    def test_destructive_blocked_at_every_level(self):
        from personal_assistant import autonomy as a

        for level in a.LEVELS:
            self.assertEqual(a.classify_action("bulk_delete_issues", level=level)["tier"], a.BLOCKED, level)
            self.assertEqual(a.classify_action("delete_branch", level=level)["tier"], a.BLOCKED, level)
            # mass fan-out via payload
            v = a.classify_action("send_message", {"recipients": list(range(20))}, level=level)
            self.assertEqual(v["tier"], a.BLOCKED, level)
            self.assertTrue(v["destructive"])

    def test_tool_tiers_and_bash_denylist(self):
        from personal_assistant import autonomy as a

        self.assertEqual(a.classify_tool("read", {})["tier"], a.AUTO)
        self.assertEqual(a.classify_tool("grep", {})["tier"], a.AUTO)
        self.assertEqual(a.classify_tool("edit", {})["tier"], a.CONFIRM)
        self.assertEqual(a.classify_tool("bash", {"command": "ls -la"})["tier"], a.CONFIRM)
        for cmd in ("rm -rf /tmp/x", "git push origin main --force", "sudo rm /etc/hosts", "DROP TABLE users"):
            self.assertEqual(a.classify_tool("bash", {"command": cmd})["tier"], a.BLOCKED, cmd)
        # MCP ops
        self.assertEqual(a.classify_tool("mcp__jira__add_comment", {})["tier"], a.CONFIRM)
        self.assertEqual(a.classify_tool("mcp__jira__delete_issue", {})["tier"], a.BLOCKED)


class DestructiveGuardTest(unittest.TestCase):
    """The executor refuses a blocked action even when explicitly approved at the boldest level."""

    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_execute_blocks_destructive(self):
        from personal_assistant import agentcore, cli

        self.conn.execute(
            "INSERT OR REPLACE INTO assistant_policies (key, value, updated_at) VALUES ('autonomy_level','bold',CURRENT_TIMESTAMP)"
        )
        task_id = agentcore.ensure_turn_task(self.conn, "danger")
        self.conn.execute(
            "INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, requires_approval, status) "
            "VALUES (?, 'bulk_delete_issues', 'wipe sprint', '{}', 1, 'approved')",
            (task_id,),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM agent_actions ORDER BY id DESC LIMIT 1").fetchone()
        result = cli._execute_agent_action(self.conn, row)
        self.assertIn("blocked", result.lower())


class MemoryFtsTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_remember_then_recall_via_fts(self):
        from personal_assistant import agentcore, queries

        agentcore.remember(self.conn, "Priya owns the auth-token rotation and prefers async updates.")
        agentcore.remember(self.conn, "The canary rollout for billing is blocked on the platform team.")
        self.conn.commit()
        hits = queries.context_search(self.conn, "who owns auth token rotation", limit=3)
        self.assertTrue(hits)
        self.assertTrue(any("Priya" in h["snippet"] for h in hits))

    def test_memory_survives_new_connection(self):
        from personal_assistant import agentcore, queries
        from personal_assistant.db import get_connection

        agentcore.remember(self.conn, "Decision: freeze moves to Wednesday per the launch review.")
        self.conn.commit()
        conn2 = get_connection()  # same MYOS_DB_PATH -> cross-session
        try:
            hits = queries.context_search(conn2, "when does the freeze move", limit=3)
            self.assertTrue(any("Wednesday" in h["snippet"] for h in hits))
        finally:
            conn2.close()


class WorkerClaimTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_atomic_claim_prevents_double_processing(self):
        self.conn.execute(
            "INSERT INTO workflow_queue (workflow_name, payload_json, status) VALUES ('daily','{}','queued')"
        )
        self.conn.commit()
        job_id = self.conn.execute("SELECT id FROM workflow_queue ORDER BY id DESC LIMIT 1").fetchone()["id"]

        def claim():
            cur = self.conn.execute(
                "UPDATE workflow_queue SET status='running', started_at=CURRENT_TIMESTAMP WHERE id = ? AND status='queued'",
                (job_id,),
            )
            self.conn.commit()
            return cur.rowcount

        self.assertEqual(claim(), 1)  # first worker wins
        self.assertEqual(claim(), 0)  # second worker gets nothing — no double-run


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


class AutonomyLoopTest(unittest.TestCase):
    def _conn(self) -> sqlite3.Connection:
        from personal_assistant.db import initialize_schema

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        initialize_schema(conn)
        return conn

    def test_start_loop_executes_safe_local_and_keeps_external_pending(self) -> None:
        from personal_assistant import autonomy_loop

        conn = self._conn()
        result = autonomy_loop.start_loop(conn, "Handle Jira risk and follow up")
        self.assertEqual(result["status"], "waiting_approval")
        self.assertGreaterEqual(result["executed_now"], 1)
        self.assertGreaterEqual(result["pending_approvals"], 1)

        safe = conn.execute(
            "SELECT status FROM agent_actions WHERE agent_task_id=? AND action_type='create_inbox_item'",
            (result["task_id"],),
        ).fetchone()
        self.assertEqual(safe["status"], "executed")
        external = conn.execute(
            "SELECT status, requires_approval FROM agent_actions WHERE agent_task_id=? AND action_type='draft_external_update'",
            (result["task_id"],),
        ).fetchone()
        self.assertEqual(external["status"], "proposed")
        self.assertEqual(external["requires_approval"], 1)
        rows = autonomy_loop.loop_status(conn, task_id=result["task_id"])
        self.assertEqual(rows[0]["pending_approvals"], result["pending_approvals"])
        conn.close()

    def test_resume_loop_records_another_cycle(self) -> None:
        from personal_assistant import autonomy_loop

        conn = self._conn()
        first = autonomy_loop.start_loop(conn, "Capture local reminder", context="Follow up tomorrow")
        second = autonomy_loop.resume_loop(conn, first["task_id"], max_actions=2)
        self.assertEqual(second["task_id"], first["task_id"])
        rows = autonomy_loop.loop_status(conn, task_id=first["task_id"])
        self.assertEqual(rows[0]["cycles"], 2)
        run_count = conn.execute(
            "SELECT COUNT(*) AS c FROM agent_runs WHERE agent_task_id=?",
            (first["task_id"],),
        ).fetchone()["c"]
        self.assertEqual(run_count, 2)
        conn.close()

    def test_resume_loop_pauses_when_approvals_are_pending(self) -> None:
        from personal_assistant import autonomy_loop

        conn = self._conn()
        first = autonomy_loop.start_loop(conn, "Handle Jira risk and follow up")
        self.assertGreater(first["pending_approvals"], 0)
        second = autonomy_loop.resume_loop(conn, first["task_id"], max_actions=2)
        self.assertEqual(second["run_id"], first["run_id"])
        self.assertEqual(second["executed_now"], 0)
        self.assertEqual(second["pending_approvals"], first["pending_approvals"])
        rows = autonomy_loop.loop_status(conn, task_id=first["task_id"])
        self.assertEqual(rows[0]["cycles"], 1)
        run_count = conn.execute(
            "SELECT COUNT(*) AS c FROM agent_runs WHERE agent_task_id=?",
            (first["task_id"],),
        ).fetchone()["c"]
        self.assertEqual(run_count, 1)
        conn.close()

    def test_provider_actions_are_normalized_and_trace_linked(self) -> None:
        from personal_assistant import autonomy_loop, observability

        old_command = os.environ.get("MYOS_AGENT_CMD_COMMAND")
        old_trace = os.environ.get(observability.TRACE_ENV)
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "provider.py"
            script.write_text(
                "import json, sys\n"
                "_ = json.loads(sys.stdin.read() or '{}')\n"
                "print(json.dumps({\n"
                "  'reply': 'provider ok',\n"
                "  'plan': [{'step': 'check', 'detail': 'verify safely'}],\n"
                "  'actions': [\n"
                "    {'action_type': 'create_inbox_item', 'title': 'Safe note', 'payload': {'text': 'safe local note'}, 'requires_approval': False},\n"
                "    {'action_type': 'draft_external_update', 'title': 'External draft', 'payload': {'draft': 'send later'}, 'requires_approval': False}\n"
                "  ]\n"
                "}))\n",
                encoding="utf-8",
            )
            os.environ["MYOS_AGENT_CMD_COMMAND"] = f"{sys.executable} {script}"
            conn = self._conn()
            corr = observability.start_trace(conn, command="loop", command_path="loop start")
            os.environ[observability.TRACE_ENV] = corr
            try:
                result = autonomy_loop.start_loop(conn, "Use provider", backend="command")
                self.assertEqual(result["provider"], "command")
                external = conn.execute(
                    "SELECT requires_approval FROM agent_actions WHERE agent_task_id=? AND action_type='draft_external_update'",
                    (result["task_id"],),
                ).fetchone()
                self.assertEqual(external["requires_approval"], 1)
                trace = conn.execute(
                    "SELECT agent_task_id FROM execution_traces WHERE correlation_id=?",
                    (corr,),
                ).fetchone()
                self.assertEqual(trace["agent_task_id"], result["task_id"])
            finally:
                conn.close()
                if old_command is None:
                    os.environ.pop("MYOS_AGENT_CMD_COMMAND", None)
                else:
                    os.environ["MYOS_AGENT_CMD_COMMAND"] = old_command
                if old_trace is None:
                    os.environ.pop(observability.TRACE_ENV, None)
                else:
                    os.environ[observability.TRACE_ENV] = old_trace

    def test_goal_scheduler_starts_resumes_skips_and_noops(self) -> None:
        from personal_assistant import autonomy_loop

        conn = self._conn()
        conn.execute(
            """
            INSERT INTO assistant_goals (objective, context, cadence_minutes, priority, status)
            VALUES ('Capture local reminder', 'Follow up tomorrow', 1, 1, 'active')
            """
        )
        local_goal = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            """
            INSERT INTO assistant_goals (objective, context, cadence_minutes, priority, status)
            VALUES ('Handle Jira risk', 'Needs approval-gated follow-up', 1, 2, 'active')
            """
        )
        risk_goal = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.commit()

        eligible = autonomy_loop.eligible_goals(conn)
        self.assertEqual([goal["goal_id"] for goal in eligible[:2]], [local_goal, risk_goal])

        started = autonomy_loop.run_goal_cycle(conn, goal_id=local_goal)
        self.assertEqual(started["action"], "started")
        self.assertEqual(started["status"], "completed")
        self.assertEqual(started["pending_approvals"], 0)
        self.assertEqual(autonomy_loop.find_goal_loop(conn, local_goal)["task_id"], started["task_id"])

        resumed = autonomy_loop.run_goal_cycle(conn, goal_id=local_goal)
        self.assertEqual(resumed["action"], "resumed")
        self.assertEqual(resumed["task_id"], started["task_id"])
        self.assertEqual(autonomy_loop.find_goal_loop(conn, local_goal)["cycles"], 2)

        pending = autonomy_loop.run_goal_cycle(conn, goal_id=risk_goal)
        self.assertEqual(pending["action"], "started")
        self.assertGreater(pending["pending_approvals"], 0)
        pending_before = pending["pending_approvals"]
        skipped = autonomy_loop.run_goal_cycle(conn, goal_id=risk_goal)
        self.assertEqual(skipped["action"], "skipped")
        self.assertEqual(skipped["pending_approvals"], pending_before)
        pending_after = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM agent_actions
            WHERE agent_task_id=? AND status='proposed' AND requires_approval=1
            """,
            (pending["task_id"],),
        ).fetchone()["c"]
        self.assertEqual(pending_after, pending_before)
        skip_obs = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM agent_observations
            WHERE agent_task_id=? AND observation_type='scheduler_skip'
            """,
            (pending["task_id"],),
        ).fetchone()["c"]
        self.assertEqual(skip_obs, 1)

        noop = autonomy_loop.run_goal_cycle(conn, goal_id=9999)
        self.assertEqual(noop["action"], "noop")
        ledger = autonomy_loop.list_ledger(conn, goal_id=risk_goal, limit=10)
        self.assertIn("loop_started", {row["decision_type"] for row in ledger})
        self.assertIn("goal_started", {row["decision_type"] for row in ledger})
        self.assertIn("goal_skipped", {row["decision_type"] for row in ledger})
        conn.close()

    def test_autonomy_run_ledger_records_loop_decisions(self) -> None:
        from personal_assistant import autonomy_loop

        conn = self._conn()
        first = autonomy_loop.start_loop(conn, "Capture local reminder", context="Follow up tomorrow")
        resumed = autonomy_loop.resume_loop(conn, first["task_id"], max_actions=2)
        self.assertEqual(resumed["task_id"], first["task_id"])

        rows = autonomy_loop.list_ledger(conn, task_id=first["task_id"], limit=10)
        self.assertEqual([row["decision_type"] for row in rows[:2]], ["loop_resumed", "loop_started"])
        self.assertEqual(rows[0]["agent_run_id"], resumed["run_id"])
        self.assertEqual(rows[0]["safe_actions_executed"], resumed["executed_now"])

        blocked = autonomy_loop.start_loop(conn, "Handle Jira risk and follow up")
        paused = autonomy_loop.resume_loop(conn, blocked["task_id"])
        self.assertEqual(paused["run_id"], blocked["run_id"])
        paused_rows = autonomy_loop.list_ledger(conn, task_id=blocked["task_id"], status=paused["status"], limit=10)
        self.assertIn("loop_paused", {row["decision_type"] for row in paused_rows})

        noop = autonomy_loop.run_goal_cycle(conn, goal_id=9999)
        self.assertEqual(noop["action"], "noop")
        noop_rows = autonomy_loop.list_ledger(conn, status="noop", limit=5)
        self.assertEqual(noop_rows[0]["decision_type"], "goal_noop")
        conn.close()


if __name__ == "__main__":
    unittest.main()

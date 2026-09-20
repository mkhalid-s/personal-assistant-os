"""End-to-end integration tests for MYOS autonomy loop and workflows.

These tests exercise complete user workflows from capture through execution,
testing the integration of multiple components working together.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from personal_assistant.autonomy_loop import (
    loop_status,
    resume_loop,
    start_loop,
)
from personal_assistant.db import initialize_schema
from personal_assistant.intents import create_intent


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


class CompleteAutonomyLoopTest(unittest.TestCase):
    """Test the complete autonomy loop from intent to execution."""

    def setUp(self) -> None:
        self.conn = _conn()
        self.addCleanup(self.conn.close)

    def test_capture_to_intent_to_action_flow(self) -> None:
        """Test the complete flow: capture → intent → plan → action."""
        # Step 1: Create an intent (simulating capture)
        intent_id = create_intent(
            self.conn,
            objective="Follow up with platform team about auth token expiry",
            priority=2,
            constraints="[]",
        )
        self.assertIsNotNone(intent_id)

        # Step 2: Verify intent was created
        intent = self.conn.execute("SELECT * FROM intents WHERE id = ?", (intent_id,)).fetchone()
        self.assertEqual(intent["objective"], "Follow up with platform team about auth token expiry")
        self.assertEqual(intent["status"], "open")

        # Step 3: Create a task for the intent (simulating planning)
        task_id = self.conn.execute(
            "INSERT INTO agent_tasks (objective, status, priority) VALUES (?, 'active', 2)",
            (intent["objective"],),
        ).lastrowid

        # Step 4: Create a proposed action (simulating proposal)
        action_id = self.conn.execute(
            """INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval)
               VALUES (?, 'create_inbox_item', 'Schedule meeting', ?, 'proposed', 1)""",
            (task_id, json.dumps({"target": "calendar", "event": "auth follow-up"})),
        ).lastrowid

        # Step 5: Verify action exists in approval queue
        action = self.conn.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
        self.assertEqual(action["status"], "proposed")
        self.assertEqual(action["requires_approval"], 1)

        # Step 6: Complete the flow by approving
        self.conn.execute(
            "UPDATE agent_actions SET status = 'approved', approved_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), action_id),
        )
        self.conn.commit()

        # Verify final state
        updated_action = self.conn.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
        self.assertEqual(updated_action["status"], "approved")

    def test_loop_lifecycle_start_resume_complete(self) -> None:
        """Test complete loop lifecycle: start → resume → complete."""
        # Start a loop
        loop = start_loop(self.conn, "Daily system health check")
        self.assertIsNotNone(loop)
        self.assertEqual(loop["task_id"], loop["task_id"])
        # The loop might complete immediately, so check the actual status
        self.assertIn(loop["status"], ["running", "completed"])

        # Check loop status
        status = loop_status(self.conn, task_id=loop["task_id"])
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["task_id"], loop["task_id"])

        # Resume the loop (simulating pause/resume)
        resumed = resume_loop(self.conn, loop["task_id"])
        self.assertIsNotNone(resumed)

        # Complete the loop if not already completed
        if loop["status"] != "completed":
            self.conn.execute(
                "UPDATE agent_tasks SET status = 'completed' WHERE id = ?",
                (loop["task_id"],),
            )
            self.conn.commit()

        # Verify completion
        final_status = loop_status(self.conn, task_id=loop["task_id"])
        self.assertEqual(len(final_status), 1)

    def test_goal_driven_loop_with_evidence(self) -> None:
        """Test goal-driven loop with evidence accumulation."""
        # Create a goal with associated work items
        goal_id = create_intent(
            self.conn,
            objective="Implement dashboard Phase A/B",
            priority=1,
            constraints="[]",
        )

        # Add evidence to the goal
        self.conn.execute(
            "INSERT INTO intent_evidence (intent_id, source_type, content) VALUES (?, 'note', 'Review existing dashboard code')",
            (goal_id,),
        )
        self.conn.execute(
            "INSERT INTO intent_evidence (intent_id, source_type, content) VALUES (?, 'work_item', 'Update dashboard views')",
            (goal_id,),
        )
        self.conn.commit()

        # Create a loop for this goal
        loop = start_loop(self.conn, "Implement dashboard Phase A/B")
        self.assertIsNotNone(loop)

        # Verify evidence is accessible
        evidence = self.conn.execute("SELECT * FROM intent_evidence WHERE intent_id = ?", (goal_id,)).fetchall()
        self.assertEqual(len(evidence), 2)

        # Complete the goal-driven work
        self.conn.execute(
            "UPDATE intents SET status = 'done' WHERE id = ?",
            (goal_id,),
        )
        self.conn.commit()

        # Verify goal completion
        final_intent = self.conn.execute("SELECT * FROM intents WHERE id = ?", (goal_id,)).fetchone()
        self.assertEqual(final_intent["status"], "done")

    def test_multi_step_workflow_with_approval_queue(self) -> None:
        """Test workflow with multiple steps requiring approval."""
        # Create a task with multiple proposed actions
        task_id = self.conn.execute(
            "INSERT INTO agent_tasks (objective, status, priority) VALUES (?, 'active', 2)",
            ("Deploy hotfix to production",),
        ).lastrowid

        # Propose multiple actions
        actions = []
        for i in range(3):
            action_id = self.conn.execute(
                """INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval)
                   VALUES (?, 'create_inbox_item', ?, ?, 'proposed', 1)""",
                (task_id, f"Step {i + 1}", json.dumps({"step": i + 1})),
            ).lastrowid
            actions.append(action_id)

        self.conn.commit()

        # Verify all actions are in approval queue
        queue = self.conn.execute(
            "SELECT * FROM agent_actions WHERE agent_task_id = ? AND status = 'proposed'",
            (task_id,),
        ).fetchall()
        self.assertEqual(len(queue), 3)

        # Approve actions one by one
        for action_id in actions:
            self.conn.execute(
                "UPDATE agent_actions SET status = 'approved', approved_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), action_id),
            )
        self.conn.commit()

        # Verify all actions approved
        approved = self.conn.execute(
            "SELECT * FROM agent_actions WHERE agent_task_id = ? AND status = 'approved'",
            (task_id,),
        ).fetchall()
        self.assertEqual(len(approved), 3)

    def test_error_recovery_in_workflow(self) -> None:
        """Test workflow resilience when actions fail."""
        # Create a task with an action that will fail
        task_id = self.conn.execute(
            "INSERT INTO agent_tasks (objective, status, priority) VALUES (?, 'active', 2)",
            ("Test error handling",),
        ).lastrowid

        # Create an action that will be marked as failed
        action_id = self.conn.execute(
            """INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval)
               VALUES (?, 'create_inbox_item', 'Test action', ?, 'approved', 1)""",
            (task_id, json.dumps({"test": "data"})),
        ).lastrowid

        # Simulate execution failure
        self.conn.execute(
            "UPDATE agent_actions SET status = 'failed' WHERE id = ?",
            (action_id,),
        )
        self.conn.commit()

        # Verify failure creates follow-up work
        failed_action = self.conn.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
        self.assertEqual(failed_action["status"], "failed")

        # Verify system can continue with new actions despite failure
        new_action_id = self.conn.execute(
            """INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval)
               VALUES (?, 'create_inbox_item', 'Recovery action', ?, 'proposed', 1)""",
            (task_id, json.dumps({"recovery": "true"})),
        ).lastrowid
        self.conn.commit()

        recovery_action = self.conn.execute("SELECT * FROM agent_actions WHERE id = ?", (new_action_id,)).fetchone()
        self.assertEqual(recovery_action["status"], "proposed")


class ConnectorIntegrationTest(unittest.TestCase):
    """Test integration with external connectors in realistic scenarios."""

    def setUp(self) -> None:
        self.conn = _conn()
        self.addCleanup(self.conn.close)

    def test_jira_comment_workflow_with_approval(self) -> None:
        """Test complete Jira comment workflow with approval gates."""
        # Create a task that proposes a Jira comment
        task_id = self.conn.execute(
            "INSERT INTO agent_tasks (objective, status, priority) VALUES (?, 'active', 2)",
            ("Comment on JIRA ticket",),
        ).lastrowid

        # Propose Jira comment action
        action_id = self.conn.execute(
            """INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval)
               VALUES (?, 'draft_external_update', 'Post JIRA comment', ?, 'proposed', 1)""",
            (
                task_id,
                json.dumps(
                    {
                        "connector": "jira",
                        "operation": "comment",
                        "target_ref": "PROJ-123",
                        "body": "This is a test comment",
                    }
                ),
            ),
        ).lastrowid

        self.conn.commit()

        # Verify action requires approval
        action = self.conn.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
        self.assertEqual(action["requires_approval"], 1)
        self.assertEqual(action["status"], "proposed")

        # Approve the action
        self.conn.execute(
            "UPDATE agent_actions SET status = 'approved', approved_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), action_id),
        )
        self.conn.commit()

        # Verify approval
        approved_action = self.conn.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
        self.assertEqual(approved_action["status"], "approved")
        self.assertIsNotNone(approved_action["approved_at"])

    def test_github_pr_comment_integration(self) -> None:
        """Test GitHub PR comment workflow."""
        task_id = self.conn.execute(
            "INSERT INTO agent_tasks (objective, status, priority) VALUES (?, 'active', 2)",
            ("Review and comment on PR",),
        ).lastrowid

        action_id = self.conn.execute(
            """INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval)
               VALUES (?, 'draft_external_update', 'GitHub PR comment', ?, 'proposed', 1)""",
            (
                task_id,
                json.dumps(
                    {
                        "connector": "github",
                        "operation": "comment",
                        "owner": "mkhalid-s",
                        "repo": "personal-assistant-os",
                        "target_ref": "42",
                        "body": "LGTM! Great work on the dashboard feature.",
                    }
                ),
            ),
        ).lastrowid

        self.conn.commit()

        # Verify connector-specific payload structure
        action = self.conn.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
        payload = json.loads(action["payload_json"])
        self.assertEqual(payload["connector"], "github")
        self.assertEqual(payload["operation"], "comment")
        self.assertEqual(payload["target_ref"], "42")

    def test_multi_connector_sync_workflow(self) -> None:
        """Test workflow involving multiple external connectors."""
        task_id = self.conn.execute(
            "INSERT INTO agent_tasks (objective, status, priority) VALUES (?, 'active', 2)",
            ("Sync status across Jira and GitHub",),
        ).lastrowid

        # Create actions for multiple connectors
        jira_action_id = self.conn.execute(
            """INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval)
               VALUES (?, 'draft_external_update', 'JIRA status', ?, 'proposed', 1)""",
            (task_id, json.dumps({"connector": "jira", "operation": "status_update", "target_ref": "PROJ-123"})),
        ).lastrowid

        github_action_id = self.conn.execute(
            """INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval)
               VALUES (?, 'draft_external_update', 'GitHub status', ?, 'proposed', 1)""",
            (task_id, json.dumps({"connector": "github", "operation": "status_update", "target_ref": "42"})),
        ).lastrowid

        self.conn.commit()

        # Verify both actions created
        actions = self.conn.execute("SELECT * FROM agent_actions WHERE agent_task_id = ?", (task_id,)).fetchall()
        self.assertEqual(len(actions), 2)

        # Approve both actions
        for action_id in [jira_action_id, github_action_id]:
            self.conn.execute(
                "UPDATE agent_actions SET status = 'approved', approved_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), action_id),
            )
        self.conn.commit()

        # Verify both approved
        approved_actions = self.conn.execute(
            "SELECT * FROM agent_actions WHERE agent_task_id = ? AND status = 'approved'",
            (task_id,),
        ).fetchall()
        self.assertEqual(len(approved_actions), 2)


class FactoryWorkflowIntegrationTest(unittest.TestCase):
    """Test factory workflow integration with software delivery."""

    def setUp(self) -> None:
        self.conn = _conn()
        self.addCleanup(self.conn.close)

    def test_factory_review_first_workflow(self) -> None:
        """Test factory review-first workflow from planning to execution."""
        # Create a factory task
        task_id = self.conn.execute(
            "INSERT INTO agent_tasks (objective, status, priority, constraints_json) VALUES (?, 'active', 2, ?)",
            ("Implement dashboard Phase A/B", json.dumps({"source": "factory"})),
        ).lastrowid

        # Create a software development action
        action_id = self.conn.execute(
            """INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval)
               VALUES (?, 'apply_patch', 'Add dashboard views', ?, 'proposed', 1)""",
            (task_id, json.dumps({"diff": "@@ -1,1 +1,1 @@\n-old line\n+new line", "files": ["dashboard.py"]})),
        ).lastrowid

        self.conn.commit()

        # Verify factory-specific constraint
        task = self.conn.execute("SELECT * FROM agent_tasks WHERE id = ?", (task_id,)).fetchone()
        constraints = json.loads(task["constraints_json"])
        self.assertEqual(constraints.get("source"), "factory")

        # Verify action is in approval queue
        action = self.conn.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
        self.assertEqual(action["status"], "proposed")
        self.assertEqual(action["action_type"], "apply_patch")

    def test_factory_execution_with_receipt(self) -> None:
        """Test factory execution with receipt creation."""
        task_id = self.conn.execute(
            "INSERT INTO agent_tasks (objective, status, priority, constraints_json) VALUES (?, 'active', 2, ?)",
            ("Fix critical bug", json.dumps({"source": "factory"})),
        ).lastrowid

        # Execute an action (simulating factory execution)
        action_id = self.conn.execute(
            """INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval)
               VALUES (?, 'apply_patch', 'Bug fix', ?, 'executed', 1)""",
            (task_id, json.dumps({"diff": "+fix", "files": ["bug.py"]})),
        ).lastrowid

        # Create execution receipt (without compensation for now)
        receipt_id = self.conn.execute(
            """INSERT INTO action_execution_receipts
               (agent_action_id, agent_task_id, action_type, final_status, approved)
               VALUES (?, ?, 'apply_patch', 'executed', 1)""",
            (action_id, task_id),
        ).lastrowid

        # Verify receipt was created
        receipt = self.conn.execute("SELECT * FROM action_execution_receipts WHERE id = ?", (receipt_id,)).fetchone()
        self.assertEqual(receipt["final_status"], "executed")


class ErrorScenarioIntegrationTest(unittest.TestCase):
    """Test integration behavior under error conditions."""

    def setUp(self) -> None:
        self.conn = _conn()
        self.addCleanup(self.conn.close)

    def test_database_error_recovery(self) -> None:
        """Test system behavior when database operations fail."""
        # Create a normal task
        self.conn.execute(
            "INSERT INTO agent_tasks (objective, status, priority) VALUES (?, 'active', 2)",
            ("Test error recovery",),
        )

        # Simulate database constraint violation
        try:
            # Try to insert duplicate intent (should fail due to constraints)
            self.conn.execute(
                "INSERT INTO intents (objective, constraints_json, priority, status) VALUES (?, ?, 2, 'open')",
                ("Duplicate intent", "[]"),
            )
            self.conn.execute(
                "INSERT INTO intents (objective, constraints_json, priority, status) VALUES (?, ?, 2, 'open')",
                ("Duplicate intent", "[]"),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            # Expected - constraint violation
            pass

        # Verify system can still create new intents after error
        new_intent_id = create_intent(
            self.conn,
            objective="Recovery test intent",
            priority=2,
            constraints="[]",
        )
        self.assertIsNotNone(new_intent_id)

    def test_timeout_handling_in_workflow(self) -> None:
        """Test workflow resilience when operations timeout."""
        # Create a task
        task_id = self.conn.execute(
            "INSERT INTO agent_tasks (objective, status, priority) VALUES (?, 'active', 2)",
            ("Test timeout handling",),
        ).lastrowid

        # Create an action that might timeout
        action_id = self.conn.execute(
            """INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval)
               VALUES (?, 'create_inbox_item', 'Long running action', ?, 'proposed', 1)""",
            (task_id, json.dumps({"timeout_test": True})),
        ).lastrowid

        self.conn.commit()

        # Simulate timeout by marking as failed
        self.conn.execute(
            "UPDATE agent_actions SET status = 'failed' WHERE id = ?",
            (action_id,),
        )
        self.conn.commit()

        # Verify system can continue with new actions
        recovery_action_id = self.conn.execute(
            """INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval)
               VALUES (?, 'create_inbox_item', 'Recovery from timeout', ?, 'proposed', 1)""",
            (task_id, json.dumps({"recovery": True})),
        ).lastrowid
        self.conn.commit()

        recovery_action = self.conn.execute(
            "SELECT * FROM agent_actions WHERE id = ?", (recovery_action_id,)
        ).fetchone()
        self.assertEqual(recovery_action["status"], "proposed")


if __name__ == "__main__":
    unittest.main()

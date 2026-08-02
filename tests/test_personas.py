from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from personal_assistant import personas
from personal_assistant.db import initialize_schema
from personal_assistant.planner import _agent_analogies
from personal_assistant.providers import BaseBackend


class PersonaCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = sqlite3.connect(Path(self.tmp.name) / "assistant.db")
        self.conn.row_factory = sqlite3.Row
        initialize_schema(self.conn)
        self.addCleanup(self.conn.close)

    def test_builtin_personas_are_seeded_with_narrow_action_scopes(self) -> None:
        rows = personas.list_personas(self.conn)
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(
            set(by_name),
            {"chief-of-staff", "coach", "engineer", "operator", "researcher", "reviewer"},
        )
        self.assertNotIn("draft_external_update", by_name["researcher"]["allowed_actions"])
        self.assertIn("apply_patch", by_name["engineer"]["allowed_actions"])

    def test_custom_persona_is_redacted_and_filters_actions(self) -> None:
        persona = personas.create_persona(
            self.conn,
            name="focus-guide",
            display_name="Focus Guide",
            description="Contact owner@example.com",
            instructions="Help owner@example.com choose one next step.",
            allowed_actions=["create_inbox_item"],
            retrieval_scopes=["work_items"],
        )
        self.assertNotIn("owner@example.com", persona["instructions"])
        accepted, rejected = personas.filter_actions(
            persona,
            [
                {"action_type": "create_inbox_item", "title": "Local", "payload": {}},
                {"action_type": "draft_external_update", "title": "External", "payload": {}},
            ],
        )
        self.assertEqual([row["action_type"] for row in accepted], ["create_inbox_item"])
        self.assertEqual(rejected, ["draft_external_update"])

    def test_empty_retrieval_scope_does_not_fall_back_to_global_memory(self) -> None:
        self.conn.execute(
            "INSERT INTO work_items (title, kind, status) VALUES ('Private launch context', 'task', 'open')"
        )
        self.conn.execute("INSERT INTO agent_tasks (objective, constraints_json) VALUES ('x', '{}')")
        self.conn.execute(
            "INSERT INTO agent_observations (agent_task_id, observation_type, content) VALUES (1, 'note', 'Private launch memory')"
        )
        self.conn.commit()
        self.assertEqual(_agent_analogies(self.conn, "private launch", scopes=set()), [])
        self.assertTrue(_agent_analogies(self.conn, "private launch", scopes={"work_items"}))

    def test_structured_chat_backend_filters_disallowed_persona_actions(self) -> None:
        class FakeBackend(BaseBackend):
            name = "fake"

            def reason(self, conn, request):
                self.objective = request["objective"]
                return {
                    "reply": "reviewed",
                    "actions": [
                        {"action_type": "create_inbox_item", "title": "keep", "payload": {}},
                        {"action_type": "draft_external_update", "title": "drop", "payload": {}},
                    ],
                }

        reviewer = personas.get_persona(self.conn, "reviewer")
        self.assertIsNotNone(reviewer)
        backend = FakeBackend()
        result = backend.run_turn(self.conn, "review release", [], persona=reviewer)
        self.assertIn("Reviewer", backend.objective)
        self.assertEqual(result["rejected_action_types"], ["draft_external_update"])
        rows = self.conn.execute("SELECT action_type FROM agent_actions ORDER BY id").fetchall()
        self.assertEqual([row["action_type"] for row in rows], ["create_inbox_item"])

    def test_claude_persona_tools_are_narrowed_and_dispatch_is_fail_closed(self) -> None:
        from personal_assistant.providers.claude import ClaudeBackend

        reviewer = personas.get_persona(self.conn, "reviewer")
        chief = personas.get_persona(self.conn, "chief-of-staff")
        assert reviewer is not None and chief is not None
        reviewer_tools = {tool["name"] for tool in ClaudeBackend._tools_for_persona(reviewer)}
        chief_tools = {tool["name"] for tool in ClaudeBackend._tools_for_persona(chief)}
        self.assertNotIn("propose_slack_message", reviewer_tools)
        self.assertIn("propose_slack_message", chief_tools)
        ctx = {"task_id": None, "ids": [], "allowed_tools": reviewer_tools, "rejected_tools": []}
        output, is_error = ClaudeBackend()._dispatch(
            self.conn,
            "propose_slack_message",
            {"channel": "team", "text": "ship it"},
            ctx,
        )
        self.assertTrue(is_error)
        self.assertIn("blocked by active persona", output)
        self.assertEqual(ctx["rejected_tools"], ["propose_slack_message"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM agent_actions").fetchone()[0], 0)

    def test_context_search_honors_persona_source_types(self) -> None:
        from personal_assistant import queries
        from personal_assistant.inbox import index_chunk

        index_chunk(self.conn, "work_item", 1, "Project aurora deadline")
        index_chunk(self.conn, "conversation", 1, "Private aurora reflection")
        self.conn.commit()
        hits = queries.context_search(self.conn, "aurora", source_types={"work_item"})
        self.assertTrue(hits)
        self.assertEqual({hit["source_type"] for hit in hits}, {"work_item"})
        self.assertEqual(queries.context_search(self.conn, "aurora", source_types=set()), [])

    def test_factory_carries_persona_and_filters_execution_actions(self) -> None:
        from personal_assistant import factory, intents

        intent_id = intents.create_intent(self.conn, objective="Prepare the aurora operating update")
        factory.set_policy(
            self.conn,
            allowed_mode="semi_autonomous",
            scope_type="intent",
            scope_id=str(intent_id),
        )
        result = factory.start_review_first_run(
            self.conn,
            intent_id=intent_id,
            mode="semi_autonomous",
            workflow_pack="daily_ops",
            persona_name="reviewer",
        )
        self.assertEqual(result["persona"], "reviewer")
        run = factory.get_factory_run(self.conn, result["id"])
        assert run is not None
        self.assertEqual(run["persona_name"], "reviewer")
        actions = self.conn.execute("SELECT action_type FROM agent_actions ORDER BY id").fetchall()
        self.assertEqual([row["action_type"] for row in actions], ["create_inbox_item"])
        packet = self.conn.execute(
            "SELECT plan_json FROM agent_runs WHERE agent_name='reviewer' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(json.loads(packet["plan_json"])["persona"], "reviewer")

    def test_factory_rejects_unknown_persona(self) -> None:
        from personal_assistant import factory, intents

        intent_id = intents.create_intent(self.conn, objective="Review the release")
        with self.assertRaisesRegex(ValueError, "persona not found"):
            factory.start_review_first_run(self.conn, intent_id=intent_id, persona_name="missing")


class PersonaCliTest(unittest.TestCase):
    def test_persona_cli_and_delegation_enforce_action_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "assistant.db"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                result = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return result.stdout

            listed = json.loads(run("persona", "list", "--json"))
            self.assertEqual(listed["schema"], "myos.persona.list.v1")
            self.assertEqual(listed["count"], 6)
            output = run(
                "delegate",
                "Risk: Jira dependency is blocked",
                "--persona",
                "reviewer",
                "--max-actions",
                "5",
            )
            self.assertIn("Persona: Reviewer (reviewer)", output)
            self.assertIn("Persona policy rejected actions: draft_external_update", output)

            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute("SELECT action_type, requires_approval FROM agent_actions ORDER BY id").fetchall()
            finally:
                conn.close()
            self.assertEqual([row[0] for row in rows], ["create_inbox_item", "draft_message"])
            self.assertEqual([row[1] for row in rows], [0, 1])


if __name__ == "__main__":
    unittest.main()

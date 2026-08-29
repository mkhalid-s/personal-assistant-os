"""Tests for the Confluence/Aha live adapters (G2) and persona guard (G7)."""

from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from personal_assistant.db import initialize_schema
from personal_assistant.execution import (
    _post_aha_comment,
    _post_confluence_comment,
    approve_and_execute,
    execute_connector_mutation,
)
from personal_assistant.personas import ensure_builtin_personas


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    ensure_builtin_personas(conn)
    conn.commit()
    return conn


def _seed_task_and_action(
    conn: sqlite3.Connection,
    action_type: str = "local_note",
    persona: str = "",
    requires_approval: int = 0,
) -> tuple[int, int]:
    meta = {}
    if persona:
        meta["persona"] = persona
    task_id = conn.execute(
        "INSERT INTO agent_tasks (objective, constraints_json) VALUES (?, ?)",
        ("test", json.dumps(meta)),
    ).lastrowid
    action_id = conn.execute(
        "INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, requires_approval) "
        "VALUES (?, ?, 'test', ?, ?)",
        (task_id, action_type, json.dumps({"body": "hello"}), requires_approval),
    ).lastrowid
    conn.commit()
    return task_id, action_id


# ---------------------------------------------------------------------------
# G2 — _post_confluence_comment
# ---------------------------------------------------------------------------


class PostConfluenceCommentTest(unittest.TestCase):
    def test_raises_on_missing_credentials(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _post_confluence_comment("12345", "some body")
        self.assertIn("Confluence", str(ctx.exception))

    def test_raises_on_missing_page_id(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {
                    "CONFLUENCE_BASE_URL": "https://example.atlassian.net",
                    "CONFLUENCE_USER_EMAIL": "user@example.com",
                    "CONFLUENCE_API_TOKEN": "token",
                },
            ),
            self.assertRaises(ValueError),
        ):
            _post_confluence_comment("", "body")

    def test_sends_correct_request(self) -> None:
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"id":"1","type":"comment"}'

        with (
            patch.dict(
                "os.environ",
                {
                    "CONFLUENCE_BASE_URL": "https://example.atlassian.net",
                    "CONFLUENCE_USER_EMAIL": "user@example.com",
                    "CONFLUENCE_API_TOKEN": "tok",
                },
            ),
            patch("urllib.request.urlopen", return_value=mock_resp) as mock_open,
        ):
            result = _post_confluence_comment("42", "Test comment body")

        req = mock_open.call_args[0][0]
        self.assertIn("/wiki/rest/api/content", req.full_url)
        sent = json.loads(req.data.decode())
        self.assertEqual(sent["type"], "comment")
        self.assertEqual(sent["container"]["id"], "42")
        self.assertIn("Test comment body", sent["body"]["storage"]["value"])
        self.assertIn("Basic ", req.get_header("Authorization"))
        self.assertIn("comment", result)


# ---------------------------------------------------------------------------
# G2 — _post_aha_comment
# ---------------------------------------------------------------------------


class PostAhaCommentTest(unittest.TestCase):
    def test_raises_on_missing_credentials(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _post_aha_comment({"target_ref": "MYOS-1"}, "body")
        self.assertIn("Aha", str(ctx.exception))

    def test_raises_on_missing_target_ref(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {
                    "AHA_BASE_URL": "https://company.aha.io",
                    "AHA_API_KEY": "key",
                },
            ),
            self.assertRaises(ValueError),
        ):
            _post_aha_comment({}, "body")

    def test_sends_feature_comment_by_default(self) -> None:
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"comment":{"id":"99"}}'

        with (
            patch.dict(
                "os.environ",
                {
                    "AHA_BASE_URL": "https://company.aha.io",
                    "AHA_API_KEY": "apikey",
                },
            ),
            patch("urllib.request.urlopen", return_value=mock_resp) as mock_open,
        ):
            _post_aha_comment({"target_ref": "MYOS-42"}, "Feature comment")

        req = mock_open.call_args[0][0]
        self.assertIn("/features/MYOS-42/comments", req.full_url)
        sent = json.loads(req.data.decode())
        self.assertEqual(sent["comment"]["body"], "Feature comment")
        self.assertIn("Bearer ", req.get_header("Authorization"))

    def test_sends_idea_comment_when_target_type_is_idea(self) -> None:
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"comment":{"id":"7"}}'

        with (
            patch.dict(
                "os.environ",
                {
                    "AHA_BASE_URL": "https://company.aha.io",
                    "AHA_API_KEY": "apikey",
                },
            ),
            patch("urllib.request.urlopen", return_value=mock_resp) as mock_open,
        ):
            _post_aha_comment({"target_ref": "IDEA-1", "target_type": "idea"}, "Idea comment")

        req = mock_open.call_args[0][0]
        self.assertIn("/ideas/IDEA-1/comments", req.full_url)


# ---------------------------------------------------------------------------
# G2 — execute_connector_mutation dispatch for confluence/aha
# ---------------------------------------------------------------------------


class ConnectorMutationDispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def _base_payload(self, connector: str) -> dict:
        return {
            "target": connector,
            "operation": "comment",
            "target_ref": "REF-1",
            "body": "hello",
            "dry_run": False,
        }

    def test_confluence_draft_does_not_call_live_adapter(self) -> None:
        payload = {**self._base_payload("confluence"), "dry_run": True}
        result = execute_connector_mutation(
            self.conn,
            agent_action_id=None,
            action_type="draft_external_update",
            title="test",
            payload=payload,
            approved=True,
            execute_live=False,
        )
        self.assertEqual(result["status"], "drafted")

    def test_aha_draft_does_not_call_live_adapter(self) -> None:
        payload = {**self._base_payload("aha"), "dry_run": True}
        result = execute_connector_mutation(
            self.conn,
            agent_action_id=None,
            action_type="draft_external_update",
            title="test",
            payload=payload,
            approved=True,
            execute_live=False,
        )
        self.assertEqual(result["status"], "drafted")

    def test_confluence_live_calls_post_confluence_comment(self) -> None:
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"id":"1"}'

        with (
            patch.dict(
                "os.environ",
                {
                    "CONFLUENCE_BASE_URL": "https://x.atlassian.net",
                    "CONFLUENCE_USER_EMAIL": "u@x.com",
                    "CONFLUENCE_API_TOKEN": "tok",
                },
            ),
            patch("personal_assistant.execution._connector_live_enabled", return_value=True),
            patch("urllib.request.urlopen", return_value=mock_resp),
        ):
            result = execute_connector_mutation(
                self.conn,
                agent_action_id=None,
                action_type="draft_external_update",
                title="test",
                payload=self._base_payload("confluence"),
                approved=True,
                execute_live=True,
            )
        self.assertEqual(result["status"], "sent")

    def test_aha_live_calls_post_aha_comment(self) -> None:
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"comment":{"id":"9"}}'

        with (
            patch.dict(
                "os.environ",
                {
                    "AHA_BASE_URL": "https://company.aha.io",
                    "AHA_API_KEY": "apikey",
                },
            ),
            patch("personal_assistant.execution._connector_live_enabled", return_value=True),
            patch("urllib.request.urlopen", return_value=mock_resp),
        ):
            result = execute_connector_mutation(
                self.conn,
                agent_action_id=None,
                action_type="draft_external_update",
                title="test",
                payload=self._base_payload("aha"),
                approved=True,
                execute_live=True,
            )
        self.assertEqual(result["status"], "sent")


# ---------------------------------------------------------------------------
# G7 — persona guard in approve_and_execute
# ---------------------------------------------------------------------------


class PersonaExecutionGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_no_persona_allows_any_action_type(self) -> None:
        _, action_id = _seed_task_and_action(self.conn, action_type="local_note", persona="")
        result = approve_and_execute(self.conn, action_id)
        self.assertNotEqual(result["code"], "blocked")

    def test_persona_blocks_disallowed_action_type(self) -> None:
        # 'researcher' allows only create_inbox_item, remember, draft_summary
        _, action_id = _seed_task_and_action(self.conn, action_type="apply_patch", persona="researcher")
        result = approve_and_execute(self.conn, action_id)
        self.assertEqual(result["code"], "blocked")
        self.assertIn("researcher", result["result"])
        self.assertIn("apply_patch", result["result"])

    def test_persona_allows_permitted_action_type(self) -> None:
        # 'researcher' allows create_inbox_item
        _, action_id = _seed_task_and_action(self.conn, action_type="create_inbox_item", persona="researcher")
        result = approve_and_execute(self.conn, action_id)
        self.assertNotEqual(result["code"], "blocked")

    def test_persona_block_sets_status_to_blocked(self) -> None:
        _, action_id = _seed_task_and_action(self.conn, action_type="apply_patch", persona="researcher")
        approve_and_execute(self.conn, action_id)
        row = self.conn.execute("SELECT status FROM agent_actions WHERE id=?", (action_id,)).fetchone()
        self.assertEqual(row["status"], "blocked")

    def test_persona_block_appends_event(self) -> None:
        _, action_id = _seed_task_and_action(self.conn, action_type="apply_patch", persona="researcher")
        approve_and_execute(self.conn, action_id)
        # event_log columns: id, event_type, entity_type, entity_id, payload, created_at
        events = self.conn.execute(
            "SELECT event_type FROM event_log WHERE entity_type='agent_action' AND entity_id=?",
            (action_id,),
        ).fetchall()
        event_types = [e["event_type"] for e in events]
        self.assertIn("persona_execution_block", event_types)

    def test_engineer_persona_allows_apply_patch(self) -> None:
        # 'engineer' persona includes apply_patch in allowed_actions
        _, action_id = _seed_task_and_action(self.conn, action_type="apply_patch", persona="engineer")
        result = approve_and_execute(self.conn, action_id)
        self.assertNotEqual(result["code"], "blocked")

    def test_unknown_persona_name_does_not_block(self) -> None:
        # If the persona doesn't exist in DB, guard is skipped (fail-open to
        # avoid locking out operators on misconfigured envs).
        _, action_id = _seed_task_and_action(self.conn, action_type="apply_patch", persona="nonexistent_persona_xyz")
        result = approve_and_execute(self.conn, action_id)
        self.assertNotEqual(result["code"], "blocked")


if __name__ == "__main__":
    unittest.main()

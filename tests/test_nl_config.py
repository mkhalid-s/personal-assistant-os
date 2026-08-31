"""Tests for nl_config.py — NL-to-config extraction."""

from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import patch

from personal_assistant.db import initialize_schema
from personal_assistant.nl_config import (
    ConfigIntent,
    _parse_intent,
    describe_intent,
    execute_intent,
    extract_config_intent,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


def _mock_backend(reply: str, available: bool = True):
    from unittest.mock import MagicMock

    b = MagicMock()
    b.available.return_value = (available, "ok")
    b.reason.return_value = {"reply": reply}
    return b


# ---------------------------------------------------------------------------
# _parse_intent — pure parsing, no network
# ---------------------------------------------------------------------------


class ParseIntentTest(unittest.TestCase):
    def test_add_rule_allow(self) -> None:
        raw = json.dumps(
            {
                "op": "add_rule",
                "action_type": "draft_external_update",
                "tier": "allow",
                "payload_match": {"target": "jira"},
                "rule_name": "jira comments",
            }
        )
        intent = _parse_intent(raw)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.op, "add_rule")
        self.assertEqual(intent.action_type, "draft_external_update")
        self.assertEqual(intent.tier, "allow")
        self.assertEqual(intent.payload_match, {"target": "jira"})
        self.assertEqual(intent.rule_name, "jira comments")

    def test_add_rule_block(self) -> None:
        raw = json.dumps({"op": "add_rule", "action_type": "apply_patch", "tier": "block"})
        intent = _parse_intent(raw)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.tier, "block")

    def test_not_config_returns_none(self) -> None:
        raw = json.dumps({"op": "not_config"})
        self.assertIsNone(_parse_intent(raw))

    def test_invalid_op_returns_none(self) -> None:
        raw = json.dumps({"op": "do_something_random"})
        self.assertIsNone(_parse_intent(raw))

    def test_invalid_action_type_returns_none(self) -> None:
        raw = json.dumps({"op": "add_rule", "action_type": "fake_type", "tier": "allow"})
        self.assertIsNone(_parse_intent(raw))

    def test_invalid_json_returns_none(self) -> None:
        self.assertIsNone(_parse_intent("not json at all"))

    def test_strips_markdown_fences(self) -> None:
        raw = "```json\n" + json.dumps({"op": "list_rules"}) + "\n```"
        intent = _parse_intent(raw)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.op, "list_rules")

    def test_remove_rule_with_id(self) -> None:
        raw = json.dumps({"op": "remove_rule", "rule_id": 5})
        intent = _parse_intent(raw)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.rule_id, 5)

    def test_add_service(self) -> None:
        raw = json.dumps(
            {
                "op": "add_service",
                "service_name": "auth-service",
                "owner": "platform",
                "deps": ["postgres", "redis"],
            }
        )
        intent = _parse_intent(raw)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.service_name, "auth-service")
        self.assertEqual(intent.owner, "platform")
        self.assertEqual(intent.deps, ["postgres", "redis"])

    def test_add_service_missing_name_returns_none(self) -> None:
        raw = json.dumps({"op": "add_service", "owner": "platform"})
        self.assertIsNone(_parse_intent(raw))

    def test_list_catalog(self) -> None:
        intent = _parse_intent(json.dumps({"op": "list_catalog"}))
        self.assertIsNotNone(intent)
        self.assertEqual(intent.op, "list_catalog")

    def test_wildcard_action_type(self) -> None:
        raw = json.dumps({"op": "add_rule", "action_type": "*", "tier": "block"})
        intent = _parse_intent(raw)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.action_type, "*")

    def test_invalid_tier_defaults_to_allow(self) -> None:
        raw = json.dumps({"op": "add_rule", "action_type": "local_note", "tier": "maybe"})
        intent = _parse_intent(raw)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.tier, "allow")


# ---------------------------------------------------------------------------
# describe_intent — human-readable output
# ---------------------------------------------------------------------------


class DescribeIntentTest(unittest.TestCase):
    def test_add_rule_with_payload_match(self) -> None:
        intent = ConfigIntent(
            op="add_rule",
            action_type="draft_external_update",
            tier="allow",
            payload_match={"target": "jira"},
            rule_name="jira",
        )
        desc = describe_intent(intent)
        self.assertIn("allow", desc)
        self.assertIn("draft_external_update", desc)
        self.assertIn("jira", desc)

    def test_add_rule_block(self) -> None:
        intent = ConfigIntent(op="add_rule", action_type="apply_patch", tier="block")
        desc = describe_intent(intent)
        self.assertIn("block", desc)

    def test_add_service(self) -> None:
        intent = ConfigIntent(op="add_service", service_name="auth", owner="platform", deps=["postgres"])
        desc = describe_intent(intent)
        self.assertIn("auth", desc)
        self.assertIn("platform", desc)
        self.assertIn("postgres", desc)

    def test_list_rules(self) -> None:
        desc = describe_intent(ConfigIntent(op="list_rules"))
        self.assertIn("rules", desc.lower())


# ---------------------------------------------------------------------------
# execute_intent — writes to DB
# ---------------------------------------------------------------------------


class ExecuteIntentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_add_rule_creates_row(self) -> None:
        intent = ConfigIntent(op="add_rule", action_type="create_inbox_item", tier="allow")
        result = execute_intent(self.conn, intent)
        self.assertIn("added", result.lower())
        rows = self.conn.execute("SELECT * FROM approval_rules").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action_type"], "create_inbox_item")
        self.assertEqual(rows[0]["tier"], "allow")

    def test_add_rule_with_payload_match(self) -> None:
        intent = ConfigIntent(
            op="add_rule",
            action_type="draft_external_update",
            tier="allow",
            payload_match={"target": "jira"},
            rule_name="jira",
        )
        execute_intent(self.conn, intent)
        row = self.conn.execute("SELECT payload_match FROM approval_rules").fetchone()
        self.assertIsNotNone(row["payload_match"])
        pm = json.loads(row["payload_match"])
        self.assertEqual(pm["target"], "jira")

    def test_remove_rule(self) -> None:
        from personal_assistant.approval_rules import add_rule

        rule_id = add_rule(self.conn, "test", action_type="local_note")
        self.conn.commit()
        intent = ConfigIntent(op="remove_rule", rule_id=rule_id)
        result = execute_intent(self.conn, intent)
        self.assertIn("removed", result.lower())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM approval_rules").fetchone())

    def test_remove_rule_not_found(self) -> None:
        intent = ConfigIntent(op="remove_rule", rule_id=999)
        result = execute_intent(self.conn, intent)
        self.assertIn("not found", result.lower())

    def test_list_rules_empty(self) -> None:
        intent = ConfigIntent(op="list_rules")
        result = execute_intent(self.conn, intent)
        self.assertIn("No approval rules", result)

    def test_list_rules_with_data(self) -> None:
        from personal_assistant.approval_rules import add_rule

        add_rule(self.conn, "r1", action_type="local_note")
        self.conn.commit()
        result = execute_intent(self.conn, ConfigIntent(op="list_rules"))
        self.assertIn("local_note", result)

    def test_add_service(self) -> None:
        intent = ConfigIntent(op="add_service", service_name="auth-service", owner="platform", deps=["postgres"])
        result = execute_intent(self.conn, intent)
        self.assertIn("auth-service", result)
        row = self.conn.execute(
            "SELECT 1 FROM knowledge_nodes WHERE node_type='service' AND label='auth-service'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_list_catalog_empty(self) -> None:
        result = execute_intent(self.conn, ConfigIntent(op="list_catalog"))
        self.assertIn("empty", result.lower())


# ---------------------------------------------------------------------------
# extract_config_intent — integration with backend
# ---------------------------------------------------------------------------


class ExtractConfigIntentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_extracts_add_rule_from_model_reply(self) -> None:
        reply = json.dumps(
            {
                "op": "add_rule",
                "action_type": "draft_external_update",
                "tier": "allow",
                "payload_match": {"target": "jira"},
            }
        )
        with patch("personal_assistant.providers.get_backend", return_value=_mock_backend(reply)):
            intent = extract_config_intent(self.conn, "always approve Jira comments")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.op, "add_rule")
        self.assertEqual(intent.action_type, "draft_external_update")

    def test_returns_none_when_not_config(self) -> None:
        reply = json.dumps({"op": "not_config"})
        with patch("personal_assistant.providers.get_backend", return_value=_mock_backend(reply)):
            intent = extract_config_intent(self.conn, "what should I work on today?")
        self.assertIsNone(intent)

    def test_returns_none_when_backend_unavailable(self) -> None:
        with patch("personal_assistant.providers.get_backend", return_value=_mock_backend("", available=False)):
            intent = extract_config_intent(self.conn, "always approve Jira comments")
        self.assertIsNone(intent)

    def test_returns_none_on_provider_error(self) -> None:
        with patch("personal_assistant.providers.get_backend", side_effect=RuntimeError("no provider")):
            intent = extract_config_intent(self.conn, "always approve Jira comments")
        self.assertIsNone(intent)

    def test_returns_none_on_malformed_reply(self) -> None:
        with patch(
            "personal_assistant.providers.get_backend", return_value=_mock_backend("Sure, I'll help with that!")
        ):
            intent = extract_config_intent(self.conn, "always approve Jira comments")
        self.assertIsNone(intent)


if __name__ == "__main__":
    unittest.main()

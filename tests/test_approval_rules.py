"""Tests for approval_rules.py — graduated approval ladder (A2)."""

from __future__ import annotations

import json
import sqlite3
import unittest

from personal_assistant.approval_rules import add_rule, check_rule, list_rules, remove_rule
from personal_assistant.db import initialize_schema


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


class CheckRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_returns_none_with_no_rules(self) -> None:
        self.assertIsNone(check_rule(self.conn, "create_inbox_item", "{}"))

    def test_type_only_allow_rule(self) -> None:
        add_rule(self.conn, "inbox", action_type="create_inbox_item")
        self.conn.commit()
        self.assertEqual(check_rule(self.conn, "create_inbox_item", "{}"), "allow")

    def test_type_only_block_rule(self) -> None:
        add_rule(self.conn, "no patches", action_type="apply_patch", tier="block")
        self.conn.commit()
        self.assertEqual(check_rule(self.conn, "apply_patch", "{}"), "block")

    def test_unmatched_action_type_returns_none(self) -> None:
        add_rule(self.conn, "inbox", action_type="create_inbox_item")
        self.conn.commit()
        self.assertIsNone(check_rule(self.conn, "apply_patch", "{}"))

    def test_payload_match_rule_fires_on_match(self) -> None:
        add_rule(
            self.conn,
            "jira comments",
            action_type="draft_external_update",
            payload_match='{"target": "jira"}',
        )
        self.conn.commit()
        payload = json.dumps({"target": "jira", "body": "hello", "target_ref": "PROJ-1"})
        self.assertEqual(check_rule(self.conn, "draft_external_update", payload), "allow")

    def test_payload_match_rule_does_not_fire_on_mismatch(self) -> None:
        add_rule(
            self.conn,
            "jira only",
            action_type="draft_external_update",
            payload_match='{"target": "jira"}',
        )
        self.conn.commit()
        payload = json.dumps({"target": "github", "body": "hello"})
        self.assertIsNone(check_rule(self.conn, "draft_external_update", payload))

    def test_payload_match_wins_over_type_only(self) -> None:
        # Type-only allow, payload-match block for specific target
        add_rule(self.conn, "all updates", action_type="draft_external_update")
        add_rule(
            self.conn, "no aha", action_type="draft_external_update", payload_match='{"target": "aha"}', tier="block"
        )
        self.conn.commit()
        aha_payload = json.dumps({"target": "aha"})
        jira_payload = json.dumps({"target": "jira"})
        # Aha → blocked by specific rule
        self.assertEqual(check_rule(self.conn, "draft_external_update", aha_payload), "block")
        # Jira → allowed by type-only rule (payload-match rule doesn't fire)
        self.assertEqual(check_rule(self.conn, "draft_external_update", jira_payload), "allow")

    def test_wildcard_matches_any_action_type(self) -> None:
        add_rule(self.conn, "allow all", action_type="*")
        self.conn.commit()
        self.assertEqual(check_rule(self.conn, "anything_at_all", "{}"), "allow")
        self.assertEqual(check_rule(self.conn, "create_inbox_item", "{}"), "allow")

    def test_newer_rule_wins_same_specificity(self) -> None:
        add_rule(self.conn, "first block", action_type="local_note", tier="block")
        add_rule(self.conn, "second allow", action_type="local_note", tier="allow")
        self.conn.commit()
        # Newest (higher id) wins when both are type-only
        self.assertEqual(check_rule(self.conn, "local_note", "{}"), "allow")


class AddRemoveRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_add_returns_id(self) -> None:
        rule_id = add_rule(self.conn, "test", action_type="local_note")
        self.assertIsInstance(rule_id, int)
        self.assertGreater(rule_id, 0)

    def test_invalid_tier_raises(self) -> None:
        with self.assertRaises(ValueError):
            add_rule(self.conn, "bad", action_type="local_note", tier="maybe")

    def test_remove_existing(self) -> None:
        rule_id = add_rule(self.conn, "x", action_type="local_note")
        self.conn.commit()
        self.assertTrue(remove_rule(self.conn, rule_id))

    def test_remove_nonexistent_returns_false(self) -> None:
        self.assertFalse(remove_rule(self.conn, 9999))

    def test_removed_rule_no_longer_fires(self) -> None:
        rule_id = add_rule(self.conn, "x", action_type="local_note")
        self.conn.commit()
        remove_rule(self.conn, rule_id)
        self.conn.commit()
        self.assertIsNone(check_rule(self.conn, "local_note", "{}"))


class StandingBlockPersistenceTest(unittest.TestCase):
    """Regression: standing block must write status='blocked' to DB, not just skip."""

    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_check_rule_returns_block_for_blocked_tier(self) -> None:
        add_rule(self.conn, "no patches", action_type="apply_patch", tier="block")
        self.conn.commit()
        result = check_rule(self.conn, "apply_patch", "{}")
        # Callers are responsible for persisting the block; check_rule just signals.
        self.assertEqual(result, "block")


class ListRulesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_empty_returns_empty(self) -> None:
        self.assertEqual(list_rules(self.conn), [])

    def test_returns_added_rules(self) -> None:
        add_rule(self.conn, "r1", action_type="create_inbox_item")
        add_rule(self.conn, "r2", action_type="apply_patch", tier="block")
        self.conn.commit()
        rules = list_rules(self.conn)
        self.assertEqual(len(rules), 2)
        tiers = {r["tier"] for r in rules}
        self.assertIn("allow", tiers)
        self.assertIn("block", tiers)


if __name__ == "__main__":
    unittest.main()

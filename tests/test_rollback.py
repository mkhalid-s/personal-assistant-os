from __future__ import annotations

import json
import sqlite3
import unittest

from personal_assistant.db import initialize_schema
from personal_assistant.rollback import (
    COMPENSATION_SCHEMA,
    STRATEGY_DELETE_ON_CREATE,
    STRATEGY_NO_OP,
    STRATEGY_REVERT_ON_UPDATE,
    RollbackError,
    derive_compensation,
    parse_compensation,
    record_compensation,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# derive_compensation — the pure derivation logic, no DB needed
# ---------------------------------------------------------------------------


class DeriveCompensationNoOpTest(unittest.TestCase):
    """Non-executed or non-compensable actions always produce no_op."""

    def _assert_no_op(self, envelope: dict, *, contains: str = "") -> None:
        self.assertEqual(envelope["schema"], COMPENSATION_SCHEMA)
        self.assertEqual(envelope["strategy"], STRATEGY_NO_OP)
        if contains:
            note = envelope.get("rollback_note", "")
            self.assertIn(contains, note)

    def test_non_executed_status_is_no_op(self) -> None:
        for status in ("blocked", "failed", "noop", "pending"):
            with self.subTest(status=status):
                env = derive_compensation(
                    action_type="draft_external_update",
                    payload={"target": "jira", "operation": "comment"},
                    final_status=status,
                )
                self._assert_no_op(env, contains=status)

    def test_local_note_is_no_op(self) -> None:
        env = derive_compensation(action_type="local_note", payload={}, final_status="executed")
        self._assert_no_op(env, contains="Local capture")

    def test_create_inbox_item_is_no_op(self) -> None:
        env = derive_compensation(action_type="create_inbox_item", payload={}, final_status="executed")
        self._assert_no_op(env, contains="Local capture")

    def test_apply_patch_is_no_op(self) -> None:
        env = derive_compensation(action_type="apply_patch", payload={}, final_status="executed")
        self._assert_no_op(env, contains="git revert")

    def test_unknown_action_type_is_no_op(self) -> None:
        env = derive_compensation(action_type="send_smoke_signal", payload={}, final_status="executed")
        self._assert_no_op(env, contains="send_smoke_signal")

    def test_unrecognised_connector_payload_is_no_op(self) -> None:
        env = derive_compensation(
            action_type="draft_external_update",
            payload={"target": "unknown_provider"},
            final_status="executed",
        )
        self._assert_no_op(env, contains="not recognized")

    def test_empty_action_type_is_no_op(self) -> None:
        env = derive_compensation(action_type="", payload={}, final_status="executed")
        self.assertEqual(env["strategy"], STRATEGY_NO_OP)


class DeriveCompensationConnectorTest(unittest.TestCase):
    """Connector mutations produce concrete compensation strategies."""

    def _jira_comment_payload(self, ref: str = "PROJ-1") -> dict:
        return {"target": "jira", "operation": "comment", "target_ref": ref}

    def _github_status_payload(self, ref: str = "PR#42") -> dict:
        return {"target": "github", "operation": "status_update", "target_ref": ref}

    def test_jira_comment_produces_delete_on_create(self) -> None:
        env = derive_compensation(
            action_type="draft_external_update",
            payload=self._jira_comment_payload(),
            final_status="executed",
        )
        self.assertEqual(env["schema"], COMPENSATION_SCHEMA)
        self.assertEqual(env["strategy"], STRATEGY_DELETE_ON_CREATE)
        self.assertIn("preconditions", env)
        self.assertGreater(len(env["preconditions"]), 0)

    def test_github_comment_produces_delete_on_create(self) -> None:
        env = derive_compensation(
            action_type="draft_external_update",
            payload={"target": "github", "operation": "comment", "target_ref": "PR#1"},
            final_status="executed",
        )
        self.assertEqual(env["strategy"], STRATEGY_DELETE_ON_CREATE)

    def test_confluence_comment_produces_delete_on_create(self) -> None:
        env = derive_compensation(
            action_type="draft_external_update",
            payload={"target": "confluence", "operation": "comment"},
            final_status="executed",
        )
        self.assertEqual(env["strategy"], STRATEGY_DELETE_ON_CREATE)

    def test_aha_comment_produces_delete_on_create(self) -> None:
        env = derive_compensation(
            action_type="draft_external_update",
            payload={"target": "aha", "operation": "comment"},
            final_status="executed",
        )
        self.assertEqual(env["strategy"], STRATEGY_DELETE_ON_CREATE)

    def test_status_update_produces_revert_on_update(self) -> None:
        env = derive_compensation(
            action_type="draft_external_update",
            payload=self._github_status_payload(),
            final_status="executed",
        )
        self.assertEqual(env["strategy"], STRATEGY_REVERT_ON_UPDATE)

    def test_draft_note_produces_delete_on_create(self) -> None:
        env = derive_compensation(
            action_type="draft_external_update",
            payload={"target": "jira", "operation": "draft_note", "target_ref": "X"},
            final_status="executed",
        )
        self.assertEqual(env["strategy"], STRATEGY_DELETE_ON_CREATE)

    def test_link_back_produces_delete_on_create(self) -> None:
        env = derive_compensation(
            action_type="draft_external_update",
            payload={"target": "github", "operation": "link_back", "target_ref": "Y"},
            final_status="executed",
        )
        self.assertEqual(env["strategy"], STRATEGY_DELETE_ON_CREATE)

    def test_envelope_has_required_schema_keys(self) -> None:
        env = derive_compensation(
            action_type="draft_external_update",
            payload=self._jira_comment_payload(),
            final_status="executed",
        )
        for key in ("schema", "strategy", "action_type", "payload", "target", "dry_run_supported"):
            self.assertIn(key, env)

    def test_compensation_payload_contains_target_ref(self) -> None:
        env = derive_compensation(
            action_type="draft_external_update",
            payload=self._jira_comment_payload("PROJ-99"),
            final_status="executed",
        )
        self.assertIn("PROJ-99", env["payload"].get("target_ref", ""))

    def test_unknown_strategy_coerced_to_no_op(self) -> None:
        # _base_envelope should coerce any unknown strategy string to no_op.
        from personal_assistant.rollback import _base_envelope

        env = _base_envelope(
            strategy="fly_away",
            action_type="some_action",
            payload={},
            target={},
        )
        self.assertEqual(env["strategy"], STRATEGY_NO_OP)
        self.assertIn("fly_away", env.get("rollback_note", ""))


# ---------------------------------------------------------------------------
# parse_compensation — tolerant deserialization
# ---------------------------------------------------------------------------


class ParseCompensationTest(unittest.TestCase):
    def _row_with(self, json_val):
        return {"compensating_action_json": json_val}

    def test_valid_json_dict_returns_dict(self) -> None:
        data = {"schema": COMPENSATION_SCHEMA, "strategy": STRATEGY_NO_OP}
        row = self._row_with(json.dumps(data))
        result = parse_compensation(row)
        self.assertIsNotNone(result)
        self.assertEqual(result["strategy"], STRATEGY_NO_OP)

    def test_none_value_returns_none(self) -> None:
        self.assertIsNone(parse_compensation(self._row_with(None)))

    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(parse_compensation(self._row_with("")))

    def test_invalid_json_returns_none(self) -> None:
        self.assertIsNone(parse_compensation(self._row_with("{not valid json")))

    def test_json_list_returns_none(self) -> None:
        self.assertIsNone(parse_compensation(self._row_with(json.dumps([1, 2, 3]))))

    def test_missing_key_returns_none(self) -> None:
        # Row with no compensating_action_json key at all
        self.assertIsNone(parse_compensation({}))


# ---------------------------------------------------------------------------
# record_compensation — DB persistence
# ---------------------------------------------------------------------------


class RecordCompensationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def _insert_receipt(self) -> int:
        task_id = self.conn.execute("INSERT INTO agent_tasks (objective) VALUES ('test')").lastrowid
        action_id = self.conn.execute(
            "INSERT INTO agent_actions (agent_task_id, action_type, title) VALUES (?, 'local_note', 'test action')",
            (task_id,),
        ).lastrowid
        receipt_id = self.conn.execute(
            """INSERT INTO action_execution_receipts
               (agent_action_id, agent_task_id, action_type, final_status, approved)
               VALUES (?, ?, 'local_note', 'executed', 1)""",
            (action_id, task_id),
        ).lastrowid
        self.conn.commit()
        return receipt_id

    def test_persists_compensation_json(self) -> None:
        rid = self._insert_receipt()
        env = {"schema": COMPENSATION_SCHEMA, "strategy": STRATEGY_NO_OP}
        record_compensation(self.conn, receipt_id=rid, compensation=env)
        self.conn.commit()
        row = self.conn.execute(
            "SELECT compensating_action_json FROM action_execution_receipts WHERE id = ?", (rid,)
        ).fetchone()
        self.assertIsNotNone(row["compensating_action_json"])
        parsed = json.loads(row["compensating_action_json"])
        self.assertEqual(parsed["strategy"], STRATEGY_NO_OP)

    def test_overwrites_existing_compensation(self) -> None:
        rid = self._insert_receipt()
        env1 = {"schema": COMPENSATION_SCHEMA, "strategy": STRATEGY_NO_OP, "v": 1}
        env2 = {"schema": COMPENSATION_SCHEMA, "strategy": STRATEGY_NO_OP, "v": 2}
        record_compensation(self.conn, receipt_id=rid, compensation=env1)
        self.conn.commit()
        record_compensation(self.conn, receipt_id=rid, compensation=env2)
        self.conn.commit()
        row = self.conn.execute(
            "SELECT compensating_action_json FROM action_execution_receipts WHERE id = ?", (rid,)
        ).fetchone()
        self.assertEqual(json.loads(row["compensating_action_json"])["v"], 2)

    def test_silently_noop_on_missing_column(self) -> None:
        # Simulate pre-migration DB by dropping the column via a temp table.
        old_conn = sqlite3.connect(":memory:")
        old_conn.row_factory = sqlite3.Row
        old_conn.execute(
            "CREATE TABLE action_execution_receipts "
            "(id INTEGER PRIMARY KEY, action_type TEXT, final_status TEXT, approved INTEGER)"
        )
        old_conn.execute("INSERT INTO action_execution_receipts VALUES (1, 'local_note', 'executed', 1)")
        old_conn.commit()
        try:
            # Should not raise even though compensating_action_json column absent.
            record_compensation(old_conn, receipt_id=1, compensation={"schema": "x"})
        finally:
            old_conn.close()


# ---------------------------------------------------------------------------
# RollbackError
# ---------------------------------------------------------------------------


class RollbackErrorTest(unittest.TestCase):
    def test_code_and_message_attributes(self) -> None:
        err = RollbackError("not_found", "Receipt not found.")
        self.assertEqual(err.code, "not_found")
        self.assertEqual(err.message, "Receipt not found.")
        self.assertIsInstance(err, Exception)

    def test_is_raised_and_caught(self) -> None:
        with self.assertRaises(RollbackError) as ctx:
            raise RollbackError("no_op", "Nothing to roll back.")
        self.assertEqual(ctx.exception.code, "no_op")


if __name__ == "__main__":
    unittest.main()

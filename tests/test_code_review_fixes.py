"""Regression tests for the PAOS-001..019 code-review remediation pass.

Each test pins a specific finding id so the fix can't silently regress.
Everything is local SQLite (in-memory or temp files) — no network.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zlib
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in __import__("sys").path:
    __import__("sys").path.insert(0, SRC)


def _memory_conn(*, foreign_keys: bool = False) -> sqlite3.Connection:
    """Fresh in-memory DB with the full current schema (mirrors the
    ``_factory_release_smoke`` pattern in cli_health)."""
    from personal_assistant.db import initialize_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys = ON")
    initialize_schema(conn)
    return conn


def _fresh_db_conn():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)  # noqa: SIM115
    tmp.close()
    os.environ["MYOS_DB_PATH"] = tmp.name
    from personal_assistant.db import get_connection

    return get_connection(), tmp.name


def _insert_task(conn: sqlite3.Connection) -> int:
    conn.execute("INSERT INTO agent_tasks (objective, context) VALUES ('test objective', '')")
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


# ---------------------------------------------------------------------------
# PAOS-001: migrations 42/43 guarded against partial application
# ---------------------------------------------------------------------------


class MigrationGuardTest(unittest.TestCase):
    def test_migrations_42_43_survive_partial_application(self):
        from personal_assistant.db import initialize_schema

        conn = _memory_conn()
        try:
            # Simulate the crash state: the persona_name columns were added but
            # the version rows were never committed (MAX(version) back at 41).
            conn.execute("DELETE FROM schema_migrations WHERE version IN (42, 43)")
            conn.commit()
            # Must not raise "duplicate column name" — re-runs the 42/43 blocks.
            initialize_schema(conn)
            versions = {int(r["version"]) for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
            self.assertIn(42, versions)
            self.assertIn(43, versions)
            factory_cols = [r["name"] for r in conn.execute("PRAGMA table_info(factory_runs)").fetchall()]
            goal_cols = [r["name"] for r in conn.execute("PRAGMA table_info(assistant_goals)").fetchall()]
            self.assertEqual(factory_cols.count("persona_name"), 1)
            self.assertEqual(goal_cols.count("persona_name"), 1)
        finally:
            conn.close()

    def test_fully_applied_schema_is_stable_on_reopen(self):
        from personal_assistant.db import initialize_schema

        conn = _memory_conn()
        try:
            # initialize_schema already ran inside _memory_conn; a second full
            # pass (every reopen) must stay a no-op.
            initialize_schema(conn)
            factory_cols = [r["name"] for r in conn.execute("PRAGMA table_info(factory_runs)").fetchall()]
            self.assertEqual(factory_cols.count("persona_name"), 1)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# PAOS-010: connection pragmas ordered busy_timeout/foreign_keys before WAL
# ---------------------------------------------------------------------------


class PragmaOrderTest(unittest.TestCase):
    def test_busy_timeout_and_foreign_keys_set_before_wal(self):
        from pathlib import Path as _Path

        source = (_Path(__file__).resolve().parents[1] / "src" / "personal_assistant" / "db.py").read_text()
        get_conn_src = source[source.index("def get_connection") : source.index("def initialize_schema")]
        wal_pos = get_conn_src.index("PRAGMA journal_mode = WAL;")
        busy_pos = get_conn_src.index("PRAGMA busy_timeout = 5000;")
        fk_pos = get_conn_src.index("PRAGMA foreign_keys = ON;")
        self.assertLess(busy_pos, wal_pos, "busy_timeout must precede the WAL switch")
        self.assertLess(fk_pos, wal_pos, "foreign_keys must precede the WAL switch")


# ---------------------------------------------------------------------------
# PAOS-002: recover_stranded_executions
# ---------------------------------------------------------------------------


class StrandedExecutionRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = _fresh_db_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)
        os.environ.pop("MYOS_DB_PATH", None)

    def _insert_action(self, *, status: str, approved_at: str | None, requires_approval: int = 0) -> int:
        task_id = _insert_task(self.conn)
        self.conn.execute(
            """
            INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval, approved_at)
            VALUES (?, 'draft_message', 'stranded probe', '{}', ?, ?, ?)
            """,
            (task_id, status, requires_approval, approved_at),
        )
        self.conn.commit()
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def test_stale_executing_row_recovered_with_inbox_and_event(self):
        from personal_assistant.execution import recover_stranded_executions

        stale_id = self._insert_action(status="executing", approved_at=None)
        # Backdate via direct SQL (approved_at is a plain TEXT column).
        self.conn.execute(
            "UPDATE agent_actions SET approved_at = datetime('now', '-60 minutes') WHERE id = ?",
            (stale_id,),
        )
        self.conn.commit()

        recovered = recover_stranded_executions(self.conn)
        self.assertEqual(recovered, [stale_id])
        row = self.conn.execute("SELECT status, result FROM agent_actions WHERE id = ?", (stale_id,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["result"].startswith("recovered: stranded in executing"))
        inbox = self.conn.execute(
            "SELECT text, kind, source FROM inbox_items WHERE text LIKE ? ORDER BY id DESC LIMIT 1",
            (f"%#{stale_id}%",),
        ).fetchone()
        self.assertIsNotNone(inbox, "follow-up inbox item must exist")
        self.assertEqual(inbox["kind"], "follow_up")
        self.assertEqual(inbox["source"], "execution_recovery")
        event = self.conn.execute(
            "SELECT COUNT(*) AS c FROM event_log WHERE event_type = 'execution_stranded_recovered' AND entity_id = ?",
            (stale_id,),
        ).fetchone()
        self.assertEqual(event["c"], 1)
        # Idempotent: a second pass finds nothing left to recover.
        self.assertEqual(recover_stranded_executions(self.conn), [])

    def test_fresh_executing_row_is_untouched(self):
        from personal_assistant.execution import recover_stranded_executions

        fresh_id = self._insert_action(status="executing", approved_at=None)
        self.conn.execute(
            "UPDATE agent_actions SET approved_at = datetime('now') WHERE id = ?",
            (fresh_id,),
        )
        self.conn.commit()
        self.assertEqual(recover_stranded_executions(self.conn), [])
        row = self.conn.execute("SELECT status FROM agent_actions WHERE id = ?", (fresh_id,)).fetchone()
        self.assertEqual(row["status"], "executing")

    def test_approve_and_execute_sweeps_stranded_rows_first(self):
        from personal_assistant.execution import approve_and_execute

        stale_id = self._insert_action(status="executing", approved_at=None)
        self.conn.execute(
            "UPDATE agent_actions SET approved_at = datetime('now', '-2 hours') WHERE id = ?",
            (stale_id,),
        )
        # A safe proposed action for the actual approve/execute call.
        task_id = _insert_task(self.conn)
        self.conn.execute(
            """
            INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval)
            VALUES (?, 'create_inbox_item', 'safe probe', '{"text": "sweep probe", "kind": "task"}', 'proposed', 0)
            """,
            (task_id,),
        )
        self.conn.commit()
        live_id = int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

        res = approve_and_execute(self.conn, live_id, do_approve=True, execute=True)
        self.assertEqual(res["code"], "executed")
        stale_row = self.conn.execute("SELECT status FROM agent_actions WHERE id = ?", (stale_id,)).fetchone()
        self.assertEqual(stale_row["status"], "failed", "stranded row must be recovered as a side effect")


# ---------------------------------------------------------------------------
# PAOS-004: deterministic _person_ref via crc32
# ---------------------------------------------------------------------------


class PersonRefTest(unittest.TestCase):
    def test_person_ref_matches_crc32_formula(self):
        from personal_assistant import context as ctx

        conn = _memory_conn()
        try:
            for name in ("Ada Lovelace", "zqx-not-a-person"):
                ref = ctx._person_ref(conn, name)
                self.assertEqual(ref, -(zlib.crc32(name.encode("utf-8")) % 1_000_000_000))
                self.assertLess(ref, 0, "synthetic refs stay negative to avoid real id collisions")
        finally:
            conn.close()

    def test_person_ref_stable_across_subprocesses(self):
        import subprocess
        import sys

        code = (
            "import sqlite3, sys;"
            f"sys.path.insert(0, {SRC!r});"
            "from personal_assistant import context as ctx;"
            "from personal_assistant.db import initialize_schema;"
            "c = sqlite3.connect(':memory:');"
            "c.row_factory = sqlite3.Row;"
            "initialize_schema(c);"
            "print(ctx._person_ref(c, 'Process Stability'))"
        )
        outs = [
            subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
            for _ in range(2)
        ]
        self.assertEqual(outs[0], outs[1], "PYTHONHASHSEED randomization must not affect person refs")


# ---------------------------------------------------------------------------
# PAOS-008: reflection purge deletes referencing suggestions first
# ---------------------------------------------------------------------------


class ReflectionPurgeTest(unittest.TestCase):
    def test_aged_reflection_purge_removes_referencing_suggestions(self):
        from personal_assistant.privacy import _cleanup_policy_retention

        conn = _memory_conn(foreign_keys=True)
        try:
            conn.execute("INSERT INTO assistant_policies (key, value) VALUES ('retention_conversation_days', '1')")
            conn.execute(
                "INSERT INTO context_insights (kind, subject, summary, created_at) "
                "VALUES ('reflection', 'Aged', 'aged insight', datetime('now', '-10 days'))"
            )
            aged_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            conn.execute(
                "INSERT INTO context_suggestions (insight_id, title) VALUES (?, 'aged suggestion')", (aged_id,)
            )
            conn.execute(
                "INSERT INTO context_insights (kind, subject, summary, created_at) "
                "VALUES ('reflection', 'Recent', 'recent insight', datetime('now'))"
            )
            recent_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            conn.execute(
                "INSERT INTO context_suggestions (insight_id, title) VALUES (?, 'recent suggestion')", (recent_id,)
            )
            conn.commit()

            # With foreign_keys=ON this is exactly where the old code died with
            # an IntegrityError (suggestion referencing the deleted insight).
            stats = _cleanup_policy_retention(conn)
            self.assertEqual(stats["conversation_turns"], 0)
            aged = conn.execute("SELECT COUNT(*) AS c FROM context_insights WHERE id = ?", (aged_id,)).fetchone()
            self.assertEqual(aged["c"], 0, "aged reflection must be purged")
            aged_sugg = conn.execute(
                "SELECT COUNT(*) AS c FROM context_suggestions WHERE insight_id = ?", (aged_id,)
            ).fetchone()
            self.assertEqual(aged_sugg["c"], 0, "referencing suggestion must be purged first/with it")
            recent = conn.execute("SELECT COUNT(*) AS c FROM context_insights WHERE id = ?", (recent_id,)).fetchone()
            self.assertEqual(recent["c"], 1, "recent reflection must survive")
            recent_sugg = conn.execute(
                "SELECT COUNT(*) AS c FROM context_suggestions WHERE insight_id = ?", (recent_id,)
            ).fetchone()
            self.assertEqual(recent_sugg["c"], 1, "recent suggestion must survive")
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# PAOS-014: noop prefixes in _status_from_result
# ---------------------------------------------------------------------------


class StatusFromResultTest(unittest.TestCase):
    def test_noop_outcomes_do_not_count_as_executed(self):
        from personal_assistant.execution import _status_from_result

        for prefix in (
            "no diff to apply",
            "marked complete; external mutation adapter not configured",
            "connector drafted: outbox #12 target=jira:PROJ-1",
            "inbox item already existed",
            "draft ready: Draft ready for review.",
        ):
            self.assertEqual(_status_from_result(prefix), "noop", prefix)

    def test_other_statuses_unchanged(self):
        from personal_assistant.execution import _status_from_result

        self.assertEqual(_status_from_result("blocked: nope"), "blocked")
        self.assertEqual(_status_from_result("patch failed: boom"), "failed")
        self.assertEqual(_status_from_result("provider execution failed: x"), "failed")
        self.assertEqual(_status_from_result("patch applied"), "executed")

    def test_noop_receipts_get_no_op_compensation(self):
        from personal_assistant.execution import _record_execution_receipt, _status_from_result

        conn = _memory_conn()
        try:
            task_id = _insert_task(conn)
            conn.execute(
                """
                INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval)
                VALUES (?, 'draft_message', 'noop probe', '{}', 'executing', 0)
                """,
                (task_id,),
            )
            action_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            row = conn.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
            result = "connector drafted: outbox #1 target=jira:PROJ-1"
            status = _status_from_result(result)
            self.assertEqual(status, "noop")
            _record_execution_receipt(conn, row, approved=False, final_status=status, result=result)
            comp = conn.execute(
                "SELECT compensating_action_json FROM action_execution_receipts WHERE agent_action_id = ?",
                (action_id,),
            ).fetchone()
            self.assertIsNotNone(comp["compensating_action_json"])
            envelope = json.loads(comp["compensating_action_json"])
            self.assertEqual(envelope["strategy"], "no_op")
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# PAOS-016: post-claim approval integrity re-verification
# ---------------------------------------------------------------------------


class PostClaimIntegrityTest(unittest.TestCase):
    def test_integrity_failure_after_claim_refuses_execution(self):
        from personal_assistant import execution
        from personal_assistant.execution import _compute_payload_hash, approve_and_execute

        conn, path = _fresh_db_conn()
        try:
            task_id = _insert_task(conn)
            payload = '{"draft": "verify me"}'
            conn.execute(
                """
                INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status,
                                           requires_approval, payload_hash, approved_at)
                VALUES (?, 'draft_message', 'integrity probe', ?, 'approved', 1, ?, CURRENT_TIMESTAMP)
                """,
                (task_id, payload, _compute_payload_hash(payload)),
            )
            action_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            conn.commit()

            calls = {"n": 0}

            def flaky_verify(row, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:  # pre-claim check passes
                    return {"ok": True, "reason": "", "payload_hash_verified": True}
                # post-claim re-check fails: payload mutated / TTL expired in the gap
                return {"ok": False, "reason": "payload_hash_mismatch", "payload_hash_verified": False}

            with mock.patch.object(execution, "verify_approval_integrity", side_effect=flaky_verify):
                res = approve_and_execute(conn, action_id, do_approve=False, execute=True)

            self.assertEqual(calls["n"], 2, "integrity must be verified both before and after the claim")
            self.assertEqual(res["code"], "failed")
            self.assertEqual(res["status"], "failed")
            self.assertTrue(res["result"].startswith("blocked: payload_hash_mismatch"))
            row = conn.execute("SELECT status, result FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
            self.assertEqual(row["status"], "failed")
            self.assertTrue(row["result"].startswith("blocked: payload_hash_mismatch"))
            event = conn.execute(
                "SELECT payload FROM event_log WHERE event_type = 'approval_integrity_block' AND entity_id = ?",
                (action_id,),
            ).fetchone()
            self.assertIsNotNone(event, "refusal must be audit-logged")
            self.assertIn("post_claim", event["payload"])
            receipt = conn.execute(
                "SELECT final_status FROM action_execution_receipts WHERE agent_action_id = ?", (action_id,)
            ).fetchone()
            self.assertIsNotNone(receipt, "refusal must leave an execution receipt")
            self.assertEqual(receipt["final_status"], "failed")
        finally:
            conn.close()
            os.unlink(path)
            os.environ.pop("MYOS_DB_PATH", None)


# ---------------------------------------------------------------------------
# PAOS-017: persisted result strings are redacted + bounded
# ---------------------------------------------------------------------------


class PersistedResultRedactionTest(unittest.TestCase):
    def test_provider_stderr_redacted_in_persisted_copies_only(self):
        from personal_assistant.execution import approve_and_execute

        conn, path = _fresh_db_conn()
        old_command = os.environ.pop("MYOS_ACTION_COMMAND", None)
        os.environ["MYOS_ACTION_COMMAND"] = "sh -c 'echo contact bob@example.com >&2; exit 1'"
        try:
            task_id = _insert_task(conn)
            conn.execute(
                """
                INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval)
                VALUES (?, 'draft_external_update', 'provider probe', '{"draft": "x"}', 'proposed', 1)
                """,
                (task_id,),
            )
            action_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            conn.commit()

            res = approve_and_execute(conn, action_id, do_approve=True, execute=True)
            self.assertEqual(res["status"], "failed")
            # Return envelope keeps the raw string for the interactive caller.
            self.assertIn("bob@example.com", res["result"])
            # Persisted copies are redacted (PAOS-017).
            stored = conn.execute("SELECT result FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
            self.assertNotIn("bob@example.com", stored["result"])
            self.assertIn("[REDACTED_EMAIL]", stored["result"])
            obs = conn.execute(
                "SELECT content FROM agent_observations WHERE agent_task_id = ? AND observation_type = 'action_result'",
                (task_id,),
            ).fetchone()
            self.assertIsNotNone(obs)
            self.assertNotIn("bob@example.com", obs["content"])
        finally:
            conn.close()
            os.unlink(path)
            os.environ.pop("MYOS_DB_PATH", None)
            if old_command is not None:
                os.environ["MYOS_ACTION_COMMAND"] = old_command

    def test_persisted_result_is_bounded(self):
        from personal_assistant.execution import approve_and_execute

        conn, path = _fresh_db_conn()
        old_command = os.environ.pop("MYOS_ACTION_COMMAND", None)
        huge = "x" * 5000
        os.environ["MYOS_ACTION_COMMAND"] = f"sh -c 'echo {huge}; exit 1'"
        try:
            task_id = _insert_task(conn)
            conn.execute(
                """
                INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval)
                VALUES (?, 'draft_external_update', 'bound probe', '{"draft": "x"}', 'proposed', 1)
                """,
                (task_id,),
            )
            action_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            conn.commit()
            approve_and_execute(conn, action_id, do_approve=True, execute=True)
            stored = conn.execute("SELECT result FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
            self.assertLessEqual(len(stored["result"]), 2000)
        finally:
            conn.close()
            os.unlink(path)
            os.environ.pop("MYOS_DB_PATH", None)
            if old_command is not None:
                os.environ["MYOS_ACTION_COMMAND"] = old_command


# ---------------------------------------------------------------------------
# PAOS-019: renew_lock + 4h stale threshold
# ---------------------------------------------------------------------------


class LocksTest(unittest.TestCase):
    def setUp(self):
        self.conn = _memory_conn()

    def tearDown(self):
        self.conn.close()

    def _backdate(self, name: str, hours: int) -> None:
        self.conn.execute(
            "UPDATE pipeline_locks SET acquired_at = datetime('now', ?) WHERE name = ?",
            (f"-{hours} hours", name),
        )
        self.conn.commit()

    def test_lock_stale_constant_is_four_hours(self):
        from personal_assistant.locks import LOCK_STALE_AFTER_HOURS

        self.assertEqual(LOCK_STALE_AFTER_HOURS, 4)

    def test_renew_lock_refreshes_lease_for_owner(self):
        from personal_assistant.locks import acquire_lock, renew_lock

        self.assertTrue(acquire_lock(self.conn, "cycle", "owner-a"))
        self._backdate("cycle", 3)
        row_before = self.conn.execute("SELECT acquired_at FROM pipeline_locks WHERE name = 'cycle'").fetchone()[
            "acquired_at"
        ]
        renew_lock(self.conn, "cycle", "owner-a")
        row_after = self.conn.execute("SELECT acquired_at FROM pipeline_locks WHERE name = 'cycle'").fetchone()[
            "acquired_at"
        ]
        self.assertNotEqual(row_before, row_after, "renew must refresh acquired_at")
        self.assertGreater(row_after, row_before)

    def test_renew_lock_never_resurrects_foreign_lock(self):
        from personal_assistant.locks import renew_lock

        self.conn.execute("INSERT INTO pipeline_locks (name, owner) VALUES ('cycle', 'owner-a')")
        self.conn.commit()
        self._backdate("cycle", 3)
        before = self.conn.execute("SELECT acquired_at FROM pipeline_locks WHERE name = 'cycle'").fetchone()
        renew_lock(self.conn, "cycle", "owner-b")  # wrong owner: no-op
        after = self.conn.execute("SELECT acquired_at FROM pipeline_locks WHERE name = 'cycle'").fetchone()
        self.assertEqual(before["acquired_at"], after["acquired_at"])
        owner = self.conn.execute("SELECT owner FROM pipeline_locks WHERE name = 'cycle'").fetchone()["owner"]
        self.assertEqual(owner, "owner-a")

    def test_two_hour_old_lock_is_still_live(self):
        from personal_assistant.locks import acquire_lock

        self.assertTrue(acquire_lock(self.conn, "autopilot", "first"))
        self._backdate("autopilot", 2)
        # Under the old 1h threshold this would steal the live lock.
        self.assertFalse(acquire_lock(self.conn, "autopilot", "second"))
        owner = self.conn.execute("SELECT owner FROM pipeline_locks WHERE name = 'autopilot'").fetchone()["owner"]
        self.assertEqual(owner, "first")

    def test_four_hour_old_lock_is_reclaimed(self):
        from personal_assistant.locks import acquire_lock

        self.assertTrue(acquire_lock(self.conn, "autopilot", "first"))
        self._backdate("autopilot", 5)
        self.assertTrue(acquire_lock(self.conn, "autopilot", "second"))
        owner = self.conn.execute("SELECT owner FROM pipeline_locks WHERE name = 'autopilot'").fetchone()["owner"]
        self.assertEqual(owner, "second")


# ---------------------------------------------------------------------------
# PAOS-005: learn() scopes receipts to the run
# ---------------------------------------------------------------------------


class LearnReceiptScopeTest(unittest.TestCase):
    def test_retrospective_only_includes_this_runs_receipts(self):
        from personal_assistant import factory

        conn = _memory_conn()
        try:
            conn.execute("INSERT INTO factory_runs (intent_id, status) VALUES (1, 'running')")
            run1 = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            conn.execute("INSERT INTO factory_runs (intent_id, status) VALUES (2, 'running')")
            run2 = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            for run_id, action_type, final_status in (
                (run1, "apply_patch", "failed"),
                (run2, "create_inbox_item", "executed"),
            ):
                task_id = _insert_task(conn)
                conn.execute(
                    """
                    INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval)
                    VALUES (?, ?, 'probe', '{}', 'executed', 0)
                    """,
                    (task_id, action_type),
                )
                action_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
                conn.execute(
                    "INSERT INTO factory_artifacts (factory_run_id, artifact_type, artifact_id, label) "
                    "VALUES (?, 'agent_action', ?, 'probe')",
                    (run_id, action_id),
                )
                conn.execute(
                    """
                    INSERT INTO action_execution_receipts (
                        agent_action_id, agent_task_id, action_type, final_status, result,
                        approved, rollback_note, follow_up_required, request_json
                    )
                    VALUES (?, ?, ?, ?, 'probe result', 1, 'note', 0, '{}')
                    """,
                    (action_id, task_id, action_type, final_status),
                )
            conn.commit()

            learning_id = factory.learn(conn, factory_run_id=run1, outcome="failed")
            row = conn.execute(
                "SELECT retrospective_json FROM factory_learning WHERE id = ?", (learning_id,)
            ).fetchone()
            retrospective = json.loads(row["retrospective_json"])
            self.assertEqual(len(retrospective["recent_receipts"]), 1, "only this run's receipt may be included")
            self.assertEqual(retrospective["recent_receipts"][0]["action_type"], "apply_patch")
            self.assertEqual(retrospective["recent_receipts"][0]["final_status"], "failed")
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# PAOS-006: retrieval run queries are privacy-filtered (write + read)
# ---------------------------------------------------------------------------


class RetrievalRunPrivacyTest(unittest.TestCase):
    def test_recorded_query_is_redacted_and_lookup_consistent(self):
        from personal_assistant.graphrag import retrieve

        conn = _memory_conn()
        try:
            raw_query = "call alice@example.com about token ghp_Abcdefghijklmnopqrst"
            hits = retrieve(conn, raw_query, limit=3, record_run=True, mode="test")
            self.assertEqual(hits, [])
            stored = conn.execute("SELECT query FROM retrieval_runs ORDER BY id DESC LIMIT 1").fetchone()
            self.assertIsNotNone(stored)
            self.assertNotIn("alice@example.com", stored["query"])
            self.assertIn("[REDACTED_EMAIL]", stored["query"])
            self.assertNotIn("ghp_Abcdefghijklmnopqrst", stored["query"])
            # The exact-match fallback (factory.py) must look the run up with
            # the filtered form: write and read stay consistent.
            from personal_assistant.privacy import apply_privacy_filters

            filtered = apply_privacy_filters(conn, raw_query)
            hit_row = conn.execute("SELECT id FROM retrieval_runs WHERE query = ?", (filtered,)).fetchone()
            self.assertIsNotNone(hit_row, "filtered lookup must match the stored (filtered) query")
            miss_row = conn.execute("SELECT id FROM retrieval_runs WHERE query = ?", (raw_query,)).fetchone()
            self.assertIsNone(miss_row, "raw lookup must NOT match the stored (filtered) query")
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# PAOS-007: skip_keys redaction bypass + bounded diff preview
# ---------------------------------------------------------------------------


class SkipKeysTest(unittest.TestCase):
    def test_skip_keys_preserves_top_level_value_verbatim(self):
        from personal_assistant.agentcore import enqueue_proposal

        conn = _memory_conn()
        try:
            task_id = _insert_task(conn)
            diff = "--- a/app.py\n+++ b/app.py\n+contact alice@example.com\n"
            action_id = enqueue_proposal(
                conn,
                task_id=task_id,
                action_type="apply_patch",
                title="patch",
                payload={"diff": diff, "note": "ping alice@example.com", "repo_root": "/repo"},
                requires_approval=1,
                skip_keys=frozenset({"diff"}),
            )
            stored = json.loads(
                conn.execute("SELECT payload_json FROM agent_actions WHERE id = ?", (action_id,)).fetchone()[
                    "payload_json"
                ]
            )
            self.assertEqual(stored["diff"], diff, "skipped key must survive byte-identical")
            self.assertNotIn("alice@example.com", stored["note"])
            self.assertIn("[REDACTED_EMAIL]", stored["note"])

            # Without skip_keys the diff itself would be redacted (old behavior).
            action_id2 = enqueue_proposal(
                conn,
                task_id=task_id,
                action_type="apply_patch",
                title="patch2",
                payload={"diff": diff},
                requires_approval=1,
            )
            stored2 = json.loads(
                conn.execute("SELECT payload_json FROM agent_actions WHERE id = ?", (action_id2,)).fetchone()[
                    "payload_json"
                ]
            )
            self.assertIn("[REDACTED_EMAIL]", stored2["diff"])
        finally:
            conn.close()


class DiffPreviewTest(unittest.TestCase):
    def test_preview_is_bounded_with_remaining_count(self):
        from personal_assistant.execution import format_diff_preview

        diff = "\n".join(f"line {i}" for i in range(100))
        preview = format_diff_preview({"diff": diff, "repo_root": "/repo"})
        self.assertIsNotNone(preview)
        lines = preview.splitlines()
        self.assertEqual(len(lines), 41)  # 40 shown + 1 truncation marker
        self.assertIn("60 more line(s) truncated", lines[-1])

    def test_preview_caps_chars(self):
        from personal_assistant.execution import DIFF_PREVIEW_MAX_CHARS, format_diff_preview

        preview = format_diff_preview({"diff": "y" * (DIFF_PREVIEW_MAX_CHARS * 3)})
        self.assertIsNotNone(preview)
        self.assertLessEqual(len(preview.splitlines()[0]), DIFF_PREVIEW_MAX_CHARS)

    def test_no_diff_returns_none(self):
        from personal_assistant.execution import format_diff_preview

        self.assertIsNone(format_diff_preview({"draft": "no diff here"}))
        self.assertIsNone(format_diff_preview({"diff": ""}))

    def test_approval_queue_entry_carries_diff_fields(self):
        from personal_assistant.cli_agent import _approval_queue_json_entry

        conn = _memory_conn()
        try:
            task_id = _insert_task(conn)
            diff = "--- a/app.py\n+++ b/app.py\n+print('hi')\n"
            conn.execute(
                """
                INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, status, requires_approval)
                VALUES (?, 'apply_patch', 'patch probe', ?, 'proposed', 1)
                """,
                (task_id, json.dumps({"diff": diff, "repo_root": "/tmp/repo"})),
            )
            action_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            row = conn.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
            entry = _approval_queue_json_entry(row)
            self.assertIn("diff_preview", entry)
            self.assertIn("+print('hi')", entry["diff_preview"])
            self.assertEqual(entry["repo_root"], "/tmp/repo")
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# PAOS-012: autopilot proposals flow through enqueue_proposal
# ---------------------------------------------------------------------------


class AutopilotProposalChokepointTest(unittest.TestCase):
    def test_unsafe_or_pii_actions_are_gated_and_redacted(self):
        from personal_assistant import autopilot

        conn = _memory_conn()
        try:
            actions = [
                {
                    "action_type": "send_message",
                    "title": "Ping",
                    "payload": {"draft": "email alice@example.com", "token": "ghp_" + "a" * 20},
                    # A planner/backend labelling an external mutation "safe"
                    # must NOT bypass the approval queue (PAOS-012).
                    "requires_approval": 0,
                }
            ]
            with (
                mock.patch.object(autopilot, "_ai_reason_artifacts", return_value=([], actions, "local")),
                mock.patch.object(autopilot, "_agent_analogies", return_value=[]),
            ):
                task_id = autopilot._create_agent_task(
                    conn,
                    objective="objective",
                    context="",
                    priority=2,
                    mode="safe",
                    max_actions=5,
                )
            rows = conn.execute(
                "SELECT action_type, title, payload_json, requires_approval, status FROM agent_actions WHERE agent_task_id = ?",
                (task_id,),
            ).fetchall()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["requires_approval"], 1, "non-AUTO type must be forced to approval")
            self.assertEqual(row["status"], "proposed")
            self.assertNotIn("alice@example.com", row["payload_json"])
            self.assertIn("[REDACTED_EMAIL]", row["payload_json"])
            self.assertIn("[REDACTED_SECRET]", row["payload_json"])
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# PAOS-011: scheduler tick claims before dispatch under the pipeline lock
# ---------------------------------------------------------------------------


class SchedulerTickTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = _fresh_db_conn()
        self._env_backup: dict[str, str] = {}
        for key in (
            "MYOS_NOTIFY_COMMAND",
            "MYOS_NOTIFY_DISABLE_HOOK",
            "MYOS_NOTIFY_DISABLE_OSASCRIPT",
            "MYOS_NOTIFY_DISABLE_NOTIFY_SEND",
        ):
            if key in os.environ:
                self._env_backup[key] = os.environ.pop(key)
        os.environ["MYOS_NOTIFY_DISABLE_OSASCRIPT"] = "1"
        os.environ["MYOS_NOTIFY_DISABLE_NOTIFY_SEND"] = "1"

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)
        os.environ.pop("MYOS_DB_PATH", None)
        for key in (
            "MYOS_NOTIFY_COMMAND",
            "MYOS_NOTIFY_DISABLE_HOOK",
            "MYOS_NOTIFY_DISABLE_OSASCRIPT",
            "MYOS_NOTIFY_DISABLE_NOTIFY_SEND",
        ):
            os.environ.pop(key, None)
        for key, value in self._env_backup.items():
            os.environ[key] = value

    def _create_due_reminder(self) -> int:
        from personal_assistant import reminders

        rid = reminders.create(self.conn, "standup now", "+0m")
        self.conn.commit()
        return rid

    def _run_tick(self, *, json_mode: bool) -> str:
        from personal_assistant.cli_reminders import cmd_scheduler_tick

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cmd_scheduler_tick(argparse.Namespace(json=json_mode))
        return out.getvalue()

    def test_tick_fires_marks_and_releases_lock(self):
        from personal_assistant import reminders

        rid = self._create_due_reminder()
        self._run_tick(json_mode=False)
        row = reminders.get(self.conn, rid)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "fired", "claim-before-dispatch must fire the due reminder")
        held = self.conn.execute("SELECT COUNT(*) AS c FROM pipeline_locks WHERE name = 'scheduler'").fetchone()
        self.assertEqual(held["c"], 0, "pipeline lock must be released at the end of the tick")

    def test_tick_json_envelope_shape_unchanged(self):
        rid = self._create_due_reminder()
        out = self._run_tick(json_mode=True)
        # The notify stdout fallback also prints its envelope line; the tick's
        # own JSON envelope is the last line printed.
        lines = [line for line in out.splitlines() if line.strip()]
        payload = json.loads(lines[-1])
        self.assertEqual(payload["schema"], "myos.scheduler.tick.v1")
        for key in ("ts", "fired", "count", "remaining_pending", "next_scheduled_at", "trace_id"):
            self.assertIn(key, payload)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["fired"][0]["id"], rid)
        self.assertTrue(payload["fired"][0]["dispatched"])
        self.assertEqual(payload["remaining_pending"], 0)

    def test_tick_skips_firing_when_lock_held_elsewhere(self):
        from personal_assistant import locks, reminders

        rid = self._create_due_reminder()
        self.assertTrue(locks.acquire_lock(self.conn, "scheduler", "other-tick"))
        try:
            self._run_tick(json_mode=False)
            row = reminders.get(self.conn, rid)
            self.assertEqual(row["status"], "pending", "another tick holding the lock must prevent double-firing")
        finally:
            locks.release_lock(self.conn, "scheduler", "other-tick")


# ---------------------------------------------------------------------------
# PAOS-003: installed-mode defaults route through data_dirs
# ---------------------------------------------------------------------------


class InstalledModePathsTest(unittest.TestCase):
    def setUp(self):
        self._old_data_dir = os.environ.pop("MYOS_DATA_DIR", None)

    def tearDown(self):
        os.environ.pop("MYOS_DATA_DIR", None)
        if self._old_data_dir is not None:
            os.environ["MYOS_DATA_DIR"] = self._old_data_dir

    def test_autopilot_digest_writes_under_data_dir(self):
        from personal_assistant.autopilot import _store_autopilot_digest

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MYOS_DATA_DIR"] = tmp
            conn = _memory_conn()
            try:
                digest_id = _store_autopilot_digest(conn, "title", "body", {"run_id": 1})
                digest_dir = Path(tmp) / "autopilot"
                self.assertTrue((digest_dir / "latest.md").exists())
                self.assertTrue((digest_dir / f"digest-{digest_id}.md").exists())
            finally:
                conn.close()

    def test_digest_explicit_output_dir_still_wins(self):
        from personal_assistant.autopilot import _store_autopilot_digest

        conn = _memory_conn()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                _store_autopilot_digest(conn, "title", "body", {"run_id": 1}, output_dir=tmp)
                self.assertTrue((Path(tmp) / "latest.md").exists())
        finally:
            conn.close()

    def test_doctor_installed_mode_downgrades_repo_file_checks(self):
        from personal_assistant import cli_health

        conn, path = _fresh_db_conn()
        try:
            out = io.StringIO()
            with (
                mock.patch.object(cli_health, "_repo_root_if_dev", return_value=None),
                contextlib.redirect_stdout(out),
            ):
                cli_health.cmd_doctor(argparse.Namespace(strict=False, json=True))
            payload = json.loads(out.getvalue())
            core_names = [c["name"] for c in payload["core_checks"]]
            optional_names = [c["name"] for c in payload["optional_checks"]]
            self.assertNotIn("env_example", core_names)
            self.assertNotIn("local_artifacts_ignored", core_names)
            self.assertIn("env_example", optional_names)
            self.assertIn("local_artifacts_ignored", optional_names)
            for check in payload["optional_checks"]:
                if check["name"] in ("env_example", "local_artifacts_ignored"):
                    self.assertFalse(check["ok"])
                    self.assertIn("not a dev checkout", check["detail"])
            self.assertTrue(payload["ok"], "installed mode must not fail core doctor checks for repo files")
        finally:
            conn.close()
            os.unlink(path)
            os.environ.pop("MYOS_DB_PATH", None)

    def test_doctor_dev_mode_keeps_repo_file_checks_core(self):
        """Tests always run from a source checkout, so the unpatched doctor must
        keep the exact pre-change dev behavior (repo checks are core)."""
        from personal_assistant import cli_health

        conn, path = _fresh_db_conn()
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli_health.cmd_doctor(argparse.Namespace(strict=False, json=True))
            payload = json.loads(out.getvalue())
            core_names = [c["name"] for c in payload["core_checks"]]
            self.assertIn("env_example", core_names)
            self.assertIn("local_artifacts_ignored", core_names)
            self.assertNotIn("env_example", [c["name"] for c in payload["optional_checks"]])
        finally:
            conn.close()
            os.unlink(path)
            os.environ.pop("MYOS_DB_PATH", None)

    def test_sanity_and_report_defaults_use_data_dir(self):
        from personal_assistant.dashboard import render_dashboard_html
        from personal_assistant.data_dirs import resolve_data_dir

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MYOS_DATA_DIR"] = tmp
            # dashboard default report dir now resolves under the data dir; no
            # reports exist yet, which must render fine (empty links list).
            conn = _memory_conn()
            try:
                html = render_dashboard_html(conn, report_dir="")
                self.assertIn("No reports found.", html)
                self.assertEqual(resolve_data_dir(), Path(tmp))
            finally:
                conn.close()


# ===========================================================================
# Pass B — PAOS-020..053 remediation
# ===========================================================================


class RemindDispatchUsageTest(unittest.TestCase):
    """PAOS-020: bare `myos remind` prints subcommand usage and exits 2."""

    def test_bare_remind_usage_exit_2_no_db(self):
        from personal_assistant import cli_reminders

        args = argparse.Namespace(remind_action=None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            cli_reminders.cmd_remind_dispatch(args)
        self.assertEqual(ctx.exception.code, 2)
        text = out.getvalue().lower()
        for word in ("usage", "create", "list", "complete", "snooze", "cancel"):
            self.assertIn(word, text)

    def test_known_action_still_dispatches(self):
        from personal_assistant import cli_reminders

        captured: list[str] = []

        def fake_list(_args):
            captured.append("ran")

        with mock.patch.object(cli_reminders, "cmd_remind_list", fake_list):
            cli_reminders.cmd_remind_dispatch(argparse.Namespace(remind_action="list", limit=5, due_only=False))
        self.assertEqual(captured, ["ran"])


class WorkerSystemExitTest(unittest.TestCase):
    """PAOS-021: cmd_worker marks the job 'failed' when the orchestrator raises SystemExit."""

    def setUp(self):
        self.conn, self.path = _fresh_db_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_system_exit_marks_job_failed_with_code_message(self):
        from personal_assistant import cli_operations

        self.conn.execute(
            "INSERT INTO workflow_queue (workflow_name, payload_json, status) VALUES ('daily', '{}', 'queued')"
        )
        self.conn.commit()

        def fake_orchestrate(_args):
            raise SystemExit("boom")

        deps = cli_operations.OperationsDependencies(
            load_env_file=lambda _path: 0, orchestrate_command=fake_orchestrate
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli_operations.cmd_worker(argparse.Namespace(limit=5), deps)
        row = self.conn.execute("SELECT status, last_error FROM workflow_queue WHERE id = 1").fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["last_error"], "boom")
        self.assertIn("boom", out.getvalue())


class LaunchdLifecycleTest(unittest.TestCase):
    """PAOS-022: stop unloads loaded agents and KEEPS plists; activate reloads."""

    def _deps(self) -> object:
        from personal_assistant import cli_launchd

        return cli_launchd.LaunchdRuntimeDependencies(
            load_env_file=lambda _p: 0,
            onboard_command=lambda _a: None,
            go_live_command=lambda _a: None,
            launchd_status_command=lambda _a: None,
            sanity_command=lambda _a: None,
        )

    def test_stop_unloads_only_loaded_agents_and_keeps_plists(self):
        from personal_assistant import cli_launchd

        with tempfile.TemporaryDirectory() as tmp:
            paths = {label: Path(tmp) / f"{label}.plist" for label in cli_launchd._LAUNCHD_LABELS}
            for path in paths.values():
                path.write_text("<plist/>")
            commands: list[list[str]] = []
            out = io.StringIO()

            def fake_run(argv, **kwargs):  # noqa: ARG001
                commands.append(list(argv))
                proc = argparse.Namespace(returncode=0)
                return proc

            deps = self._deps()
            with (
                mock.patch.object(cli_launchd, "_launchd_plist_paths", lambda: paths),
                mock.patch.object(cli_launchd, "_launchctl", lambda: "launchctl"),
                mock.patch.object(cli_launchd, "_agent_loaded", lambda _lc, label: label == "com.myos.sync"),
                mock.patch.object(cli_launchd.subprocess, "run", fake_run),
                contextlib.redirect_stdout(out),
            ):
                cli_launchd.cmd_stop(argparse.Namespace(), deps)
            self.assertEqual(commands, [["launchctl", "unload", str(paths["com.myos.sync"])]])
            self.assertIn("plists kept", out.getvalue())
            for path in paths.values():
                self.assertTrue(path.exists(), "stop must not delete plist files")

    def test_load_unloaded_agents_reloads_installed_but_stopped(self):
        from personal_assistant import cli_launchd

        with tempfile.TemporaryDirectory() as tmp:
            paths = {label: Path(tmp) / f"{label}.plist" for label in cli_launchd._LAUNCHD_LABELS}
            paths["com.myos.pulse"].write_text("<plist/>")  # only one exists on disk
            commands: list[list[str]] = []

            def fake_run(argv, **kwargs):  # noqa: ARG001
                commands.append(list(argv))
                return argparse.Namespace(returncode=0)

            with (
                mock.patch.object(cli_launchd, "_launchd_plist_paths", lambda: paths),
                mock.patch.object(cli_launchd, "_launchctl", lambda: "launchctl"),
                mock.patch.object(cli_launchd, "_agent_loaded", lambda _lc, _label: False),
                mock.patch.object(cli_launchd.subprocess, "run", fake_run),
            ):
                count = cli_launchd._load_unloaded_agents()
            self.assertEqual(count, 1)
            self.assertEqual(commands, [["launchctl", "load", str(paths["com.myos.pulse"])]])


class LaunchdStatusLabelsTest(unittest.TestCase):
    """PAOS-023: launchd-status must report the scheduler agent label too."""

    def test_status_includes_all_four_labels(self):
        from personal_assistant import cli_runtime

        out = io.StringIO()
        with (
            mock.patch.object(cli_runtime.shutil, "which", lambda _name: None),
            contextlib.redirect_stdout(out),
        ):
            cli_runtime.cmd_launchd_status(argparse.Namespace())
        text = out.getvalue()
        for label in ("com.myos.sync", "com.myos.pulse", "com.myos.autopilot", "com.myos.scheduler"):
            self.assertIn(label, text)


class WatchIngestOSErrorTest(unittest.TestCase):
    """PAOS-024: unreadable/unhashable watch-dir files skip instead of crashing."""

    def setUp(self):
        self.conn = _memory_conn()

    def tearDown(self):
        self.conn.close()

    def _seed_watch_file(self) -> tuple[str, str]:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        watch_dir = Path(tmp) / "watched"
        watch_dir.mkdir()
        target = watch_dir / "notes.md"
        target.write_text("plain content")
        self.conn.execute("INSERT INTO assistant_watch_dirs (path, status) VALUES (?, 'active')", (str(watch_dir),))
        self.conn.commit()
        return tmp, str(target)

    def test_unreadable_file_is_marked_skipped_error(self):
        from personal_assistant import cli_workflow

        _tmp, target = self._seed_watch_file()

        def boom(_self, *args, **kwargs):  # noqa: ARG001
            raise OSError("rotated away")

        with mock.patch.object(Path, "read_text", boom):
            files, _suggestions = cli_workflow._scan_watch_dirs(self.conn, limit=5)
        self.assertEqual(files, 0)
        row = self.conn.execute("SELECT status FROM file_ingests WHERE file_path = ?", (target,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "skipped_error")

    def test_unhashable_file_is_skipped_without_stranding(self):
        from personal_assistant import cli_workflow

        _tmp, _target = self._seed_watch_file()
        with mock.patch.object(cli_workflow, "_file_sha256", side_effect=OSError("gone")):
            files, _suggestions = cli_workflow._scan_watch_dirs(self.conn, limit=5)
        self.assertEqual(files, 0)
        stranded = self.conn.execute("SELECT COUNT(*) AS c FROM file_ingests WHERE status='processing'").fetchone()["c"]
        self.assertEqual(stranded, 0)


class GoalPauseResumeTest(unittest.TestCase):
    """PAOS-050: pausing/resuming an unknown goal exits 1 with a clear message."""

    def setUp(self):
        self.conn, self.path = _fresh_db_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_pause_and_resume_unknown_goal_exit_1(self):
        from personal_assistant import cli_autonomy

        for action in ("pause", "resume"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
                cli_autonomy.cmd_goal(argparse.Namespace(goal_action=action, id=9999))
            self.assertEqual(ctx.exception.code, 1, action)
            self.assertIn("Goal #9999 not found.", out.getvalue())

    def test_pause_and_resume_known_goal(self):
        from personal_assistant import cli_autonomy

        self.conn.execute(
            "INSERT INTO assistant_goals (objective, context, cadence_minutes, priority, persona_name) "
            "VALUES ('ship', '', 60, 1, NULL)"
        )
        self.conn.commit()
        cli_autonomy.cmd_goal(argparse.Namespace(goal_action="pause", id=1))
        status = self.conn.execute("SELECT status FROM assistant_goals WHERE id=1").fetchone()["status"]
        self.assertEqual(status, "paused")
        cli_autonomy.cmd_goal(argparse.Namespace(goal_action="resume", id=1))
        status = self.conn.execute("SELECT status FROM assistant_goals WHERE id=1").fetchone()["status"]
        self.assertEqual(status, "active")


class DestructiveHintTokenTest(unittest.TestCase):
    """PAOS-033: destructive hints match whole tokens, not substrings."""

    def test_benign_embedded_hints_are_not_blocked(self):
        from personal_assistant import autonomy

        for name in ("produce_report", "dropdown_update", "deployment_check", "workforce_report"):
            verdict = autonomy.classify_action(name)
            self.assertNotEqual(verdict["tier"], autonomy.BLOCKED, name)
            self.assertFalse(verdict["destructive"], name)

    def test_destructive_names_stay_blocked(self):
        from personal_assistant import autonomy

        for name in (
            "delete_comment",
            "drop_table",
            "purge_cache",
            "force_push",
            "deploy_prod",
            "uninstall_agent",
        ):
            verdict = autonomy.classify_action(name)
            self.assertEqual(verdict["tier"], autonomy.BLOCKED, name)

    def test_multiword_hints_match_token_sequences(self):
        from personal_assistant import autonomy

        for name in ("close_all", "close_all_items", "git_reset_hard", "remove_branch", "reset_hard"):
            self.assertTrue(autonomy._matches_destructive_hint(name), name)
        for name in ("close_day", "close_alliance", "reset_hardware"):
            self.assertFalse(autonomy._matches_destructive_hint(name), name)


class DraftNudgeDedupTest(unittest.TestCase):
    """PAOS-035: draft_nudges must not re-enqueue a still-pending nudge."""

    def setUp(self):
        self.conn = _memory_conn()

    def tearDown(self):
        self.conn.close()

    def _finding(self, ref, kind="overdue"):
        from personal_assistant.watch import _finding

        return _finding(kind, "high", "work_item", ref, f"title {ref}", "reason", owner=None)

    def test_second_run_enqueues_nothing_new(self):
        from personal_assistant import watch

        finding = self._finding(1)
        first = watch.draft_nudges(self.conn, [finding])
        self.assertEqual(len(first), 1)
        second = watch.draft_nudges(self.conn, [finding])
        self.assertEqual(second, [])
        rows = self.conn.execute(
            "SELECT COUNT(*) AS c FROM agent_actions WHERE status IN ('proposed','executing')"
        ).fetchone()["c"]
        self.assertEqual(rows, 1)

    def test_distinct_refs_are_not_suppressed(self):
        from personal_assistant import watch

        watch.draft_nudges(self.conn, [self._finding(1), self._finding(2)])
        rows = self.conn.execute("SELECT COUNT(*) AS c FROM agent_actions WHERE status='proposed'").fetchone()["c"]
        self.assertEqual(rows, 2)


class DashboardTokenTest(unittest.TestCase):
    """PAOS-042: dashboard HTTP handler requires the ?token= query parameter."""

    def test_handler_requires_token(self):
        from personal_assistant import dashboard

        conn = _memory_conn()
        captured: dict[str, object] = {}

        class FakeServer:
            def __init__(self, _addr, handler):
                captured["handler"] = handler

            def serve_forever(self):
                return  # stop serving immediately

            def server_close(self):
                return

        out = io.StringIO()
        try:
            with (
                mock.patch.object(dashboard, "HTTPServer", FakeServer),
                contextlib.redirect_stdout(out),
            ):
                dashboard.serve_dashboard(conn, host="127.0.0.1", port=8787)
            printed = [line for line in out.getvalue().splitlines() if "token=" in line]
            self.assertEqual(len(printed), 1, out.getvalue())
            token = printed[0].split("token=")[1].strip()

            handler_cls = captured["handler"]

            def make_request(path: str):
                handler = handler_cls.__new__(handler_cls)
                handler.request_version = "HTTP/1.0"
                handler.command = "GET"
                handler.path = path
                # log_request interpolates requestline before our no-op
                # log_message runs, so it must exist.
                handler.requestline = f"GET {path} HTTP/1.0"
                handler.wfile = io.BytesIO()
                return handler

            good = make_request(f"/?token={token}")
            good.do_GET()
            self.assertTrue(good.wfile.getvalue().startswith(b"HTTP/1.0 200"))
            self.assertIn(b"text/html", good.wfile.getvalue())

            bad = make_request("/")
            bad.do_GET()
            self.assertTrue(bad.wfile.getvalue().startswith(b"HTTP/1.0 401"), bad.wfile.getvalue())
        finally:
            conn.close()


class AudioHelperTest(unittest.TestCase):
    """PAOS-025: transcription helper runs under sys.executable + surfaces stderr."""

    def _fake_run(self, calls, returncode, stdout, stderr):
        def fake_run(argv, **kwargs):  # noqa: ARG001
            calls["argv"] = argv
            return argparse.Namespace(returncode=returncode, stdout=stdout, stderr=stderr)

        return fake_run

    def test_uses_sys_executable(self):
        from personal_assistant.ingest import audio

        calls: dict[str, object] = {}
        with mock.patch.object(audio.subprocess, "run", self._fake_run(calls, 0, "hello world", "")):
            result = audio.transcribe_audio("/tmp/x.wav")
        self.assertEqual(result, "hello world")
        self.assertEqual(calls["argv"][0], sys.executable)  # noqa: E501

    def test_nonzero_exit_surfaces_stderr_snippet(self):
        from personal_assistant.ingest import audio

        err = io.StringIO()
        calls: dict[str, object] = {}
        with (
            mock.patch.object(audio.subprocess, "run", self._fake_run(calls, 2, "", "boom boom\nsecond line")),
            contextlib.redirect_stderr(err),
        ):
            result = audio.transcribe_audio("/tmp/x.wav")
        self.assertEqual(result, "")
        self.assertIn("rc=2", err.getvalue())
        self.assertIn("boom boom", err.getvalue())


class SchemaV44Test(unittest.TestCase):
    """PAOS-009: fresh DB reaches migration 44 and carries the new indexes."""

    def test_fresh_db_reaches_version_44_with_indexes(self):
        from personal_assistant import db as db_mod

        self.assertEqual(db_mod.EXPECTED_SCHEMA_VERSION, 44)
        conn = _memory_conn()
        try:
            top = conn.execute("SELECT MAX(version) AS m FROM schema_migrations").fetchone()["m"]
            self.assertEqual(top, 44)
            applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
            self.assertEqual(applied, set(range(1, 45)))
            indexes = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
            self.assertIn("idx_chunks_created", indexes)
            self.assertIn("idx_retrieval_runs_query", indexes)
        finally:
            conn.close()


class SchemaFastPathTest(unittest.TestCase):
    """PAOS-030: steady-state initialize_schema skips the whole chain."""

    def test_second_initialize_hits_fast_path(self):
        from personal_assistant import db as db_mod

        conn = _memory_conn()
        try:
            with mock.patch.object(db_mod, "_ensure_fts5") as fts:
                db_mod.initialize_schema(conn)
            fts.assert_not_called()
        finally:
            conn.close()

    def test_gapped_ledger_still_runs_full_chain(self):
        from personal_assistant import db as db_mod

        conn = _memory_conn()
        try:
            conn.execute("DELETE FROM schema_migrations WHERE version=41")
            conn.commit()
            with mock.patch.object(db_mod, "_ensure_fts5") as fts:
                db_mod.initialize_schema(conn)
            fts.assert_called_once()
            present = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
            self.assertIn(41, present)
        finally:
            conn.close()


class OperationalRetentionTest(unittest.TestCase):
    """PAOS-018: only rows older than the window are removed, from every
    covered table; the audit trail survives untouched.

    Review R1: the connection runs with PRAGMA foreign_keys=ON (mirroring
    production ``db.get_connection``) so a child row that outlives its
    deleted parent (route_feedback.event_log_id → event_log) fails the
    sweep with IntegrityError instead of passing silently.
    """

    OLD_TS = "2020-01-01 00:00:00"
    AUDIT_TABLES = (
        "agent_actions",
        "action_execution_receipts",
        "action_outbox",
        "autonomy_run_ledger",
        "action_provider_executions",
    )

    def setUp(self):
        self.conn = _memory_conn(foreign_keys=True)
        self.new_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        # Parent rows referenced by child tables below (one old + one new each).
        self.conn.execute("INSERT INTO agent_tasks (objective) VALUES ('retention')")
        for ts in (self.OLD_TS, self.new_ts):
            self.conn.execute("INSERT INTO retrieval_runs (query, created_at) VALUES ('q', ?)", (ts,))
            self.conn.execute(
                "INSERT INTO route_eval_runs (fixture_path, total_cases, passed_cases, accuracy, created_at) "
                "VALUES ('f', 1, 1, 1.0, ?)",
                (ts,),
            )
            self.conn.execute(
                "INSERT INTO agent_actions (agent_task_id, action_type, title, created_at) "
                "VALUES (1, 'create_inbox_item', 't', ?)",
                (ts,),
            )
            self.conn.execute(
                "INSERT INTO event_log (event_type, entity_type, entity_id, payload, created_at) "
                "VALUES ('e', 'x', NULL, '{}', ?)",
                (ts,),
            )
        self.runs = [r["id"] for r in self.conn.execute("SELECT id FROM retrieval_runs ORDER BY id").fetchall()]
        self.eval_runs = [r["id"] for r in self.conn.execute("SELECT id FROM route_eval_runs ORDER BY id").fetchall()]
        self.actions = [r["id"] for r in self.conn.execute("SELECT id FROM agent_actions ORDER BY id").fetchall()]
        self.events = [r["id"] for r in self.conn.execute("SELECT id FROM event_log ORDER BY id").fetchall()]
        self.run_ids: list[int] = []
        self._insert_all_rows()
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _insert_all_rows(self) -> None:
        for idx, ts in enumerate((self.OLD_TS, self.new_ts)):
            suffix = "old" if idx == 0 else "new"
            self.conn.execute(
                "INSERT INTO agent_observations (agent_task_id, observation_type, content, created_at) "
                "VALUES (1, 'o', 'c', ?)",
                (ts,),
            )
            self.conn.execute(
                "INSERT INTO agent_runs (agent_task_id, agent_name, provider, status, started_at) "
                "VALUES (1, 'a', 'local', 'completed', ?)",
                (ts,),
            )
            self.run_ids.append(int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]))
            self.conn.execute(
                "INSERT INTO autopilot_signals (signal_key, signal_type, source_type, title, created_at) "
                "VALUES (?, 't', 's', 'ti', ?)",
                (f"sig-{suffix}", ts),
            )
            self.conn.execute("INSERT INTO assistant_digests (title, body, created_at) VALUES ('t', 'b', ?)", (ts,))
            self.conn.execute(
                "INSERT INTO ai_provider_calls (provider, purpose, status, created_at) VALUES ('p', 'pr', 'ok', ?)",
                (ts,),
            )
            self.conn.execute(
                "INSERT INTO route_feedback (event_log_id, expected_intent, actual_intent, created_at) "
                "VALUES (?, 'e', 'a', ?)",
                (self.events[idx], ts),
            )
            # Review R1: execution_traces.route_event_id is a second FK child
            # of event_log — a trace linked to a doomed event must go too.
            self.conn.execute(
                "INSERT INTO execution_traces (correlation_id, command, command_path, route_event_id, started_at) "
                "VALUES (?, 'cmd', 'myos cmd', ?, ?)",
                (f"trace-{suffix}", self.events[idx], ts),
            )
            self.conn.execute(
                "INSERT INTO retrieval_run_sources (retrieval_run_id, rank, source_type, source_id, "
                "citation, score, reason, created_at) VALUES (?, 1, 'chunk', 1, 'c', 0.5, 'r', ?)",
                (self.runs[idx], ts),
            )
            self.conn.execute(
                "INSERT INTO route_eval_cases (route_eval_run_id, fixture_id, category, text_hash, "
                "expected_intent, actual_intent, backend, confidence, created_at) "
                "VALUES (?, 'fx', 'cat', 'h', 'ei', 'ai', 'be', 0.9, ?)",
                (self.eval_runs[idx], ts),
            )
            # Audit-trail rows (owner-decision items) — must survive retention.
            self.conn.execute(
                "INSERT INTO action_outbox (provider, target_type, title, body, status, created_at) "
                "VALUES ('p', 'jira', 't', 'b', 'drafted', ?)",
                (ts,),
            )
            self.conn.execute(
                "INSERT INTO action_execution_receipts (agent_action_id, agent_task_id, action_type, "
                "final_status, created_at) VALUES (?, 1, 'create_inbox_item', 'executed', ?)",
                (self.actions[idx], ts),
            )
            self.conn.execute(
                "INSERT INTO autonomy_run_ledger (decision_type, status, agent_run_id, created_at) "
                "VALUES ('cycle', 'completed', ?, ?)",
                (self.run_ids[idx], ts),
            )
            self.conn.execute(
                "INSERT INTO action_provider_executions (agent_action_id, status, created_at) VALUES (?, 'ok', ?)",
                (self.actions[idx], ts),
            )

    def test_old_rows_removed_new_kept_audit_trail_untouched(self):
        from personal_assistant import observability

        counts = observability.apply_operational_retention(self.conn, days=30)
        self.assertGreaterEqual(counts["event_log"], 1)
        for table in (
            "event_log",
            "agent_observations",
            "agent_runs",
            "autopilot_signals",
            "assistant_digests",
            "ai_provider_calls",
            "route_feedback",
            "retrieval_run_sources",
            "retrieval_runs",
            "route_eval_cases",
            "route_eval_runs",
        ):
            remaining = self.conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            self.assertEqual(remaining, 1, f"{table} should keep only the new row")
        for table in self.AUDIT_TABLES:
            remaining = self.conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            self.assertEqual(remaining, 2, f"{table} is an owner-decision audit trail")

    def test_child_rows_referencing_doomed_events_are_removed(self):
        """Review R1: with foreign_keys=ON, retention must delete the
        route_feedback/execution_traces children of old event_log rows before
        the parents, or the sweep raises IntegrityError."""
        from personal_assistant import observability

        old_event_id, new_event_id = self.events
        counts = observability.apply_operational_retention(self.conn, days=30)
        self.assertGreaterEqual(counts["route_feedback"], 1)
        self.assertGreaterEqual(counts["execution_traces"], 1)
        # Old parent gone, new parent kept.
        remaining_events = {r["id"] for r in self.conn.execute("SELECT id FROM event_log").fetchall()}
        self.assertEqual(remaining_events, {new_event_id})
        # The old child rows are gone, not orphaned.
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM route_feedback WHERE event_log_id = ?", (old_event_id,)).fetchone()
        )
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM execution_traces WHERE route_event_id = ?", (old_event_id,)).fetchone()
        )
        # New rows (and their links) survive.
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM route_feedback WHERE event_log_id = ?", (new_event_id,)).fetchone()
        )
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM execution_traces WHERE route_event_id = ?", (new_event_id,)).fetchone()
        )
        # No dangling references remain anywhere.
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_audit_children_survive_with_dangling_reference_nulled(self):
        """Review R1 companion: autonomy_run_ledger is an audit table that
        references agent_runs. Its rows must survive retention with the now
        meaningless agent_run_id set to NULL — not block the parent delete."""
        from personal_assistant import observability

        old_run_id, new_run_id = self.run_ids
        counts = observability.apply_operational_retention(self.conn, days=30)
        self.assertGreaterEqual(counts["autonomy_run_ledger_unlinked"], 1)
        rows = {
            r["agent_run_id"]: r["id"] for r in self.conn.execute("SELECT id, agent_run_id FROM autonomy_run_ledger")
        }
        self.assertEqual(len(rows), 2, "both ledger rows survive")
        self.assertIn(None, rows, "old ledger row's agent_run_id was nulled")
        self.assertIn(new_run_id, rows, "new ledger row keeps its link")
        self.assertNotIn(old_run_id, rows)

    def test_env_window_is_honored_and_defaults_to_180(self):
        from personal_assistant import observability

        self.assertEqual(observability.DEFAULT_OPERATIONAL_RETENTION_DAYS, 180)
        with mock.patch.dict(os.environ, {"MYOS_RETENTION_DAYS": "30"}):
            counts = observability.apply_operational_retention(self.conn)
        self.assertGreaterEqual(counts["event_log"], 1)


class PersonaSeedGateTest(unittest.TestCase):
    """PAOS-041: second ensure_builtin_personas call in the same process writes nothing."""

    def test_second_call_performs_no_writes(self):
        from personal_assistant import personas as personas_mod

        personas_mod._BUILTIN_PERSONAS_SEEDED = False
        conn = _memory_conn()
        try:
            personas_mod.ensure_builtin_personas(conn)
            self.assertGreater(conn.total_changes, 0)
            changes_after_first = conn.total_changes
            personas_mod.ensure_builtin_personas(conn)
            self.assertEqual(conn.total_changes, changes_after_first)
        finally:
            personas_mod._BUILTIN_PERSONAS_SEEDED = False
            conn.close()


class ConnectorClientErrorTest(unittest.TestCase):
    """PAOS-046: 4xx HTTP errors are not retried; 5xx still are."""

    def setUp(self):
        self.conn = _memory_conn()

    def tearDown(self):
        self.conn.close()

    def _make(self):
        from personal_assistant.connectors.base import BaseConnector

        class NoopConnector(BaseConnector):
            name = "noop"

        return NoopConnector(self.conn)

    def test_401_raises_without_retry(self):
        import urllib.error

        from personal_assistant.connectors import base as base_mod

        connector = self._make()
        calls = {"count": 0}
        real_urlopen = base_mod.urllib.request.urlopen

        def fake_urlopen(req, timeout=25):  # noqa: ARG001
            calls["count"] += 1
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", None, None)

        base_mod.urllib.request.urlopen = fake_urlopen
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                connector.json_get("https://example.test/secret", {})
        finally:
            base_mod.urllib.request.urlopen = real_urlopen
        ctx.exception.close()  # the fake fp-less error still warns on GC
        self.assertEqual(calls["count"], 1)

    def test_500_retries_then_raises(self):
        import urllib.error

        from personal_assistant.connectors import base as base_mod

        connector = self._make()
        calls = {"count": 0}
        real_urlopen = base_mod.urllib.request.urlopen
        os.environ["MYOS_CONNECTOR_RETRIES"] = "3"
        os.environ["MYOS_CONNECTOR_BACKOFF_SEC"] = "0"

        def fake_urlopen(req, timeout=25):  # noqa: ARG001
            calls["count"] += 1
            raise urllib.error.HTTPError(req.full_url, 500, "server error", None, None)

        base_mod.urllib.request.urlopen = fake_urlopen
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                connector.json_get("https://example.test/flaky", {})
        finally:
            base_mod.urllib.request.urlopen = real_urlopen
            os.environ.pop("MYOS_CONNECTOR_RETRIES", None)
            os.environ.pop("MYOS_CONNECTOR_BACKOFF_SEC", None)
        ctx.exception.close()
        self.assertEqual(calls["count"], 3)


class InboxListCommandTest(unittest.TestCase):
    """PAOS-026: `myos inbox list` is a real read-only command."""

    def setUp(self):
        # cmd_inbox_list opens its own connection() — point MYOS_DB_PATH at a
        # temp file so the handler reads the same rows we seed here.
        self.conn, self.path = _fresh_db_conn()
        for text in ("first capture", "second capture"):
            self.conn.execute("INSERT INTO inbox_items (text, kind, source) VALUES (?, 'note', 'manual')", (text,))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_inbox_list_prints_recent_items_newest_first(self):
        from personal_assistant.cli_workflow import cmd_inbox_list

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cmd_inbox_list(argparse.Namespace(limit=10))
        self.assertIn("Inbox items", out.getvalue())
        self.assertIn("#2", out.getvalue())
        self.assertIn("second capture", out.getvalue())
        # newest-first ordering (id 2 printed before id 1)
        self.assertLess(out.getvalue().index("#2"), out.getvalue().index("#1"))

    def test_inbox_registered_as_read_only(self):
        from personal_assistant import command_registry

        spec = command_registry.find_command("inbox")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.subcommands, ("list",))
        self.assertEqual(spec.safety, "read_only")


class QueuePayloadValidationTest(unittest.TestCase):
    """PAOS-049: malformed --payload JSON exits 1 with a clean message."""

    def setUp(self):
        self.conn, self.path = _fresh_db_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_invalid_json_exits_1(self):
        from personal_assistant import cli_operations

        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            cli_operations.cmd_queue_add(argparse.Namespace(workflow="daily", payload="{not json"))
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Invalid --payload JSON", out.getvalue())
        queued = self.conn.execute("SELECT COUNT(*) AS c FROM workflow_queue").fetchone()["c"]
        self.assertEqual(queued, 0)


if __name__ == "__main__":
    unittest.main()

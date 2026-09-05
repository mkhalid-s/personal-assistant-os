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
import sqlite3
import tempfile
import unittest
import zlib
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


if __name__ == "__main__":
    unittest.main()

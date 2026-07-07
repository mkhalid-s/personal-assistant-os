from __future__ import annotations

import json
import os
import tempfile
import unittest


class AutonomyPolicyDecisionTest(unittest.TestCase):
    def test_command_decisions_cover_allowed_approval_and_blocked(self) -> None:
        from personal_assistant import autonomy

        read_only = autonomy.decide_command("context", safety="read_only", level="balanced")
        self.assertEqual(read_only["decision"], "allowed")
        self.assertEqual(read_only["tier"], autonomy.AUTO)

        local_write = autonomy.decide_command("capture", safety="local_write", level="balanced")
        self.assertEqual(local_write["decision"], "allowed")
        self.assertEqual(local_write["tier"], autonomy.AUTO)

        external = autonomy.decide_command("sync", safety="external_write", level="balanced")
        self.assertEqual(external["decision"], "needs_approval")
        self.assertEqual(external["tier"], autonomy.CONFIRM)

        blocked = autonomy.decide_command("delete-everything", safety="unknown", level="bold")
        self.assertEqual(blocked["decision"], autonomy.BLOCKED)
        self.assertTrue(blocked["requires_approval"])

    def test_recommend_next_steps_for_decisions(self) -> None:
        from personal_assistant import autonomy

        allowed = autonomy.decide_command("capture", safety="local_write")
        allowed_steps = autonomy.recommend_next_steps(allowed, command="do", intent="capture")
        self.assertEqual(allowed_steps[0]["label"], "continue")

        gated = autonomy.decide_command("factory", safety="approval_gated", requires_confirmation=True)
        gated_steps = autonomy.recommend_next_steps(gated, command="factory", factory_run_id=7)
        self.assertEqual(gated_steps[0]["command"], "myos factory review --id 7")

        routed_factory_steps = autonomy.recommend_next_steps(gated, command="do", intent="factory_run")
        self.assertEqual(routed_factory_steps[0]["command"], "myos factory review --id <run_id>")

        blocked = autonomy.decide_command("delete-everything", safety="unknown")
        blocked_steps = autonomy.recommend_next_steps(blocked, command="delete-everything")
        self.assertIn("myos help diagnostic", [step["command"] for step in blocked_steps])

    def test_runtime_recommendations_use_side_effect_metadata(self) -> None:
        from personal_assistant import autonomy

        setup = autonomy.decide_command("setup-live")
        self.assertEqual(setup["decision"], "allowed")
        self.assertTrue(setup["dry_run_by_default"])
        self.assertIn("os_service_write", setup["side_effects"])
        setup_steps = autonomy.recommend_next_steps(setup, command="setup-live")
        self.assertEqual(setup_steps[0]["command"], "myos setup-live --check")

        launchd = autonomy.decide_command("launchd-install")
        self.assertEqual(launchd["decision"], "needs_approval")
        self.assertIn("os_service_write", launchd["side_effects"])
        launchd_steps = autonomy.recommend_next_steps(launchd, command="launchd-install")
        self.assertEqual(launchd_steps[0]["label"], "dry_run_runtime_change")
        self.assertIn("myos launchd-status", [step["command"] for step in launchd_steps])

        start = autonomy.decide_command("start")
        self.assertEqual(start["decision"], "needs_approval")
        start_steps = autonomy.recommend_next_steps(start, command="start")
        self.assertEqual(start_steps[0]["command"], "myos launchd-status")

        restore = autonomy.decide_command("restore")
        self.assertEqual(restore["decision"], "needs_approval")
        restore_steps = autonomy.recommend_next_steps(restore, command="restore")
        self.assertEqual(restore_steps[0]["command"], "myos backup")
        self.assertIn("myos migrations verify --strict", [step["command"] for step in restore_steps])

        sync = autonomy.decide_command("sync")
        self.assertEqual(sync["decision"], "needs_approval")
        sync_steps = autonomy.recommend_next_steps(sync, command="sync", intent="connector_update")
        self.assertEqual(sync_steps[0]["label"], "review_approvals")
        self.assertEqual(sync_steps[0]["command"], "myos approve --list")
        self.assertNotIn("--execute", sync_steps[0]["command"])

        dashboard = autonomy.decide_command("dashboard")
        self.assertEqual(dashboard["decision"], "allowed")
        dashboard_steps = autonomy.recommend_next_steps(dashboard, command="dashboard")
        self.assertEqual(dashboard_steps[0]["command"], "myos health")

        for step in setup_steps + launchd_steps + start_steps + restore_steps + sync_steps + dashboard_steps:
            self.assertNotIn("auto_execute", step)
            self.assertNotIn("execute_now", step)

    def test_approval_context_uses_command_side_effect_metadata(self) -> None:
        from personal_assistant import approval_context

        context = approval_context.action_review_context(
            "run_command",
            {"command": "myos launchd-install --dry-run", "dry_run": True},
            requires_approval=True,
        )
        self.assertIn("os_service_write", context["side_effects"])
        self.assertEqual(context["approval_reason"], "os_service_change_requires_review")
        self.assertIn("myos launchd-status", context["safer_commands"])

    def test_eval_and_feedback_store_no_raw_note(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        os.environ["MYOS_DB_PATH"] = db_path
        try:
            from personal_assistant import autonomy, observability
            from personal_assistant.db import get_connection

            conn = get_connection()
            result = autonomy.evaluate_command_decisions()
            self.assertEqual(result["summary"]["failed"], 0)
            run_id = autonomy.record_command_decision_eval(conn, result)
            self.assertGreater(run_id, 0)
            corr = observability.start_trace(conn, command="sync", command_path="sync")
            observability.link_trace(conn, corr, safety_level="external_write")
            trace_id = conn.execute("SELECT id FROM execution_traces WHERE correlation_id=?", (corr,)).fetchone()["id"]
            feedback_id = autonomy.record_command_decision_feedback(
                conn,
                trace_id=trace_id,
                expected_decision="needs_approval",
                note="This should remain approval-gated.",
            )
            row = conn.execute("SELECT * FROM autonomy_feedback WHERE id=?", (feedback_id,)).fetchone()
            self.assertEqual(row["actual_decision"], "needs_approval")
            self.assertTrue(row["note_hash"])
            self.assertEqual(row["note_length"], len("This should remain approval-gated."))
            raw = "\n".join(str(value) for value in row)
            self.assertNotIn("This should remain", raw)
            conn.close()
        finally:
            os.environ.pop("MYOS_DB_PATH", None)
            os.unlink(db_path)

    def test_recommendation_feedback_ranks_without_raw_note_storage(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        os.environ["MYOS_DB_PATH"] = db_path
        try:
            from personal_assistant import autonomy
            from personal_assistant.db import get_connection

            conn = get_connection()
            blocked = autonomy.decide_command("delete-everything", safety="unknown")
            steps = autonomy.recommend_next_steps(blocked, command="delete-everything")
            self.assertEqual(steps[0]["label"], "inspect_safe_commands")
            autonomy.record_recommendation_feedback(
                conn,
                label="inspect_safe_commands",
                command="myos help diagnostic",
                useful=False,
                note="This was not the useful suggestion.",
            )
            autonomy.record_recommendation_feedback(
                conn,
                label="inspect_recent_traces",
                command="myos trace list",
                useful=True,
                note="This was useful.",
            )
            ranked = autonomy.ranked_recommendations(conn, steps)
            self.assertEqual(ranked[0]["label"], "inspect_recent_traces")
            rows = autonomy.recommendation_feedback_summary(conn)
            self.assertEqual(rows[0]["label"], "inspect_recent_traces")
            raw = "\n".join(str(tuple(row)) for row in conn.execute("SELECT * FROM recommendation_feedback").fetchall())
            self.assertNotIn("This was useful", raw)
            conn.close()
        finally:
            os.environ.pop("MYOS_DB_PATH", None)
            os.unlink(db_path)

    def test_daily_recommendation_helpers_share_command_scope(self) -> None:
        from personal_assistant import autonomy

        self.assertTrue(autonomy.is_daily_recommendation("daily_reduce_risk", "myos next-action"))
        self.assertTrue(autonomy.is_daily_recommendation("daily_reduce_risk", "myos now"))
        self.assertFalse(autonomy.is_daily_recommendation("daily_reduce_risk", "myos trace list"))
        self.assertFalse(autonomy.is_daily_recommendation("review_approvals", "myos approve --list"))
        self.assertTrue(autonomy.is_goal_scheduler_recommendation("run_goal_cycle", "myos loop run-goal --goal 1"))
        self.assertTrue(autonomy.is_goal_scheduler_recommendation("review_goals", "myos goal list"))
        self.assertFalse(autonomy.is_goal_scheduler_recommendation("review_goals", "myos loop goals"))
        self.assertEqual(autonomy.DAILY_RECOMMENDATION_FEEDBACK_WINDOW_DAYS, 30)
        self.assertEqual(autonomy.DAILY_RECOMMENDATION_FEEDBACK_SCORE_LIMIT, 3)

    def test_learned_side_effect_risk_ranks_matching_recommendations(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        os.environ["MYOS_DB_PATH"] = db_path
        try:
            from personal_assistant import autonomy, intents, plans
            from personal_assistant.db import get_connection

            conn = get_connection()
            intent_id = intents.create_intent(conn, objective="Review connector approval learning")
            plan_id = plans.create_plan(conn, intent_id=intent_id)
            conn.execute(
                """
                INSERT INTO factory_runs (intent_id, plan_id, mode, workflow_pack, status)
                VALUES (?, ?, 'full_autonomous', 'connector_ops', 'learned')
                """,
                (intent_id, plan_id),
            )
            factory_run_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            conn.execute(
                """
                INSERT INTO factory_learning (factory_run_id, outcome, notes, retrospective_json)
                VALUES (?, 'failed', '', ?)
                """,
                (
                    factory_run_id,
                    json.dumps(
                        {
                            "recent_receipts": [
                                {
                                    "final_status": "blocked",
                                    "side_effects": ["external_write"],
                                }
                            ],
                            "receipt_side_effects": {"external_write": 1},
                        },
                        ensure_ascii=True,
                    ),
                ),
            )
            conn.commit()
            steps = [
                {
                    "label": "inspect_traces",
                    "command": "myos trace list",
                    "reason": "Inspect traces.",
                    "side_effects": [],
                },
                {
                    "label": "review_approvals",
                    "command": "myos approve --list",
                    "reason": "Review approvals.",
                    "side_effects": ["external_write"],
                },
            ]
            ranked = autonomy.ranked_recommendations(conn, steps)
            self.assertEqual(ranked[0]["label"], "review_approvals")
            self.assertGreater(ranked[0]["learning_score"], 0)
            self.assertIn("Prior factory learning flagged external_write", ranked[0]["reason"])
            autonomy.record_recommendation_feedback(
                conn,
                label="review_approvals",
                command="myos approve --list",
                useful=True,
                note="This helped me catch the risky connector update.",
            )
            summary = autonomy.recommendation_feedback_summary(conn)
            self.assertEqual(summary[0]["label"], "review_approvals")
            self.assertIn("external_write", summary[0]["side_effects"])
            self.assertGreater(summary[0]["learning_score"], 0)
            raw = "\n".join(str(tuple(row)) for row in conn.execute("SELECT * FROM recommendation_feedback").fetchall())
            self.assertNotIn("risky connector", raw)
            decision = autonomy.decide_command("sync")
            self.assertEqual(decision["decision"], "needs_approval")
            conn.close()
        finally:
            os.environ.pop("MYOS_DB_PATH", None)
            os.unlink(db_path)

    def test_recommendation_feedback_summary_label_side_effect_coverage(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        os.environ["MYOS_DB_PATH"] = db_path
        try:
            from personal_assistant import autonomy
            from personal_assistant.db import get_connection

            conn = get_connection()
            examples = [
                ("review_approvals", "myos approve --list", {"external_write"}),
                ("review_factory", "myos factory review --id 1", {"local_db_write", "external_write"}),
                (
                    "dry_run_runtime_change",
                    "myos setup-live --check",
                    {"local_file_write", "local_db_write", "os_service_write"},
                ),
                ("inspect_loop_status", "myos loop status --task 1", set()),
                ("run_goal_cycle", "myos loop run-goal --goal 1", {"local_db_write"}),
                ("review_goals", "myos goal list", set()),
            ]
            for label, command, _expected_effects in examples:
                autonomy.record_recommendation_feedback(
                    conn,
                    label=label,
                    command=command,
                    useful=True,
                    note=f"{label} recommendation was useful.",
                )

            rows = autonomy.recommendation_feedback_summary(conn, limit=10)
            by_label = {row["label"]: row for row in rows}
            for label, _command, expected_effects in examples:
                self.assertIn(label, by_label)
                self.assertEqual(set(by_label[label]["side_effects"]), expected_effects)
                expected_surface = "goal_scheduler" if label in {"run_goal_cycle", "review_goals"} else "general"
                self.assertEqual(by_label[label]["surface"], expected_surface)
            for label, command, _expected_effects in examples:
                self.assertEqual(by_label[label]["command"], command)
                self.assertNotIn("note_hash", by_label[label])
                self.assertNotIn("note_length", by_label[label])
            self.assertEqual(by_label["inspect_loop_status"]["learning_score"], 0)
            raw = "\n".join(str(tuple(row)) for row in conn.execute("SELECT * FROM recommendation_feedback").fetchall())
            self.assertNotIn("recommendation was useful", raw)
            conn.close()
        finally:
            os.environ.pop("MYOS_DB_PATH", None)
            os.unlink(db_path)

    def test_recommendation_feedback_summary_public_hygiene(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        os.environ["MYOS_DB_PATH"] = db_path
        try:
            from personal_assistant import autonomy
            from personal_assistant.db import get_connection

            conn = get_connection()
            autonomy.record_recommendation_feedback(
                conn,
                label="review_approvals",
                command="myos approve --list",
                useful=True,
                note="Review note mentions " + "Guide" + "wire" + " and must stay hashed.",
            )
            summary_text = json.dumps(autonomy.recommendation_feedback_summary(conn), sort_keys=True)
            forbidden = [
                "Guide" + "wire",
                "GW Bed" + "rock",
                "/Users/" + "mshaikh",
                "raw_text",
                "raw_request",
                "raw_command_args",
                "request_json",
                "note_hash",
                "note_length",
                "must stay hashed",
            ]
            for pattern in forbidden:
                self.assertNotIn(pattern, summary_text)
            self.assertIn("external_write", summary_text)
            self.assertIn("review_approvals", summary_text)
            conn.close()
        finally:
            os.environ.pop("MYOS_DB_PATH", None)
            os.unlink(db_path)

    def test_goal_scheduler_feedback_uses_learning_signal_without_execution_change(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        os.environ["MYOS_DB_PATH"] = db_path
        try:
            from personal_assistant import autonomy, intents, plans
            from personal_assistant.db import get_connection

            conn = get_connection()
            intent_id = intents.create_intent(conn, objective="Review goal scheduler learning")
            plan_id = plans.create_plan(conn, intent_id=intent_id)
            conn.execute(
                """
                INSERT INTO factory_runs (intent_id, plan_id, mode, workflow_pack, status)
                VALUES (?, ?, 'semi_autonomous', 'daily_ops', 'learned')
                """,
                (intent_id, plan_id),
            )
            factory_run_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            conn.execute(
                """
                INSERT INTO factory_learning (factory_run_id, outcome, notes, retrospective_json)
                VALUES (?, 'failed', '', ?)
                """,
                (
                    factory_run_id,
                    json.dumps({"receipt_side_effects": {"local_db_write": 1}}, ensure_ascii=True),
                ),
            )
            conn.commit()

            steps = [
                {
                    "label": "review_goals",
                    "command": "myos goal list",
                    "reason": "Review goals.",
                },
                {
                    "label": "run_goal_cycle",
                    "command": "myos loop run-goal --goal 1",
                    "reason": "Run the next goal cycle.",
                },
            ]
            ranked = autonomy.ranked_recommendations(conn, steps)
            self.assertEqual(ranked[0]["label"], "run_goal_cycle")
            self.assertGreater(ranked[0]["learning_score"], 0)
            self.assertIn("Prior factory learning flagged local_db_write", ranked[0]["reason"])
            self.assertEqual(ranked[1]["label"], "review_goals")
            self.assertEqual(ranked[1]["learning_score"], 0)
            conn.close()
        finally:
            os.environ.pop("MYOS_DB_PATH", None)
            os.unlink(db_path)

    def test_recommendation_feedback_summary_orders_recent_daily_rows_first(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        os.environ["MYOS_DB_PATH"] = db_path
        try:
            from personal_assistant import autonomy
            from personal_assistant.db import get_connection

            conn = get_connection()
            for _ in range(2):
                autonomy.record_recommendation_feedback(
                    conn,
                    label="inspect_recent_traces",
                    command="myos trace list",
                    useful=True,
                    note="Older trace feedback should not hide active daily learning.",
                )
            conn.execute(
                """
                UPDATE recommendation_feedback
                SET created_at = datetime('now', '-90 days')
                WHERE label = 'inspect_recent_traces'
                """
            )
            autonomy.record_recommendation_feedback(
                conn,
                label="daily_reduce_risk",
                command="myos next-action",
                useful=True,
                note="Recent daily feedback should stay visible in the summary.",
            )
            rows = autonomy.recommendation_feedback_summary(conn, limit=2)
            self.assertEqual(rows[0]["label"], "daily_reduce_risk")
            self.assertEqual(rows[0]["surface"], "daily")
            self.assertEqual(rows[0]["recent_score"], 1)
            self.assertEqual(rows[1]["label"], "inspect_recent_traces")
            self.assertEqual(rows[1]["score"], 2)
            self.assertEqual(rows[1]["recent_score"], 0)
            raw = "\n".join(str(tuple(row)) for row in conn.execute("SELECT * FROM recommendation_feedback").fetchall())
            self.assertNotIn("Recent daily feedback", raw)
            conn.close()
        finally:
            os.environ.pop("MYOS_DB_PATH", None)
            os.unlink(db_path)

    def test_recommendation_feedback_summary_limit_keeps_daily_row_visible(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        os.environ["MYOS_DB_PATH"] = db_path
        try:
            from personal_assistant import autonomy
            from personal_assistant.db import get_connection

            conn = get_connection()
            for _ in range(3):
                autonomy.record_recommendation_feedback(
                    conn,
                    label="inspect_recent_traces",
                    command="myos trace list",
                    useful=True,
                    note="General trace feedback remains private.",
                )
            for _ in range(2):
                autonomy.record_recommendation_feedback(
                    conn,
                    label="inspect_safe_commands",
                    command="myos help diagnostic",
                    useful=True,
                    note="General diagnostic feedback remains private.",
                )
            autonomy.record_recommendation_feedback(
                conn,
                label="daily_reduce_risk",
                command="myos next-action",
                useful=True,
                note="Daily feedback should not disappear behind a tiny limit.",
            )

            rows = autonomy.recommendation_feedback_summary(conn, limit=1)
            labels = [row["label"] for row in rows]
            self.assertEqual(len(rows), 2)
            self.assertEqual(labels[0], "inspect_recent_traces")
            self.assertIn("daily_reduce_risk", labels)
            daily = next(row for row in rows if row["label"] == "daily_reduce_risk")
            self.assertEqual(daily["surface"], "daily")
            self.assertEqual(daily["recent_score"], 1)
            self.assertNotIn("inspect_safe_commands", labels)
            raw = "\n".join(str(tuple(row)) for row in conn.execute("SELECT * FROM recommendation_feedback").fetchall())
            self.assertNotIn("Daily feedback should not disappear", raw)
            conn.close()
        finally:
            os.environ.pop("MYOS_DB_PATH", None)
            os.unlink(db_path)

    def test_recommendation_feedback_summary_limit_keeps_negative_daily_audit_row(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        os.environ["MYOS_DB_PATH"] = db_path
        try:
            from personal_assistant import autonomy
            from personal_assistant.db import get_connection

            conn = get_connection()
            for _ in range(3):
                autonomy.record_recommendation_feedback(
                    conn,
                    label="inspect_recent_traces",
                    command="myos trace list",
                    useful=True,
                    note="General trace feedback remains private.",
                )
            for _ in range(2):
                autonomy.record_recommendation_feedback(
                    conn,
                    label="daily_nudge_owner",
                    command="myos next-action",
                    useful=False,
                    note="Negative daily feedback should remain auditable.",
                )

            rows = autonomy.recommendation_feedback_summary(conn, limit=1)
            labels = [row["label"] for row in rows]
            self.assertEqual(len(rows), 2)
            self.assertEqual(labels[0], "inspect_recent_traces")
            self.assertIn("daily_nudge_owner", labels)
            daily = next(row for row in rows if row["label"] == "daily_nudge_owner")
            self.assertEqual(daily["surface"], "daily")
            self.assertEqual(daily["score"], -2)
            self.assertEqual(daily["recent_score"], -2)
            self.assertEqual(daily["not_useful_count"], 2)
            raw = "\n".join(str(tuple(row)) for row in conn.execute("SELECT * FROM recommendation_feedback").fetchall())
            self.assertNotIn("Negative daily feedback", raw)
            conn.close()
        finally:
            os.environ.pop("MYOS_DB_PATH", None)
            os.unlink(db_path)

    def test_recommendation_feedback_summary_marks_mixed_daily_feedback(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        os.environ["MYOS_DB_PATH"] = db_path
        try:
            from personal_assistant import autonomy
            from personal_assistant.db import get_connection

            conn = get_connection()
            for _ in range(3):
                autonomy.record_recommendation_feedback(
                    conn,
                    label="inspect_recent_traces",
                    command="myos trace list",
                    useful=True,
                    note="General trace feedback remains private.",
                )
            autonomy.record_recommendation_feedback(
                conn,
                label="daily_reduce_risk",
                command="myos next-action",
                useful=True,
                note="Positive side of mixed daily feedback.",
            )
            autonomy.record_recommendation_feedback(
                conn,
                label="daily_reduce_risk",
                command="myos next-action",
                useful=False,
                note="Negative side of mixed daily feedback.",
            )

            rows = autonomy.recommendation_feedback_summary(conn, limit=1)
            labels = [row["label"] for row in rows]
            self.assertEqual(len(rows), 2)
            self.assertEqual(labels[0], "inspect_recent_traces")
            self.assertIn("daily_reduce_risk", labels)
            daily = next(row for row in rows if row["label"] == "daily_reduce_risk")
            self.assertEqual(daily["surface"], "daily")
            self.assertEqual(daily["score"], 0)
            self.assertEqual(daily["recent_score"], 0)
            self.assertEqual(daily["recent_useful_count"], 1)
            self.assertEqual(daily["recent_not_useful_count"], 1)
            self.assertTrue(daily["mixed_recent_feedback"])
            raw = "\n".join(str(tuple(row)) for row in conn.execute("SELECT * FROM recommendation_feedback").fetchall())
            self.assertNotIn("mixed daily feedback", raw)
            conn.close()
        finally:
            os.environ.pop("MYOS_DB_PATH", None)
            os.unlink(db_path)


if __name__ == "__main__":
    unittest.main()

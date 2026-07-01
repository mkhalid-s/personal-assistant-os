from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()

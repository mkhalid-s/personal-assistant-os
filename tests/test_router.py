from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


class SmartRouterTest(unittest.TestCase):
    def test_command_inventory_and_route_classification(self) -> None:
        from personal_assistant import router

        inventory = router.command_inventory()
        self.assertIn("do", inventory["daily"])
        self.assertIn("factory", inventory["workflow"])
        self.assertIn("release-check", inventory["diagnostic"])

        self.assertEqual(router.route_text("what should I work on today?").intent, "daily_brief")
        connector = router.route_text("draft a Confluence update for the launch page")
        self.assertEqual(connector.intent, "connector_update")
        self.assertTrue(connector.requires_confirmation)
        self.assertEqual(connector.workflow_pack, "connector_ops")
        unknown = router.route_text("blue elephant")
        self.assertEqual(unknown.intent, "unknown")
        self.assertTrue(unknown.requires_confirmation)

    def test_autopilot_workflow_selection_uses_signals(self) -> None:
        from personal_assistant import router

        routed = router.choose_autopilot_workflow(
            [{"title": "Draft Jira update", "detail": "post comment for launch blocker"}]
        )
        self.assertEqual(routed["intent"], "connector_update")
        self.assertEqual(routed["workflow_pack"], "connector_ops")

        daily = router.choose_autopilot_workflow([])
        self.assertEqual(daily["workflow_pack"], "daily_ops")

    def test_execute_route_captures_and_records_event(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        os.environ["MYOS_DB_PATH"] = db_path
        try:
            from personal_assistant import router
            from personal_assistant.db import get_connection

            conn = get_connection()
            result = router.execute_route(conn, "remember to follow up with platform", surface="test")
            conn.commit()
            self.assertEqual(result["decision"]["intent"], "capture")
            self.assertEqual(result["status"], "captured")
            events = conn.execute("SELECT COUNT(*) AS c FROM event_log WHERE event_type='smart_route'").fetchone()["c"]
            self.assertEqual(events, 1)
            payload = conn.execute("SELECT payload FROM event_log WHERE event_type='smart_route'").fetchone()["payload"]
            self.assertIn("text_hash", payload)
            conn.close()
        finally:
            os.environ.pop("MYOS_DB_PATH", None)
            os.unlink(db_path)

    def test_router_model_fallback_accepts_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "router_model.py"
            script.write_text(
                "import json, sys\n"
                "request = json.loads(sys.stdin.read() or '{}')\n"
                "assert any(item.get('command') == 'do' for item in request.get('command_catalog', []))\n"
                "print(json.dumps({"
                "'intent': 'daily_brief', "
                "'confidence': 0.91, "
                "'reason': 'local tiny model classified request', "
                "'recommended_workflow': 'Summarize the day.', "
                "'requires_confirmation': False, "
                "'command_tier': 'daily', "
                "'workflow_pack': 'daily_ops'"
                "}))\n"
            )
            os.environ["MYOS_ROUTER_COMMAND"] = f"{sys.executable} {script}"
            os.environ["MYOS_ROUTER_MIN_CONFIDENCE"] = "0.90"
            try:
                from personal_assistant import router

                decision = router.route_text("blue elephant")
                self.assertEqual(decision.intent, "daily_brief")
                self.assertEqual(decision.backend, "command")
                self.assertIn("model latency_ms", decision.fallback_reason)
            finally:
                os.environ.pop("MYOS_ROUTER_COMMAND", None)
                os.environ.pop("MYOS_ROUTER_MIN_CONFIDENCE", None)

    def test_router_model_fallback_ignores_bad_json_and_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.py"
            bad.write_text("print('not json')\n")
            os.environ["MYOS_ROUTER_COMMAND"] = f"{sys.executable} {bad}"
            os.environ["MYOS_ROUTER_MIN_CONFIDENCE"] = "0.90"
            try:
                from personal_assistant import router

                decision = router.route_text("blue elephant")
                self.assertEqual(decision.intent, "unknown")
                self.assertIn("router model fallback ignored", decision.fallback_reason)

                slow = Path(tmp) / "slow.py"
                slow.write_text("import time\ntime.sleep(2)\n")
                os.environ["MYOS_ROUTER_COMMAND"] = f"{sys.executable} {slow}"
                os.environ["MYOS_ROUTER_TIMEOUT_SEC"] = "1"
                timed = router.route_text("blue elephant")
                self.assertEqual(timed.intent, "unknown")
                self.assertIn("router model fallback ignored", timed.fallback_reason)
            finally:
                os.environ.pop("MYOS_ROUTER_COMMAND", None)
                os.environ.pop("MYOS_ROUTER_MIN_CONFIDENCE", None)
                os.environ.pop("MYOS_ROUTER_TIMEOUT_SEC", None)

    def test_route_eval_scores_packaged_fixtures(self) -> None:
        from personal_assistant import router

        result = router.evaluate_routes()
        summary = result["summary"]
        self.assertGreaterEqual(summary["total"], 6)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["accuracy"], 1.0)
        self.assertIn("daily_brief", summary["by_intent"])
        first = result["cases"][0]
        self.assertIn("text_hash", first)
        self.assertNotIn("text", first)

    def test_route_eval_model_shadow_handles_missing_and_bad_model(self) -> None:
        from personal_assistant import router

        os.environ.pop("MYOS_ROUTER_COMMAND", None)
        missing = router.evaluate_routes(model_shadow=True)
        self.assertTrue(missing["summary"]["model_shadow"])
        self.assertEqual(missing["summary"]["model_overrides"], 0)
        self.assertIn("model_shadow", missing["cases"][0])

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad_router.py"
            bad.write_text("print('not json')\n")
            os.environ["MYOS_ROUTER_COMMAND"] = f"{sys.executable} {bad}"
            try:
                bad_result = router.evaluate_routes(model_shadow=True)
                self.assertEqual(bad_result["summary"]["model_overrides"], 0)
                self.assertIn("router model fallback ignored", bad_result["cases"][0]["model_shadow"]["fallback_reason"])
            finally:
                os.environ.pop("MYOS_ROUTER_COMMAND", None)

    def test_route_feedback_stores_metadata_without_raw_note(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        os.environ["MYOS_DB_PATH"] = db_path
        try:
            from personal_assistant import router
            from personal_assistant.db import get_connection

            conn = get_connection()
            result = router.execute_route(conn, "remember to follow up with platform", surface="test")
            conn.commit()
            self.assertEqual(result["decision"]["intent"], "capture")
            event_id = conn.execute(
                "SELECT id FROM event_log WHERE event_type='smart_route' ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
            feedback_id = router.record_route_feedback(
                conn,
                event_id=event_id,
                expected_intent="daily_brief",
                note="Expected daily planning, not raw request text.",
            )
            row = conn.execute("SELECT * FROM route_feedback WHERE id=?", (feedback_id,)).fetchone()
            self.assertEqual(row["expected_intent"], "daily_brief")
            self.assertEqual(row["actual_intent"], "capture")
            self.assertTrue(row["text_hash"])
            self.assertTrue(row["note_hash"])
            self.assertEqual(row["note_length"], len("Expected daily planning, not raw request text."))
            raw = "\n".join(str(value) for value in row)
            self.assertNotIn("Expected daily planning", raw)
            override = conn.execute("SELECT * FROM route_overrides WHERE text_hash=?", (row["text_hash"],)).fetchone()
            self.assertEqual(override["expected_intent"], "daily_brief")
            learned = router.route_with_feedback(conn, "remember to follow up with platform", surface="test")
            self.assertEqual(learned.intent, "daily_brief")
            self.assertEqual(learned.backend, "feedback")
            unchanged = router.route_with_feedback(conn, "remember to follow up with platform tomorrow", surface="test")
            self.assertEqual(unchanged.intent, "capture")
            overrides = router.list_route_overrides(conn)
            self.assertEqual(len(overrides), 1)
            conn.close()
        finally:
            os.environ.pop("MYOS_DB_PATH", None)
            os.unlink(db_path)


if __name__ == "__main__":
    unittest.main()

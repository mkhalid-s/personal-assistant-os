from __future__ import annotations

import os
import tempfile
import unittest


class ObservabilityTest(unittest.TestCase):
    def test_schema_trace_lifecycle_and_cleanup_rollup(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        os.environ["MYOS_DB_PATH"] = db_path
        try:
            from personal_assistant import observability
            from personal_assistant.db import get_connection, verify_schema

            conn = get_connection()
            report = verify_schema(conn)
            self.assertTrue(report["ok"])
            self.assertEqual(report["expected_version"], 37)

            corr = observability.start_trace(
                conn,
                command="do",
                command_path="do",
                argv_hash=observability._hash_text("private request text"),
            )
            observability.link_trace(conn, corr, intent="capture", command_tier="daily", safety_level="local_write")
            observability.finish_trace(conn, corr, status="completed", duration_ms=12, summary="do completed")

            rows = observability.list_traces(conn)
            self.assertEqual(rows[0]["correlation_id"], corr)
            self.assertEqual(rows[0]["intent"], "capture")
            self.assertNotIn("private request text", str(rows[0]))

            conn.execute(
                "UPDATE execution_traces SET started_at = '2000-01-01T00:00:00Z' WHERE correlation_id = ?", (corr,)
            )
            conn.commit()
            result = observability.cleanup_traces(conn, retention_days=1, max_rows=100)
            self.assertEqual(result["deleted"], 1)
            rollups = observability.rollups(conn)
            self.assertEqual(rollups[0]["command_path"], "do")
            self.assertEqual(rollups[0]["trace_count"], 1)
            conn.close()
        finally:
            os.environ.pop("MYOS_DB_PATH", None)
            os.unlink(db_path)

    def test_current_trace_links_route_event(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        os.environ["MYOS_DB_PATH"] = db_path
        try:
            from personal_assistant import observability, router
            from personal_assistant.db import get_connection

            conn = get_connection()
            corr = observability.start_trace(conn, command="do", command_path="do")
            os.environ[observability.TRACE_ENV] = corr
            try:
                router.execute_route(conn, "remember to follow up with platform", surface="test")
                conn.commit()
            finally:
                os.environ.pop(observability.TRACE_ENV, None)
            row = conn.execute(
                "SELECT route_event_id, intent, command_tier FROM execution_traces WHERE correlation_id = ?",
                (corr,),
            ).fetchone()
            self.assertIsNotNone(row[0])
            self.assertEqual(row[1], "capture")
            self.assertEqual(row[2], "daily")
            conn.close()
        finally:
            os.environ.pop("MYOS_DB_PATH", None)
            os.unlink(db_path)


if __name__ == "__main__":
    unittest.main()

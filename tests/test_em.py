"""Phase 2/3 tests: inference-first ingestion (route_note), people/evidence/1:1
persistence, meeting capture, review-packet assembly, and the brain EM tools.
All deterministic — no model/network required."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _fresh_db_conn():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["MYOS_DB_PATH"] = tmp.name
    from personal_assistant.db import get_connection

    return get_connection(), tmp.name


class InferRouteTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_evidence_is_inferred_and_filed(self):
        from personal_assistant import em

        res = em.route_note(self.conn, "Priya led the incident response calmly and kept stakeholders updated.")
        self.assertEqual(res["routed"], "evidence")
        self.assertEqual(res["person"], "Priya")
        row = self.conn.execute(
            "SELECT person, person_id, category FROM review_evidence ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["person"], "Priya")
        self.assertIsNotNone(row["person_id"])  # linked to a created person record

    def test_one_on_one_inferred_with_action_item(self):
        from personal_assistant import em

        res = em.route_note(self.conn, "1:1 with Sam: discussed growth, he will write the design doc by Friday")
        self.assertEqual(res["routed"], "one_on_one")
        self.assertEqual(res["person"], "Sam")
        self.assertTrue(res["action_item_ids"])  # extracted at least one follow-up
        # the action item is in the inbox, owned by Sam
        owned = self.conn.execute(
            "SELECT COUNT(*) AS c FROM inbox_items WHERE owner = 'Sam' AND source = 'one_on_one'"
        ).fetchone()["c"]
        self.assertGreaterEqual(owned, 1)

    def test_decision_routes_to_inbox(self):
        from personal_assistant import em

        res = em.route_note(self.conn, "Decision: move the freeze to Wednesday per launch review.")
        self.assertEqual(res["routed"], "inbox")
        self.assertEqual(res["kind"], "decision")


class MeetingCaptureTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_capture_extracts_actions_decisions_risks(self):
        from personal_assistant import em

        text = (
            "Launch sync.\n"
            "We decided to cut the export feature from v1.\n"
            "Raj will own the canary rollout by Monday.\n"
            "Risk: still blocked on the platform team's token rotation.\n"
        )
        res = em.capture_meeting(self.conn, "Launch sync", text)
        self.assertGreaterEqual(res["action_items"], 1)
        kinds = {
            r["kind"]
            for r in self.conn.execute(
                "SELECT kind FROM meeting_items WHERE meeting_id = ?", (res["meeting_id"],)
            ).fetchall()
        }
        self.assertIn("decision", kinds)
        self.assertIn("action", kinds)
        self.assertIn("risk", kinds)
        # an action item surfaces as a tracked inbox commitment
        c = self.conn.execute("SELECT COUNT(*) AS c FROM inbox_items WHERE source='meeting'").fetchone()["c"]
        self.assertGreaterEqual(c, 1)


class DossierReviewTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_review_packet_assembles_from_dossier(self):
        from personal_assistant import em

        em.upsert_person(self.conn, "Maya", role="Senior Engineer", relation="report")
        em.record_evidence(self.conn, "Maya", "delivery", "Shipped the billing migration a week early.")
        em.record_evidence(self.conn, "Maya", "leadership", "Mentored two juniors through their first on-call.")
        em.log_one_on_one(self.conn, "Maya", "Wants a tech-lead stretch project next quarter.", sentiment="positive")
        em.record_competency(self.conn, "Maya", "technical", "exceeds")
        self.conn.commit()

        packet = em.build_review_packet(self.conn, "Maya")
        self.assertIn("Maya", packet)
        self.assertIn("delivery", packet)
        self.assertIn("leadership", packet)
        self.assertIn("technical", packet)
        self.assertIn("billing migration", packet)


class BrainEmToolsTest(unittest.TestCase):
    """The Claude brain's EM tools persist locally and auto-run (no approval, no network)."""

    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_log_evidence_tool_and_dossier_read(self):
        from personal_assistant.providers.claude import ClaudeBackend

        b = ClaudeBackend()
        out, is_error = b._dispatch(
            self.conn,
            "log_evidence",
            {"person": "Dev", "category": "ownership", "impact": "Drove the Sev2 to resolution overnight."},
            {"task_id": None, "ids": []},
        )
        self.assertFalse(is_error)
        self.assertIn("logged evidence", out)
        # read it back through the dossier read tool
        dossier = b._read(self.conn, "person_dossier", {"person": "Dev"})
        self.assertEqual(dossier["person"]["name"], "Dev")
        self.assertTrue(any("Sev2" in e["impact"] for e in dossier["evidence"]))


if __name__ == "__main__":
    unittest.main()

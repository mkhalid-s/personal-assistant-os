"""P4 tests: project-risk scanning + proactive nudges (approval-gated). No network."""

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


def _seed(conn):
    conn.execute(
        "INSERT INTO work_items (title, kind, status, priority, risk_score, owner, due_date) "
        "VALUES ('Ship billing migration','task','open',1,30,'Maya','2020-01-01')"
    )  # overdue
    conn.execute(
        "INSERT INTO work_items (title, kind, status, priority, risk_score, owner) "
        "VALUES ('Auth token rotation','risk','open',1,85,'Priya')"
    )  # at-risk
    conn.execute(
        "INSERT INTO external_items (connector, external_id, item_type, title, status, owner) "
        "VALUES ('github','42','pull_request','Add retry to connector','open','Raj')"
    )  # PR awaiting review
    conn.execute(
        "INSERT INTO external_items (connector, external_id, item_type, title, status, priority, owner) "
        "VALUES ('jira','ABC-9','issue','Gateway dependency','open','p1','Sam')"
    )  # priority issue
    conn.commit()


class RiskScanTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()
        _seed(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_scan_finds_all_risk_kinds(self):
        from personal_assistant import watch

        kinds = {f["kind"] for f in watch.scan_project_risks(self.conn)}
        self.assertEqual(kinds, {"overdue", "at_risk", "pr_review", "priority_issue"})

    def test_findings_carry_a_suggested_nudge(self):
        from personal_assistant import watch

        for f in watch.scan_project_risks(self.conn):
            self.assertTrue(f["suggested_nudge"])
            self.assertIn(f["severity"], ("high", "medium", "low"))

    def test_draft_nudges_are_proposed_not_sent(self):
        from personal_assistant import watch

        findings = watch.scan_project_risks(self.conn)
        ids = watch.draft_nudges(self.conn, findings)
        self.assertEqual(len(ids), len(findings))
        rows = self.conn.execute(
            "SELECT action_type, requires_approval, status FROM agent_actions WHERE id IN (%s)"
            % ",".join("?" * len(ids)),
            ids,
        ).fetchall()
        for r in rows:
            self.assertEqual(r["action_type"], "draft_external_update")
            self.assertEqual(r["requires_approval"], 1)   # gated
            self.assertEqual(r["status"], "proposed")      # not executed / sent
        # nothing was sent to the outbox
        sent = self.conn.execute("SELECT COUNT(*) AS c FROM action_outbox").fetchone()["c"]
        self.assertEqual(sent, 0)

    def test_risk_signals_have_stable_keys(self):
        from personal_assistant import watch

        sig1 = {s["key"] for s in watch.risk_signals(self.conn)}
        sig2 = {s["key"] for s in watch.risk_signals(self.conn)}
        self.assertEqual(sig1, sig2)  # deterministic, dedupe-friendly across cycles
        self.assertTrue(all(k.startswith("risk:") for k in sig1))

    def test_brain_scan_risks_tool(self):
        from personal_assistant.providers.claude import ClaudeBackend

        findings = ClaudeBackend()._read(self.conn, "scan_risks", {})
        self.assertTrue(findings)
        self.assertTrue(any(f["kind"] == "at_risk" for f in findings))


if __name__ == "__main__":
    unittest.main()

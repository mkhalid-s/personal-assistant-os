"""Regression tests for the post-#12-refactor remediation:
A-1/C-1/C-2/C-4 (apply_patch guard), C-3 (privacy JSON corruption), B-1/B-2 (FTS ledger)."""

from __future__ import annotations

import json
import os
import subprocess
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


class GuardUnitTest(unittest.TestCase):
    def test_protected_path_covers_package_and_blocks_escape(self):
        from personal_assistant.execution import _path_is_protected

        for p in (
            "personal_assistant/privacy.py",
            "src/personal_assistant/autopilot.py",
            ".claude/settings.json",
            ".git/hooks/pre-commit",
            "x/../../etc/passwd",
            "/etc/passwd",
            "settings.local.json",
        ):
            self.assertTrue(_path_is_protected(p), p)
        for p in ("tests/test_db.py", "docs/cli.py.md", "myproject/main.py", "src/app/db.py"):
            self.assertFalse(_path_is_protected(p), p)  # A-1/C-4: don't over-block

    def test_patch_target_paths_reads_rename_and_copy_headers(self):
        from personal_assistant.execution import _patch_target_paths

        diff = (
            "diff --git a/notes.txt b/notes.txt\n"
            "similarity index 100%\n"
            "rename from notes.txt\n"
            "rename to src/personal_assistant/evil.py\n"
        )
        paths = _patch_target_paths(diff)
        self.assertIn("src/personal_assistant/evil.py", paths)  # C-1: rename target not hidden


class ApplyPatchHardeningTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()
        self.repo = tempfile.mkdtemp()
        for a in (
            ("init", "-q", "-b", "main"),
            ("config", "user.email", "t@t"),
            ("config", "user.name", "t"),
            ("config", "commit.gpgsign", "false"),
        ):
            subprocess.run(["git", "-C", self.repo, *a], capture_output=True, check=True)
        Path(self.repo, "seed.txt").write_text("seed\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "init"], capture_output=True, check=True)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)

    def _run_patch(self, diff):
        from personal_assistant import agentcore, cli

        task_id = agentcore.ensure_turn_task(self.conn, "patch")
        self.conn.execute(
            "INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, requires_approval, status) "
            "VALUES (?, 'apply_patch', 'p', ?, 1, 'approved')",
            (task_id, json.dumps({"diff": diff, "repo_root": self.repo})),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM agent_actions ORDER BY id DESC LIMIT 1").fetchone()
        return cli._execute_agent_action(self.conn, row)

    def test_rename_header_into_package_is_blocked(self):
        diff = (
            "diff --git a/notes.txt b/notes.txt\nsimilarity index 100%\n"
            "rename from notes.txt\nrename to src/personal_assistant/evil.py\n"
        )
        self.assertIn("blocked", self._run_patch(diff).lower())  # C-1 + A-1

    def test_symlink_hunk_is_blocked(self):
        diff = (
            "diff --git a/link b/link\nnew file mode 120000\n--- /dev/null\n+++ b/link\n@@ -0,0 +1 @@\n+/etc/passwd\n"
        )
        self.assertIn("blocked", self._run_patch(diff).lower())  # C-2

    def test_patch_to_extracted_safety_module_is_blocked(self):
        diff = (
            "diff --git a/src/personal_assistant/privacy.py b/src/personal_assistant/privacy.py\n"
            "--- a/src/personal_assistant/privacy.py\n+++ b/src/personal_assistant/privacy.py\n"
            "@@ -1 +1 @@\n-x\n+y\n"
        )
        self.assertIn("blocked", self._run_patch(diff).lower())  # A-1: new modules protected


class RedactObjTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_redact_preserves_non_strings_and_valid_json(self):
        from personal_assistant.privacy import redact_obj

        payload = {
            "issue_number": 123456789012,
            "count": 42,
            "ok": True,
            "x": None,
            "draft": "reach me at 555-867-5309",
            "nested": {"phone": "+1 555 222 3333", "n": 7},
        }
        out = redact_obj(self.conn, payload)
        self.assertEqual(out["issue_number"], 123456789012)  # int NOT mangled (C-3)
        self.assertEqual(out["count"], 42)
        self.assertIs(out["ok"], True)
        self.assertIsNone(out["x"])
        self.assertIn("[REDACTED_PHONE]", out["draft"])
        self.assertIn("[REDACTED_PHONE]", out["nested"]["phone"])
        self.assertEqual(out["nested"]["n"], 7)
        json.dumps(out)  # must remain serializable (would have raised pre-fix)


class FtsLedgerTest(unittest.TestCase):
    def test_fts_migrations_recorded(self):  # B-1/B-2
        conn, db_path = _fresh_db_conn()
        try:
            rows = {
                r[0] for r in conn.execute("SELECT version FROM schema_migrations WHERE version IN (17, 19)").fetchall()
            }
            self.assertEqual(rows, {17, 19})
        finally:
            conn.close()
            os.unlink(db_path)
            os.environ.pop("MYOS_DB_PATH", None)


if __name__ == "__main__":
    unittest.main()

"""Regression tests for the code-review remediation (safety-critical fixes).

Each test pins a specific review finding so the fix can't silently regress.
No network: the Agent SDK is stubbed; everything else is local SQLite.
"""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _detect_fts5() -> bool:
    """True iff this Python's sqlite3 was built with the FTS5 extension (A7).

    FTS-dependent tests SKIP (not ERROR) where FTS5 is absent — the app self-heals
    to a LIKE scan there, so a missing module is a build property, not a failure."""
    import sqlite3

    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE _probe USING fts5(x)")
        c.close()
        return True
    except sqlite3.OperationalError:
        return False


HAS_FTS5 = _detect_fts5()


def _fresh_db_conn():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["MYOS_DB_PATH"] = tmp.name
    from personal_assistant.db import get_connection

    return get_connection(), tmp.name


def _install_fake_sdk():
    """Provide a minimal claude_agent_sdk.types so the can_use_tool gate is testable."""
    mod = types.ModuleType("claude_agent_sdk")
    tmod = types.ModuleType("claude_agent_sdk.types")

    class PermissionResultAllow:
        def __init__(self, updated_input=None):
            self.updated_input = updated_input
            self.decision = "allow"

    class PermissionResultDeny:
        def __init__(self, message=""):
            self.message = message
            self.decision = "deny"

    tmod.PermissionResultAllow = PermissionResultAllow
    tmod.PermissionResultDeny = PermissionResultDeny
    mod.types = tmod
    sys.modules["claude_agent_sdk"] = mod
    sys.modules["claude_agent_sdk.types"] = tmod


class SdkGateTest(unittest.TestCase):
    """Finding #1/#13: can_use_tool is the sole authority and blocks destructive at every level."""

    def setUp(self):
        _install_fake_sdk()
        from personal_assistant.providers.claude_sdk import ClaudeSdkBackend

        self.backend = ClaudeSdkBackend()

    def _decide(self, level, interactive, tool, inp):
        cb = self.backend._make_can_use_tool(level=level, interactive=interactive)
        return asyncio.run(cb(tool, inp, None)).decision

    def test_destructive_denied_even_at_bold(self):
        for cmd in ("rm -rf /tmp/x", "git push origin main --force", "git reset --hard", "find . -delete"):
            self.assertEqual(self._decide("bold", True, "bash", {"command": cmd}), "deny", cmd)
        self.assertEqual(self._decide("bold", True, "mcp__jira__delete_issue", {}), "deny")

    def test_reads_allowed_writes_gated_unattended(self):
        self.assertEqual(self._decide("safe", True, "read", {}), "allow")
        # confirm-tier write with no human present -> deny (can't get a tap)
        self.assertEqual(self._decide("bold", False, "edit", {"path": "x"}), "deny")

    def test_setting_sources_not_loaded_by_default(self):
        # Finding #1 (A6): without opt-in, .claude allow-rules must NOT be loaded — they'd
        # short-circuit can_use_tool and bypass the autonomy gate. Assert the REAL kwargs the
        # SDK is constructed with, not a substring of the source (which can't catch a logic bug).
        os.environ.pop("MYOS_SDK_LOAD_SETTINGS", None)
        kw = self.backend._options_kwargs(level="bold", interactive=True)
        self.assertNotIn("setting_sources", kw)  # default: gate is the sole authority
        # can_use_tool is always wired (the gate), regardless of settings.
        self.assertTrue(callable(kw["can_use_tool"]))

    def test_setting_sources_loaded_only_on_explicit_opt_in(self):
        # The opt-in path must produce exactly the documented allow-list, and nothing else enables it.
        for val, expected in (("1", True), ("true", True), ("yes", True), ("0", False), ("", False)):
            if val:
                os.environ["MYOS_SDK_LOAD_SETTINGS"] = val
            else:
                os.environ.pop("MYOS_SDK_LOAD_SETTINGS", None)
            kw = self.backend._options_kwargs(level="bold", interactive=True)
            if expected:
                self.assertEqual(kw.get("setting_sources"), ["project", "user"], val)
            else:
                self.assertNotIn("setting_sources", kw, val)
        os.environ.pop("MYOS_SDK_LOAD_SETTINGS", None)


class AutonomyHardeningTest(unittest.TestCase):
    def test_bold_never_auto_sends_external(self):
        from personal_assistant import autonomy as a

        self.assertEqual(a._BOLD_AUTO, frozenset())  # finding #2
        self.assertEqual(a.classify_action("draft_external_update", level="bold")["tier"], a.CONFIRM)

    def test_bash_evasions_now_blocked(self):
        from personal_assistant import autonomy as a

        for cmd in (
            "rm --recursive --force /",
            "find . -delete",
            "git push origin +main",
            "rm$IFS-rf$IFS/tmp",
            "echo aGk | base64 -d | sh",
            "chmod -R 777 /",
            "shred -u secrets",
        ):
            self.assertEqual(a.classify_tool("bash", {"command": cmd}, level="bold")["tier"], a.BLOCKED, cmd)

    def test_write_checked_before_read_substring(self):
        from personal_assistant import autonomy as a

        # 'create_widget' contains the read token 'get' as a substring -> must NOT auto-run.
        self.assertEqual(a.classify_tool("mcp__svc__create_widget", {}, level="bold")["tier"], a.CONFIRM)
        self.assertEqual(a.classify_tool("mcp__svc__get_widget", {}, level="bold")["tier"], a.AUTO)

    def test_auto_safe_derived_from_tier_table(self):
        from personal_assistant import agentcore, autonomy

        self.assertEqual(agentcore.AUTO_SAFE_ACTION_TYPES, autonomy.AUTO_ACTION_TYPES)
        self.assertEqual(autonomy.AUTO_ACTION_TYPES, frozenset({"create_inbox_item"}))


class ApplyPatchGuardTest(unittest.TestCase):
    """Finding #4: an approved patch can't rewrite the safety policy or escape the tree."""

    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()
        self.repo = tempfile.mkdtemp()
        for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@t"),
                     ("config", "user.name", "t"), ("config", "commit.gpgsign", "false")):
            subprocess.run(["git", "-C", self.repo, *args], capture_output=True, check=True)
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
            (task_id, __import__("json").dumps({"diff": diff, "repo_root": self.repo})),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM agent_actions ORDER BY id DESC LIMIT 1").fetchone()
        return cli._execute_agent_action(self.conn, row)

    def test_patch_touching_autonomy_is_blocked(self):
        diff = "diff --git a/src/personal_assistant/autonomy.py b/src/personal_assistant/autonomy.py\n--- a/src/personal_assistant/autonomy.py\n+++ b/src/personal_assistant/autonomy.py\n@@ -1 +1 @@\n-x\n+y\n"
        self.assertIn("blocked", self._run_patch(diff).lower())

    def test_patch_escaping_tree_is_blocked(self):
        diff = "--- a/../../etc/evil\n+++ b/../../etc/evil\n@@ -1 +1 @@\n-x\n+y\n"
        self.assertIn("blocked", self._run_patch(diff).lower())


class ClearBugTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_json_extract_survives_trailing_brace(self):  # finding #5
        from personal_assistant.providers.agent_cli import _parse_output

        raw = '{"reply":"ok","plan":[],"actions":[{"action_type":"draft_external_update","title":"t","payload":{},"requires_approval":1}]}\nThanks! {see you}'
        out = _parse_output(raw)
        self.assertEqual(len(out["actions"]), 1)

    def test_meeting_with_decision_not_misfiled(self):  # finding #6
        from personal_assistant import em

        text = "Launch sync.\nWe decided to cut export.\nRaj will own the rollout by Monday.\n"
        self.assertEqual(em.classify_note(text), "meeting")

    @unittest.skipUnless(HAS_FTS5, "sqlite3 built without FTS5; app falls back to LIKE scan")
    def test_fts_index_actually_populated(self):  # finding #15 (A7: skip, don't error, without FTS5)
        from personal_assistant import agentcore

        agentcore.remember(self.conn, "Priya owns auth token rotation")
        self.conn.commit()
        n = self.conn.execute("SELECT COUNT(*) AS c FROM text_chunks_fts").fetchone()["c"]
        self.assertGreaterEqual(n, 1)  # proves migration 17 + trigger, not just the scan fallback

    def test_worker_processes_once_via_cmd_worker(self):  # finding #14
        import argparse

        from personal_assistant import cli

        self.conn.execute("INSERT INTO workflow_queue (workflow_name, payload_json, status) VALUES ('daily','{}','queued')")
        self.conn.commit()
        calls = []
        orig = cli.cmd_orchestrate
        cli.cmd_orchestrate = lambda args: calls.append(1)
        try:
            cli.cmd_worker(argparse.Namespace(limit=10))
            cli.cmd_worker(argparse.Namespace(limit=10))
        finally:
            cli.cmd_orchestrate = orig
        self.assertEqual(len(calls), 1)  # claimed and processed exactly once

    def test_worker_claim_skips_row_claimed_after_select(self):  # A8: exercise the atomic claim under a race
        import argparse

        from personal_assistant import cli

        # Two jobs: both returned by the worker's initial SELECT as 'queued'.
        self.conn.execute("INSERT INTO workflow_queue (workflow_name, payload_json, status) VALUES ('daily','{}','queued')")
        self.conn.execute("INSERT INTO workflow_queue (workflow_name, payload_json, status) VALUES ('daily','{}','queued')")
        self.conn.commit()
        ids = [int(r["id"]) for r in self.conn.execute("SELECT id FROM workflow_queue ORDER BY id ASC").fetchall()]
        second = ids[1]
        seen = []
        orig = cli.cmd_orchestrate

        def fake_orchestrate(args):
            # Simulate a *competing* worker claiming the second job mid-loop — after the
            # initial SELECT already handed it to us as 'queued'. Our claim must then lose.
            seen.append(1)
            self.conn.execute(
                "UPDATE workflow_queue SET status='running' WHERE id = ? AND status='queued'", (second,)
            )
            self.conn.commit()

        cli.cmd_orchestrate = fake_orchestrate
        try:
            cli.cmd_worker(argparse.Namespace(limit=10))
        finally:
            cli.cmd_orchestrate = orig
        # Only the first job ran; the second's compare-and-claim UPDATE matched 0 rows.
        self.assertEqual(len(seen), 1)
        statuses = {int(r["id"]): r["status"] for r in self.conn.execute("SELECT id, status FROM workflow_queue").fetchall()}
        self.assertEqual(statuses[second], "running")  # held by the simulated competitor, never re-claimed by us

    def test_json_extract_prefers_contract_over_leading_decoy(self):  # A9: decoy BEFORE the contract
        from personal_assistant.providers.agent_cli import _parse_output

        # A parseable non-contract object appears FIRST; the real {plan,actions} object follows.
        # A naive "first balanced object wins" parser would return the decoy and drop all actions.
        raw = (
            '{"note":"thinking out loud, ignore me"}\n'
            '{"reply":"done","plan":[],"actions":[{"action_type":"create_inbox_item",'
            '"title":"t","payload":{},"requires_approval":0}]}'
        )
        out = _parse_output(raw)
        self.assertEqual(out["reply"], "done")
        self.assertEqual(len(out["actions"]), 1)  # contract chosen by shape, not by position


if __name__ == "__main__":
    unittest.main()

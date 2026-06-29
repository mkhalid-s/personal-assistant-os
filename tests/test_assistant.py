"""Tests for the provider-agnostic assistant: propose-and-approve safety,
the harness/delegation path, and the CLI-backend JSON contract.

None of these touch the network: the Claude tool-loop is exercised with a fake
Anthropic client, and the external agent CLIs are stubbed with shell scripts.
"""

from __future__ import annotations

import os
import stat
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


def _write_script(path: Path, body: str) -> str:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(path)


class ProposeAndApproveTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_propose_tool_enqueues_and_posts_nothing(self):
        from personal_assistant.providers.claude import ClaudeBackend

        backend = ClaudeBackend()
        ctx = {"task_id": None, "ids": []}
        out, is_error = backend._dispatch(
            self.conn, "propose_jira_comment",
            {"issue_key": "ABC-123", "body": "Following up on the launch dependency."}, ctx,
        )
        self.assertFalse(is_error)
        self.assertIn("pending approval", out.lower())
        self.assertEqual(len(ctx["ids"]), 1)

        row = self.conn.execute(
            "SELECT action_type, requires_approval, status, payload_json FROM agent_actions WHERE id = ?",
            (ctx["ids"][0],),
        ).fetchone()
        self.assertEqual(row["action_type"], "draft_external_update")
        self.assertEqual(row["requires_approval"], 1)  # external mutation MUST be gated
        self.assertEqual(row["status"], "proposed")    # never auto-executed
        self.assertIn("ABC-123", row["payload_json"])

    def test_read_tool_does_not_write(self):
        from personal_assistant.providers.claude import ClaudeBackend

        backend = ClaudeBackend()
        before = self.conn.execute("SELECT COUNT(*) AS c FROM agent_actions").fetchone()["c"]
        out, is_error = backend._dispatch(self.conn, "get_brief", {}, {"task_id": None, "ids": []})
        self.assertFalse(is_error)
        after = self.conn.execute("SELECT COUNT(*) AS c FROM agent_actions").fetchone()["c"]
        self.assertEqual(before, after)

    def test_capture_item_dedups(self):
        from personal_assistant import agentcore

        inbox_id, created = agentcore.capture_item(self.conn, text="Ship the canary checks")
        self.conn.commit()
        self.assertTrue(created)
        self.assertIsNotNone(inbox_id)
        dup_id, dup_created = agentcore.capture_item(self.conn, text="Ship the canary checks")
        self.conn.commit()
        self.assertFalse(dup_created)
        self.assertIsNone(dup_id)

    def test_enqueue_forces_approval_for_non_safe_types(self):
        from personal_assistant import agentcore

        task_id = agentcore.ensure_turn_task(self.conn, "test")
        # Even if a caller asks for requires_approval=0 on an external type, it is clamped.
        aid = agentcore.enqueue_proposal(
            self.conn, task_id=task_id, action_type="draft_external_update",
            title="x", payload={"draft": "y"}, requires_approval=0,
        )
        self.conn.commit()
        row = self.conn.execute("SELECT requires_approval FROM agent_actions WHERE id = ?", (aid,)).fetchone()
        self.assertEqual(row["requires_approval"], 1)


class ClaudeLoopTest(unittest.TestCase):
    """Exercise the manual tool-use loop with a fake Anthropic client (no network)."""

    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_run_turn_dispatches_tool_then_replies(self):
        from personal_assistant.providers.claude import ClaudeBackend

        class _Block:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class _Resp:
            def __init__(self, content, stop_reason):
                self.content = content
                self.stop_reason = stop_reason

        class _Stream:
            def __init__(self, resp):
                self._resp = resp

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                return iter(())

            def get_final_message(self):
                return self._resp

        class _Messages:
            def __init__(self):
                self.calls = 0

            def stream(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return _Stream(_Resp(
                        [_Block(type="tool_use", id="t1", name="propose_jira_comment",
                                input={"issue_key": "ABC-9", "body": "nudge"})],
                        "tool_use",
                    ))
                return _Stream(_Resp([_Block(type="text", text="Drafted a Jira nudge for your approval.")], "end_turn"))

        class _Client:
            def __init__(self):
                self.messages = _Messages()

        backend = ClaudeBackend()
        backend._client_and_model = lambda: (_Client(), "claude-opus-4-8")
        result = backend.run_turn(self.conn, "nudge the platform team", [])

        self.assertIn("approval", result["reply"].lower())
        self.assertEqual(len(result["proposed_action_ids"]), 1)
        row = self.conn.execute(
            "SELECT action_type, requires_approval FROM agent_actions WHERE id = ?",
            (result["proposed_action_ids"][0],),
        ).fetchone()
        self.assertEqual(row["action_type"], "draft_external_update")
        self.assertEqual(row["requires_approval"], 1)


class AgentCliBackendTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_cli_backend_parses_json_contract(self):
        from personal_assistant.providers.agent_cli import AgentCliBackend

        script = _write_script(
            Path(self.tmp) / "fake_agent.sh",
            '#!/bin/sh\ncat >/dev/null\n'
            'echo \'{"reply":"ok","plan":[{"step":"s","detail":"d"}],'
            '"actions":[{"action_type":"draft_external_update","title":"t","payload":{"draft":"x"},"requires_approval":1}]}\'\n',
        )
        backend = AgentCliBackend(name="command", command=script, input_mode="prompt")
        ok, _ = backend.available()
        self.assertTrue(ok)
        result = backend.reason(self.conn, {"purpose": "chat", "objective": "do a thing"})
        self.assertEqual(result["reply"], "ok")
        self.assertEqual(len(result["plan"]), 1)
        self.assertEqual(result["actions"][0]["action_type"], "draft_external_update")

    def test_cli_backend_freeform_output_has_no_actions(self):
        from personal_assistant.providers.agent_cli import AgentCliBackend

        script = _write_script(Path(self.tmp) / "freeform.sh", '#!/bin/sh\ncat >/dev/null\necho "just some advice"\n')
        backend = AgentCliBackend(name="command", command=script, input_mode="prompt")
        result = backend.reason(self.conn, {"purpose": "chat", "objective": "advise"})
        self.assertIn("advice", result["reply"])
        self.assertEqual(result["actions"], [])


class DelegateHarnessTest(unittest.TestCase):
    """A harnessed coding agent runs in a throwaway worktree; its changes are
    proposed as a patch and never touch the real tree without approval."""

    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()
        self.repo = tempfile.mkdtemp()

        def _git(*args):
            subprocess.run(["git", "-C", self.repo, *args], capture_output=True, check=True)

        _git("init", "-q", "-b", "main")
        _git("config", "user.email", "t@example.com")
        _git("config", "user.name", "Test")
        _git("config", "commit.gpgsign", "false")  # CI/sandbox may force-sign without a key
        Path(self.repo, "seed.txt").write_text("seed\n")
        _git("add", "-A")
        _git("commit", "-qm", "init")

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_delegate_proposes_patch_without_mutating_real_tree(self):
        from personal_assistant import assistant

        exec_script = _write_script(
            Path(self.repo) / "fake_cursor.sh",
            '#!/bin/sh\necho "added by harnessed agent" > harnessed.txt\n',
        )
        os.environ["MYOS_AGENT_EXEC_CURSOR"] = exec_script
        try:
            result = assistant.delegate_to_agent(self.conn, "cursor", "add a file", cwd=self.repo)
        finally:
            os.environ.pop("MYOS_AGENT_EXEC_CURSOR", None)

        self.assertNotIn("error", result, msg=result.get("error", ""))
        self.assertEqual(len(result["proposed_action_ids"]), 1)
        self.assertIn("harnessed.txt", result["diff"])
        # The real working tree must be untouched -- only a proposal exists.
        self.assertFalse(Path(self.repo, "harnessed.txt").exists())
        row = self.conn.execute(
            "SELECT action_type, requires_approval FROM agent_actions WHERE id = ?",
            (result["proposed_action_ids"][0],),
        ).fetchone()
        self.assertEqual(row["action_type"], "apply_patch")
        self.assertEqual(row["requires_approval"], 1)

    def test_delegate_requires_git_repo(self):
        from personal_assistant import assistant

        non_git = tempfile.mkdtemp()
        os.environ["MYOS_AGENT_EXEC_CURSOR"] = "/bin/true"
        try:
            result = assistant.delegate_to_agent(self.conn, "cursor", "task", cwd=non_git)
        finally:
            os.environ.pop("MYOS_AGENT_EXEC_CURSOR", None)
        self.assertIn("error", result)
        self.assertIn("git repo", result["error"])


if __name__ == "__main__":
    unittest.main()

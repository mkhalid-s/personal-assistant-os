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

    def test_run_turn_persists_retrieval_trace_ids(self):
        from personal_assistant import assistant
        from personal_assistant.inbox import ensure_work_item_node, index_chunk

        self.conn.execute(
            """
            INSERT INTO work_items (title, kind, status, priority, risk_score)
            VALUES ('Dashboard launch evidence', 'task', 'open', 2, 10)
            """
        )
        item_id = int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        ensure_work_item_node(self.conn, item_id, "Dashboard launch evidence")
        index_chunk(self.conn, "work_item", item_id, "Dashboard launch evidence")
        self.conn.commit()

        class _Backend:
            name = "fake"

            def run_turn(self, conn, user_text, history, on_text=None):
                return {"reply": "Here is the cited dashboard answer.", "backend": "fake", "proposed_action_ids": []}

        original = assistant.get_backend
        assistant.get_backend = lambda name=None: _Backend()
        try:
            result = assistant.run_turn(self.conn, "dashboard launch", [], backend_name="fake")
        finally:
            assistant.get_backend = original

        self.assertIn("retrieval_run_ids", result)
        self.assertIn("route_decision", result)
        self.assertEqual(result["route_decision"]["intent"], "unknown")
        self.assertEqual(result["reply"], "Here is the cited dashboard answer.")
        turn = self.conn.execute(
            "SELECT retrieval_run_ids FROM conversation_turns ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIn(str(result["retrieval_run_ids"][0]), turn["retrieval_run_ids"])


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

    def test_zero_backend_registered_and_executor_override(self):
        from personal_assistant.providers import available_backends, get_backend
        from personal_assistant.providers.zero import ZeroBackend

        script = _write_script(
            Path(self.tmp) / "zero",
            '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "zero test"; exit 0; fi\necho "zero ok"\n',
        )
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.tmp + os.pathsep + old_path
        os.environ["MYOS_AGENT_EXEC_ZERO"] = script
        try:
            backend = get_backend("zero")
            self.assertIsInstance(backend, ZeroBackend)
            ok, detail = backend.available()
            self.assertTrue(ok, detail)
            self.assertIn("zero test", detail)
            self.assertIn("zero", {row["name"] for row in available_backends()})
            self.assertEqual(backend.executor_argv("fix tests"), [script, "fix tests"])
        finally:
            os.environ["PATH"] = old_path
            os.environ.pop("MYOS_AGENT_EXEC_ZERO", None)


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
            Path(self.repo) / "fake_agent.sh",
            '#!/bin/sh\necho "added by harnessed agent" > harnessed.txt\n',
        )
        os.environ["MYOS_AGENT_CMD_COMMAND"] = exec_script
        try:
            result = assistant.delegate_to_agent(self.conn, "command", "add a file", cwd=self.repo)
        finally:
            os.environ.pop("MYOS_AGENT_CMD_COMMAND", None)

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

    def test_delegate_zero_backend_proposes_patch_without_mutating_real_tree(self):
        from personal_assistant import assistant

        exec_script = _write_script(
            Path(self.repo) / "fake_zero_exec.sh",
            '#!/bin/sh\necho "added by zero" > zeroed.txt\n',
        )
        os.environ["MYOS_AGENT_EXEC_ZERO"] = exec_script
        try:
            result = assistant.delegate_to_agent(self.conn, "zero", "add a zero file", cwd=self.repo)
        finally:
            os.environ.pop("MYOS_AGENT_EXEC_ZERO", None)

        self.assertNotIn("error", result, msg=result.get("error", ""))
        self.assertEqual(len(result["proposed_action_ids"]), 1)
        self.assertIn("zeroed.txt", result["diff"])
        self.assertFalse(Path(self.repo, "zeroed.txt").exists())
        row = self.conn.execute(
            "SELECT action_type, requires_approval, payload_json FROM agent_actions WHERE id = ?",
            (result["proposed_action_ids"][0],),
        ).fetchone()
        self.assertEqual(row["action_type"], "apply_patch")
        self.assertEqual(row["requires_approval"], 1)
        self.assertIn('"agent": "zero"', row["payload_json"])

    def test_delegate_requires_git_repo(self):
        from personal_assistant import assistant

        non_git = tempfile.mkdtemp()
        os.environ["MYOS_AGENT_CMD_COMMAND"] = "/bin/true"
        try:
            result = assistant.delegate_to_agent(self.conn, "command", "task", cwd=non_git)
        finally:
            os.environ.pop("MYOS_AGENT_CMD_COMMAND", None)
        self.assertIn("error", result)
        self.assertIn("git repo", result["error"])


class ZeroExecutorStreamTest(unittest.TestCase):
    def test_parse_zero_stream_collects_terminal_metadata(self):
        from personal_assistant import zero_executor

        stream = "\n".join(
            [
                '{"schemaVersion":2,"type":"run_start","runId":"run_1","sessionId":"s1","cwd":"/repo","provider":"openai","model":"gpt","apiModel":"gpt"}',
                '{"schemaVersion":2,"type":"tool_result","runId":"run_1","id":"call_1","name":"write_file","status":"ok","changedFiles":["app.py"],"truncated":false}',
                '{"schemaVersion":2,"type":"permission_request","runId":"run_1","id":"call_2","name":"bash","action":"prompt","permission":"prompt","sideEffect":"shell","reason":"verify"}',
                '{"schemaVersion":2,"type":"usage","runId":"run_1","promptTokens":10,"completionTokens":5,"totalTokens":15}',
                '{"schemaVersion":2,"type":"final","runId":"run_1","text":"done"}',
                '{"schemaVersion":2,"type":"run_end","runId":"run_1","status":"success","exitCode":0}',
            ]
        )

        result = zero_executor.parse_zero_stream(stream, exit_code=0)
        self.assertTrue(result.terminal_ok())
        self.assertEqual(result.run_id, "run_1")
        self.assertEqual(result.session_id, "s1")
        self.assertEqual(result.changed_files, ["app.py"])
        self.assertEqual(result.final_text, "done")
        self.assertEqual(result.usage["totalTokens"], 15)
        self.assertEqual(len(result.permission_events), 1)

    def test_parse_zero_stream_rejects_unknown_schema(self):
        from personal_assistant import zero_executor

        result = zero_executor.parse_zero_stream(
            '{"schemaVersion":99,"type":"run_end","runId":"run_1","status":"success","exitCode":0}\n',
            exit_code=0,
        )
        self.assertEqual(result.status, "protocol_error")
        self.assertTrue(result.protocol_errors)


class FactoryZeroExecutorTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_factory_start_persists_zero_executor_metadata(self):
        from personal_assistant import factory, intents

        intent_id = intents.create_intent(self.conn, objective="Fix repo tests")
        result = factory.start_review_first_run(
            self.conn,
            intent_id=intent_id,
            workflow_pack="software_delivery",
            executor_backend="zero",
            executor_context={"repo": "/tmp/repo", "timeout": 123},
        )
        self.conn.commit()

        self.assertEqual(result["executor_backend"], "zero")
        row = self.conn.execute("SELECT executor_backend, executor_context_json FROM factory_runs WHERE id = ?", (result["id"],)).fetchone()
        self.assertEqual(row["executor_backend"], "zero")
        self.assertIn("/tmp/repo", row["executor_context_json"])


if __name__ == "__main__":
    unittest.main()

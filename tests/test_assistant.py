"""Tests for the provider-agnostic assistant: propose-and-approve safety,
the harness/delegation path, and the CLI-backend JSON contract.

None of these touch the network: the Claude tool-loop is exercised with a fake
Anthropic client, and the external agent CLIs are stubbed with shell scripts.
"""

from __future__ import annotations

import json
import os
import shlex
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

    def test_approval_integrity_binds_hash_and_timestamp(self):
        from personal_assistant import agentcore
        from personal_assistant.execution import approve_and_execute, _compute_payload_hash

        task_id = agentcore.ensure_turn_task(self.conn, "integrity")
        aid = agentcore.enqueue_proposal(
            self.conn, task_id=task_id, action_type="create_inbox_item",
            title="capture note", payload={"text": "note body", "kind": "task"},
            requires_approval=1,
        )
        self.conn.commit()
        pre = self.conn.execute(
            "SELECT payload_hash, approved_at, payload_json FROM agent_actions WHERE id = ?",
            (aid,),
        ).fetchone()
        self.assertIsNone(pre["payload_hash"])
        self.assertIsNone(pre["approved_at"])
        expected_hash = _compute_payload_hash(pre["payload_json"])

        result = approve_and_execute(self.conn, aid, do_approve=True, execute=True)
        self.assertEqual(result["code"], "executed")

        post = self.conn.execute(
            "SELECT payload_hash, approved_at FROM agent_actions WHERE id = ?",
            (aid,),
        ).fetchone()
        self.assertEqual(post["payload_hash"], expected_hash)
        self.assertIsNotNone(post["approved_at"])

        receipt = self.conn.execute(
            "SELECT request_json FROM action_execution_receipts WHERE agent_action_id = ?",
            (aid,),
        ).fetchone()
        request = json.loads(receipt["request_json"])
        integrity = request["approval_integrity"]
        self.assertEqual(integrity["schema"], "myos.approval_integrity.v1")
        self.assertTrue(integrity["ok"])
        self.assertTrue(integrity["payload_hash_verified"])
        self.assertIn("approved_age_seconds", integrity)
        self.assertIn("approval_ttl_seconds", integrity)

    def test_approval_integrity_refuses_tampered_payload(self):
        from personal_assistant import agentcore
        from personal_assistant.execution import approve_and_execute

        task_id = agentcore.ensure_turn_task(self.conn, "tamper")
        aid = agentcore.enqueue_proposal(
            self.conn, task_id=task_id, action_type="create_inbox_item",
            title="original", payload={"text": "original body", "kind": "task"},
            requires_approval=1,
        )
        self.conn.commit()
        # Approve first, then simulate tampering between approval and execution.
        approved_only = approve_and_execute(self.conn, aid, do_approve=True, execute=False)
        self.assertEqual(approved_only["code"], "approved_only")
        self.conn.execute(
            "UPDATE agent_actions SET payload_json = ? WHERE id = ?",
            (json.dumps({"text": "tampered body", "kind": "task"}), aid),
        )
        self.conn.commit()

        result = approve_and_execute(self.conn, aid, do_approve=False, execute=True)
        self.assertEqual(result["code"], "failed")
        self.assertIn("payload_hash_mismatch", result["result"])
        # No inbox item created — execution never ran.
        inbox_count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM inbox_items WHERE text = 'tampered body'"
        ).fetchone()["c"]
        self.assertEqual(inbox_count, 0)
        receipt = self.conn.execute(
            "SELECT request_json FROM action_execution_receipts WHERE agent_action_id = ?",
            (aid,),
        ).fetchone()
        integrity = json.loads(receipt["request_json"])["approval_integrity"]
        self.assertFalse(integrity["ok"])
        self.assertEqual(integrity["reason"], "payload_hash_mismatch")

    def test_approval_integrity_refuses_stale_approval(self):
        from personal_assistant import agentcore
        from personal_assistant.execution import approve_and_execute

        task_id = agentcore.ensure_turn_task(self.conn, "stale")
        aid = agentcore.enqueue_proposal(
            self.conn, task_id=task_id, action_type="create_inbox_item",
            title="ttl gated", payload={"text": "stale body", "kind": "task"},
            requires_approval=1,
        )
        self.conn.commit()
        approved_only = approve_and_execute(self.conn, aid, do_approve=True, execute=False)
        self.assertEqual(approved_only["code"], "approved_only")
        # Backdate the approval well beyond the default 24h TTL.
        self.conn.execute(
            "UPDATE agent_actions SET approved_at = '2020-01-01 00:00:00' WHERE id = ?",
            (aid,),
        )
        self.conn.commit()

        result = approve_and_execute(self.conn, aid, do_approve=False, execute=True)
        self.assertEqual(result["code"], "failed")
        self.assertIn("approval_ttl_exceeded", result["result"])
        receipt = self.conn.execute(
            "SELECT request_json FROM action_execution_receipts WHERE agent_action_id = ?",
            (aid,),
        ).fetchone()
        integrity = json.loads(receipt["request_json"])["approval_integrity"]
        self.assertFalse(integrity["ok"])
        self.assertEqual(integrity["reason"], "approval_ttl_exceeded")

    def _approval_row_for(self, aid: int):
        # Load a row with the same column shape cmd_approve --list would see,
        # so the integrity summary picks up payload_hash and approved_at.
        return self.conn.execute(
            """
            SELECT id, agent_task_id, action_type, title, status, payload_json,
                   requires_approval, created_at, payload_hash, approved_at
            FROM agent_actions WHERE id = ?
            """,
            (aid,),
        ).fetchone()

    def test_approval_integrity_summary_proposed_shows_not_yet_approved(self):
        from personal_assistant import agentcore
        from personal_assistant.cli_agent import _approval_integrity_summary

        task_id = agentcore.ensure_turn_task(self.conn, "queue-vis")
        aid = agentcore.enqueue_proposal(
            self.conn, task_id=task_id, action_type="create_inbox_item",
            title="proposed only", payload={"text": "queued", "kind": "task"},
            requires_approval=1,
        )
        self.conn.commit()
        summary = _approval_integrity_summary(self._approval_row_for(aid))
        self.assertEqual(summary["state"], "not_yet_approved")
        # Not-yet-approved rows must not carry TTL or age fields — nothing to
        # verify until an approval hash is pinned.
        self.assertNotIn("approved_age_seconds", summary)
        self.assertNotIn("approval_ttl_seconds", summary)

    def test_approval_integrity_summary_approved_shows_fresh(self):
        from personal_assistant import agentcore
        from personal_assistant.cli_agent import _approval_integrity_summary
        from personal_assistant.execution import approve_and_execute

        task_id = agentcore.ensure_turn_task(self.conn, "queue-vis-fresh")
        aid = agentcore.enqueue_proposal(
            self.conn, task_id=task_id, action_type="create_inbox_item",
            title="fresh approved", payload={"text": "will exec", "kind": "task"},
            requires_approval=1,
        )
        self.conn.commit()
        approved = approve_and_execute(self.conn, aid, do_approve=True, execute=False)
        self.assertEqual(approved["code"], "approved_only")
        summary = _approval_integrity_summary(self._approval_row_for(aid))
        self.assertEqual(summary["state"], "fresh")
        self.assertIn("payload_hash", summary)
        self.assertIn("approved_at", summary)
        self.assertGreaterEqual(int(summary["ttl_remaining_seconds"]), 0)
        # Approval hash must be a 64-char hex sha256; anything shorter means
        # we accidentally exposed a partial/mangled value to consumers.
        self.assertEqual(len(summary["payload_hash"]), 64)

    def test_approval_integrity_summary_expired_shows_expired(self):
        from personal_assistant import agentcore
        from personal_assistant.cli_agent import _approval_integrity_summary
        from personal_assistant.execution import approve_and_execute

        task_id = agentcore.ensure_turn_task(self.conn, "queue-vis-expired")
        aid = agentcore.enqueue_proposal(
            self.conn, task_id=task_id, action_type="create_inbox_item",
            title="expired approved", payload={"text": "stale", "kind": "task"},
            requires_approval=1,
        )
        self.conn.commit()
        approve_and_execute(self.conn, aid, do_approve=True, execute=False)
        self.conn.execute(
            "UPDATE agent_actions SET approved_at = '2020-01-01 00:00:00' WHERE id = ?",
            (aid,),
        )
        self.conn.commit()
        summary = _approval_integrity_summary(self._approval_row_for(aid))
        self.assertEqual(summary["state"], "expired")
        self.assertEqual(summary["reason"], "approval_ttl_exceeded")

    def test_approval_integrity_summary_tampered_shows_tampered(self):
        from personal_assistant import agentcore
        from personal_assistant.cli_agent import _approval_integrity_summary
        from personal_assistant.execution import approve_and_execute

        task_id = agentcore.ensure_turn_task(self.conn, "queue-vis-tampered")
        aid = agentcore.enqueue_proposal(
            self.conn, task_id=task_id, action_type="create_inbox_item",
            title="tampered approved", payload={"text": "original body", "kind": "task"},
            requires_approval=1,
        )
        self.conn.commit()
        approve_and_execute(self.conn, aid, do_approve=True, execute=False)
        # Mutate the payload after approval — hash mismatch must surface as
        # `tampered` in the queue view so a supervisor sees it before the
        # execution refusal path kicks in.
        self.conn.execute(
            "UPDATE agent_actions SET payload_json = ? WHERE id = ?",
            (json.dumps({"text": "TAMPERED body", "kind": "task"}), aid),
        )
        self.conn.commit()
        summary = _approval_integrity_summary(self._approval_row_for(aid))
        self.assertEqual(summary["state"], "tampered")
        self.assertEqual(summary["reason"], "payload_hash_mismatch")

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
    def test_zero_stream_argv_uses_streaming_executor_override(self):
        from personal_assistant import zero_executor

        os.environ["MYOS_AGENT_EXEC_ZERO_STREAM"] = "/tmp/zero-wrapper --flag"
        try:
            argv = zero_executor.zero_stream_argv(cwd="/repo", max_turns=2)
        finally:
            os.environ.pop("MYOS_AGENT_EXEC_ZERO_STREAM", None)

        self.assertEqual(argv[:2], ["/tmp/zero-wrapper", "--flag"])
        self.assertIn("--input-format", argv)
        self.assertIn("stream-json", argv)
        self.assertIn("--max-turns", argv)
        self.assertIn("2", argv)

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

    def test_run_zero_stream_reports_missing_executable(self):
        from personal_assistant import zero_executor

        os.environ["MYOS_AGENT_EXEC_ZERO_STREAM"] = "/tmp/myos-definitely-missing-zero"
        try:
            result = zero_executor.run_zero_stream("do work", cwd=tempfile.gettempdir())
        finally:
            os.environ.pop("MYOS_AGENT_EXEC_ZERO_STREAM", None)

        self.assertEqual(result.status, "missing")
        self.assertEqual(result.exit_code, 127)
        self.assertEqual(result.errors[0]["code"], "missing_zero")

    def test_run_zero_stream_reports_timeout(self):
        from personal_assistant import zero_executor

        with tempfile.TemporaryDirectory() as tmp:
            script = _write_script(
                Path(tmp) / "sleepy_zero.py",
                "#!/usr/bin/env python3\n"
                "import time\n"
                "time.sleep(5)\n",
            )
            os.environ["MYOS_AGENT_EXEC_ZERO_STREAM"] = f"{sys.executable} {script}"
            try:
                result = zero_executor.run_zero_stream("do work", cwd=tmp, timeout=1)
            finally:
                os.environ.pop("MYOS_AGENT_EXEC_ZERO_STREAM", None)

        self.assertEqual(result.status, "timed_out")
        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, 124)


class FactoryZeroExecutorTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.repo = self.tmp / "repo with spaces"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "Test")
        self._git("config", "commit.gpgsign", "false")
        Path(self.repo, "README.md").write_text("seed\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "init")

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)
        os.environ.pop("MYOS_AGENT_EXEC_ZERO_STREAM", None)
        self.tmpdir.cleanup()

    def _git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, check=True)

    def _install_fake_zero(
        self,
        *,
        changed_file: str = "zeroed.txt",
        text: str = "hello from zero\n",
        reported_changed_file: str | None = None,
    ) -> None:
        script = self.tmp / "fake_zero.py"
        script.write_text(
            "import json, os, pathlib\n"
            f"changed_file = {changed_file!r}\n"
            f"reported_changed_file = {reported_changed_file!r}\n"
            f"text = {text!r}\n"
            "if changed_file:\n"
            "    pathlib.Path(changed_file).parent.mkdir(parents=True, exist_ok=True)\n"
            "    pathlib.Path(changed_file).write_text(text)\n"
            "reported = reported_changed_file if reported_changed_file is not None else changed_file\n"
            "events = [\n"
            "    {'schemaVersion': 2, 'type': 'run_start', 'runId': 'run_fake', 'sessionId': 'session_fake', 'cwd': os.getcwd(), 'provider': 'fake', 'model': 'fake'},\n"
            "    {'schemaVersion': 2, 'type': 'permission_request', 'runId': 'run_fake', 'id': 'perm_1', 'name': 'bash', 'permission': 'prompt', 'sideEffect': 'shell', 'reason': 'verify'},\n"
            "    {'schemaVersion': 2, 'type': 'tool_result', 'runId': 'run_fake', 'id': 'tool_1', 'name': 'write_file', 'status': 'ok', 'changedFiles': [reported] if reported else []},\n"
            "    {'schemaVersion': 2, 'type': 'warning', 'runId': 'run_fake', 'message': 'verification skipped with token ' + 'ghp_' + 'abcdefghijklmnop'},\n"
            "    {'schemaVersion': 2, 'type': 'usage', 'runId': 'run_fake', 'totalTokens': 3},\n"
            "    {'schemaVersion': 2, 'type': 'final', 'runId': 'run_fake', 'text': 'fake zero finished'},\n"
            "    {'schemaVersion': 2, 'type': 'run_end', 'runId': 'run_fake', 'status': 'success', 'exitCode': 0},\n"
            "]\n"
            "for event in events:\n"
            "    print(json.dumps(event), flush=True)\n"
        )
        os.environ["MYOS_AGENT_EXEC_ZERO_STREAM"] = f"{sys.executable} {script}"

    def _install_hanging_zero(self) -> None:
        """A fake Zero that never returns — used to prove the wall-clock
        timeout guard trips, marks `timed_out=True` in the artifact, and
        still cleans up the worktree."""
        script = self.tmp / "hanging_zero.py"
        script.write_text(
            "import sys, time\n"
            "sys.stderr.write('starting long run\\n')\n"
            "sys.stderr.flush()\n"
            "time.sleep(30)\n"
        )
        os.environ["MYOS_AGENT_EXEC_ZERO_STREAM"] = f"{sys.executable} {script}"

    def test_factory_zero_proof_loop_reaches_receipt_and_learning(self):
        from personal_assistant import factory, intents, plans
        from personal_assistant.execution import approve_and_execute

        self._install_fake_zero()
        intent_id = intents.create_intent(
            self.conn,
            objective="Use Zero to add a proof file",
            success_criteria="Patch is reviewable before approval",
        )
        factory.set_policy(self.conn, allowed_mode="semi_autonomous", scope_type="intent", scope_id=str(intent_id))

        result = factory.start_review_first_run(
            self.conn,
            intent_id=intent_id,
            mode="semi_autonomous",
            workflow_pack="software_delivery",
            executor_backend="zero",
            executor_context={
                "repo": str(self.repo),
                "timeout": 30,
                "max_turns": 1,
                "verification_commands": ["python -m pytest"],
            },
        )
        self.conn.commit()

        self.assertEqual(result["status"], "execution_ready")
        self.assertEqual(result["executor_backend"], "zero")
        self.assertEqual(len(result["proposed_action_ids"]), 1)
        action_id = result["proposed_action_ids"][0]
        self.assertFalse(Path(self.repo, "zeroed.txt").exists())

        row = self.conn.execute("SELECT executor_backend, executor_context_json FROM factory_runs WHERE id = ?", (result["id"],)).fetchone()
        self.assertEqual(row["executor_backend"], "zero")
        self.assertIn(str(self.repo), row["executor_context_json"])

        action = self.conn.execute(
            "SELECT action_type, requires_approval, status, payload_json FROM agent_actions WHERE id = ?",
            (action_id,),
        ).fetchone()
        self.assertEqual(action["action_type"], "apply_patch")
        self.assertEqual(action["requires_approval"], 1)
        self.assertEqual(action["status"], "proposed")
        self.assertIn("zeroed.txt", action["payload_json"])
        action_payload = json.loads(action["payload_json"])
        self.assertEqual(action_payload["zero"]["schema"], "myos.zero_executor.action_metadata.v1")
        self.assertNotIn("cwd", action_payload["zero"])
        self.assertNotIn("permission_events", action_payload["zero"])
        self.assertEqual(action_payload["zero"]["permission_events_count"], 1)
        self.assertEqual(action_payload["zero"]["warnings"], ["verification skipped with token [REDACTED_SECRET]"])
        self.assertNotIn("ghp_" + "abcdefghijklmnop", json.dumps(action_payload, sort_keys=True))

        packet = plans.get_review_packet(self.conn, result["review_packet_id"])
        artifacts = packet["packet"]["executor_artifacts"]
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["type"], "zero_executor")
        self.assertEqual(artifacts[0]["agent_action_id"], action_id)
        self.assertEqual(artifacts[0]["changed_files"], ["zeroed.txt"])
        self.assertEqual(artifacts[0]["diff_stats"], {"files": 1, "additions": 1, "deletions": 0, "binary_files": 0})
        self.assertEqual(artifacts[0]["run_id"], "run_fake")
        self.assertEqual(artifacts[0]["session_id"], "session_fake")
        self.assertTrue(artifacts[0]["executor_isolated_worktree"])
        self.assertFalse(artifacts[0]["executor_worktree_retained"])
        self.assertNotIn("executor_cwd", artifacts[0])
        self.assertEqual(artifacts[0]["permission_events_count"], 1)
        self.assertNotIn("permission_events", artifacts[0])
        self.assertEqual(artifacts[0]["warnings"], ["verification skipped with token [REDACTED_SECRET]"])
        self.assertNotIn("ghp_" + "abcdefghijklmnop", json.dumps(artifacts[0], sort_keys=True))
        self.assertEqual(artifacts[0]["verification_commands"], ["python -m pytest"])
        self.assertIn("myos factory start --intent", artifacts[0]["retry_command"])
        self.assertIn("--executor zero", artifacts[0]["retry_command"])
        self.assertIn("--verify-command 'python -m pytest'", artifacts[0]["retry_command"])
        retry_argv = shlex.split(artifacts[0]["retry_command"])
        self.assertEqual(retry_argv[retry_argv.index("--repo") + 1].split("/")[-1], "repo with spaces")
        self.assertEqual(artifacts[0]["approval_command"], f"myos approve --action {action_id} --execute")

        approved = approve_and_execute(self.conn, action_id, do_approve=True, execute=True)
        self.assertEqual(approved["status"], "executed")
        self.assertEqual(Path(self.repo, "zeroed.txt").read_text(), "hello from zero\n")

        receipt_count = self.conn.execute("SELECT COUNT(*) AS c FROM action_execution_receipts").fetchone()["c"]
        self.assertEqual(receipt_count, 1)
        receipt = self.conn.execute(
            "SELECT request_json FROM action_execution_receipts WHERE agent_action_id = ?",
            (action_id,),
        ).fetchone()
        receipt_request = json.loads(receipt["request_json"])
        self.assertEqual(receipt_request["verification"]["schema"], "myos.verification_receipt.v1")
        self.assertEqual(receipt_request["verification"]["status"], "not_run")
        self.assertEqual(receipt_request["verification"]["commands"], ["python -m pytest"])
        self.assertIn("does not auto-run", receipt_request["verification"]["reason"])
        self.assertEqual(receipt_request["approval_integrity"]["schema"], "myos.approval_integrity.v1")
        self.assertTrue(receipt_request["approval_integrity"]["ok"])
        self.assertTrue(receipt_request["approval_integrity"]["payload_hash_verified"])
        learning_id = factory.learn(self.conn, factory_run_id=result["id"], outcome="success", notes="Fake Zero patch applied.")
        retro = factory.latest_retrospective(self.conn, result["id"])
        self.assertGreaterEqual(learning_id, 1)
        self.assertEqual(retro["outcome"], "success")
        self.assertEqual(retro["retrospective"]["recent_receipts"][0]["final_status"], "executed")

    def test_factory_zero_changed_files_come_from_git_diff(self):
        from personal_assistant import factory, intents, plans

        self._install_fake_zero(changed_file="actual.txt", text="actual\n", reported_changed_file="reported.txt")
        intent_id = intents.create_intent(self.conn, objective="Use Zero with mismatched changedFiles")
        result = factory.start_review_first_run(
            self.conn,
            intent_id=intent_id,
            workflow_pack="software_delivery",
            executor_backend="zero",
            executor_context={"repo": str(self.repo), "timeout": 30},
        )
        self.conn.commit()

        action_id = result["proposed_action_ids"][0]
        action = self.conn.execute("SELECT payload_json FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
        payload = json.loads(action["payload_json"])
        self.assertEqual(payload["changed_files"], ["actual.txt"])
        self.assertEqual(payload["zero"]["changed_files"], ["actual.txt"])
        self.assertNotIn("reported.txt", payload["changed_files"])

        packet = plans.get_review_packet(self.conn, result["review_packet_id"])
        artifact = packet["packet"]["executor_artifacts"][0]
        self.assertEqual(artifact["changed_files"], ["actual.txt"])
        self.assertNotIn("reported.txt", artifact["changed_files"])

    def test_factory_review_first_zero_prepares_action_before_approval(self):
        from personal_assistant import factory, intents

        self._install_fake_zero(changed_file="review-first.txt", text="review first\n")
        intent_id = intents.create_intent(self.conn, objective="Use Zero in review-first mode")
        result = factory.start_review_first_run(
            self.conn,
            intent_id=intent_id,
            workflow_pack="software_delivery",
            executor_backend="zero",
            executor_context={"repo": str(self.repo), "timeout": 30},
        )
        self.conn.commit()

        self.assertEqual(result["status"], "awaiting_approval")
        self.assertEqual(len(result["proposed_action_ids"]), 1)
        self.assertFalse(Path(self.repo, "review-first.txt").exists())
        stages = {
            row["stage_name"]: json.loads(row["output_json"] or "{}")
            for row in self.conn.execute(
                "SELECT stage_name, output_json FROM factory_stages WHERE factory_run_id = ?",
                (result["id"],),
            )
        }
        self.assertEqual(stages["execution"]["prepared_action_ids"], result["proposed_action_ids"])

    def test_factory_zero_empty_diff_becomes_review_action(self):
        from personal_assistant import factory, intents

        self._install_fake_zero(changed_file="", text="")
        intent_id = intents.create_intent(self.conn, objective="Ask Zero for advice only")
        result = factory.start_review_first_run(
            self.conn,
            intent_id=intent_id,
            workflow_pack="software_delivery",
            executor_backend="zero",
            executor_context={"repo": str(self.repo), "timeout": 30},
        )
        self.conn.commit()

        action_id = result["proposed_action_ids"][0]
        row = self.conn.execute("SELECT action_type, payload_json FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
        self.assertEqual(row["action_type"], "draft_external_update")
        self.assertIn("fake zero finished", row["payload_json"])

    def test_factory_zero_oversized_diff_becomes_review_action(self):
        from personal_assistant import factory, intents, plans

        self._install_fake_zero(changed_file="large.txt", text="x" * 210000)
        intent_id = intents.create_intent(self.conn, objective="Ask Zero for a large patch")
        result = factory.start_review_first_run(
            self.conn,
            intent_id=intent_id,
            workflow_pack="software_delivery",
            executor_backend="zero",
            executor_context={"repo": str(self.repo), "timeout": 30},
        )
        self.conn.commit()

        action_id = result["proposed_action_ids"][0]
        action = self.conn.execute("SELECT action_type, payload_json FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
        payload = json.loads(action["payload_json"])
        self.assertEqual(action["action_type"], "draft_external_update")
        self.assertTrue(payload["diff_too_large"])
        self.assertNotIn("diff", payload)
        self.assertIn("above the MYOS approval patch limit", payload["draft"])

        packet = plans.get_review_packet(self.conn, result["review_packet_id"])
        artifact = packet["packet"]["executor_artifacts"][0]
        self.assertEqual(artifact["action_type"], "draft_external_update")
        self.assertTrue(artifact["diff_too_large"])
        self.assertGreater(artifact["diff_bytes"], artifact["diff_limit_bytes"])
        self.assertEqual(artifact["diff_stats"]["files"], 1)

    def test_factory_zero_failed_run_creates_follow_up_work(self):
        from personal_assistant import factory, intents, plans

        os.environ["MYOS_AGENT_EXEC_ZERO_STREAM"] = str(self.tmp / "missing-zero")
        intent_id = intents.create_intent(self.conn, objective="Use Zero when the executable is missing")
        result = factory.start_review_first_run(
            self.conn,
            intent_id=intent_id,
            workflow_pack="software_delivery",
            executor_backend="zero",
            executor_context={"repo": str(self.repo), "timeout": 30},
        )
        self.conn.commit()

        action_id = result["proposed_action_ids"][0]
        action = self.conn.execute("SELECT action_type, payload_json FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
        self.assertEqual(action["action_type"], "draft_external_update")
        self.assertIn('"status": "missing"', action["payload_json"])

        inbox = self.conn.execute(
            "SELECT id, text, source FROM inbox_items WHERE source = 'zero_executor' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(inbox)
        self.assertIn("Zero executor missing", inbox["text"])

        artifact_row = self.conn.execute(
            "SELECT artifact_type, artifact_id, label FROM factory_artifacts WHERE factory_run_id = ? AND artifact_type = 'inbox_item'",
            (result["id"],),
        ).fetchone()
        self.assertEqual(artifact_row["artifact_id"], inbox["id"])
        self.assertEqual(artifact_row["label"], "zero follow-up")

        packet = plans.get_review_packet(self.conn, result["review_packet_id"])
        zero_artifact = packet["packet"]["executor_artifacts"][0]
        self.assertEqual(zero_artifact["status"], "missing")
        self.assertEqual(zero_artifact["errors"][0]["code"], "missing_zero")
        self.assertEqual(zero_artifact["follow_up_inbox_id"], inbox["id"])

    def test_factory_zero_timeout_surfaces_timed_out_and_cleans_worktree(self):
        from personal_assistant import factory, intents, plans

        self._install_hanging_zero()
        intent_id = intents.create_intent(self.conn, objective="Trigger a Zero wall-clock timeout")
        result = factory.start_review_first_run(
            self.conn,
            intent_id=intent_id,
            workflow_pack="software_delivery",
            executor_backend="zero",
            executor_context={"repo": str(self.repo), "timeout": 1},
        )
        self.conn.commit()

        action_id = result["proposed_action_ids"][0]
        action = self.conn.execute(
            "SELECT action_type, payload_json FROM agent_actions WHERE id = ?",
            (action_id,),
        ).fetchone()
        payload = json.loads(action["payload_json"])
        self.assertEqual(action["action_type"], "draft_external_update")
        self.assertTrue(payload["zero"]["timed_out"])
        self.assertEqual(payload["zero"]["status"], "timed_out")

        packet = plans.get_review_packet(self.conn, result["review_packet_id"])
        artifact = packet["packet"]["executor_artifacts"][0]
        self.assertTrue(artifact["timed_out"])
        self.assertEqual(artifact["timeout_seconds"], 1)
        self.assertEqual(artifact["status"], "timed_out")
        self.assertFalse(artifact["executor_worktree_retained"])

        # Every temp worktree under our tmp dir was cleaned up on the finally
        # path, regardless of the timeout.
        stray = [
            path.name
            for path in self.tmp.iterdir()
            if path.is_dir() and path.name.startswith("myos-zero-wt-")
        ]
        self.assertEqual(stray, [])

    def test_factory_zero_timeout_honors_env_override(self):
        from personal_assistant import factory, intents

        self._install_hanging_zero()
        intent_id = intents.create_intent(self.conn, objective="Global timeout via env")
        os.environ["MYOS_ZERO_TIMEOUT_SECONDS"] = "1"
        try:
            result = factory.start_review_first_run(
                self.conn,
                intent_id=intent_id,
                workflow_pack="software_delivery",
                executor_backend="zero",
                # No explicit `timeout` in the context — the env override wins.
                executor_context={"repo": str(self.repo)},
            )
        finally:
            os.environ.pop("MYOS_ZERO_TIMEOUT_SECONDS", None)
        self.conn.commit()

        action_id = result["proposed_action_ids"][0]
        payload = json.loads(
            self.conn.execute(
                "SELECT payload_json FROM agent_actions WHERE id = ?", (action_id,)
            ).fetchone()["payload_json"]
        )
        self.assertTrue(payload["zero"]["timed_out"])

    def test_zero_apply_patch_guard_blocks_protected_paths(self):
        from personal_assistant import agentcore
        from personal_assistant.execution import approve_and_execute

        task_id = agentcore.ensure_turn_task(self.conn, "unsafe zero patch")
        diff = (
            "diff --git a/.claude/settings.json b/.claude/settings.json\n"
            "new file mode 100644\n"
            "index 0000000..e69de29\n"
            "--- /dev/null\n"
            "+++ b/.claude/settings.json\n"
            "@@ -0,0 +1 @@\n"
            "+{}\n"
        )
        action_id = agentcore.enqueue_proposal(
            self.conn,
            task_id=task_id,
            action_type="apply_patch",
            title="Apply unsafe Zero patch",
            payload={"agent": "zero", "repo_root": str(self.repo), "diff": diff},
        )
        self.conn.commit()

        result = approve_and_execute(self.conn, action_id, do_approve=True, execute=True)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("protected", result["result"])
        receipt = self.conn.execute(
            "SELECT final_status, follow_up_required FROM action_execution_receipts WHERE agent_action_id = ?",
            (action_id,),
        ).fetchone()
        self.assertEqual(receipt["final_status"], "blocked")
        self.assertEqual(receipt["follow_up_required"], 1)


if __name__ == "__main__":
    unittest.main()

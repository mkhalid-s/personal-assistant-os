from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliFlowTest(unittest.TestCase):
    def test_ci_release_readiness_smokes_installed_command(self) -> None:
        workflow = Path(".github/workflows/ci.yml").read_text()
        self.assertIn("Smoke installed myos command", workflow)
        self.assertIn("myos --help >/dev/null", workflow)

    def test_ci_hygiene_scans_new_commit_messages_only(self) -> None:
        workflow = Path(".github/workflows/ci.yml").read_text()
        self.assertIn('range="${{ github.event.before }}..${{ github.sha }}"', workflow)
        self.assertIn('git log --format=%B "$range"', workflow)
        self.assertNotIn("git log --format=%B | grep", workflow)

    def test_ci_release_readiness_uses_installed_command_path(self) -> None:
        workflow = Path(".github/workflows/ci.yml").read_text()
        release_job = workflow.split("  release-readiness:", 1)[1]
        self.assertLess(
            release_job.index("Smoke installed myos command"), release_job.index("Build wheel artifact smoke")
        )
        self.assertLess(
            release_job.index("Build wheel artifact smoke"), release_job.index("Run release readiness gate")
        )
        self.assertIn("run: myos release-check --strict", release_job)
        self.assertNotIn("PYTHONPATH: src", release_job)

    def test_ci_release_readiness_builds_wheel_artifact(self) -> None:
        workflow = Path(".github/workflows/ci.yml").read_text()
        self.assertIn("Build wheel artifact smoke", workflow)
        self.assertIn("python -m pip wheel --no-deps . -w dist/wheel-smoke", workflow)
        self.assertIn("wheel build produced no wheel", workflow)
        self.assertIn("myos release-check --strict", workflow)

    def test_release_workflow_aligns_with_ci_packaging_gates(self) -> None:
        workflow = Path(".github/workflows/release.yml").read_text()
        self.assertIn("Smoke installed myos command", workflow)
        self.assertIn("myos --help >/dev/null", workflow)
        self.assertIn("python -m pip wheel --no-deps . -w dist/wheel-smoke", workflow)
        self.assertIn("myos dependency-check --strict", workflow)
        self.assertIn("myos doctor --strict", workflow)
        self.assertIn("myos migrations verify --strict", workflow)
        self.assertIn("myos release-check --strict", workflow)
        self.assertNotIn("PYTHONPATH: src", workflow)
        self.assertNotIn("twine upload", workflow)

    def test_standalone_binary_packaging_remains_explicit_decision(self) -> None:
        readme = Path("README.md").read_text()
        self.assertIn("packaged as a Python console application", readme)
        self.assertIn("not a standalone signed binary", readme)
        self.assertIn("Standalone executable packaging can be layered later", readme)

    def test_zero_proof_runbook_is_discoverable_and_approval_gated(self) -> None:
        readme = Path("README.md").read_text()
        runbook = Path("examples/demo-zero-proof.md").read_text()
        bounded = Path("docs/BOUNDED_AUTONOMY.md").read_text()

        self.assertIn("examples/demo-zero-proof.md", readme)
        self.assertIn("examples/demo-zero-proof.md", bounded)
        self.assertIn("myos factory start", runbook)
        self.assertIn("--executor zero", runbook)
        self.assertIn("myos approve --action <action_id> --execute", runbook)
        self.assertIn("Do not commit, push, open PRs, or mutate external systems", runbook)
        self.assertIn("MYOS approval is still required", runbook)
        self.assertIn('"examples"', Path("src/personal_assistant/cli_health.py").read_text())

    def test_router_commands_exposes_model_safe_metadata(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        out = subprocess.run(
            [
                sys.executable,
                "-m",
                "personal_assistant.cli",
                "router",
                "commands",
                "--safety",
                "approval_gated",
                "--limit",
                "10",
            ],
            cwd=Path.cwd(),
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("Router command registry:", out)
        self.assertIn("myos autopilot", out)
        self.assertIn("side_effects=local_db_write,long_running", out)
        self.assertIn("long_running=yes", out)
        self.assertIn("subcommands=--once,--factory,--loop-goal", out)
        self.assertNotIn("raw_text", out)

    def test_code_command_and_factory_executor_help(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        code_help = subprocess.run(
            [sys.executable, "-m", "personal_assistant.cli", "code", "--help"],
            cwd=Path.cwd(),
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("--backend", code_help)
        self.assertIn("--repo", code_help)
        self.assertIn("zero", code_help)

        factory_help = subprocess.run(
            [sys.executable, "-m", "personal_assistant.cli", "factory", "start", "--help"],
            cwd=Path.cwd(),
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("--executor", factory_help)
        self.assertIn("--repo", factory_help)
        self.assertIn("--verify-command", factory_help)

    def test_release_check_validates_console_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            out = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "release-check", "--strict"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("PASS package_entrypoint: myos -> personal_assistant.cli:main", out)
            self.assertIn("PASS command_contract:", out)
            self.assertIn("commands covered", out)

    def test_release_check_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            out = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "release-check", "--strict", "--json"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            # The command must emit exactly one JSON object.
            payload = json.loads(out)
            self.assertEqual(payload["schema"], "myos.release_check.v1")
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["strict"])
            names = {check["name"] for check in payload["checks"]}
            self.assertIn("schema", names)
            self.assertIn("command_contract", names)
            self.assertIn("factory_smoke", names)
            self.assertTrue(all(check["ok"] for check in payload["checks"]))

    def test_doctor_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            out = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "doctor", "--json"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            payload = json.loads(out)
            self.assertEqual(payload["schema"], "myos.doctor.v1")
            self.assertIn("ok", payload)
            self.assertIn("counts", payload)
            self.assertIn("core_checks", payload)
            self.assertIn("optional_checks", payload)
            self.assertIn("autonomy_level", payload)
            core_names = {check["name"] for check in payload["core_checks"]}
            self.assertIn("db_connection", core_names)
            self.assertIn("schema_migrations", core_names)
            optional_names = {check["name"] for check in payload["optional_checks"]}
            self.assertIn("zero_stream_executor", optional_names)

    def test_capture_triage_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)
            provider_script = Path(tmp) / "role_provider.py"
            provider_script.write_text(
                "import json, sys\n"
                "req=json.loads(sys.stdin.read() or '{}')\n"
                "print(json.dumps({'reply': 'role ok ' + req.get('purpose', ''), 'plan': [{'step': 'check', 'detail': 'ok'}], 'actions': []}))\n"
            )
            env["MYOS_FACTORY_ROLE_BACKEND"] = "command"
            env["MYOS_AI_COMMAND"] = f"{sys.executable} {provider_script}"

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("capture", "Decision: rollout canary first")
            run("triage")
            out = run("context", "canary rollout")
            self.assertIn("Context results", out)

            conn = sqlite3.connect(db_path)
            count = conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
            conn.close()
            self.assertEqual(count, 1)

    def test_smart_do_and_tiered_help(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            help_out = run("help", "daily")
            self.assertIn("Primary: myos chat | myos voice | myos autopilot --factory | myos do", help_out)
            self.assertIn("myos do", help_out)
            self.assertIn("Daily commands", help_out)

            captured = run("do", "remember to follow up with platform")
            self.assertIn("Autonomy: decision=allowed", captured)
            self.assertIn("safety=local_write", captured)
            self.assertIn("Recommendation: Proceed with the local routed workflow", captured)
            self.assertIn("Smart route: capture", captured)
            self.assertIn("Inbox item: #", captured)

            planned = run("do", "plan this launch checklist")
            self.assertIn("Autonomy: decision=allowed", planned)
            self.assertIn("Smart route: plan_intent", planned)
            self.assertIn("Intent: #", planned)
            self.assertIn("Plan: #", planned)

            conn = sqlite3.connect(db_path)
            inbox_count = conn.execute("SELECT COUNT(*) FROM inbox_items WHERE source='smart_do'").fetchone()[0]
            plan_count = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
            route_events = conn.execute("SELECT COUNT(*) FROM event_log WHERE event_type='smart_route'").fetchone()[0]
            traces = conn.execute(
                "SELECT COUNT(*) FROM execution_traces WHERE command_path='do' AND route_event_id IS NOT NULL"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(inbox_count, 1)
            self.assertEqual(plan_count, 1)
            self.assertEqual(route_events, 2)
            self.assertEqual(traces, 2)

            trace_out = run("trace", "list", "--command", "do")
            self.assertIn("Execution traces:", trace_out)
            self.assertIn("do status=completed", trace_out)
            self.assertIn("route_event=#", trace_out)

            empty_trace = run("trace", "list", "--command", "trace")
            self.assertNotIn("status=running", empty_trace)

            autonomy_eval = run("autonomy", "eval")
            self.assertIn("Autonomy eval:", autonomy_eval)
            self.assertIn("accuracy=100.00%", autonomy_eval)
            recommendation_help = run("autonomy", "recommendation-feedback", "--help")
            self.assertIn('[label=daily_reduce_risk command="myos next-action"]', recommendation_help)
            self.assertIn("Printed label, e.g. daily_reduce_risk.", recommendation_help)
            self.assertIn("myos next-action or myos now", recommendation_help)
            self.assertIn('--label review_approvals --command "myos approve --list"', recommendation_help)
            self.assertIn('--label run_goal_cycle --command "myos loop run-goal --goal 1"', recommendation_help)
            self.assertIn('--label review_goals --command "myos goal list"', recommendation_help)
            recommendations_help = run("autonomy", "recommendations", "--help")
            self.assertIn("recent_score_30d", recommendations_help)
            self.assertIn("mixed_recent", recommendations_help)
            self.assertIn("Command context is shown", recommendations_help)
            self.assertIn("raw notes, note_hash, and note_length are not shown", recommendations_help)
            self.assertIn("run_goal_cycle and review_goals", recommendations_help)
            self.assertIn("surface=goal_scheduler", recommendations_help)
            self.assertIn("active daily feedback visible", recommendations_help)
            conn = sqlite3.connect(db_path)
            trace_id = conn.execute(
                "SELECT id FROM execution_traces WHERE command_path='do' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            conn.close()
            feedback = run(
                "autonomy",
                "feedback",
                "--trace",
                str(trace_id),
                "--expected-decision",
                "allowed",
                "--note",
                "Expected allowed local route.",
            )
            self.assertIn("Autonomy feedback recorded:", feedback)
            self.assertIn("Privacy: note text was hashed", feedback)

    def test_model_setup_cli_and_doctor_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            recommend = run("model", "recommend", "--purpose", "router")
            self.assertIn("Recommended router model: qwen2.5:0.5b", recommend)

            setup = run("model", "setup", "--router", "--runtime", "command", "--command", "local-router")
            self.assertIn("Router model setup plan:", setup)
            self.assertIn("MYOS_ROUTER_COMMAND=local-router", setup)
            self.assertIn("Dry run only", setup)

            status = run("model", "status")
            self.assertIn("Router model status:", status)

            doctor = run("doctor")
            self.assertIn("router_model", doctor)
            self.assertIn("cursor:", doctor)
            self.assertIn("claude-code:", doctor)
            self.assertIn("claude-code-sdk:", doctor)

            live = run(
                "setup-live",
                "--router-model",
                "--router-runtime",
                "command",
                "--data-dir",
                str(Path(tmp) / "data"),
                "--env-file",
                str(Path(tmp) / "data" / ".env.myos"),
                "--db-path",
                str(Path(tmp) / "data" / "assistant.db"),
                "--watch-dir",
                str(Path(tmp) / "data" / "inbox"),
            )
            self.assertIn("Router model setup plan:", live)
            self.assertIn("Dry run only", live)

            live_data = Path(tmp) / "live-data"
            live_env = live_data / ".env.myos"
            live_db = live_data / "assistant.db"
            live_watch = live_data / "inbox"
            applied = run(
                "setup-live",
                "--apply",
                "--data-dir",
                str(live_data),
                "--env-file",
                str(live_env),
                "--db-path",
                str(live_db),
                "--watch-dir",
                str(live_watch),
            )
            self.assertIn("Setup complete.", applied)
            check = run(
                "setup-live",
                "--check",
                "--data-dir",
                str(live_data),
                "--env-file",
                str(live_env),
                "--db-path",
                str(live_db),
                "--watch-dir",
                str(live_watch),
            )
            self.assertIn("Readiness summary:", check)
            self.assertIn("INFO jira_credentials: missing", check)
            self.assertNotIn("WARN jira_credentials", check)
            self.assertIn("Ready: myos autopilot", check)

    def test_doctor_reports_zero_stream_executor_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            fake_zero = Path(tmp) / "zero"
            fake_zero.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "if '--help' in sys.argv:\n"
                "    print('usage: zero exec --input-format stream-json --output-format stream-json')\n"
                "    raise SystemExit(0)\n"
                "print('zero fake')\n"
            )
            fake_zero.chmod(0o755)
            env["PATH"] = f"{tmp}{os.pathsep}{env.get('PATH', '')}"
            env["MYOS_AGENT_EXEC_ZERO_STREAM"] = "zero exec"

            out = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "doctor"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout

            self.assertIn("PASS zero_stream_executor:", out)
            self.assertIn("stream-json support detected", out)
            self.assertIn("input/output format flags", out)

    def test_first_class_agent_backend_chat_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            agent = bin_dir / "agent"
            agent.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "if len(sys.argv) > 1 and sys.argv[1] == 'status':\n"
                "    print('Logged in as fake@example.com')\n"
                "    raise SystemExit(0)\n"
                "print('cursor reply: ' + sys.argv[-1].splitlines()[-1])\n",
                encoding="utf-8",
            )
            claude = bin_dir / "claude"
            claude.write_text(
                "#!/usr/bin/env python3\nimport sys\nprint('claude-code reply: ' + sys.argv[-1].splitlines()[-1])\n",
                encoding="utf-8",
            )
            os.chmod(agent, 0o755)
            os.chmod(claude, 0o755)
            env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"

            def run_chat(backend: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", "chat", "--backend", backend],
                    cwd=Path.cwd(),
                    env=env,
                    input="hello\nexit\n",
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                return out.stdout

            cursor = run_chat("cursor")
            self.assertIn("MYOS chat [cursor]", cursor)
            self.assertIn("cursor reply:", cursor)

            claude_code = run_chat("claude-code")
            self.assertIn("MYOS chat [claude-code]", claude_code)
            self.assertIn("claude-code reply:", claude_code)

    def test_durable_autonomy_loop_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            started = run("loop", "start", "Handle Jira risk and follow up")
            self.assertIn("Autonomy loop task #1", started)
            self.assertIn("status=waiting_approval", started)
            self.assertIn("pending_approvals=", started)
            self.assertIn("Recommendation: review pending approvals -> myos approve --list", started)
            self.assertIn("[label=review_approvals]", started)

            status = run("loop", "status", "--task", "1")
            self.assertIn("Autonomy loop tasks:", status)
            self.assertIn("task #1 status=waiting_approval", status)
            self.assertIn("Recommendation: myos approve --list [label=review_approvals]", status)

            # Same-data assertion for loop status --json: the JSON envelope
            # exposes the same task state a supervising process would need to
            # act on (task id, status, pending approvals, cycles).
            status_json_out = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "loop", "status", "--task", "1", "--json"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            status_payload = json.loads(status_json_out)
            self.assertEqual(status_payload["schema"], "myos.loop.status.v1")
            self.assertEqual(status_payload["task_filter"], 1)
            self.assertEqual(status_payload["count"], 1)
            task_entry = status_payload["tasks"][0]
            self.assertEqual(task_entry["task_id"], 1)
            self.assertEqual(task_entry["status"], "waiting_approval")
            self.assertGreaterEqual(task_entry["pending_approvals"], 1)
            self.assertIn("cycles", task_entry)
            self.assertIn("mode", task_entry)

            resumed = run("loop", "resume", "--task", "1", "--max-actions", "2")
            self.assertIn("Autonomy loop task #1", resumed)
            self.assertIn("run #1", resumed)
            self.assertIn("safe_executed=0", resumed)

            # Same-data assertion for loop ledger --json: after start+resume
            # the ledger has at least one entry per bounded cycle. Automation
            # consumers can filter by task and consume parsed metadata.
            ledger_json_out = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "loop", "ledger", "--task", "1", "--json"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            ledger_payload = json.loads(ledger_json_out)
            self.assertEqual(ledger_payload["schema"], "myos.loop.ledger.v1")
            self.assertEqual(ledger_payload["filters"]["task_id"], 1)
            self.assertGreaterEqual(ledger_payload["count"], 1)
            for entry in ledger_payload["entries"]:
                self.assertEqual(entry["agent_task_id"], 1)
                self.assertIn("decision_type", entry)
                self.assertIn("status", entry)
                self.assertIn("actions_proposed", entry)
                self.assertIn("pending_approvals", entry)

            conn = sqlite3.connect(db_path)
            trace_link = conn.execute(
                "SELECT COUNT(*) FROM execution_traces WHERE command_path LIKE 'loop%' AND agent_task_id=1"
            ).fetchone()[0]
            pending_external = conn.execute(
                """
                SELECT COUNT(*)
                FROM agent_actions
                WHERE agent_task_id=1 AND action_type='draft_external_update'
                  AND status='proposed' AND requires_approval=1
                """
            ).fetchone()[0]
            cycles = json.loads(conn.execute("SELECT constraints_json FROM agent_tasks WHERE id=1").fetchone()[0])[
                "cycles"
            ]
            conn.close()
            self.assertGreaterEqual(trace_link, 2)
            self.assertEqual(pending_external, 1)
            self.assertEqual(cycles, 1)

    def test_goal_driven_autonomy_scheduler_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            created = run(
                "goal",
                "add",
                "Handle Jira risk",
                "--context",
                "Needs approval-gated follow-up",
                "--cadence-minutes",
                "1",
                "--priority",
                "1",
            )
            self.assertIn("Added assistant goal #1", created)

            goals = run("loop", "goals")
            self.assertIn("Eligible autonomy goals:", goals)
            self.assertIn("goal #1", goals)
            self.assertIn("loop_task=none", goals)

            first = run("loop", "run-goal", "--goal", "1")
            self.assertIn("Goal scheduler: action=started goal=#1", first)
            self.assertIn("pending_approvals=", first)
            self.assertIn("Recommendation: review pending approvals -> myos approve --list", first)
            self.assertIn("[label=review_approvals]", first)

            second = run("loop", "run-goal", "--goal", "1")
            self.assertIn("Goal scheduler: action=skipped goal=#1", second)
            self.assertIn("Recommendation: review pending approvals -> myos approve --list", second)
            self.assertIn("[label=review_approvals]", second)

            conn = sqlite3.connect(db_path)
            pending = conn.execute(
                """
                SELECT COUNT(*)
                FROM agent_actions
                WHERE agent_task_id=1 AND status='proposed' AND requires_approval=1
                """
            ).fetchone()[0]
            skipped_events = conn.execute(
                "SELECT COUNT(*) FROM event_log WHERE event_type='autonomy_goal_skipped'"
            ).fetchone()[0]
            trace_links = conn.execute(
                "SELECT COUNT(*) FROM execution_traces WHERE command_path LIKE 'loop run-goal%' AND agent_task_id=1"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(pending, 2)
            self.assertEqual(skipped_events, 1)
            self.assertEqual(trace_links, 2)

    def test_router_eval_and_feedback_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            eval_out = run("router", "eval")
            self.assertIn("Router eval:", eval_out)
            self.assertIn("accuracy=100.00%", eval_out)
            self.assertIn("recorded_eval_run", eval_out)

            run("do", "remember to follow up with platform")
            conn = sqlite3.connect(db_path)
            event_id = conn.execute(
                "SELECT id FROM event_log WHERE event_type='smart_route' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            conn.close()

            feedback = run(
                "router",
                "feedback",
                "--event",
                str(event_id),
                "--expected-intent",
                "daily_brief",
                "--note",
                "Expected daily planning.",
            )
            self.assertIn("Router feedback recorded:", feedback)
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT expected_intent, actual_intent, note_hash, note_length, text_hash FROM route_feedback"
            ).fetchone()
            override = conn.execute(
                "SELECT expected_intent FROM route_overrides WHERE text_hash=?", (row[4],)
            ).fetchone()
            conn.close()
            self.assertEqual(row[0], "daily_brief")
            self.assertEqual(row[1], "capture")
            self.assertTrue(row[2])
            self.assertEqual(row[3], len("Expected daily planning."))
            self.assertTrue(row[4])
            self.assertEqual(override[0], "daily_brief")

            overrides = run("router", "overrides")
            self.assertIn("Router overrides:", overrides)
            self.assertIn("intent=daily_brief", overrides)
            learned = run("do", "remember to follow up with platform")
            self.assertIn("Smart route: daily_brief", learned)

            commands = run("router", "commands", "--tier", "workflow", "--limit", "5")
            self.assertIn("Router command registry:", commands)
            self.assertIn("myos factory", commands)
            self.assertIn("safety=approval_gated", commands)

    def test_context_graph_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("capture", "Launch dashboard tracks customer escalations")
            run("capture", "Backend ingestion job supplies upstream metrics")
            run("triage")
            run("link", "--from-item", "1", "--to-item", "2", "--relation", "depends_on", "--weight", "0.8")

            out = run("context", "customer escalation dashboard", "--graph")
            self.assertIn("Graph context results for: customer escalation dashboard", out)
            self.assertIn("retrieval run: #", out)
            self.assertIn("work_item#1", out)
            self.assertIn("work_item#2", out)
            self.assertIn("reason: graph expansion", out)
            self.assertIn("path: work_item#1 -> depends_on:0.80 -> work_item#2", out)

    def test_trace_cleanup_rolls_up_old_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("capture", "Trace cleanup should retain aggregate audit visibility")
            traces = run("trace", "list", "--command", "capture")
            self.assertIn("Execution traces:", traces)
            self.assertIn("capture", traces)

            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                UPDATE execution_traces
                SET started_at = '2000-01-01T00:00:00Z',
                    finished_at = '2000-01-01T00:00:01Z'
                WHERE command_path LIKE 'capture%'
                """
            )
            conn.commit()
            conn.close()

            cleanup = run("trace", "cleanup", "--retention-days", "1", "--max-rows", "100")
            self.assertIn("Trace cleanup:", cleanup)
            self.assertIn("rolled_up=1", cleanup)
            rollups = run("trace", "rollups")
            self.assertIn("Execution trace rollups:", rollups)
            self.assertIn("capture", rollups)
            self.assertIn("count=1", rollups)

    def test_why_graph_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("capture", "Launch dashboard tracks customer escalations")
            run("capture", "Backend ingestion job supplies upstream metrics")
            run("triage")
            run("link", "--from-item", "1", "--to-item", "2", "--relation", "depends_on", "--weight", "0.8")

            out = run("why", "--item", "1", "--graph")
            self.assertIn("Work item #1: Launch dashboard tracks customer escalations", out)
            self.assertIn("retrieval run: #", out)
            self.assertIn("graph evidence:", out)
            self.assertIn("work_item#2", out)
            self.assertIn("reason: graph expansion", out)
            self.assertIn("path: work_item#1 -> depends_on:0.80 -> work_item#2", out)

    def test_retrieval_run_list_and_show(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("capture", "Launch dashboard tracks customer escalations")
            run("capture", "Backend ingestion job supplies upstream metrics")
            run("triage")
            run("link", "--from-item", "1", "--to-item", "2", "--relation", "depends_on", "--weight", "0.8")

            context_out = run("context", "customer escalation dashboard", "--graph")
            run_id_line = next(line for line in context_out.splitlines() if line.startswith("retrieval run: #"))
            run_id = run_id_line.rsplit("#", 1)[1]

            list_out = run("retrieval-run", "list")
            self.assertIn("Retrieval runs:", list_out)
            self.assertIn(f"#{run_id} [context_graph] customer escalation dashboard", list_out)

            show_out = run("retrieval-run", "show", "--id", run_id)
            self.assertIn(f"Retrieval run #{run_id} [context_graph]", show_out)
            self.assertIn("query: customer escalation dashboard", show_out)
            self.assertIn("sources:", show_out)
            self.assertIn("work_item#2", show_out)
            self.assertIn("reason: graph expansion", show_out)
            self.assertIn("path: work_item#1 -> depends_on:0.80 -> work_item#2", show_out)

    def test_duplicate_capture_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("capture", "Decision: use canary")
            out = run("capture", "Decision: use canary")
            self.assertIn("Duplicate capture ignored", out)

    def test_ingest_external_to_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()
            conn.close()

            run("doctor")
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                INSERT INTO external_items (
                    connector, external_id, item_type, title, body, owner, status, due_date, url, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "github",
                    "123",
                    "pull_request",
                    "Fix auth dependency risk",
                    "",
                    "testuser",
                    "open",
                    None,
                    "https://example/pr/123",
                    "{}",
                ),
            )
            conn.commit()
            conn.close()

            out = run("ingest-external", "--limit", "10")
            self.assertIn("Ingested 1 external items", out)
            run("triage")
            conn = sqlite3.connect(db_path)
            inbox_count = conn.execute("SELECT COUNT(*) FROM inbox_items").fetchone()[0]
            work_count = conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
            conn.close()
            self.assertEqual(inbox_count, 1)
            self.assertEqual(work_count, 1)

    def test_brief_and_stop_doing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("capture", "Implement migration for auth service")
            run("capture", "Task: clean up obsolete dashboard filters")
            run("triage")
            brief_out = run("brief", "--meeting-hours", "6")
            stop_out = run("stop-doing", "--capacity", "1", "--deep-budget", "0")
            self.assertIn("Executive brief", brief_out)
            self.assertIn("Stop-doing review", stop_out)

    def test_run_day_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            report_dir = Path(tmp) / "reports"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("capture", "Task: finalize architecture review")
            run("triage")
            run("onboard")
            out = run(
                "run-day",
                "--meeting-hours",
                "4",
                "--connector",
                "all",
                "--output-dir",
                str(report_dir),
            )
            self.assertIn("Pipeline summary", out)
            report_files = list(report_dir.glob("daily-brief-*.md"))
            self.assertTrue(report_files)

    def test_config_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            cfg = Path(tmp) / ".env.myos"
            out = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "config-init", "--path", str(cfg)],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Created config template", out.stdout)
            self.assertTrue(cfg.exists())

    def test_doctor_strict_and_public_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            out = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "doctor", "--strict"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Core checks", out.stdout)
            self.assertIn("PASS python_version", out.stdout)
            self.assertIn("PASS sqlite_fts5", out.stdout)
            self.assertIn("Doctor strict: core checks passed", out.stdout)

            env_example = Path.cwd() / ".env.example"
            demo = Path.cwd() / "examples" / "demo-local.md"
            launchd_sync = Path.cwd() / "deploy" / "launchd" / "com.myos.sync.plist"
            launchd_pulse = Path.cwd() / "deploy" / "launchd" / "com.myos.pulse.plist"
            self.assertTrue(env_example.exists())
            self.assertTrue(demo.exists())
            self.assertTrue(launchd_sync.exists())
            self.assertTrue(launchd_pulse.exists())
            self.assertIn("MYOS_ACTION_COMMAND=myos action-provider", env_example.read_text())
            self.assertIn("myos doctor --strict", demo.read_text())
            self.assertIn("/path/to/personal-assistant-os", launchd_sync.read_text())
            self.assertIn("/path/to/personal-assistant-os", launchd_pulse.read_text())

    def test_backup_restore_and_migration_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            backup_path = Path(tmp) / "backup.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("capture", "Task: keep backup restore reliable")
            run("triage")
            verify_out = run("migrations", "verify", "--strict")
            self.assertIn("Schema migrations verified", verify_out)
            list_out = run("migrations", "list")
            self.assertIn("Schema migrations:", list_out)
            self.assertIn("28 add_action_execution_receipts", list_out)
            self.assertIn("30 add_router_quality_loop", list_out)
            self.assertIn("31 add_router_feedback_overrides", list_out)
            self.assertIn("32 add_lightweight_observability", list_out)
            self.assertIn("33 add_autonomy_decision_calibration", list_out)
            self.assertIn("34 add_autonomy_run_ledger", list_out)
            self.assertIn("35 add_recommendation_feedback", list_out)
            self.assertIn("36 add_factory_executor_backend", list_out)
            self.assertIn("37 add_approval_integrity_binding", list_out)
            self.assertIn("Current version: 37 / expected 37", list_out)

            backup_out = run("backup", "--output", str(backup_path))
            self.assertIn("Backup created", backup_out)
            self.assertTrue(backup_path.exists())

            run("capture", "Task: this should disappear after restore")
            run("triage")
            db_path.with_name(db_path.name + "-wal").write_text("stale wal")
            db_path.with_name(db_path.name + "-shm").write_text("stale shm")
            restore_out = run("restore", "--from", str(backup_path))
            self.assertIn("Current database backed up", restore_out)
            self.assertIn("Database restored from", restore_out)
            self.assertIn("Schema migrations verified", restore_out)
            self.assertFalse(db_path.with_name(db_path.name + "-wal").exists())
            self.assertFalse(db_path.with_name(db_path.name + "-shm").exists())
            self.assertTrue(list((db_path.parent / "backups").glob("pre-restore-*.db")))

            conn = sqlite3.connect(db_path)
            work_count = conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
            title = conn.execute("SELECT title FROM work_items ORDER BY id").fetchone()[0]
            conn.close()
            self.assertEqual(work_count, 1)
            self.assertIn("keep backup restore reliable", title)

            invalid_restore = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "restore", "--from", str(Path(tmp) / "missing.db")],
                cwd=Path.cwd(),
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(invalid_restore.returncode, 0)
            self.assertIn("Restore refused", invalid_restore.stdout)

    def test_intent_lifecycle_and_redacted_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            base_cmd = [sys.executable, "-m", "personal_assistant.cli"]

            created = subprocess.run(
                base_cmd
                + [
                    "intent",
                    "create",
                    "Ship public assistant baseline",
                    "--context",
                    "Local-only release",
                    "--constraint",
                    "No external services",
                    "--success",
                    "Repeatable demo works",
                    "--priority",
                    "1",
                ],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Created intent #1", created.stdout)

            listed = subprocess.run(
                base_cmd + ["intent", "list", "--status", "open"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("#1 status=open priority=1", listed.stdout)
            self.assertIn("Ship public assistant baseline", listed.stdout)

            listed_json = subprocess.run(
                base_cmd + ["intent", "list", "--status", "open", "--json"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            intent_payload = json.loads(listed_json.stdout)
            self.assertEqual(intent_payload["schema"], "myos.intent.list.v1")
            self.assertEqual(intent_payload["count"], 1)
            self.assertEqual(intent_payload["status_filter"], "open")
            self.assertEqual(intent_payload["intents"][0]["id"], 1)
            self.assertEqual(intent_payload["intents"][0]["objective"], "Ship public assistant baseline")

            evidence = subprocess.run(
                base_cmd
                + [
                    "intent",
                    "evidence",
                    "add",
                    "--id",
                    "1",
                    "--text",
                    "Owner test@example.com has token ghp_abcdefghijklmnopqrstuvwxyz123456.",
                    "--source-type",
                    "note",
                    "--summary",
                    "Email test@example.com confirmed",
                ],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Added evidence #1 to intent #1", evidence.stdout)

            shown = subprocess.run(
                base_cmd + ["intent", "show", "--id", "1"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Intent #1", shown.stdout)
            self.assertIn("Constraints:", shown.stdout)
            self.assertIn("No external services", shown.stdout)
            self.assertIn("[REDACTED_EMAIL]", shown.stdout)
            self.assertIn("[REDACTED_SECRET]", shown.stdout)
            self.assertNotIn("test@example.com", shown.stdout)
            self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz123456", shown.stdout)

    def test_plan_review_packet_lifecycle_with_retrieval_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("intent", "create", "Ship dashboard safely", "--success", "Owner approves cited plan")
            run("capture", "Launch dashboard tracks customer escalations")
            run("capture", "Backend ingestion job supplies upstream metrics")
            run("triage")
            run("link", "--from-item", "1", "--to-item", "2", "--relation", "depends_on", "--weight", "0.8")
            context_out = run("context", "customer escalation dashboard", "--graph")
            run_id_line = next(line for line in context_out.splitlines() if line.startswith("retrieval run: #"))
            run_id = run_id_line.rsplit("#", 1)[1]

            evidence_out = run("evidence", "attach", "--intent", "1", "--retrieval-run", run_id)
            self.assertIn(f"Attached retrieval run #{run_id}", evidence_out)

            plan_out = run("plan", "create", "--intent", "1", "--assumption", "No external mutation without approval")
            self.assertIn("Created plan #1 for intent #1", plan_out)

            show_out = run("plan", "show", "--id", "1")
            self.assertIn("Plan #1 intent=1 status=draft", show_out)
            self.assertIn("Steps:", show_out)
            self.assertIn("Risks:", show_out)
            self.assertIn("Validations:", show_out)

            show_json = json.loads(run("plan", "show", "--id", "1", "--json"))
            self.assertEqual(show_json["schema"], "myos.plan.show.v1")
            self.assertEqual(show_json["plan"]["id"], 1)
            self.assertEqual(show_json["plan"]["intent_id"], 1)
            self.assertEqual(show_json["plan"]["status"], "draft")
            self.assertTrue(show_json["steps"])
            self.assertTrue(show_json["risks"])
            self.assertTrue(show_json["validations"])

            packet_out = run("review-packet", "--plan", "1", "--retrieval-run", run_id)
            self.assertIn("Review packet #1 for plan #1", packet_out)
            self.assertIn("Intent: #1 Ship dashboard safely", packet_out)
            self.assertIn("Retrieval sources:", packet_out)
            self.assertIn("work_item#2", packet_out)
            self.assertIn("Approval required: True", packet_out)
            self.assertIn("Rollback:", packet_out)

    def test_factory_review_first_policy_packs_and_learning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
                return subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=check,
                    capture_output=True,
                    text=True,
                )

            run("intent", "create", "Ship dashboard safely", "--success", "Factory review passes")
            run("capture", "Dashboard launch needs customer escalation context")
            run("triage")

            started = run("factory", "start", "--intent", "1").stdout
            self.assertIn("Autonomy: decision=needs_approval", started)
            self.assertIn("safety=approval_gated", started)
            self.assertIn(
                "Recommendation: Review the generated packet before approving execution -> myos factory review --id <run_id>",
                started,
            )
            self.assertIn("Factory run #1 for intent #1 status=awaiting_approval", started)
            self.assertIn(
                "Recommendation: Review the generated packet before approving execution -> myos factory review --id 1",
                started,
            )
            self.assertIn("review_packet=#", started)
            self.assertIn("stopped_before_execution=True", started)

            status = run("factory", "status", "--id", "1").stdout
            self.assertIn("mode=review_first pack=intent_execution status=awaiting_approval", status)
            self.assertIn("- critic status=completed", status)
            self.assertIn("- execution status=blocked", status)
            self.assertIn("review_packet#1", status)

            denied = run("factory", "start", "--intent", "1", "--mode", "full_autonomous", check=False)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("full_autonomous requires an explicit factory policy", denied.stdout)

            policy = run(
                "factory",
                "policy",
                "set",
                "--mode",
                "full_autonomous",
                "--scope-type",
                "intent",
                "--scope-id",
                "1",
            ).stdout
            self.assertIn("mode=full_autonomous", policy)
            packed = run(
                "factory",
                "start",
                "--intent",
                "1",
                "--mode",
                "full_autonomous",
                "--pack",
                "software_delivery",
            ).stdout
            self.assertIn("mode=full_autonomous pack=software_delivery", packed)

            review = run("factory", "review", "--id", "2").stdout
            self.assertIn("Factory review #2:", review)
            self.assertIn("Side effects: local_db_write, external_write", review)
            self.assertIn("Review gate: factory_execution_approval_required", review)
            self.assertIn("Safer next: myos approve --list; myos execution-receipt list", review)
            self.assertIn("Execution remains approval-gated.", review)
            learn = run(
                "factory", "learn", "--id", "2", "--outcome", "partial", "--notes", "Reviewer caught missing validation"
            ).stdout
            self.assertIn("Factory learning #1 recorded for run #2: partial", learn)
            retro = run("factory", "retrospective", "--id", "2").stdout
            self.assertIn("Factory retrospective #2: outcome=partial", retro)
            self.assertIn("artifacts=", retro)

            conn = sqlite3.connect(db_path)
            counts = dict(
                conn.execute(
                    """
                    SELECT artifact_type, COUNT(*)
                    FROM factory_artifacts
                    WHERE factory_run_id = 1
                    GROUP BY artifact_type
                    """
                ).fetchall()
            )
            stages = dict(
                conn.execute(
                    """
                    SELECT stage_name, status
                    FROM factory_stages
                    WHERE factory_run_id = 1
                    """
                ).fetchall()
            )
            pack = conn.execute("SELECT workflow_pack FROM factory_runs WHERE id = 2").fetchone()[0]
            conn.close()
            self.assertGreaterEqual(counts.get("plan", 0), 1)
            self.assertGreaterEqual(counts.get("retrieval_run", 0), 1)
            self.assertGreaterEqual(counts.get("review_packet", 0), 1)
            self.assertGreaterEqual(counts.get("agent_run", 0), 5)
            self.assertEqual(stages["approval"], "waiting")
            self.assertEqual(stages["execution"], "blocked")
            self.assertEqual(pack, "software_delivery")

    def test_factory_zero_review_surfaces_patch_approval_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True, capture_output=True
            )
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "commit.gpgsign", "false"], check=True, capture_output=True
            )
            Path(repo, "README.md").write_text("seed\n")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True, capture_output=True)
            fake_zero = Path(tmp) / "fake_zero.py"
            fake_zero.write_text(
                "import json, os, pathlib\n"
                "pathlib.Path('cli-zero.txt').write_text('cli proof\\n')\n"
                "events = [\n"
                "  {'schemaVersion': 2, 'type': 'run_start', 'runId': 'cli_run', 'sessionId': 'cli_session', 'cwd': os.getcwd(), 'provider': 'fake', 'model': 'fake'},\n"
                "  {'schemaVersion': 2, 'type': 'permission_request', 'runId': 'cli_run', 'id': 'perm_1', 'name': 'bash', 'permission': 'prompt', 'sideEffect': 'shell', 'reason': 'verify'},\n"
                "  {'schemaVersion': 2, 'type': 'tool_result', 'runId': 'cli_run', 'id': 'tool_1', 'name': 'write_file', 'status': 'ok', 'changedFiles': ['cli-zero.txt']},\n"
                "  {'schemaVersion': 2, 'type': 'warning', 'runId': 'cli_run', 'message': 'cli verification skipped'},\n"
                "  {'schemaVersion': 2, 'type': 'final', 'runId': 'cli_run', 'text': 'cli fake zero finished'},\n"
                "  {'schemaVersion': 2, 'type': 'run_end', 'runId': 'cli_run', 'status': 'success', 'exitCode': 0},\n"
                "]\n"
                "for event in events:\n"
                "    print(json.dumps(event), flush=True)\n"
            )
            env["MYOS_AGENT_EXEC_ZERO_STREAM"] = f"{sys.executable} {fake_zero}"

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("intent", "create", "Use Zero for a CLI proof patch")
            started = run(
                "factory",
                "start",
                "--intent",
                "1",
                "--pack",
                "software_delivery",
                "--executor",
                "zero",
                "--repo",
                str(repo),
                "--timeout",
                "30",
                "--max-turns",
                "1",
                "--verify-command",
                "python -m pytest",
            )
            self.assertIn("status=awaiting_approval", started)
            self.assertIn("executor=zero", started)
            self.assertIn("approval_actions=#1", started)
            self.assertIn("approve=myos approve --action 1 --execute", started)
            self.assertFalse(Path(repo, "cli-zero.txt").exists())

            status = run("factory", "status", "--id", "1")
            self.assertIn("Executor artifacts:", status)
            self.assertIn("- zero status=success exit_code=0 action=#1", status)
            self.assertIn("zero_ref=run:cli_run session:cli_session", status)
            self.assertIn("executor_worktree=isolated retained=False", status)
            self.assertIn("signals=permissions:1 warnings:1 errors:0 protocol_errors:0", status)
            self.assertIn("warning=cli verification skipped", status)

            # Same-data assertion for factory status --json: the JSON envelope
            # exposes the run metadata, stage list, artifacts, and the parsed
            # executor artifacts a supervising process needs to reconcile a
            # factory cycle without regex-parsing the human-readable output.
            factory_json_out = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "factory", "status", "--id", "1", "--json"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            factory_payload = json.loads(factory_json_out)
            self.assertEqual(factory_payload["schema"], "myos.factory.status.v1")
            self.assertEqual(factory_payload["run"]["id"], 1)
            self.assertEqual(factory_payload["run"]["executor_backend"], "zero")
            stage_names = {stage["stage_name"] for stage in factory_payload["stages"]}
            self.assertIn("execution", stage_names)
            self.assertTrue(factory_payload["executor_artifacts"])
            zero_artifact = next(a for a in factory_payload["executor_artifacts"] if a.get("type") == "zero_executor")
            self.assertEqual(zero_artifact.get("status"), "success")
            self.assertEqual(zero_artifact.get("exit_code"), 0)
            self.assertIn("changed_files=cli-zero.txt", status)
            self.assertIn("diff_stats=files:1 +1 -0 binary:0", status)
            self.assertIn("verify=python -m pytest", status)
            self.assertIn("approve=myos approve --action 1 --execute", status)
            self.assertIn("retry=myos factory start --intent 1", status)
            self.assertIn("--verify-command 'python -m pytest'", status)

            review = run("factory", "review", "--id", "1")
            self.assertIn("Factory review #1: ready_for_approval", review)
            self.assertIn("Executor artifacts:", review)
            self.assertIn("summary=cli fake zero finished", review)
            self.assertIn("zero_ref=run:cli_run session:cli_session", review)
            self.assertIn("signals=permissions:1 warnings:1 errors:0 protocol_errors:0", review)
            self.assertIn("diff_stats=files:1 +1 -0 binary:0", review)
            self.assertIn("verify=python -m pytest", review)
            self.assertIn("retry=myos factory start --intent 1", review)
            self.assertIn("Execution remains approval-gated.", review)

            approvals = run("approve", "--list")
            self.assertIn("zero: status=success exit_code=0 run=cli_run session=cli_session", approvals)
            self.assertIn("zero_changed_files: cli-zero.txt", approvals)
            self.assertIn("zero_diff_stats: files=1 additions=1 deletions=0 binary=0", approvals)
            self.assertIn("zero_verify: python -m pytest", approvals)

            # Assert approve --list --json exposes the same queue as the text
            # output on the same DB state, so automation consumers see the
            # exact set of pending approvals the human reviewer would.
            approve_json_out = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "approve", "--list", "--json"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            approve_payload = json.loads(approve_json_out)
            self.assertEqual(approve_payload["schema"], "myos.approve.list.v1")
            self.assertGreaterEqual(approve_payload["count"], 1)
            self.assertTrue(
                any(a["id"] == 1 for a in approve_payload["actions"]),
                f"expected action id=1 in JSON queue, got {approve_payload['actions']}",
            )
            action_entry = next(a for a in approve_payload["actions"] if a["id"] == 1)
            self.assertIn("action_type", action_entry)
            self.assertIn("review_context", action_entry)
            self.assertIn("target", action_entry)
            # Integrity block must be present so supervisors can spot near-
            # expiry, expired, or tampered approvals before execution refuses
            # them. Action 1 is still `proposed` at this point (never
            # approved yet), so state must be `not_yet_approved`.
            self.assertIn("integrity", action_entry)
            self.assertEqual(action_entry["integrity"]["state"], "not_yet_approved")
            self.assertEqual(action_entry["integrity"]["schema"], "myos.approval_integrity_view.v1")

            executed = run("approve", "--action", "1", "--execute")
            self.assertIn("Executed action #1: patch applied", executed)
            receipts = run("execution-receipt", "list")
            self.assertIn("verification: not_run", receipts)
            self.assertIn("verification_command: python -m pytest", receipts)
            receipt = run("execution-receipt", "show", "--id", "1")
            self.assertIn("Verification: not_run", receipt)
            self.assertIn("Verification Command: python -m pytest", receipt)
            self.assertIn("Verification Reason: Suggested verification is recorded for the operator", receipt)

            # Same-data assertion for execution-receipt list --json + show --json.
            # The JSON output must expose the approval integrity envelope and
            # verification block downstream automation and audit consumers need.
            list_json_out = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "execution-receipt", "list", "--json"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            list_payload = json.loads(list_json_out)
            self.assertEqual(list_payload["schema"], "myos.execution_receipt.list.v1")
            self.assertGreaterEqual(list_payload["count"], 1)
            receipt_entry = list_payload["receipts"][0]
            self.assertIn("approval_integrity", receipt_entry)
            self.assertIsNotNone(receipt_entry["approval_integrity"])
            self.assertTrue(receipt_entry["approval_integrity"]["ok"])
            self.assertTrue(receipt_entry["approval_integrity"]["payload_hash_verified"])
            self.assertIn("verification", receipt_entry)
            self.assertIsNotNone(receipt_entry["verification"])
            self.assertEqual(receipt_entry["verification"]["status"], "not_run")

            show_json_out = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "execution-receipt", "show", "--id", "1", "--json"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            show_payload = json.loads(show_json_out)
            self.assertEqual(show_payload["schema"], "myos.execution_receipt.show.v1")
            self.assertEqual(show_payload["receipt"]["id"], 1)
            self.assertIn("title", show_payload["receipt"])
            self.assertIn("result", show_payload["receipt"])
            self.assertIn("outbox", show_payload["receipt"])

    def test_deep_factory_autonomous_execution_and_proactive_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)
            provider_script = Path(tmp) / "role_provider.py"
            provider_script.write_text(
                "import json, sys\n"
                "req=json.loads(sys.stdin.read() or '{}')\n"
                "print(json.dumps({'reply': 'role ok ' + req.get('purpose', ''), "
                "'plan': [{'step': 'check', 'detail': 'ok'}], 'actions': []}))\n"
            )
            env["MYOS_FACTORY_ROLE_BACKEND"] = "command"
            env["MYOS_AI_COMMAND"] = f"{sys.executable} {provider_script}"

            def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
                return subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=check,
                    capture_output=True,
                    text=True,
                )

            run("intent", "create", "Prepare launch follow-up", "--priority", "1")
            run("factory", "policy", "set", "--mode", "semi_autonomous", "--scope-type", "intent", "--scope-id", "1")
            semi = run("factory", "start", "--intent", "1", "--mode", "semi_autonomous").stdout
            self.assertIn("status=execution_completed", semi)
            self.assertIn("stopped_before_execution=False", semi)
            semi_status = run("factory", "status", "--id", "1").stdout
            self.assertIn("- execution status=completed", semi_status)
            self.assertIn("agent_action#1", semi_status)
            self.assertIn("execution_receipt#1", semi_status)
            conn = sqlite3.connect(db_path)
            provider_row = conn.execute(
                "SELECT provider, plan_json FROM agent_runs WHERE agent_name='reviewer' ORDER BY id ASC LIMIT 1"
            ).fetchone()
            conn.close()
            self.assertEqual(provider_row[0], "factory_command")
            self.assertIn("role ok factory_reviewer", provider_row[1])

            run("intent", "create", "Update connector stakeholders", "--priority", "2")
            denied = run(
                "factory", "start", "--intent", "2", "--mode", "full_autonomous", "--pack", "connector_ops", check=False
            )
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("full_autonomous requires an explicit factory policy", denied.stdout)
            run("factory", "policy", "set", "--mode", "full_autonomous", "--scope-type", "intent", "--scope-id", "2")
            run(
                "factory",
                "policy",
                "set",
                "--mode",
                "full_autonomous",
                "--connector",
                "jira",
                "--action-type",
                "draft_external_update",
            )
            full = run(
                "factory", "start", "--intent", "2", "--mode", "full_autonomous", "--pack", "connector_ops"
            ).stdout
            self.assertIn("status=execution_completed", full)
            full_status = run("factory", "status", "--id", "2").stdout
            self.assertIn("execution_receipt#", full_status)
            self.assertIn("agent_action#", full_status)

            learn = run(
                "factory",
                "learn",
                "--id",
                "2",
                "--outcome",
                "failed",
                "--notes",
                "blocked connector update needed reviewer",
            ).stdout
            self.assertIn("Factory learning", learn)
            insights = run("factory", "insights", "--intent", "2").stdout
            self.assertIn('outcomes={"failed": 1}', insights)
            self.assertIn("side_effects=", insights)
            self.assertIn("external_write", insights)
            self.assertIn("blocked connector update", insights)
            feedback = run(
                "autonomy",
                "recommendation-feedback",
                "--label",
                "review_approvals",
                "--command",
                "myos approve --list",
                "--useful",
                "yes",
                "--note",
                "Approval review caught connector risk.",
            ).stdout
            self.assertIn("Recommendation feedback recorded:", feedback)
            self.assertIn("Privacy: note text was hashed", feedback)
            recommendations = run("autonomy", "recommendations").stdout
            self.assertIn("Recommendation feedback summary:", recommendations)
            self.assertIn("label=review_approvals command=myos approve --list", recommendations)
            self.assertIn("learning_score=", recommendations)
            self.assertIn("side_effects=external_write", recommendations)
            self.assertNotIn("Approval review caught connector risk.", recommendations)
            self.assertNotIn("note_hash", recommendations)
            self.assertNotIn("note_length", recommendations)

            run("intent", "create", "Daily factory priority", "--priority", "1")
            autopilot = run(
                "autopilot",
                "--once",
                "--no-sync",
                "--no-process",
                "--factory",
                "--factory-mode",
                "review_first",
                "--factory-pack",
                "daily_ops",
            ).stdout
            self.assertIn("factory=started", autopilot)
            morning = run("morning").stdout
            self.assertIn("Factory runs:", morning)
            close = run("close-day").stdout
            self.assertIn("Active factory runs:", close)

    def test_connector_action_provider_hardening(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
                return subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=check,
                    capture_output=True,
                    text=True,
                )

            run("doctor")
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO agent_tasks (objective, context, constraints_json, priority, status) VALUES ('connector hardening', '', '{}', 1, 'open')"
            )
            task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            payloads = [
                {
                    "connector": "jira",
                    "operation": "comment",
                    "target_ref": "PROJ-1",
                    "draft": "Jira dry run",
                    "rollback_note": "Remove Jira comment.",
                    "dry_run": True,
                },
                {
                    "connector": "github",
                    "operation": "comment",
                    "target_ref": "owner/repo#7",
                    "draft": "GitHub dry run",
                    "rollback_note": "Remove GitHub comment.",
                    "dry_run": True,
                },
                {
                    "connector": "confluence",
                    "operation": "draft_note",
                    "target_ref": "PAGE-1",
                    "draft": "Confluence dry run",
                    "rollback_note": "Remove Confluence draft.",
                    "dry_run": True,
                },
                {
                    "connector": "aha",
                    "operation": "link_back",
                    "target_ref": "FEAT-1",
                    "draft": "Aha dry run",
                    "rollback_note": "Remove Aha update.",
                    "dry_run": True,
                },
                {"connector": "jira", "operation": "comment", "draft": "Missing target", "rollback_note": "No-op."},
            ]
            for index, payload in enumerate(payloads, start=1):
                conn.execute(
                    """
                    INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, requires_approval, status)
                    VALUES (?, 'draft_external_update', ?, ?, 1, 'proposed')
                    """,
                    (task_id, f"connector action {index}", json.dumps(payload)),
                )
            conn.commit()
            conn.close()

            listed = run("act", "--list").stdout
            self.assertIn("jira:PROJ-1 operation=comment mode=dry_run", listed)
            self.assertIn("side_effects: external_write", listed)
            self.assertIn("review_gate: external_write_requires_approval", listed)
            self.assertIn("safer_next: myos approve --list; myos execution-receipt list", listed)
            self.assertIn("rollback: Remove Jira comment.", listed)
            approved_list = run("approve", "--list").stdout
            self.assertIn("github:owner/repo#7 operation=comment mode=dry_run", approved_list)
            self.assertIn("dry_run: true", approved_list)

            for action_id in range(1, 5):
                out = run("act", "--action", str(action_id), "--approve", "--execute").stdout
                self.assertIn("connector drafted: outbox #", out)

            blocked = run("act", "--action", "5", "--approve", "--execute").stdout
            self.assertIn("blocked: jira mutation requires target_ref", blocked)

            receipt = run("execution-receipt", "show", "--id", "1").stdout
            self.assertIn("Target: jira:PROJ-1 operation=comment mode=dry_run", receipt)
            self.assertIn("Side Effects: external_write", receipt)
            self.assertIn("Review Gate: external_write_requires_approval", receipt)
            self.assertIn("Outbox: #1 provider=connector:jira target=jira:PROJ-1 status=drafted", receipt)

            conn = sqlite3.connect(db_path)
            outbox = dict(
                conn.execute("SELECT target_type, COUNT(*) FROM action_outbox GROUP BY target_type").fetchall()
            )
            receipt_statuses = dict(
                conn.execute(
                    "SELECT final_status, COUNT(*) FROM action_execution_receipts GROUP BY final_status"
                ).fetchall()
            )
            follow_up = conn.execute(
                "SELECT follow_up_required, follow_up_inbox_id FROM action_execution_receipts WHERE agent_action_id = 5"
            ).fetchone()
            receipt_request = json.loads(
                conn.execute("SELECT request_json FROM action_execution_receipts WHERE agent_action_id = 1").fetchone()[
                    0
                ]
            )
            conn.close()
            self.assertEqual(outbox, {"aha": 1, "confluence": 1, "github": 1, "jira": 1})
            self.assertEqual(receipt_statuses.get("executed"), 4)
            self.assertEqual(receipt_statuses.get("blocked"), 1)
            self.assertEqual(follow_up[0], 1)
            self.assertIsNotNone(follow_up[1])
            self.assertEqual(receipt_request["approval_context"]["side_effects"], ["external_write"])
            self.assertTrue(receipt_request["approval_context"]["dry_run"])
            self.assertEqual(receipt_request["approval_context"]["approval_reason"], "external_write_requires_approval")

            run("intent", "create", "Update Confluence launch note", "--priority", "1")
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                INSERT INTO external_items (connector, external_id, item_type, title, body, url)
                VALUES ('confluence', 'PAGE-9', 'page', 'Launch readiness page', 'Needs update.', 'https://example.test/wiki/PAGE-9')
                """
            )
            conn.commit()
            conn.close()
            run("evidence", "sync-external", "--intent", "1", "--connector", "confluence")
            run("factory", "policy", "set", "--mode", "full_autonomous", "--scope-type", "intent", "--scope-id", "1")
            run(
                "factory",
                "policy",
                "set",
                "--mode",
                "full_autonomous",
                "--connector",
                "confluence",
                "--action-type",
                "draft_external_update",
            )
            factory_out = run(
                "factory", "start", "--intent", "1", "--mode", "full_autonomous", "--pack", "connector_ops"
            ).stdout
            self.assertIn("status=execution_completed", factory_out)
            conn = sqlite3.connect(db_path)
            target = conn.execute(
                "SELECT target_type, target_ref FROM action_outbox WHERE target_type='confluence' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            artifact_count = conn.execute(
                "SELECT COUNT(*) FROM factory_artifacts WHERE factory_run_id = 1 AND artifact_type='execution_receipt'"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(tuple(target), ("confluence", "PAGE-9"))
            self.assertGreaterEqual(artifact_count, 1)

    def test_claim_extraction_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            out = run(
                "claim",
                "extract",
                "--text",
                "Project Atlas requires Service Billing API.",
                "--source-type",
                "work_item",
                "--source-id",
                "7",
            )
            self.assertIn("Recorded 1 claim", out)
            self.assertIn("Project Atlas requires Service Billing API", out)
            listed = run("claim", "list", "--source-type", "work_item")
            self.assertIn("Claims:", listed)
            self.assertIn("source=work_item:7", listed)

    def test_agent_role_run_records_local_control_plane_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("intent", "create", "Ship dashboard safely")
            run("plan", "create", "--intent", "1")
            out = run("agent-run", "--intent", "1", "--plan", "1", "--role", "reviewer")
            self.assertIn("Agent run #1 [reviewer] for intent #1", out)
            self.assertIn("approval_gate: True", out)

            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT agent_name, provider, status, summary FROM agent_runs WHERE id = 1").fetchone()
            conn.close()
            self.assertEqual(row[0], "reviewer")
            self.assertEqual(row[1], "local")
            self.assertEqual(row[2], "completed")
            self.assertIn("intent #1", row[3])

    def test_daily_loops_surface_intents_approvals_and_evidence_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("intent", "create", "Prepare launch review", "--priority", "1")
            morning = run("morning")
            self.assertIn("Morning brief:", morning)
            self.assertIn("intent #1 priority=1 Prepare launch review", morning)
            self.assertIn("Evidence gaps:", morning)
            self.assertIn("intent #1 needs evidence", morning)

            close = run("close-day", "--note", "Captured day-end state")
            self.assertIn("Day closed.", close)
            self.assertIn("Open intents: 1", close)
            self.assertIn("Pending approvals: 0", close)

            weekly = run("weekly-review")
            self.assertIn("Weekly review", weekly)
            self.assertIn("open_intents=1 evidence_gaps=1", weekly)

            run("capture", "Risk: dashboard escalation needs owner by tomorrow")
            run("triage")
            next_action = run("next-action")
            self.assertIn("Next action recommendation", next_action)
            self.assertIn('[label=daily_focus_block command="myos next-action"]', next_action)
            now = run("now")
            self.assertIn('[label=daily_focus_block command="myos now"]', now)
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                INSERT INTO work_items (title, kind, status, priority, risk_score, owner, due_date)
                VALUES ('Waiting on launch owner', 'commitment', 'open', 1, 20, 'team-owner', date('now', '+1 day'))
                """
            )
            conn.execute(
                """
                INSERT INTO work_items (title, kind, status, priority, risk_score, owner, due_date)
                VALUES ('Escalation risk needs reduction', 'risk', 'open', 1, 95, NULL, date('now', '+2 days'))
                """
            )
            conn.commit()
            conn.close()
            default_meeting = run("next-action", "--meeting-hours", "7")
            self.assertIn('[label=daily_nudge_owner command="myos next-action"]', default_meeting)
            self.assertNotIn("ranking context:", default_meeting)
            feedback_ack = run(
                "autonomy",
                "recommendation-feedback",
                "--label",
                "daily_reduce_risk",
                "--command",
                "myos next-action",
                "--useful",
                "yes",
                "--note",
                "Risk reduction was the better daily recommendation.",
            )
            self.assertIn("Recommendation feedback recorded:", feedback_ack)
            self.assertIn("useful=True", feedback_ack)
            self.assertIn(
                "Privacy: note text was hashed; raw recommendation feedback text was not stored.", feedback_ack
            )
            self.assertNotIn("Risk reduction was the better daily recommendation.", feedback_ack)
            for _ in range(2):
                run(
                    "autonomy",
                    "recommendation-feedback",
                    "--label",
                    "daily_reduce_risk",
                    "--command",
                    "myos next-action",
                    "--useful",
                    "yes",
                    "--note",
                    "Risk reduction was the better daily recommendation.",
                )
            tuned_meeting = run("next-action", "--meeting-hours", "7")
            self.assertIn('[label=daily_reduce_risk command="myos next-action"]', tuned_meeting)
            self.assertIn(
                "ranking context: feedback adjusted selection from daily_nudge_owner to daily_reduce_risk (selected_score=+3 baseline_score=+0)",
                tuned_meeting,
            )
            self.assertNotIn("Risk reduction was the better daily recommendation.", tuned_meeting)
            now_after_next_action_feedback = run("now", "--meeting-hours", "7")
            self.assertIn('[label=daily_nudge_owner command="myos now"]', now_after_next_action_feedback)
            self.assertNotIn("ranking context:", now_after_next_action_feedback)
            for _ in range(3):
                run(
                    "autonomy",
                    "recommendation-feedback",
                    "--label",
                    "daily_reduce_risk",
                    "--command",
                    "myos now",
                    "--useful",
                    "yes",
                    "--note",
                    "Now should prefer the risk recommendation independently.",
                )
            tuned_now = run("now", "--meeting-hours", "7")
            self.assertIn('[label=daily_reduce_risk command="myos now"]', tuned_now)
            self.assertIn(
                "ranking context: feedback adjusted selection from daily_nudge_owner to daily_reduce_risk (selected_score=+3 baseline_score=+0)",
                tuned_now,
            )
            self.assertNotIn("Now should prefer the risk recommendation independently.", tuned_now)
            summary = run("autonomy", "recommendations", "--limit", "5")
            self.assertIn("Recommendation feedback summary:", summary)
            self.assertIn("surface=daily label=daily_reduce_risk command=myos next-action", summary)
            self.assertIn("surface=daily label=daily_reduce_risk command=myos now", summary)
            self.assertIn("recent_score_30d=3", summary)
            self.assertIn("side_effects=none", summary)
            self.assertNotIn("Risk reduction was the better daily recommendation.", summary)
            self.assertNotIn("Now should prefer the risk recommendation independently.", summary)
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                UPDATE recommendation_feedback
                SET created_at = datetime('now', '-90 days')
                WHERE label = 'daily_reduce_risk' AND command IN ('myos next-action', 'myos now')
                """
            )
            conn.commit()
            conn.close()
            decayed_meeting = run("next-action", "--meeting-hours", "7")
            self.assertIn('[label=daily_nudge_owner command="myos next-action"]', decayed_meeting)
            self.assertNotIn("ranking context:", decayed_meeting)
            decayed_now = run("now", "--meeting-hours", "7")
            self.assertIn('[label=daily_nudge_owner command="myos now"]', decayed_now)
            self.assertNotIn("ranking context:", decayed_now)
            decayed_summary = run("autonomy", "recommendations", "--limit", "5")
            self.assertIn("score=3 useful=3 not_useful=0 recent_score_30d=0", decayed_summary)
            for _ in range(3):
                run(
                    "autonomy",
                    "recommendation-feedback",
                    "--label",
                    "daily_nudge_owner",
                    "--command",
                    "myos next-action",
                    "--useful",
                    "no",
                    "--note",
                    "Owner nudges were not useful for this daily surface.",
                )
            negative_signal = run("next-action", "--meeting-hours", "7")
            self.assertIn('[label=daily_reduce_risk command="myos next-action"]', negative_signal)
            self.assertIn(
                "ranking context: feedback adjusted selection from daily_nudge_owner to daily_reduce_risk (selected_score=+0 baseline_score=-3)",
                negative_signal,
            )
            self.assertNotIn("Owner nudges were not useful for this daily surface.", negative_signal)
            negative_summary = run("autonomy", "recommendations", "--limit", "1")
            self.assertIn("surface=daily label=daily_nudge_owner command=myos next-action", negative_summary)
            self.assertIn("score=-3 useful=0 not_useful=3 recent_score_30d=-3", negative_summary)
            self.assertNotIn("Owner nudges were not useful for this daily surface.", negative_summary)
            run(
                "autonomy",
                "recommendation-feedback",
                "--label",
                "daily_nudge_owner",
                "--command",
                "myos next-action",
                "--useful",
                "yes",
                "--note",
                "Owner nudges had one useful counter-signal.",
            )
            mixed_summary = run("autonomy", "recommendations", "--limit", "1")
            self.assertIn("mixed_recent=yes", mixed_summary)
            self.assertNotIn("Owner nudges had one useful counter-signal.", mixed_summary)

    def test_external_items_can_sync_into_intent_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("intent", "create", "Resolve dashboard escalation")
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                INSERT INTO external_items (connector, external_id, item_type, title, body, url)
                VALUES ('jira', 'PROJ-1', 'issue', 'Dashboard escalation is blocked', 'Owner needs mitigation.', 'https://example.test/browse/PROJ-1')
                """
            )
            conn.commit()
            conn.close()

            out = run("evidence", "sync-external", "--intent", "1", "--connector", "jira")
            self.assertIn("Attached 1 external evidence item", out)
            repeated = run("evidence", "sync-external", "--intent", "1", "--connector", "jira")
            self.assertIn("Attached 0 external evidence item", repeated)

            shown = run("intent", "show", "--id", "1")
            self.assertIn("source=external_item:1", shown)
            self.assertIn("Dashboard escalation is blocked", shown)

    def test_hardening_commands_report_dependency_and_performance_baselines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            dep = run("dependency-check", "--strict")
            self.assertIn("Dependency and license check:", dep)
            self.assertIn("PASS license_metadata", dep)
            perf = run("performance-baseline", "--query", "dashboard launch")
            self.assertIn("Performance baseline:", perf)
            self.assertIn("retrieval_ms=", perf)
            self.assertIn("readiness_query_ms=", perf)
            release = run("release-check", "--strict")
            self.assertIn("Release readiness check:", release)
            self.assertIn("PASS schema", release)
            self.assertIn("PASS factory_smoke", release)
            self.assertIn("docs, changelog, license, workflows", release)
            self.assertIn("PASS public_hygiene", release)

    def test_setup_live_dry_run_and_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            data_dir = Path(tmp) / "data"
            env_file = data_dir / ".env.myos"
            db_path = data_dir / "assistant.db"
            watch_dir = data_dir / "inbox"

            dry = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "personal_assistant.cli",
                    "setup-live",
                    "--data-dir",
                    str(data_dir),
                    "--env-file",
                    str(env_file),
                    "--db-path",
                    str(db_path),
                    "--watch-dir",
                    str(watch_dir),
                ],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Dry run only", dry.stdout)
            self.assertFalse(env_file.exists())

            pre_check = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "personal_assistant.cli",
                    "setup-live",
                    "--check",
                    "--data-dir",
                    str(data_dir),
                    "--env-file",
                    str(env_file),
                    "--db-path",
                    str(db_path),
                    "--watch-dir",
                    str(watch_dir),
                ],
                cwd=Path.cwd(),
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(pre_check.returncode, 0)
            self.assertIn("Live readiness check", pre_check.stdout)
            self.assertIn("WARN env_file", pre_check.stdout)
            self.assertFalse(env_file.exists())

            applied = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "personal_assistant.cli",
                    "setup-live",
                    "--apply",
                    "--data-dir",
                    str(data_dir),
                    "--env-file",
                    str(env_file),
                    "--db-path",
                    str(db_path),
                    "--watch-dir",
                    str(watch_dir),
                ],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Setup complete", applied.stdout)
            self.assertTrue(env_file.exists())
            self.assertTrue(watch_dir.exists())
            self.assertIn("MYOS_ACTION_COMMAND=myos action-provider", env_file.read_text())
            self.assertEqual(oct(env_file.stat().st_mode & 0o777), "0o600")
            conn = sqlite3.connect(db_path)
            watch_count = conn.execute("SELECT COUNT(*) FROM assistant_watch_dirs").fetchone()[0]
            goal_count = conn.execute("SELECT COUNT(*) FROM assistant_goals").fetchone()[0]
            conn.close()
            self.assertEqual(watch_count, 1)
            self.assertGreaterEqual(goal_count, 2)

            post_check = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "personal_assistant.cli",
                    "setup-live",
                    "--check",
                    "--data-dir",
                    str(data_dir),
                    "--env-file",
                    str(env_file),
                    "--db-path",
                    str(db_path),
                    "--watch-dir",
                    str(watch_dir),
                ],
                cwd=Path.cwd(),
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(post_check.returncode, 0)
            self.assertIn("PASS env_file", post_check.stdout)
            self.assertIn("PASS action_provider", post_check.stdout)
            self.assertIn("PASS standing_goals", post_check.stdout)
            self.assertIn("INFO jira_credentials", post_check.stdout)

            env_file.write_text(
                "\n".join(
                    [
                        f"export MYOS_DB_PATH={db_path}",
                        "export JIRA_BASE_URL=https://jira.example",
                        "export JIRA_USER_EMAIL=me@example.com",
                        "export JIRA_API_TOKEN=token",
                        "export GITHUB_TOKEN=token",
                        "export GITHUB_OWNER=owner",
                        "export GITHUB_REPO=repo",
                        "export CONFLUENCE_BASE_URL=https://confluence.example",
                        "export CONFLUENCE_USER_EMAIL=me@example.com",
                        "export CONFLUENCE_API_TOKEN=token",
                        "export AHA_BASE_URL=https://aha.example",
                        "export AHA_API_TOKEN=token",
                        "export MYOS_ACTION_COMMAND=myos action-provider",
                    ]
                )
                + "\n"
            )
            ready_check = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "personal_assistant.cli",
                    "setup-live",
                    "--check",
                    "--data-dir",
                    str(data_dir),
                    "--env-file",
                    str(env_file),
                    "--watch-dir",
                    str(watch_dir),
                ],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Ready: myos autopilot", ready_check.stdout)
            self.assertIn("INFO autopilot_smoke", ready_check.stdout)

    def test_setup_live_resolves_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "personal_assistant.cli",
                    "setup-live",
                    "--apply",
                    "--data-dir",
                    "live-data",
                    "--env-file",
                    "live-data/.env.myos",
                    "--db-path",
                    "live-data/assistant.db",
                    "--watch-dir",
                    "live-data/inbox",
                ],
                cwd=tmp,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            env_file = Path(tmp) / "live-data" / ".env.myos"
            db_path = Path(tmp) / "live-data" / "assistant.db"
            self.assertIn(f"MYOS_DB_PATH={db_path.resolve()}", env_file.read_text())
            conn = sqlite3.connect(db_path)
            watch_path = conn.execute("SELECT path FROM assistant_watch_dirs LIMIT 1").fetchone()[0]
            conn.close()
            self.assertEqual(watch_path, str((Path(tmp) / "live-data" / "inbox").resolve()))

    def test_inbox_process_idempotent_for_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("transcribe", "/tmp/fake.m4a", "--text", "Decision: canary rollout. Follow up by Friday.")
            run("inbox-process")
            conn = sqlite3.connect(db_path)
            first_count = conn.execute("SELECT COUNT(*) FROM inbox_items").fetchone()[0]
            conn.close()
            run("inbox-process")
            conn = sqlite3.connect(db_path)
            second_count = conn.execute("SELECT COUNT(*) FROM inbox_items").fetchone()[0]
            conn.close()
            self.assertEqual(first_count, second_count)

    def test_metrics_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            out = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "metrics", "--days", "3"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("KPI snapshot", out.stdout)

    def test_launchd_dry_run_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            out_install = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "launchd-install"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            out_uninstall = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "launchd-uninstall"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Dry run only", out_install.stdout)
            self.assertIn("env file for pulse", out_install.stdout)
            self.assertIn("Dry run only", out_uninstall.stdout)

            out_autopilot = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "launchd-install", "--autopilot"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("com.myos.autopilot.plist", out_autopilot.stdout)

    def test_review_evidence_and_weekly_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("capture", "Follow up with product on roadmap")
            run("triage")
            run("resolve-commitment", "--item", "1", "--outcome", "completed_on_time")
            run(
                "log-evidence",
                "--person",
                "self",
                "--category",
                "leadership",
                "--impact",
                "Unblocked roadmap decision with product and engineering alignment",
            )
            ev = run("review-evidence", "--person", "self")
            wk = run("weekly-review", "--days", "7")
            self.assertIn("Review evidence", ev)
            self.assertIn("Weekly review", wk)

    def test_go_live_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            cfg = Path(tmp) / ".env.myos"
            cfg.write_text("JIRA_BASE_URL=\nGITHUB_TOKEN=\n")
            out = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "go-live", "--env-file", str(cfg)],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Go-live summary", out.stdout)

    def test_activate_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            cfg = Path(tmp) / ".env.myos"
            cfg.write_text("")
            out = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "activate", "--env-file", str(cfg)],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Activation flow", out.stdout)
            self.assertIn("Go-live summary", out.stdout)

    def test_dashboard_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            output_html = Path(tmp) / "dashboard.html"
            out = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "personal_assistant.cli",
                    "dashboard",
                    "--once",
                    "--output-html",
                    str(output_html),
                ],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Dashboard snapshot written", out.stdout)
            self.assertTrue(output_html.exists())

    def test_sanity_and_runbook_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            sanity = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "sanity"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            runbook = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "runbook", "--short"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Sanity check", sanity.stdout)
            self.assertIn("MYOS Operational Runbook", runbook.stdout)
            self.assertIn("myos setup-live --check", runbook.stdout)
            self.assertIn("myos doctor --strict && myos migrations verify --strict", runbook.stdout)
            self.assertIn("myos backup", runbook.stdout)
            self.assertIn("myos approve --list", runbook.stdout)
            self.assertIn("myos execution-receipt list", runbook.stdout)

    def test_launchd_status_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            out = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "launchd-status"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Launchd status", out.stdout)

    def test_start_and_stop_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            cfg = Path(tmp) / ".env.myos"
            cfg.write_text("")
            start = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "start", "--env-file", str(cfg)],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            stop = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "stop"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Starting MYOS runtime", start.stdout)
            self.assertIn("Stopping MYOS runtime", stop.stdout)

    def test_start_with_install_launchd_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            cfg = Path(tmp) / ".env.myos"
            cfg.write_text("")
            out = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "start", "--env-file", str(cfg), "--install-launchd"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Launchd plan", out.stdout)

    def test_cleanup_and_renegotiate_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("capture", "Follow up with architecture council by tomorrow", "--due", "2026-06-24")
            run("triage")
            reneg = run("renegotiate", "--days-ahead", "2")
            self.assertIn("Renegotiation candidates", reneg)
            clean = run("cleanup", "--days", "0", "--limit", "10")
            self.assertIn("Cleanup complete", clean)

    def test_next_action_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("capture", "Blocker: dependency on infra rollback plan", "--due", "2026-06-24")
            run("triage")
            out = run("next-action", "--meeting-hours", "5")
            self.assertIn("Next action recommendation", out)

    def test_snapshot_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("capture", "Decision: approve phased rollout")
            run("triage")
            output_json = Path(tmp) / "snapshot.json"
            out = run("snapshot", "--output", str(output_json))
            self.assertIn("Snapshot written", out)
            self.assertTrue(output_json.exists())
            text = output_json.read_text()
            self.assertIn('"counts"', text)
            self.assertIn('"connectors"', text)

    def test_orchestrate_and_workflow_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")

            out1 = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "personal_assistant.cli",
                    "orchestrate",
                    "--workflow",
                    "daily",
                    "--connector",
                    "all",
                ],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            out2 = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "workflow-runs", "--limit", "5"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Workflow complete", out1.stdout)
            self.assertIn("Workflow runs", out2.stdout)

    def test_simple_shortcuts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            cfg = Path(tmp) / ".env.myos"
            cfg.write_text("")

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("capture", "Decision: simplify commands")
            run("triage")
            self.assertIn("Running day pipeline", run("morning", "--env-file", str(cfg)))
            self.assertIn("Next action recommendation", run("now"))
            self.assertIn("Day closed.", run("end"))
            self.assertIn("Workflow complete", run("weekly"))
            self.assertIn("Activation flow", run("live", "--env-file", str(cfg)))
            self.assertIn("Sanity check", run("health"))

    def test_policy_and_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            self.assertIn("Policy settings", run("policy"))
            self.assertIn("Policy updated", run("policy", "--set", "redact_emails=1"))
            run("transcribe", "/tmp/fake.m4a", "--text", "email me at test@example.com and call +1 415-555-1212")
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT transcript_text FROM media_assets ORDER BY id DESC LIMIT 1").fetchone()
            conn.close()
            assert row is not None
            self.assertIn("[REDACTED_EMAIL]", row[0])
            self.assertIn("[REDACTED_PHONE]", row[0])

    def test_run_day_uses_env_file_db_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            cfg = Path(tmp) / ".env.myos"
            db_path = Path(tmp) / "alt_assistant.db"
            cfg.write_text(f"MYOS_DB_PATH={db_path}\n")
            out = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "personal_assistant.cli",
                    "run-day",
                    "--env-file",
                    str(cfg),
                    "--connector",
                    "all",
                ],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Running day pipeline", out.stdout)
            self.assertTrue(db_path.exists())

    def test_queue_worker_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")

            add = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "queue-add", "--workflow", "weekly"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            run = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "worker", "--limit", "1"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Queued workflow job", add.stdout)
            self.assertIn("Worker completed job", run.stdout)

    def test_cutover_check_and_uat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            cut = run("cutover-check")
            self.assertIn("Cutover readiness", cut)
            run("capture", "Decision: simplify command set")
            run("triage")
            uat = run("uat", "--days", "7")
            self.assertIn("UAT quality snapshot", uat)

    def test_tune_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("capture", "Blocker: production deploy risk")
            run("capture", "Task: update docs")
            run("triage")
            out = run("tune", "--days", "14", "--apply-policy")
            self.assertIn("Tuning recommendations", out)
            self.assertIn("Applied recommendations", out)

    def test_autonomous_assistant_core_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("capture", "Blocker: dependency on API platform for launch")
            run("triage")
            delegated = run(
                "delegate",
                "Handle blocked launch dependency and draft Jira update",
                "--context",
                "Need owner confirmation and timeline renegotiation",
            )
            self.assertIn("Delegated task #1", delegated)
            self.assertIn("Proposed actions", delegated)
            listed = run("act", "--task", "1", "--list")
            self.assertIn("Agent actions", listed)
            executed = run("act", "--action", "1", "--execute")
            self.assertIn("Executed action #1", executed)
            learned = run("learn", "--task", "1", "--outcome", "success", "--notes", "Owner confirmed reduced scope")
            self.assertIn("Learned from task #1", learned)
            coach = run("coach", "blocked launch dependency")
            self.assertIn("Assistant coach", coach)
            status = run("agent-status", "--task", "1")
            self.assertIn("Agent task #1", status)
            conn = sqlite3.connect(db_path)
            action_count = conn.execute("SELECT COUNT(*) FROM agent_actions").fetchone()[0]
            obs_count = conn.execute("SELECT COUNT(*) FROM agent_observations").fetchone()[0]
            conn.close()
            self.assertGreaterEqual(action_count, 2)
            self.assertGreaterEqual(obs_count, 1)

    def test_delegate_uses_ai_command_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)
            ai_script = Path(tmp) / "fake_ai.py"
            request_path = Path(tmp) / "ai_request.json"
            ai_script.write_text(
                "import json, sys\n"
                f"request_path = {str(request_path)!r}\n"
                "payload = json.load(sys.stdin)\n"
                "open(request_path, 'w').write(json.dumps(payload))\n"
                "print(json.dumps({\n"
                "  'plan': [{'step': 'ai_prioritize', 'detail': 'AI selected the key next move.'}],\n"
                "  'actions': [{'action_type': 'draft_message', 'title': 'AI draft update', 'payload': {'draft': 'AI drafted response'}, 'requires_approval': 'false'}]\n"
                "}))\n"
            )
            env["MYOS_AI_COMMAND"] = f"{sys.executable} {ai_script}"
            env["MYOS_AI_PROVIDER"] = "fake-ai"

            subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "capture", "Launch risk contact test@example.com"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "triage"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            out = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "delegate", "Launch risk update"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("ai_prioritize", out.stdout)
            self.assertIn("AI draft update", out.stdout)
            conn = sqlite3.connect(db_path)
            provider = conn.execute("SELECT provider FROM agent_runs LIMIT 1").fetchone()[0]
            call_status = conn.execute("SELECT status FROM ai_provider_calls LIMIT 1").fetchone()[0]
            requires_approval = conn.execute("SELECT requires_approval FROM agent_actions LIMIT 1").fetchone()[0]
            conn.close()
            self.assertEqual(provider, "fake-ai")
            self.assertEqual(call_status, "ok")
            self.assertEqual(requires_approval, 1)
            self.assertIn("[REDACTED_EMAIL]", request_path.read_text())
            self.assertNotIn("test@example.com", request_path.read_text())

    def test_autopilot_once_and_approval_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)
            digest_dir = Path(tmp) / "digests"
            notify_path = Path(tmp) / "notify.json"
            notify_script = Path(tmp) / "notify.py"
            notify_script.write_text(f"import sys\nopen({str(notify_path)!r}, 'w').write(sys.stdin.read())\n")
            env["MYOS_NOTIFY_COMMAND"] = f"{sys.executable} {notify_script}"

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("capture", "Blocker: production launch dependency on test@example.com", "--due", "2026-06-25")
            out = run("autopilot", "--once", "--no-sync", "--digest-dir", str(digest_dir))
            self.assertIn("Autopilot cycle complete", out)
            self.assertIn("digest_id=", out)
            self.assertIn("Run: myos approve --list [label=review_approvals]", out)
            approvals = run("approve", "--list")
            self.assertIn("Approval queue", approvals)
            self.assertIn("preview:", approvals)
            status = run("autopilot-status")
            self.assertIn("Autopilot runs", status)
            self.assertIn("approvals_pending", status)

            # Same-data assertion for autopilot-status --json: after the
            # autopilot --once run, the JSON envelope must expose at least
            # one autopilot run row and the aggregate state a supervising
            # process needs (open agent tasks, approvals pending).
            autopilot_json_out = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "autopilot-status", "--json"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            autopilot_payload = json.loads(autopilot_json_out)
            self.assertEqual(autopilot_payload["schema"], "myos.autopilot_status.v1")
            self.assertGreaterEqual(autopilot_payload["count"], 1)
            self.assertIn("state", autopilot_payload)
            self.assertIn("open_agent_tasks", autopilot_payload["state"])
            self.assertIn("approvals_pending", autopilot_payload["state"])
            self.assertGreaterEqual(autopilot_payload["state"]["approvals_pending"], 1)
            digest = run("digest")
            self.assertIn("What I handled", digest)
            self.assertTrue((digest_dir / "latest.md").exists())
            self.assertTrue(notify_path.exists())
            self.assertIn("Autopilot digest", notify_path.read_text())
            self.assertIn("[REDACTED_EMAIL]", notify_path.read_text())
            self.assertNotIn("test@example.com", notify_path.read_text())

            conn = sqlite3.connect(db_path)
            task_count = conn.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0]
            digest_count = conn.execute("SELECT COUNT(*) FROM assistant_digests").fetchone()[0]
            safe_executed = conn.execute(
                "SELECT COUNT(*) FROM agent_actions WHERE requires_approval=0 AND status='executed'"
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM agent_actions WHERE requires_approval=1 AND status='proposed'"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(task_count, 1)
            self.assertEqual(digest_count, 1)
            self.assertGreaterEqual(safe_executed, 1)
            self.assertGreaterEqual(pending, 1)

    def test_autopilot_does_not_execute_manual_safe_actions_or_churn_backlog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("delegate", "Manual assistant task that should wait")
            run("autopilot", "--once", "--no-sync", "--no-process")
            conn = sqlite3.connect(db_path)
            manual_safe_status = conn.execute(
                "SELECT status FROM agent_actions WHERE agent_task_id=1 AND requires_approval=0"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(manual_safe_status, "proposed")

            run("capture", "Loose note that remains in inbox")
            run("autopilot", "--once", "--no-sync", "--no-process")
            run("autopilot", "--once", "--no-sync", "--no-process")
            conn = sqlite3.connect(db_path)
            backlog_signals = conn.execute(
                "SELECT COUNT(*) FROM autopilot_signals WHERE signal_key='inbox:new:backlog'"
            ).fetchone()[0]
            task_count = conn.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0]
            conn.close()
            self.assertEqual(backlog_signals, 1)
            self.assertEqual(task_count, 2)

    def test_standing_goal_autopilot_and_action_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)
            action_request = Path(tmp) / "action_request.json"
            action_script = Path(tmp) / "action_provider.py"
            action_script.write_text(
                "import json, sys\n"
                f"request_path = {str(action_request)!r}\n"
                "payload = json.load(sys.stdin)\n"
                "open(request_path, 'w').write(json.dumps(payload))\n"
                "print(json.dumps({'ok': True, 'target': payload.get('action_type')}))\n"
            )
            env["MYOS_ACTION_COMMAND"] = f"{sys.executable} {action_script}"
            env["MYOS_ACTION_PROVIDER"] = "fake-action"

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            added = run(
                "goal",
                "add",
                "Keep Jira launch updates current",
                "--context",
                "Draft status updates when launch risk changes",
                "--cadence-minutes",
                "0",
            )
            self.assertIn("Added assistant goal", added)
            auto = run("autopilot", "--once", "--no-sync", "--no-process")
            self.assertIn("tasks_created=1", auto)
            goals = run("goal", "list")
            self.assertIn("last=", goals)
            approval = run("approve", "--list")
            self.assertIn("draft_external_update", approval)

            conn = sqlite3.connect(db_path)
            action_id = conn.execute(
                "SELECT id FROM agent_actions WHERE action_type='draft_external_update' LIMIT 1"
            ).fetchone()[0]
            conn.close()
            executed = run("approve", "--action", str(action_id), "--execute")
            self.assertIn("Executed action", executed)
            self.assertTrue(action_request.exists())
            conn = sqlite3.connect(db_path)
            exec_count = conn.execute("SELECT COUNT(*) FROM action_provider_executions WHERE status='ok'").fetchone()[0]
            receipt = conn.execute(
                """
                SELECT final_status, approved, follow_up_required, rollback_note
                FROM action_execution_receipts
                WHERE agent_action_id = ?
                """,
                (action_id,),
            ).fetchone()
            review_count_before = conn.execute("SELECT COUNT(*) FROM assistant_self_reviews").fetchone()[0]
            conn.close()
            self.assertEqual(exec_count, 1)
            self.assertEqual(receipt[0], "executed")
            self.assertEqual(receipt[1], 1)
            self.assertEqual(receipt[2], 0)
            self.assertIn("rollback", receipt[3].lower())
            self.assertEqual(review_count_before, 0)
            receipt_list = run("execution-receipt", "list")
            self.assertIn("Execution receipts:", receipt_list)
            self.assertIn("status=executed", receipt_list)
            receipt_show = run("execution-receipt", "show", "--id", "1")
            self.assertIn("Execution receipt #1", receipt_show)
            self.assertIn("Follow-up required: False", receipt_show)
            review = run("self-review")
            self.assertIn("Autonomy self-review", review)
            conn = sqlite3.connect(db_path)
            review_count_after = conn.execute("SELECT COUNT(*) FROM assistant_self_reviews").fetchone()[0]
            conn.close()
            self.assertEqual(review_count_after, 1)

    def test_failed_action_provider_can_be_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)
            fail_script = Path(tmp) / "fail_action.py"
            ok_script = Path(tmp) / "ok_action.py"
            fail_script.write_text("import sys\nprint('temporary failure', file=sys.stderr)\nsys.exit(2)\n")
            ok_script.write_text("import json, sys\njson.load(sys.stdin)\nprint('{\"ok\": true}')\n")

            def run(*args: str, check: bool = True, extra_env: dict[str, str] | None = None):
                merged = env.copy()
                if extra_env:
                    merged.update(extra_env)
                return subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=merged,
                    check=check,
                    capture_output=True,
                    text=True,
                )

            run("delegate", "Draft Jira update for launch")
            conn = sqlite3.connect(db_path)
            action_id = conn.execute(
                "SELECT id FROM agent_actions WHERE action_type='draft_external_update' LIMIT 1"
            ).fetchone()[0]
            conn.close()

            failed = run(
                "approve",
                "--action",
                str(action_id),
                "--execute",
                check=False,
                extra_env={"MYOS_ACTION_COMMAND": f"{sys.executable} {fail_script}"},
            )
            self.assertNotEqual(failed.returncode, 0)
            conn = sqlite3.connect(db_path)
            failed_receipt = conn.execute(
                """
                SELECT final_status, follow_up_required, follow_up_inbox_id
                FROM action_execution_receipts
                WHERE agent_action_id = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (action_id,),
            ).fetchone()
            follow_up_text = conn.execute(
                "SELECT text FROM inbox_items WHERE id = ?",
                (failed_receipt[2],),
            ).fetchone()[0]
            conn.close()
            self.assertEqual(failed_receipt[0], "failed")
            self.assertEqual(failed_receipt[1], 1)
            self.assertIsNotNone(failed_receipt[2])
            self.assertIn("Follow up on failed action", follow_up_text)

            failed_again = run(
                "approve",
                "--action",
                str(action_id),
                "--execute",
                check=False,
                extra_env={"MYOS_ACTION_COMMAND": f"{sys.executable} {fail_script}"},
            )
            self.assertNotEqual(failed_again.returncode, 0)
            conn = sqlite3.connect(db_path)
            receipt_rows = conn.execute(
                """
                SELECT final_status, follow_up_inbox_id
                FROM action_execution_receipts
                WHERE agent_action_id = ?
                ORDER BY id ASC
                """,
                (action_id,),
            ).fetchall()
            follow_up_count = conn.execute(
                "SELECT COUNT(*) FROM inbox_items WHERE source = 'action_receipt'"
            ).fetchone()[0]
            conn.close()
            self.assertEqual([row[0] for row in receipt_rows], ["failed", "failed"])
            self.assertEqual(receipt_rows[0][1], receipt_rows[1][1])
            self.assertEqual(follow_up_count, 1)

            retried = run(
                "approve",
                "--action",
                str(action_id),
                "--execute",
                "--limit",
                "20",
                check=True,
                extra_env={"MYOS_ACTION_COMMAND": f"{sys.executable} {ok_script}"},
            )
            self.assertIn("Executed action", retried.stdout)
            receipts = run("execution-receipt", "list")
            self.assertIn("status=failed", receipts.stdout)
            self.assertIn("follow_up=#", receipts.stdout)
            self.assertIn("status=executed", receipts.stdout)

    def test_builtin_action_provider_dry_run_and_execute_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)
            request = {
                "action_id": 42,
                "agent_task_id": 7,
                "action_type": "draft_external_update",
                "title": "Post Jira launch update",
                "payload": {
                    "target": "jira",
                    "issue_key": "ABC-123",
                    "draft": "Launch update for test@example.com",
                },
                "safety": {"approved": False, "requires_approval": True},
            }
            dry = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "action-provider"],
                cwd=Path.cwd(),
                env=env,
                input=json.dumps(request),
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn('"status": "drafted"', dry.stdout)
            self.assertIn('"target": "jira:ABC-123"', dry.stdout)
            conn = sqlite3.connect(db_path)
            outbox = conn.execute("SELECT body, status, payload_json FROM action_outbox LIMIT 1").fetchone()
            conn.close()
            self.assertEqual(outbox[1], "drafted")
            self.assertIn("[REDACTED_EMAIL]", outbox[0])
            self.assertIn("[REDACTED_EMAIL]", outbox[2])
            self.assertNotIn("test@example.com", outbox[2])
            self.assertTrue((db_path.parent / "outbox").exists())

            blocked = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "action-provider", "--execute"],
                cwd=Path.cwd(),
                env=env,
                input=json.dumps(request),
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("approved action required", blocked.stdout)

    def test_watch_dir_scan_and_autopilot_ingest_files_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)
            watched = Path(tmp) / "watched"
            watched.mkdir()
            (watched / "meeting.md").write_text(
                "Decision: launch stays on Friday. Follow up with test@example.com by tomorrow. "
                "Risk: platform dependency."
            )

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            self.assertIn("Watching directory", run("watch-dir", "add", str(watched), "--label", "meetings"))
            self.assertIn("Watch directories", run("watch-dir", "list"))
            first = run("watch-scan")
            second = run("watch-scan")
            self.assertIn("files_ingested=1", first)
            self.assertIn("files_ingested=0", second)

            conn = sqlite3.connect(db_path)
            media_text = conn.execute("SELECT transcript_text FROM media_assets LIMIT 1").fetchone()[0]
            inbox_count = conn.execute("SELECT COUNT(*) FROM inbox_items").fetchone()[0]
            ingest_count = conn.execute("SELECT COUNT(*) FROM file_ingests").fetchone()[0]
            conn.close()
            self.assertIn("[REDACTED_EMAIL]", media_text)
            self.assertNotIn("test@example.com", media_text)
            self.assertGreaterEqual(inbox_count, 2)
            self.assertEqual(ingest_count, 1)

            auto = run("autopilot", "--once", "--no-sync", "--watch-limit", "5")
            self.assertIn("watched_files=0", auto)
            conn = sqlite3.connect(db_path)
            work_count = conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
            conn.close()
            self.assertGreaterEqual(work_count, 2)

    def test_autopilot_watch_dir_triages_in_first_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)
            watched = Path(tmp) / "watched"
            watched.mkdir()
            (watched / "standup.txt").write_text(
                "Decision: keep rollout staged. Task: implement rollback checklist. Risk: API dependency."
            )

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            run("watch-dir", "add", str(watched))
            out = run("autopilot", "--once", "--no-sync", "--watch-limit", "5")
            self.assertIn("watched_files=1", out)
            conn = sqlite3.connect(db_path)
            work_count = conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
            processing_count = conn.execute("SELECT COUNT(*) FROM file_ingests WHERE status='processing'").fetchone()[0]
            conn.close()
            self.assertEqual(work_count, 3)
            self.assertEqual(processing_count, 0)

    def test_autonomy_run_ledger_cli_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            ledger_help = run("loop", "ledger", "--help")
            self.assertIn("read-only audit trail", ledger_help)
            self.assertIn("Filter by goal, task, or status", ledger_help)
            self.assertIn("pending approval rows point to myos approve --list", ledger_help)
            self.assertIn("myos loop ledger --status waiting_approval", ledger_help)
            self.assertIn("myos loop ledger --status skipped --goal 1", ledger_help)
            self.assertIn("completed", ledger_help)
            self.assertIn("noop", ledger_help)
            self.assertIn("waiting_approval", ledger_help)

            empty_unfiltered = run("loop", "ledger")
            self.assertIn("No autonomy ledger entries found.", empty_unfiltered)
            self.assertNotIn("- filters:", empty_unfiltered)

            run(
                "goal",
                "add",
                "Handle Jira risk",
                "--context",
                "Needs approval-gated follow-up",
                "--cadence-minutes",
                "1",
                "--priority",
                "1",
            )
            run("loop", "run-goal", "--goal", "1")
            run("loop", "run-goal", "--goal", "1")

            ledger = run("loop", "ledger", "--goal", "1")
            self.assertIn("Autonomy run ledger:", ledger)
            self.assertIn("decision=goal_skipped status=skipped goal=#1 task=#1", ledger)
            self.assertIn("decision=goal_started status=waiting_approval goal=#1 task=#1 run=#1", ledger)
            self.assertIn("decision=loop_started status=waiting_approval goal=#1 task=#1 run=#1", ledger)

            skipped = run("loop", "ledger", "--status", "skipped")
            self.assertIn("decision=goal_skipped status=skipped", skipped)
            self.assertNotIn("decision=goal_started", skipped)
            empty_filtered = run("loop", "ledger", "--status", "completed", "--goal", "1")
            self.assertIn("No autonomy ledger entries found.", empty_filtered)
            self.assertIn("- filters: goal=#1, status=completed", empty_filtered)
            invalid_status = subprocess.run(
                [sys.executable, "-m", "personal_assistant.cli", "loop", "ledger", "--status", "unknown"],
                cwd=Path.cwd(),
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(invalid_status.returncode, 0)
            self.assertIn("invalid choice", invalid_status.stderr)

            task_rows = run("loop", "ledger", "--task", "1")
            self.assertIn("pending=2", task_rows)
            self.assertIn("Recommendation: myos approve --list [label=review_approvals]", task_rows)

            conn = sqlite3.connect(db_path)
            ledger_count = conn.execute("SELECT COUNT(*) FROM autonomy_run_ledger").fetchone()[0]
            schema_version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            conn.close()
            self.assertEqual(ledger_count, 3)
            self.assertEqual(schema_version, 37)

    def test_autopilot_loop_goal_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str, check: bool = True):
                return subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=check,
                    capture_output=True,
                    text=True,
                )

            rejected = run("autopilot", "--loop-goal", check=False)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("one-shot only", rejected.stdout)
            goals_help = run("loop", "goals", "--help")
            self.assertIn("print the next handoff command", goals_help.stdout)
            self.assertIn("Feedback labels mark run-goal and goal-review handoffs", goals_help.stdout)
            self.assertIn("review standing goals with myos goal list", goals_help.stdout)
            self.assertIn("myos loop run-goal --goal 1", goals_help.stdout)
            run_goal_help = run("loop", "run-goal", "--help")
            self.assertIn("Start or resume exactly one eligible goal loop", run_goal_help.stdout)
            self.assertIn("Pending approvals remain gated", run_goal_help.stdout)
            no_goals = run("loop", "goals")
            self.assertIn("No eligible assistant goals are due.", no_goals.stdout)
            self.assertIn(
                "Recommendation: review assistant goals -> myos goal list [label=review_goals]", no_goals.stdout
            )
            no_goal_cycle = run("loop", "run-goal")
            self.assertIn("Goal scheduler: action=noop", no_goal_cycle.stdout)
            self.assertIn(
                "Recommendation: review assistant goals -> myos goal list [label=review_goals]", no_goal_cycle.stdout
            )
            no_goal_autopilot = run("autopilot", "--once", "--loop-goal")
            self.assertIn("Autopilot goal wrapper complete", no_goal_autopilot.stdout)
            self.assertIn("Goal scheduler: action=noop", no_goal_autopilot.stdout)
            self.assertIn(
                "Recommendation: review assistant goals -> myos goal list [label=review_goals]",
                no_goal_autopilot.stdout,
            )
            self.assertIn("Ledger: myos loop ledger --limit 1", no_goal_autopilot.stdout)

            added = run(
                "goal",
                "add",
                "Handle Jira risk",
                "--context",
                "Needs approval-gated follow-up",
                "--cadence-minutes",
                "1",
                "--priority",
                "1",
            )
            self.assertIn("Added assistant goal #1", added.stdout)
            goals = run("loop", "goals")
            self.assertIn("Eligible autonomy goals:", goals.stdout)
            self.assertIn("Recommendation: myos loop run-goal --goal 1 [label=run_goal_cycle]", goals.stdout)

            first = run("autopilot", "--once", "--loop-goal", "--loop-goal-id", "1")
            self.assertIn("Autopilot goal wrapper complete", first.stdout)
            self.assertIn("Goal scheduler: action=started goal=#1", first.stdout)
            self.assertIn("myos approve --list [label=review_approvals]", first.stdout)
            self.assertIn("Ledger: myos loop ledger --limit 1", first.stdout)

            second = run("autopilot", "--once", "--loop-goal", "--loop-goal-id", "1")
            self.assertIn("Goal scheduler: action=skipped goal=#1", second.stdout)
            self.assertIn("myos approve --list [label=review_approvals]", second.stdout)

            conn = sqlite3.connect(db_path)
            pending = conn.execute(
                """
                SELECT COUNT(*)
                FROM agent_actions
                WHERE agent_task_id=1 AND status='proposed' AND requires_approval=1
                """
            ).fetchone()[0]
            autopilot_runs = conn.execute("SELECT COUNT(*) FROM autopilot_runs WHERE mode='loop_goal'").fetchone()[0]
            skipped = conn.execute(
                "SELECT COUNT(*) FROM autonomy_run_ledger WHERE decision_type='goal_skipped'"
            ).fetchone()[0]
            event_count = conn.execute(
                "SELECT COUNT(*) FROM event_log WHERE event_type='autopilot_loop_goal'"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(pending, 2)
            self.assertEqual(autopilot_runs, 3)
            self.assertEqual(skipped, 1)
            self.assertEqual(event_count, 3)

    def test_recommendation_feedback_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    [sys.executable, "-m", "personal_assistant.cli", *args],
                    cwd=Path.cwd(),
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return out.stdout

            recorded = run(
                "autonomy",
                "recommendation-feedback",
                "--label",
                "inspect_recent_traces",
                "--command",
                "myos trace list",
                "--decision",
                "blocked",
                "--useful",
                "yes",
                "--note",
                "Helpful next step",
            )
            self.assertIn("Recommendation feedback recorded", recorded)
            self.assertIn("raw recommendation feedback text was not stored", recorded)
            summary = run("autonomy", "recommendations")
            self.assertIn("Recommendation feedback summary:", summary)
            self.assertIn("label=inspect_recent_traces command=myos trace list score=1", summary)
            self.assertIn("surface=general", summary)
            self.assertIn("side_effects=none", summary)
            self.assertNotIn("Helpful next step", summary)
            self.assertNotIn("note_hash", summary)
            self.assertNotIn("note_length", summary)

            goal_run_feedback = run(
                "autonomy",
                "recommendation-feedback",
                "--label",
                "run_goal_cycle",
                "--command",
                "myos loop run-goal --goal 1",
                "--useful",
                "yes",
                "--note",
                "Run goal was the right scheduler handoff.",
            )
            self.assertIn("Recommendation feedback recorded", goal_run_feedback)
            goal_review_feedback = run(
                "autonomy",
                "recommendation-feedback",
                "--label",
                "review_goals",
                "--command",
                "myos goal list",
                "--useful",
                "no",
                "--note",
                "Goal review was not useful right now.",
            )
            self.assertIn("Recommendation feedback recorded", goal_review_feedback)
            goal_summary = run("autonomy", "recommendations")
            self.assertIn("label=run_goal_cycle command=myos loop run-goal --goal 1 score=1", goal_summary)
            self.assertIn("label=review_goals command=myos goal list score=-1", goal_summary)
            self.assertIn("side_effects=local_db_write", goal_summary)
            self.assertIn("surface=goal_scheduler", goal_summary)
            self.assertNotIn("Run goal was the right scheduler handoff.", goal_summary)
            self.assertNotIn("Goal review was not useful right now.", goal_summary)

            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT note_hash, note_length FROM recommendation_feedback LIMIT 1").fetchone()
            schema_version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            raw = "\n".join(str(value) for value in row)
            conn.close()
            self.assertTrue(row[0])
            self.assertEqual(row[1], len("Helpful next step"))
            self.assertNotIn("Helpful next step", raw)
            self.assertEqual(schema_version, 37)


if __name__ == "__main__":
    unittest.main()

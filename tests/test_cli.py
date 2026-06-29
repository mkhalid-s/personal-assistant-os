from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


class CliFlowTest(unittest.TestCase):
    def test_capture_triage_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    ["python", "-m", "personal_assistant.cli", *args],
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

    def test_duplicate_capture_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            db_path = Path(tmp) / "assistant.db"
            env["MYOS_DB_PATH"] = str(db_path)

            def run(*args: str) -> str:
                out = subprocess.run(
                    ["python", "-m", "personal_assistant.cli", *args],
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
                    ["python", "-m", "personal_assistant.cli", *args],
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
                    ["python", "-m", "personal_assistant.cli", *args],
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
                    ["python", "-m", "personal_assistant.cli", *args],
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
                ["python", "-m", "personal_assistant.cli", "config-init", "--path", str(cfg)],
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
                ["python", "-m", "personal_assistant.cli", "doctor", "--strict"],
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
            self.assertTrue(env_example.exists())
            self.assertTrue(demo.exists())
            self.assertIn("MYOS_ACTION_COMMAND=myos action-provider", env_example.read_text())
            self.assertIn("myos doctor --strict", demo.read_text())

    def test_intent_lifecycle_and_redacted_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            base_cmd = ["python", "-m", "personal_assistant.cli"]

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
                    "python",
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
                    "python",
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
                    "python",
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
                    "python",
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
            self.assertNotEqual(post_check.returncode, 0)
            self.assertIn("PASS env_file", post_check.stdout)
            self.assertIn("PASS action_provider", post_check.stdout)
            self.assertIn("PASS standing_goals", post_check.stdout)
            self.assertIn("WARN jira_credentials", post_check.stdout)

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
                    "python",
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
                    "python",
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
                    ["python", "-m", "personal_assistant.cli", *args],
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
                ["python", "-m", "personal_assistant.cli", "metrics", "--days", "3"],
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
                ["python", "-m", "personal_assistant.cli", "launchd-install"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            out_uninstall = subprocess.run(
                ["python", "-m", "personal_assistant.cli", "launchd-uninstall"],
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
                ["python", "-m", "personal_assistant.cli", "launchd-install", "--autopilot"],
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
                    ["python", "-m", "personal_assistant.cli", *args],
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
                ["python", "-m", "personal_assistant.cli", "go-live", "--env-file", str(cfg)],
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
                ["python", "-m", "personal_assistant.cli", "activate", "--env-file", str(cfg)],
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
                ["python", "-m", "personal_assistant.cli", "dashboard", "--once", "--output-html", str(output_html)],
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
                ["python", "-m", "personal_assistant.cli", "sanity"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            runbook = subprocess.run(
                ["python", "-m", "personal_assistant.cli", "runbook", "--short"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Sanity check", sanity.stdout)
            self.assertIn("MYOS Operational Runbook", runbook.stdout)

    def test_launchd_status_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            out = subprocess.run(
                ["python", "-m", "personal_assistant.cli", "launchd-status"],
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
                ["python", "-m", "personal_assistant.cli", "start", "--env-file", str(cfg)],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            stop = subprocess.run(
                ["python", "-m", "personal_assistant.cli", "stop"],
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
                ["python", "-m", "personal_assistant.cli", "start", "--env-file", str(cfg), "--install-launchd"],
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
                    ["python", "-m", "personal_assistant.cli", *args],
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
                    ["python", "-m", "personal_assistant.cli", *args],
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
                    ["python", "-m", "personal_assistant.cli", *args],
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
            self.assertIn("\"counts\"", text)
            self.assertIn("\"connectors\"", text)

    def test_orchestrate_and_workflow_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")

            out1 = subprocess.run(
                ["python", "-m", "personal_assistant.cli", "orchestrate", "--workflow", "daily", "--connector", "all"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            out2 = subprocess.run(
                ["python", "-m", "personal_assistant.cli", "workflow-runs", "--limit", "5"],
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
                    ["python", "-m", "personal_assistant.cli", *args],
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
                    ["python", "-m", "personal_assistant.cli", *args],
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
            row = conn.execute(
                "SELECT transcript_text FROM media_assets ORDER BY id DESC LIMIT 1"
            ).fetchone()
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
                ["python", "-m", "personal_assistant.cli", "run-day", "--env-file", str(cfg), "--connector", "all"],
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
                ["python", "-m", "personal_assistant.cli", "queue-add", "--workflow", "weekly"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            run = subprocess.run(
                ["python", "-m", "personal_assistant.cli", "worker", "--limit", "1"],
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
                    ["python", "-m", "personal_assistant.cli", *args],
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
                    ["python", "-m", "personal_assistant.cli", *args],
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
                    ["python", "-m", "personal_assistant.cli", *args],
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
            env["MYOS_AI_COMMAND"] = f"python {ai_script}"
            env["MYOS_AI_PROVIDER"] = "fake-ai"

            subprocess.run(
                ["python", "-m", "personal_assistant.cli", "capture", "Launch risk contact test@example.com"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["python", "-m", "personal_assistant.cli", "triage"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            out = subprocess.run(
                ["python", "-m", "personal_assistant.cli", "delegate", "Launch risk update"],
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
            notify_script.write_text(
                "import sys\n"
                f"open({str(notify_path)!r}, 'w').write(sys.stdin.read())\n"
            )
            env["MYOS_NOTIFY_COMMAND"] = f"python {notify_script}"

            def run(*args: str) -> str:
                out = subprocess.run(
                    ["python", "-m", "personal_assistant.cli", *args],
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
            approvals = run("approve", "--list")
            self.assertIn("Approval queue", approvals)
            self.assertIn("preview:", approvals)
            status = run("autopilot-status")
            self.assertIn("Autopilot runs", status)
            self.assertIn("approvals_pending", status)
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
                    ["python", "-m", "personal_assistant.cli", *args],
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
            env["MYOS_ACTION_COMMAND"] = f"python {action_script}"
            env["MYOS_ACTION_PROVIDER"] = "fake-action"

            def run(*args: str) -> str:
                out = subprocess.run(
                    ["python", "-m", "personal_assistant.cli", *args],
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
            review_count_before = conn.execute("SELECT COUNT(*) FROM assistant_self_reviews").fetchone()[0]
            conn.close()
            self.assertEqual(exec_count, 1)
            self.assertEqual(review_count_before, 0)
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
                    ["python", "-m", "personal_assistant.cli", *args],
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
                extra_env={"MYOS_ACTION_COMMAND": f"python {fail_script}"},
            )
            self.assertNotEqual(failed.returncode, 0)
            retried = run(
                "approve",
                "--action",
                str(action_id),
                "--execute",
                "--limit",
                "20",
                check=True,
                extra_env={"MYOS_ACTION_COMMAND": f"python {ok_script}"},
            )
            self.assertIn("Executed action", retried.stdout)

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
                ["python", "-m", "personal_assistant.cli", "action-provider"],
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
                ["python", "-m", "personal_assistant.cli", "action-provider", "--execute"],
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
                    ["python", "-m", "personal_assistant.cli", *args],
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
                    ["python", "-m", "personal_assistant.cli", *args],
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
            processing_count = conn.execute(
                "SELECT COUNT(*) FROM file_ingests WHERE status='processing'"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(work_count, 3)
            self.assertEqual(processing_count, 0)


if __name__ == "__main__":
    unittest.main()

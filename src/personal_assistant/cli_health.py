from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import autonomy, model_setup, providers
from .db import get_connection, resolve_db_path, verify_schema
from .privacy import get_policy_map


def _sqlite_fts5_available(conn: sqlite3.Connection) -> tuple[bool, str]:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.myos_fts_check USING fts5(content)")
        conn.execute("DROP TABLE temp.myos_fts_check")
        return True, "FTS5 available"
    except sqlite3.Error as exc:
        return False, str(exc)


def _repo_file(path: str) -> Path:
    return Path(__file__).resolve().parents[2] / path


def _zero_stream_preflight() -> tuple[bool, str]:
    command = os.getenv("MYOS_AGENT_EXEC_ZERO_STREAM", "").strip() or "zero exec"
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return False, f"invalid command: {exc}"
    if not argv:
        return False, "not configured"
    found = shutil.which(argv[0])
    if not found:
        return False, f"{argv[0]} not installed; set MYOS_AGENT_EXEC_ZERO_STREAM for structured Zero factory runs"
    try:
        proc = subprocess.run(
            [*argv, "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"{command} --help timed out"
    output = f"{proc.stdout}\n{proc.stderr}"
    if proc.returncode != 0:
        return False, f"{command} --help exit={proc.returncode}: {output.strip()[:200]}"
    if "stream-json" not in output:
        return False, f"{command} help did not advertise stream-json support"
    formats = []
    if "--input-format" in output:
        formats.append("input")
    if "--output-format" in output:
        formats.append("output")
    detail = "stream-json support detected"
    if formats:
        detail += f" ({'/'.join(formats)} format flags)"
    # Surface the effective wall-clock cap so operators can see whether an
    # env override is in place before starting a long Zero factory run.
    from . import zero_executor

    env_timeout = os.getenv("MYOS_ZERO_TIMEOUT_SECONDS", "").strip()
    effective_timeout = zero_executor.DEFAULT_TIMEOUT
    timeout_source = "default"
    if env_timeout:
        try:
            effective_timeout = max(1, int(env_timeout))
            timeout_source = "env"
        except ValueError:
            timeout_source = f"invalid env value {env_timeout!r}, using default"
    detail += f"; wall_clock_timeout={effective_timeout}s ({timeout_source})"
    return True, f"{found}: {detail}"


def cmd_doctor(args: argparse.Namespace) -> None:
    conn = get_connection()
    json_mode = bool(getattr(args, "json", False))
    if not json_mode:
        print("System health:")
    counts = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM inbox_items) AS inbox_count,
          (SELECT COUNT(*) FROM work_items WHERE status='open') AS open_work,
          (SELECT COUNT(*) FROM external_items) AS external_count,
          (SELECT COUNT(*) FROM event_log) AS event_count
        """
    ).fetchone()
    if not json_mode:
        print(
            f"- inbox={counts['inbox_count']} open_work={counts['open_work']} "
            f"external={counts['external_count']} events={counts['event_count']}"
        )

    core_checks: list[tuple[str, bool, str]] = []
    optional_checks: list[tuple[str, bool, str]] = []

    db_path = resolve_db_path()
    db_parent = db_path.expanduser().parent
    fts_ok, fts_detail = _sqlite_fts5_available(conn)
    schema_status = verify_schema(conn)
    gitignore_text = _repo_file(".gitignore").read_text() if _repo_file(".gitignore").exists() else ""

    core_checks.extend(
        [
            (
                "python_version",
                sys.version_info >= (3, 10),
                f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            ),
            (
                "package_import",
                importlib.util.find_spec("personal_assistant") is not None,
                "personal_assistant importable",
            ),
            ("db_connection", conn.execute("SELECT 1").fetchone() is not None, str(db_path)),
            (
                "db_parent_writable",
                db_parent.exists() and os.access(db_parent, os.W_OK),
                str(db_parent),
            ),
            ("sqlite_fts5", fts_ok, fts_detail),
            (
                "schema_migrations",
                bool(schema_status["ok"]),
                f"current={schema_status['current_version']} expected={schema_status['expected_version']}",
            ),
            ("env_example", _repo_file(".env.example").exists(), str(_repo_file(".env.example"))),
            (
                "local_artifacts_ignored",
                "data/" in gitignore_text and ".env" in gitignore_text,
                ".gitignore covers data and env files",
            ),
        ]
    )

    credential_groups = {
        "jira_credentials": ["JIRA_BASE_URL", "JIRA_USER_EMAIL", "JIRA_API_TOKEN"],
        "github_credentials": ["GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO"],
        "confluence_credentials": ["CONFLUENCE_BASE_URL", "CONFLUENCE_USER_EMAIL", "CONFLUENCE_API_TOKEN"],
        "aha_credentials": ["AHA_BASE_URL", "AHA_API_TOKEN"],
    }
    for name, keys in credential_groups.items():
        missing = [key for key in keys if not os.getenv(key, "").strip()]
        optional_checks.append((name, not missing, "ready" if not missing else "missing " + ", ".join(missing)))

    optional_checks.extend(
        [
            ("tesseract", bool(shutil.which("tesseract")), shutil.which("tesseract") or "not installed"),
            ("launchctl", bool(shutil.which("launchctl")), shutil.which("launchctl") or "not available"),
            (
                "action_provider",
                bool(os.getenv("MYOS_ACTION_COMMAND", "").strip()),
                os.getenv("MYOS_ACTION_COMMAND", "") or "not configured",
            ),
        ]
    )
    optional_checks.append(("zero_stream_executor", *_zero_stream_preflight()))
    router_status = model_setup.router_status()
    optional_checks.append(
        (
            "router_model",
            bool(router_status["available"]),
            f"{router_status['backend']} {router_status['model']} ({router_status['detail']})",
        )
    )

    autonomy_level = autonomy.level_from_policy(conn)
    active_backend = providers.resolve_backend_name()
    backends = providers.available_backends()
    connector_rows = conn.execute(
        """
        SELECT connector, last_status, last_success_at, last_error
        FROM sync_state
        ORDER BY connector ASC
        """
    ).fetchall()
    core_ok = all(ok for _, ok, _ in core_checks)

    if json_mode:
        payload = {
            "schema": "myos.doctor.v1",
            "ok": core_ok,
            "strict": bool(args.strict),
            "counts": {
                "inbox": int(counts["inbox_count"]),
                "open_work": int(counts["open_work"]),
                "external": int(counts["external_count"]),
                "events": int(counts["event_count"]),
            },
            "core_checks": [
                {"name": name, "ok": bool(ok), "detail": detail}
                for name, ok, detail in core_checks
            ],
            "optional_checks": [
                {"name": name, "ok": bool(ok), "detail": detail}
                for name, ok, detail in optional_checks
            ],
            "autonomy_level": autonomy_level,
            "active_backend": active_backend,
            "backends": [
                {"name": b["name"], "available": bool(b["available"]), "detail": b["detail"]}
                for b in backends
            ],
            "connectors": [
                {
                    "connector": row["connector"],
                    "last_status": row["last_status"],
                    "last_success_at": row["last_success_at"],
                    "last_error": row["last_error"] or "",
                }
                for row in connector_rows
            ],
        }
        print(json.dumps(payload, ensure_ascii=True))
        if args.strict and not core_ok:
            raise SystemExit(1)
        return

    print("Core checks:")
    for name, ok, detail in core_checks:
        print(f"- {'PASS' if ok else 'FAIL'} {name}: {detail}")

    print("Optional checks:")
    for name, ok, detail in optional_checks:
        print(f"- {'PASS' if ok else 'INFO'} {name}: {detail}")

    print(f"Autonomy level: {autonomy_level} (auto-run safe / one-tap non-destructive / block destructive)")
    print(f"Agent backends (active: {active_backend}):")
    for b in backends:
        mark = "PASS" if b["available"] else "INFO"
        print(f"- {mark} {b['name']}: {b['detail']}")

    if not connector_rows:
        print("- sync_state: no connector runs yet")
        if args.strict and not core_ok:
            print("Doctor strict: core checks failed.")
            raise SystemExit(1)
        if args.strict:
            print("Doctor strict: core checks passed.")
        return
    print("Connector status:")
    for row in connector_rows:
        err = f" err={row['last_error']}" if row["last_error"] else ""
        print(
            f"- {row['connector']}: status={row['last_status']} "
            f"last_success={row['last_success_at']}{err}"
        )
    if args.strict and not core_ok:
        print("Doctor strict: core checks failed.")
        raise SystemExit(1)
    if args.strict:
        print("Doctor strict: core checks passed.")


def cmd_sanity(args: argparse.Namespace) -> None:
    conn = get_connection()
    checks: list[tuple[str, bool, str]] = []

    db_ok = conn.execute("SELECT 1").fetchone() is not None
    checks.append(("db_connection", db_ok, "SQLite connection and basic query"))

    required_tables = [
        "inbox_items",
        "work_items",
        "external_items",
        "sync_state",
        "review_evidence",
        "commitment_log",
    ]
    existing = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    missing = [t for t in required_tables if t not in existing]
    checks.append(("schema_tables", len(missing) == 0, f"missing={','.join(missing) if missing else 'none'}"))

    sync_rows = conn.execute("SELECT connector, last_status FROM sync_state").fetchall()
    if not sync_rows:
        checks.append(("connector_sync_state", False, "no connector state yet"))
    else:
        bad = [r["connector"] for r in sync_rows if r["last_status"] == "error"]
        checks.append(("connector_sync_state", len(bad) == 0, f"errors={','.join(bad) if bad else 'none'}"))

    inbox_new = conn.execute("SELECT COUNT(*) AS c FROM inbox_items WHERE status='new'").fetchone()["c"]
    open_items = conn.execute("SELECT COUNT(*) AS c FROM work_items WHERE status='open'").fetchone()["c"]
    checks.append(("load_levels", True, f"inbox_new={inbox_new}, open_items={open_items}"))

    report_dir = Path(args.report_dir) if args.report_dir else Path(__file__).resolve().parents[2] / "data" / "reports"
    latest_reports = sorted(report_dir.glob("daily-brief-*.md"), reverse=True)[:1] if report_dir.exists() else []
    checks.append(("daily_report", len(latest_reports) > 0, f"latest={latest_reports[0].name if latest_reports else 'none'}"))

    all_pass = True
    print("Sanity check:")
    for name, ok, detail in checks:
        status = "PASS" if ok else "WARN"
        print(f"- {status} {name}: {detail}")
        if not ok and name in ("db_connection", "schema_tables"):
            all_pass = False

    if args.strict and any(not ok for _, ok, _ in checks):
        raise SystemExit(1)
    if all_pass:
        print("Sanity complete: core checks passed.")
    else:
        print("Sanity complete: core issues found.")


def cmd_cutover_check(_: argparse.Namespace) -> None:
    conn = get_connection()
    required = {
        "jira": ["JIRA_BASE_URL", "JIRA_USER_EMAIL", "JIRA_API_TOKEN"],
        "github": ["GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO"],
        "confluence": ["CONFLUENCE_BASE_URL", "CONFLUENCE_USER_EMAIL", "CONFLUENCE_API_TOKEN"],
        "aha": ["AHA_BASE_URL", "AHA_API_TOKEN"],
    }
    print("Cutover readiness:")
    ready = 0
    for name, keys in required.items():
        missing = [k for k in keys if not os.getenv(k)]
        if missing:
            print(f"- {name}: MISSING {', '.join(missing)}")
            continue
        state = conn.execute(
            "SELECT last_status, last_success_at FROM sync_state WHERE connector = ?",
            (name,),
        ).fetchone()
        if not state:
            print(f"- {name}: CREDS_READY sync=never")
            continue
        print(f"- {name}: CREDS_READY sync={state['last_status']} last_success={state['last_success_at']}")
        ready += 1
    print(f"Connectors credential-ready: {ready}/{len(required)}")
    if ready == len(required):
        print("Cutover check: READY for go-live.")
    else:
        print("Cutover check: NOT_READY. Fill env vars and rerun.")


def cmd_uat(args: argparse.Namespace) -> None:
    conn = get_connection()
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM work_items WHERE created_at >= datetime('now', ?)",
        (f"-{args.days} days",),
    ).fetchone()["c"]
    hi_risk = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM work_items
        WHERE created_at >= datetime('now', ?) AND risk_score >= ?
        """,
        (f"-{args.days} days", args.risk_threshold),
    ).fetchone()["c"]
    commitments = conn.execute(
        """
        SELECT
          SUM(CASE WHEN outcome='completed_on_time' THEN 1 ELSE 0 END) AS on_time,
          SUM(CASE WHEN outcome IN ('completed_on_time','completed_late','missed') THEN 1 ELSE 0 END) AS resolved
        FROM commitment_log
        WHERE COALESCE(resolved_on, promised_on, due_on) >= date('now', ?)
        """,
        (f"-{args.days} days",),
    ).fetchone()
    interventions = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM event_log
        WHERE event_type IN ('stop_doing_review', 'renegotiate_review')
          AND created_at >= datetime('now', ?)
        """,
        (f"-{args.days} days",),
    ).fetchone()["c"]
    activity = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM event_log
        WHERE created_at >= datetime('now', ?)
        """,
        (f"-{args.days} days",),
    ).fetchone()["c"]
    backlog_new = conn.execute("SELECT COUNT(*) AS c FROM inbox_items WHERE status='new'").fetchone()["c"]
    on_time = commitments["on_time"] or 0
    resolved = commitments["resolved"] or 0
    acceptance_rate = (100.0 * on_time / resolved) if resolved else 0.0
    intervention_rate = (100.0 * interventions / activity) if activity else 0.0
    risk_focus = (100.0 * hi_risk / total) if total else 0.0

    print(f"UAT quality snapshot ({args.days}d):")
    print(f"- throughput: work_items={total} backlog_new={backlog_new}")
    print(
        f"- prioritization_focus: high_risk_items={hi_risk}/{total} "
        f"({risk_focus:.1f}%) threshold={args.risk_threshold}"
    )
    print(
        f"- commitment_reliability: on_time={on_time}/{resolved} "
        f"({acceptance_rate:.1f}%)"
    )
    print(
        f"- intervention_signal: interventions={interventions}/{activity} "
        f"({intervention_rate:.1f}%)"
    )
    if backlog_new > args.backlog_warn:
        print("- ALERT: inbox backlog too high; run `myos triage`.")
    if acceptance_rate < args.acceptance_warn and resolved >= args.min_sample:
        print("- ALERT: acceptance rate low; revisit prioritization and renegotiation cadence.")
    if risk_focus < args.risk_focus_warn and total >= args.min_sample:
        print("- ALERT: risk focus too low; raise risk threshold tuning or adjust inference.")


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 60
    ordered = sorted(values)
    idx = int(round((len(ordered) - 1) * max(0.0, min(1.0, pct))))
    return int(ordered[idx])


def cmd_tune(args: argparse.Namespace) -> None:
    conn = get_connection()
    risk_rows = conn.execute(
        """
        SELECT risk_score
        FROM work_items
        WHERE created_at >= datetime('now', ?) AND status='open'
        ORDER BY risk_score ASC
        """,
        (f"-{args.days} days",),
    ).fetchall()
    risk_scores = [int(r["risk_score"]) for r in risk_rows]
    suggested_risk_threshold = max(45, min(85, _percentile(risk_scores, 0.75)))

    commitments = conn.execute(
        """
        SELECT
          SUM(CASE WHEN outcome='completed_on_time' THEN 1 ELSE 0 END) AS on_time,
          SUM(CASE WHEN outcome IN ('completed_on_time','completed_late','missed') THEN 1 ELSE 0 END) AS resolved
        FROM commitment_log
        WHERE COALESCE(resolved_on, promised_on, due_on) >= date('now', ?)
        """,
        (f"-{args.days} days",),
    ).fetchone()
    on_time = int(commitments["on_time"] or 0)
    resolved = int(commitments["resolved"] or 0)
    acceptance_rate = (100.0 * on_time / resolved) if resolved else 70.0
    suggested_acceptance_warn = max(50.0, min(90.0, acceptance_rate - 10.0))

    backlog_new = int(conn.execute("SELECT COUNT(*) AS c FROM inbox_items WHERE status='new'").fetchone()["c"])
    open_items = int(conn.execute("SELECT COUNT(*) AS c FROM work_items WHERE status='open'").fetchone()["c"])
    suggested_backlog_warn = max(8, min(40, int((open_items * 0.5) + 5)))

    hi_risk = int(
        conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM work_items
            WHERE created_at >= datetime('now', ?) AND risk_score >= ?
            """,
            (f"-{args.days} days", suggested_risk_threshold),
        ).fetchone()["c"]
    )
    total = int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM work_items WHERE created_at >= datetime('now', ?)",
            (f"-{args.days} days",),
        ).fetchone()["c"]
    )
    risk_focus_pct = (100.0 * hi_risk / total) if total else 25.0
    suggested_risk_focus_warn = max(15.0, min(45.0, risk_focus_pct - 5.0))

    print(f"Tuning recommendations ({args.days}d window):")
    print(f"- current_state: open_items={open_items} backlog_new={backlog_new} resolved_commitments={resolved}")
    print(f"- suggested risk_threshold={suggested_risk_threshold}")
    print(f"- suggested backlog_warn={suggested_backlog_warn}")
    print(f"- suggested acceptance_warn={suggested_acceptance_warn:.1f}")
    print(f"- suggested risk_focus_warn={suggested_risk_focus_warn:.1f}")
    print(
        "- suggested uat command: "
        f"myos uat --days {args.days} "
        f"--risk-threshold {suggested_risk_threshold} "
        f"--backlog-warn {suggested_backlog_warn} "
        f"--acceptance-warn {suggested_acceptance_warn:.1f} "
        f"--risk-focus-warn {suggested_risk_focus_warn:.1f}"
    )

    if args.apply_policy:
        updates = {
            "uat_risk_threshold": str(suggested_risk_threshold),
            "uat_backlog_warn": str(suggested_backlog_warn),
            "uat_acceptance_warn": f"{suggested_acceptance_warn:.1f}",
            "uat_risk_focus_warn": f"{suggested_risk_focus_warn:.1f}",
        }
        for key, value in updates.items():
            conn.execute(
                """
                INSERT INTO assistant_policies (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
                """,
                (key, value),
            )
        conn.commit()
        print("Applied recommendations into policy keys: uat_*")


def cmd_snapshot(args: argparse.Namespace) -> None:
    conn = get_connection()
    counts = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM work_items WHERE status='open') AS open_items,
          (SELECT COUNT(*) FROM work_items WHERE status='done') AS done_items,
          (SELECT COUNT(*) FROM work_items WHERE status='archived') AS archived_items,
          (SELECT COUNT(*) FROM work_items WHERE status='open' AND risk_score >= ?) AS at_risk,
          (SELECT COUNT(*) FROM inbox_items WHERE status='new') AS inbox_new
        """,
        (args.risk_threshold,),
    ).fetchone()
    top_risk = conn.execute(
        """
        SELECT id, title, kind, risk_score, due_date
        FROM work_items
        WHERE status='open' AND risk_score >= ?
        ORDER BY risk_score DESC, COALESCE(due_date, '9999-12-31') ASC
        LIMIT ?
        """,
        (args.risk_threshold, args.limit),
    ).fetchall()
    connectors = conn.execute(
        """
        SELECT connector, last_status, last_success_at, last_error
        FROM sync_state
        ORDER BY connector ASC
        """
    ).fetchall()
    commitments = conn.execute(
        """
        SELECT
          SUM(CASE WHEN outcome='completed_on_time' THEN 1 ELSE 0 END) AS on_time,
          SUM(CASE WHEN outcome='completed_late' THEN 1 ELSE 0 END) AS late,
          SUM(CASE WHEN outcome='missed' THEN 1 ELSE 0 END) AS missed,
          SUM(CASE WHEN outcome='open' THEN 1 ELSE 0 END) AS open_c
        FROM commitment_log
        """
    ).fetchone()

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "counts": {
            "open_items": counts["open_items"],
            "done_items": counts["done_items"],
            "archived_items": counts["archived_items"],
            "at_risk": counts["at_risk"],
            "inbox_new": counts["inbox_new"],
        },
        "top_risk": [
            {
                "id": r["id"],
                "title": r["title"],
                "kind": r["kind"],
                "risk_score": r["risk_score"],
                "due_date": r["due_date"],
            }
            for r in top_risk
        ],
        "connectors": [
            {
                "name": r["connector"],
                "status": r["last_status"],
                "last_success_at": r["last_success_at"],
                "last_error": r["last_error"],
            }
            for r in connectors
        ],
        "commitments": {
            "on_time": commitments["on_time"] or 0,
            "late": commitments["late"] or 0,
            "missed": commitments["missed"] or 0,
            "open": commitments["open_c"] or 0,
        },
    }

    body = json.dumps(payload, indent=2, ensure_ascii=True)
    if args.output:
        out_path = Path(args.output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(body + "\n")
        print(f"Snapshot written: {out_path}")
        return
    print(body)

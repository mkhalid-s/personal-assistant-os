from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from .db import get_connection, resolve_db_path, verify_schema
from .privacy import _cleanup_policy_retention


def _check_sqlite_file(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, f"missing {path}"
    try:
        conn = sqlite3.connect(path)
        quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
        has_migrations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        conn.close()
    except sqlite3.Error as exc:
        return False, f"sqlite error: {exc}"
    if quick_check != "ok":
        return False, f"quick_check={quick_check}"
    if not has_migrations:
        return False, "schema_migrations table missing"
    return True, "ok"


def cmd_migrations(args: argparse.Namespace) -> None:
    conn = get_connection()
    action = getattr(args, "migrations_action", "verify") or "verify"
    if action == "list":
        rows = conn.execute(
            """
            SELECT version, name, applied_at
            FROM schema_migrations
            ORDER BY version ASC
            """
        ).fetchall()
        print("Schema migrations:")
        for row in rows:
            print(f"- {row['version']:02d} {row['name']} applied_at={row['applied_at']}")
        status = verify_schema(conn)
        print(f"Current version: {status['current_version']} / expected {status['expected_version']}")
        return

    status = verify_schema(conn)
    print("Migration verification:")
    print(f"- current_version={status['current_version']} expected={status['expected_version']}")
    print(f"- quick_check={status['quick_check']}")
    print(f"- foreign_key_violations={status['foreign_key_violations']}")
    missing_versions = status["missing_versions"]
    missing_tables = status["missing_tables"]
    print(f"- missing_versions={missing_versions if missing_versions else 'none'}")
    print(f"- missing_tables={missing_tables if missing_tables else 'none'}")
    if not status["ok"]:
        print("Schema migrations verification failed.")
        if getattr(args, "strict", False):
            raise SystemExit(1)
        return
    print("Schema migrations verified.")


def cmd_backup(args: argparse.Namespace) -> None:
    source = resolve_db_path()
    conn = get_connection()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = Path(args.output).expanduser() if args.output else source.parent / "backups" / f"assistant-{timestamp}.db"
    output.parent.mkdir(parents=True, exist_ok=True)
    dest = sqlite3.connect(output)
    try:
        conn.backup(dest)
    finally:
        dest.close()
        conn.close()
    print(f"Backup created: {output}")


def cmd_restore(args: argparse.Namespace) -> None:
    source = Path(args.source).expanduser()
    ok, detail = _check_sqlite_file(source)
    if not ok:
        print(f"Restore refused: {detail}")
        raise SystemExit(1)

    target = resolve_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safety_backup = target.parent / "backups" / f"pre-restore-{timestamp}.db"
        safety_backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, safety_backup)
        print(f"Current database backed up: {safety_backup}")
    shutil.copy2(source, target)
    for sidecar in (target.with_name(target.name + "-wal"), target.with_name(target.name + "-shm")):
        if sidecar.exists():
            sidecar.unlink()

    conn = get_connection()
    status = verify_schema(conn)
    conn.close()
    if not status["ok"]:
        print("Restore completed, but schema verification failed.")
        raise SystemExit(1)
    print(f"Database restored from: {source}")
    print("Schema migrations verified.")


def cmd_config_init(args: argparse.Namespace) -> None:
    target = Path(args.path).expanduser()
    if target.exists() and not args.force:
        print(f"Config already exists: {target}")
        print("Use --force to overwrite.")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "\n".join(
            [
                "# Personal Assistant OS credentials",
                "JIRA_BASE_URL=",
                "JIRA_USER_EMAIL=",
                "JIRA_API_TOKEN=",
                "",
                "GITHUB_TOKEN=",
                "GITHUB_OWNER=",
                "GITHUB_REPO=",
                "",
                "CONFLUENCE_BASE_URL=",
                "CONFLUENCE_USER_EMAIL=",
                "CONFLUENCE_API_TOKEN=",
                "",
                "AHA_BASE_URL=",
                "AHA_API_TOKEN=",
                "",
            ]
        )
        + "\n"
    )
    print(f"Created config template: {target}")
    print("Fill values, then run: myos run-day --env-file " + str(target))


def cmd_cleanup(args: argparse.Namespace) -> None:
    conn = get_connection()
    stale_rows = conn.execute(
        """
        SELECT id, title
        FROM work_items
        WHERE status='open' AND created_at < datetime('now', ?)
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (f"-{args.days} days", args.limit),
    ).fetchall()
    archived = 0
    for row in stale_rows:
        conn.execute(
            "UPDATE work_items SET status='archived', updated_at=CURRENT_TIMESTAMP WHERE id = ?",
            (row["id"],),
        )
        archived += 1
    retention = _cleanup_policy_retention(conn)
    conn.commit()
    print(f"Cleanup complete. Archived {archived} stale open items.")
    print(
        f"Policy retention cleanup: media_deleted={retention['media']} "
        f"evidence_deleted={retention['evidence']} conversation_turns_deleted={retention['conversation_turns']}"
    )

from __future__ import annotations

import argparse
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import cli_diagnostics, model_setup
from .db import get_connection


@dataclass(frozen=True)
class SetupLiveDependencies:
    launchd_install_command: Callable[[argparse.Namespace], None] | None = None


def _env_template(db_path: Path) -> str:
    return (
        "\n".join(
            [
                "# Personal Assistant OS live configuration",
                f"MYOS_DB_PATH={db_path}",
                "",
                "# Jira",
                "JIRA_BASE_URL=",
                "JIRA_USER_EMAIL=",
                "JIRA_API_TOKEN=",
                "",
                "# GitHub",
                "GITHUB_TOKEN=",
                "GITHUB_OWNER=",
                "GITHUB_REPO=",
                "",
                "# Confluence",
                "CONFLUENCE_BASE_URL=",
                "CONFLUENCE_USER_EMAIL=",
                "CONFLUENCE_API_TOKEN=",
                "",
                "# Aha",
                "AHA_BASE_URL=",
                "AHA_API_TOKEN=",
                "",
                "# Optional AI reasoning provider",
                "MYOS_AI_PROVIDER=local",
                "MYOS_AI_COMMAND=",
                "",
                "# Optional tiny local router model for intent finding",
                "MYOS_ROUTER_BACKEND=",
                "MYOS_ROUTER_MODEL=",
                "MYOS_ROUTER_COMMAND=",
                "MYOS_ROUTER_TIMEOUT_SEC=8",
                "MYOS_ROUTER_MIN_CONFIDENCE=0.70",
                "",
                "# Safe default: approved external actions go to local outbox",
                "MYOS_ACTION_PROVIDER=builtin",
                "MYOS_ACTION_COMMAND=myos action-provider",
                "",
                "# Optional notification hook for assistant digests",
                "MYOS_NOTIFY_COMMAND=",
                "",
            ]
        )
        + "\n"
    )


def _read_env_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        if raw.startswith("export "):
            raw = raw[len("export ") :].strip()
        key, value = raw.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def _upsert_env_lines(path: Path, lines: list[str], *, header: str = "# Managed tiny router model") -> None:
    keys = {line.split("=", 1)[0].strip() for line in lines if "=" in line}
    existing = path.read_text().splitlines() if path.exists() else []
    kept = []
    for line in existing:
        raw = line.strip()
        candidate = raw[len("export ") :].strip() if raw.startswith("export ") else raw
        key = candidate.split("=", 1)[0].strip() if "=" in candidate else ""
        if key in keys:
            continue
        kept.append(line)
    if kept and kept[-1].strip():
        kept.append("")
    kept.append(header)
    kept.extend(lines)
    path.write_text("\n".join(kept).rstrip() + "\n")


def _setup_live_paths(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    project_root = Path(__file__).resolve().parents[2]
    data_dir = (Path(args.data_dir).expanduser() if args.data_dir else project_root / "data").resolve()
    env_path = (Path(args.env_file).expanduser() if args.env_file else data_dir / ".env.myos").resolve()
    env_values = _read_env_values(env_path)
    configured_db = args.db_path or os.getenv("MYOS_DB_PATH", "") or env_values.get("MYOS_DB_PATH", "")
    db_path = (Path(configured_db).expanduser() if configured_db else data_dir / "assistant.db").resolve()
    watch_dir = (Path(args.watch_dir).expanduser() if args.watch_dir else data_dir / "inbox").resolve()
    return data_dir, env_path, db_path, watch_dir


def _env_or_file(key: str, values: dict[str, str]) -> str:
    return os.getenv(key, "") or values.get(key, "")


def _cmd_setup_live_check(env_path: Path, db_path: Path, watch_dir: Path) -> bool:
    env_values = _read_env_values(env_path)
    print("Live readiness check:")
    ok_count = 0
    total = 0

    def check(name: str, ok: bool, detail: str, *, required: bool = True) -> None:
        nonlocal ok_count, total
        if not required:
            print(f"- {'PASS' if ok else 'INFO'} {name}: {detail}")
            return
        total += 1
        if ok:
            ok_count += 1
        print(f"- {'PASS' if ok else 'WARN'} {name}: {detail}")

    check("env_file", env_path.exists(), str(env_path) if env_path.exists() else f"missing {env_path}")
    if env_path.exists():
        mode = env_path.stat().st_mode & 0o777
        check("env_permissions", mode & 0o077 == 0, oct(mode))
    else:
        check("env_permissions", False, "env file missing")

    credential_groups = {
        "jira_credentials": ["JIRA_BASE_URL", "JIRA_USER_EMAIL", "JIRA_API_TOKEN"],
        "github_credentials": ["GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO"],
        "confluence_credentials": ["CONFLUENCE_BASE_URL", "CONFLUENCE_USER_EMAIL", "CONFLUENCE_API_TOKEN"],
        "aha_credentials": ["AHA_BASE_URL", "AHA_API_TOKEN"],
    }
    for name, keys in credential_groups.items():
        missing = [key for key in keys if not _env_or_file(key, env_values)]
        check(name, not missing, "ready" if not missing else "missing " + ", ".join(missing), required=False)

    action_provider = _env_or_file("MYOS_ACTION_COMMAND", env_values)
    check("action_provider", bool(action_provider), action_provider or "missing MYOS_ACTION_COMMAND")
    check("watch_dir", watch_dir.exists(), str(watch_dir) if watch_dir.exists() else f"missing {watch_dir}")
    check("database_file", db_path.exists(), str(db_path) if db_path.exists() else f"missing {db_path}")

    if not db_path.exists():
        print(f"Readiness summary: {ok_count}/{total} checks passing")
        print("Next: run `myos setup-live --apply`, then fill the env file.")
        return ok_count == total

    try:
        conn = sqlite3.connect(db_path)
        active_goals = conn.execute("SELECT COUNT(*) FROM assistant_goals WHERE status='active'").fetchone()[0]
        active_watch_dirs = conn.execute("SELECT COUNT(*) FROM assistant_watch_dirs WHERE status='active'").fetchone()[
            0
        ]
        recent_autopilot = conn.execute(
            "SELECT COUNT(*) FROM autopilot_runs WHERE started_at >= datetime('now', '-24 hours')"
        ).fetchone()[0]
        conn.close()
        check("standing_goals", active_goals > 0, f"active_goals={active_goals}")
        check("watch_config", active_watch_dirs > 0, f"active_watch_dirs={active_watch_dirs}")
        check("autopilot_smoke", recent_autopilot > 0, f"runs_24h={recent_autopilot}", required=False)
    except sqlite3.Error as exc:
        check("database_schema", False, str(exc))

    print(f"Readiness summary: {ok_count}/{total} checks passing")
    if ok_count == total:
        print(f"Ready: myos autopilot --env-file {env_path} --once")
    else:
        print(f"Next: fix WARN items, then run `myos autopilot --env-file {env_path} --once`.")
    return ok_count == total


def cmd_setup_live(args: argparse.Namespace, dependencies: SetupLiveDependencies | None = None) -> None:
    dependencies = dependencies or SetupLiveDependencies()
    data_dir, env_path, db_path, watch_dir = _setup_live_paths(args)
    router_model_plan = None
    if getattr(args, "router_model", False):
        try:
            router_model_plan = model_setup.setup_plan(
                runtime=getattr(args, "router_runtime", "auto"),
                model=getattr(args, "router_model_name", ""),
            )
        except ValueError as exc:
            print(str(exc))
            raise SystemExit(1) from exc
    goals = [
        (
            "Keep my work commitments and risks current",
            "Monitor synced work, notes, transcripts, due dates, blockers, and approval-needed updates.",
            240,
            1,
        ),
        (
            "Prepare daily executive digest",
            "Summarize what changed, what was handled, what needs approval, and the next best action.",
            720,
            2,
        ),
    ]

    if args.check:
        if not _cmd_setup_live_check(env_path, db_path, watch_dir):
            raise SystemExit(1)
        return

    print("Live setup plan:")
    print(f"- data_dir: {data_dir}")
    print(f"- env_file: {env_path}")
    print(f"- db_path: {db_path}")
    print(f"- default_watch_dir: {watch_dir}")
    print("- default action provider: MYOS_ACTION_COMMAND=myos action-provider")
    print("- default goals: commitment/risk monitoring, daily digest")
    if router_model_plan:
        cli_diagnostics._print_model_plan(router_model_plan)
    print("- launchd autopilot: " + ("yes" if args.install_launchd else "no"))
    if not args.apply:
        print("Dry run only. Re-run with --apply to create files and DB records.")
        return

    if args.install_launchd and dependencies.launchd_install_command is None:
        print("Launchd install requested, but no launchd installer is configured.")
        raise SystemExit(1)

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "autopilot").mkdir(parents=True, exist_ok=True)
    (data_dir / "outbox").mkdir(parents=True, exist_ok=True)
    watch_dir.mkdir(parents=True, exist_ok=True)
    if not env_path.exists() or args.force:
        env_path.write_text(_env_template(db_path))
        env_path.chmod(0o600)
        print(f"Wrote env template: {env_path}")
    else:
        env_path.chmod(0o600)
        print(f"Env file already exists: {env_path}")
    if router_model_plan:
        setup_result = model_setup.apply_setup(router_model_plan, dry_run=False)
        _upsert_env_lines(env_path, list(router_model_plan["env_lines"]))
        env_path.chmod(0o600)
        print(f"Router model setup: {setup_result['status']}")
        if setup_result.get("wrapper"):
            print(f"Router wrapper: {setup_result['wrapper']}")
        if setup_result["status"] == "failed":
            print(setup_result.get("stderr") or setup_result.get("stdout") or "model setup failed")
            raise SystemExit(1)

    os.environ["MYOS_DB_PATH"] = str(db_path)
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO assistant_watch_dirs (path, label, status, updated_at)
        VALUES (?, 'default-inbox', 'active', CURRENT_TIMESTAMP)
        ON CONFLICT(path) DO UPDATE SET status='active', updated_at=CURRENT_TIMESTAMP
        """,
        (str(watch_dir),),
    )
    for objective, context, cadence, priority in goals:
        existing = conn.execute(
            "SELECT id FROM assistant_goals WHERE objective=? LIMIT 1",
            (objective,),
        ).fetchone()
        if existing:
            continue
        conn.execute(
            """
            INSERT INTO assistant_goals (objective, context, cadence_minutes, priority, status)
            VALUES (?, ?, ?, ?, 'active')
            """,
            (objective, context, cadence, priority),
        )
    conn.execute(
        """
        INSERT INTO assistant_policies (key, value, updated_at)
        VALUES ('action_timeout_sec', '30', CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
        """
    )
    conn.commit()
    print("Configured default watch directory, goals, and policy.")

    if args.install_launchd:
        dependencies.launchd_install_command(
            argparse.Namespace(
                apply=True,
                load=args.load_launchd,
                env_file=str(env_path),
                interval_sec=1800,
                meeting_hours=0.0,
                autopilot=True,
                autopilot_interval_sec=args.autopilot_interval_sec,
            )
        )

    print("Setup complete.")
    print(f"Next: fill credentials in {env_path}")
    print(f"Then: myos autopilot --env-file {env_path} --once")
    print("Review: myos digest && myos approve --list && myos self-review")

"""``myos install`` and ``myos uninstall`` — one-shot bootstrap.

The one-line ``scripts/install.sh`` bootstrapper calls ``myos install``
after ``pipx install`` finishes; this module owns everything that
happens on the machine after the wheel is on disk:

- Ensure the runtime data directory exists (from
  :mod:`personal_assistant.data_dirs`).
- Seed ``.env.myos`` from ``.env.example`` if the operator does not
  already have one; if they do, leave it alone.
- Register background services:
  * **macOS**: hand off to ``cli_launchd.cmd_launchd_install`` with
    ``--scheduler`` on by default (so ``myos scheduler tick`` runs on
    a 60 s cadence — the S4 reminder pipeline).
  * **Linux**: emit a ``systemd --user`` timer + service pair under
    ``~/.config/systemd/user/`` so the same scheduler cadence works
    without launchd. macOS ``launchd`` is not available on Linux;
    ``systemd --user`` is the standard per-user substitute and does
    not require root.

``myos uninstall`` mirrors: unload/remove launchd or systemd units and
leave the data dir intact by default (opt in via ``--purge`` to also
delete state).

Both commands are idempotent — running ``myos install`` twice must be
a no-op the second time (existing ``.env.myos`` preserved, existing
plists rewritten in place, systemd unit re-enabled without error).
"""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import cli_launchd, data_dirs

# systemd-user paths
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
SYSTEMD_SERVICE_NAME = "myos-scheduler.service"
SYSTEMD_TIMER_NAME = "myos-scheduler.timer"


def _repo_env_example() -> Path | None:
    """Return the path to ``.env.example`` when running from a repo
    checkout, else ``None``. The wheel does not ship ``.env.example``
    today, so an installed-mode install falls back to writing a minimal
    stub (see :func:`_seed_env_file`) instead of copying the template.
    """
    repo = data_dirs._repo_root_if_dev()
    if repo is None:
        return None
    candidate = repo / ".env.example"
    return candidate if candidate.is_file() else None


def _seed_env_file(env_path: Path) -> str:
    """Create ``env_path`` from ``.env.example`` if it does not already
    exist. Returns a short status string suitable for CLI output.
    """
    if env_path.exists():
        return f"env file kept in place at {env_path}"
    template = _repo_env_example()
    if template is None:
        # Installed mode without a repo template — write a minimal
        # stub so `myos launchd-install` still has something to point
        # its ``--env-file`` at. Operators can fill in secrets later.
        env_path.write_text(
            "# MYOS env file — populate real values, then re-run `myos install`.\n"
            "# See https://github.com/mkhalid-s/personal-assistant-os for the full template.\n"
        )
        return f"env file seeded (minimal stub) at {env_path}"
    env_path.write_text(template.read_text())
    return f"env file seeded from {template} at {env_path}"


def _systemd_scheduler_unit(myos_bin: str, env_file: Path, data_dir: Path) -> str:
    """Return the systemd-user ``[Service]`` file contents for the
    scheduler tick.

    ``ExecStart`` uses an absolute path to the ``myos`` binary. We
    stamp ``MYOS_DATA_DIR`` and ``MYOS_ENV_FILE`` into
    ``Environment=`` so the tick inherits the same paths the installer
    just resolved, mirroring what the launchd plist does on macOS.
    """
    escaped_data = shlex.quote(str(data_dir))
    escaped_env = shlex.quote(str(env_file))
    return (
        "[Unit]\n"
        "Description=MYOS reminder scheduler tick\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"Environment=MYOS_DATA_DIR={escaped_data}\n"
        f"Environment=MYOS_ENV_FILE={escaped_env}\n"
        f"ExecStart={shlex.quote(myos_bin)} scheduler tick\n"
    )


def _systemd_scheduler_timer(interval_sec: int) -> str:
    """Return the systemd-user ``[Timer]`` contents.

    Matches the launchd ``StartInterval=60`` shape by default so both
    platforms tick at the same cadence.
    """
    return (
        "[Unit]\n"
        "Description=Fire MYOS reminder scheduler every "
        f"{interval_sec} seconds\n"
        "\n"
        "[Timer]\n"
        f"OnBootSec={interval_sec}s\n"
        f"OnUnitActiveSec={interval_sec}s\n"
        "Unit=myos-scheduler.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def _install_linux_scheduler(env_file: Path, data_dir: Path, interval_sec: int, apply: bool) -> None:
    """Write + enable the systemd-user timer/service pair."""
    myos_bin = shutil.which("myos") or "myos"
    service_body = _systemd_scheduler_unit(myos_bin, env_file, data_dir)
    timer_body = _systemd_scheduler_timer(interval_sec)
    service_dst = SYSTEMD_USER_DIR / SYSTEMD_SERVICE_NAME
    timer_dst = SYSTEMD_USER_DIR / SYSTEMD_TIMER_NAME

    print("systemd --user scheduler plan:")
    print(f"- write {service_dst}")
    print(f"- write {timer_dst}")
    print(f"- ExecStart: {myos_bin} scheduler tick")
    print(f"- interval: {interval_sec}s (OnBootSec + OnUnitActiveSec)")
    if not apply:
        print("Dry run only. Re-run with --apply to execute.")
        return

    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    service_dst.write_text(service_body)
    timer_dst.write_text(timer_body)

    systemctl = shutil.which("systemctl")
    if systemctl is None:
        print("systemctl not found on PATH; unit files written but not enabled.")
        print("Enable manually with:")
        print(f"  systemctl --user daemon-reload && systemctl --user enable --now {SYSTEMD_TIMER_NAME}")
        return

    subprocess.run([systemctl, "--user", "daemon-reload"], check=False)
    subprocess.run([systemctl, "--user", "enable", "--now", SYSTEMD_TIMER_NAME], check=False)
    print(f"Enabled systemd --user timer: {SYSTEMD_TIMER_NAME}")


def _uninstall_linux_scheduler(apply: bool) -> None:
    """Reverse ``_install_linux_scheduler`` — stop + disable + delete."""
    service_dst = SYSTEMD_USER_DIR / SYSTEMD_SERVICE_NAME
    timer_dst = SYSTEMD_USER_DIR / SYSTEMD_TIMER_NAME
    print("systemd --user uninstall plan:")
    print(f"- disable + remove {timer_dst}")
    print(f"- remove {service_dst}")
    if not apply:
        print("Dry run only. Re-run with --apply to execute.")
        return

    systemctl = shutil.which("systemctl")
    if systemctl is not None:
        subprocess.run([systemctl, "--user", "disable", "--now", SYSTEMD_TIMER_NAME], check=False)
        subprocess.run([systemctl, "--user", "daemon-reload"], check=False)
    if timer_dst.exists():
        timer_dst.unlink()
    if service_dst.exists():
        service_dst.unlink()
    print("systemd --user units removed.")


def cmd_install(args: argparse.Namespace) -> None:
    """One-shot install: mkdir data dir, seed env file, register
    background scheduler on the current platform.

    ``--dry-run`` (default: real install) shows the plan without
    touching disk or running any service manager — the ``install.sh``
    bootstrap smoke-checks this shape in CI to catch a regression
    before touching a real user's machine.
    """
    apply = not bool(getattr(args, "dry_run", False))
    data_dir = data_dirs.resolve_data_dir()
    env_file = data_dirs.resolve_env_file()
    log_dir = data_dirs.resolve_log_dir()

    print("MYOS install plan:")
    print(f"- platform: {sys.platform}")
    print(f"- data dir: {data_dir}")
    print(f"- env file: {env_file}")
    print(f"- log dir:  {log_dir}")
    if not apply:
        print("(dry-run) — pass without --dry-run to execute.")
        return

    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    print(_seed_env_file(env_file))

    if sys.platform == "darwin":
        print()
        cli_launchd.cmd_launchd_install(
            argparse.Namespace(
                apply=True,
                load=bool(getattr(args, "load", True)),
                env_file=str(env_file),
                interval_sec=1800,
                meeting_hours=0.0,
                autopilot=False,
                autopilot_interval_sec=900,
                scheduler=True,
                scheduler_interval_sec=int(getattr(args, "scheduler_interval_sec", 60)),
            )
        )
    elif sys.platform.startswith("linux"):
        print()
        _install_linux_scheduler(
            env_file=env_file,
            data_dir=data_dir,
            interval_sec=int(getattr(args, "scheduler_interval_sec", 60)),
            apply=True,
        )
    else:
        print(f"(no scheduler installer for platform {sys.platform!r} — set MYOS_NOTIFY_COMMAND and cron manually)")

    print()
    print("Done. Try:")
    print("  myos --help")
    print("  myos remind create 'first reminder' --at +2m")


def cmd_uninstall(args: argparse.Namespace) -> None:
    """Reverse ``cmd_install``: stop/remove agents, optionally purge state.

    ``--purge`` deletes the entire ``resolve_data_dir()`` — DB, logs,
    env file, everything. Off by default because a partial reinstall
    should not lose the user's local knowledge base.
    """
    apply = not bool(getattr(args, "dry_run", False))
    purge = bool(getattr(args, "purge", False))
    data_dir = data_dirs.resolve_data_dir()

    print("MYOS uninstall plan:")
    print(f"- platform: {sys.platform}")
    if sys.platform == "darwin":
        print("- remove launchd agents (sync, pulse, autopilot, scheduler)")
    elif sys.platform.startswith("linux"):
        print(f"- remove systemd --user units ({SYSTEMD_TIMER_NAME}, {SYSTEMD_SERVICE_NAME})")
    if purge:
        print(f"- PURGE data dir: {data_dir}")
    else:
        print(f"- keep data dir: {data_dir} (pass --purge to delete)")

    if not apply:
        print("(dry-run) — pass without --dry-run to execute.")
        return

    if sys.platform == "darwin":
        print()
        cli_launchd.cmd_launchd_uninstall(argparse.Namespace(apply=True))
    elif sys.platform.startswith("linux"):
        print()
        _uninstall_linux_scheduler(apply=True)

    if purge and data_dir.exists():
        print()
        print(f"Purging {data_dir}")
        shutil.rmtree(data_dir)

    print("Done.")


def register_subparsers(sub: argparse._SubParsersAction) -> None:
    """Wire the ``install`` and ``uninstall`` subcommands into the
    top-level ``myos`` parser. Kept as a helper so ``cli.py`` only has
    to call one function rather than duplicating argument setup — the
    same shape used for other cli_* modules.
    """
    install = sub.add_parser(
        "install",
        help="One-shot install: create data dir, seed env file, register scheduler.",
    )
    install.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without touching disk or agents.",
    )
    install.add_argument(
        "--scheduler-interval-sec",
        type=int,
        default=60,
        help="Cadence (in seconds) for the reminder scheduler tick (default: 60).",
    )
    install.add_argument(
        "--no-load",
        dest="load",
        action="store_false",
        default=True,
        help="On macOS, write the plists but do not launchctl load them (default: load).",
    )
    install.set_defaults(func=cmd_install)

    uninstall = sub.add_parser(
        "uninstall",
        help="Reverse `myos install`: unload/remove agents. --purge also deletes the data dir.",
    )
    uninstall.add_argument("--dry-run", action="store_true")
    uninstall.add_argument(
        "--purge",
        action="store_true",
        help="Also delete the entire MYOS data directory (DB, logs, env file). Not reversible.",
    )
    uninstall.set_defaults(func=cmd_uninstall)


__all__ = [
    "cmd_install",
    "cmd_uninstall",
    "register_subparsers",
    "SYSTEMD_USER_DIR",
    "SYSTEMD_SERVICE_NAME",
    "SYSTEMD_TIMER_NAME",
]

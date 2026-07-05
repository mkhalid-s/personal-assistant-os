from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape


@dataclass(frozen=True)
class LaunchdRuntimeDependencies:
    load_env_file: Callable[[str], int]
    onboard_command: Callable[[argparse.Namespace], None]
    go_live_command: Callable[[argparse.Namespace], None]
    launchd_status_command: Callable[[argparse.Namespace], None]
    sanity_command: Callable[[argparse.Namespace], None]


def cmd_launchd_install(args: argparse.Namespace) -> None:
    project_root = Path(__file__).resolve().parents[2]
    env_file = args.env_file or str(project_root / "data" / ".env.myos")
    env_file_q = str(Path(env_file).expanduser().resolve())
    project_q = shlex.quote(str(project_root))
    env_q = shlex.quote(str(env_file_q))
    sync_cmd = f"cd {project_q} && source .venv/bin/activate && myos sync --connector all --env-file {env_q}"
    pulse_cmd = (
        f"cd {project_q} && source .venv/bin/activate && "
        f"myos pulse --env-file {env_q} --interval-sec {int(args.interval_sec)} "
        f"--meeting-hours {float(args.meeting_hours)}"
    )
    autopilot_cmd = (
        f"cd {project_q} && source .venv/bin/activate && "
        f"myos autopilot --env-file {env_q} --interval-sec {int(args.autopilot_interval_sec)}"
    )
    (project_root / "data").mkdir(parents=True, exist_ok=True)
    target_dir = Path.home() / "Library" / "LaunchAgents"
    dst_sync = target_dir / "com.myos.sync.plist"
    dst_pulse = target_dir / "com.myos.pulse.plist"
    dst_autopilot = target_dir / "com.myos.autopilot.plist"

    sync_plist = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
  <key>Label</key>
  <string>com.myos.sync</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>{xml_escape(sync_cmd)}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>{args.interval_sec}</integer>
  <key>StandardOutPath</key>
  <string>{xml_escape(str(project_root / 'data' / 'sync.log'))}</string>
  <key>StandardErrorPath</key>
  <string>{xml_escape(str(project_root / 'data' / 'sync.err.log'))}</string>
</dict>
</plist>
"""
    pulse_plist = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
  <key>Label</key>
  <string>com.myos.pulse</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>{xml_escape(pulse_cmd)}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{xml_escape(str(project_root / 'data' / 'pulse.log'))}</string>
  <key>StandardErrorPath</key>
  <string>{xml_escape(str(project_root / 'data' / 'pulse.err.log'))}</string>
</dict>
</plist>
"""
    autopilot_plist = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
  <key>Label</key>
  <string>com.myos.autopilot</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>{xml_escape(autopilot_cmd)}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{xml_escape(str(project_root / 'data' / 'autopilot.log'))}</string>
  <key>StandardErrorPath</key>
  <string>{xml_escape(str(project_root / 'data' / 'autopilot.err.log'))}</string>
</dict>
</plist>
"""

    print("Launchd plan:")
    print(f"- write {dst_sync}")
    print(f"- write {dst_pulse}")
    if args.autopilot:
        print(f"- write {dst_autopilot}")
    print(f"- env file for sync: {env_file_q}")
    print(f"- env file for pulse: {env_file_q}")
    if args.autopilot:
        print(f"- env file for autopilot: {env_file_q}")
    print(f"- load agents: {args.load}")
    if not args.apply:
        print("Dry run only. Re-run with --apply to execute.")
        return

    target_dir.mkdir(parents=True, exist_ok=True)
    dst_sync.write_text(sync_plist)
    dst_pulse.write_text(pulse_plist)
    if args.autopilot:
        dst_autopilot.write_text(autopilot_plist)
    print("Copied launchd files.")
    if args.load:
        launchctl = shutil.which("launchctl")
        if not launchctl:
            print("launchctl unavailable; copied files but skipped loading launch agents.")
            return
        subprocess.run([launchctl, "unload", str(dst_sync)], check=False)
        subprocess.run([launchctl, "unload", str(dst_pulse)], check=False)
        if args.autopilot:
            subprocess.run([launchctl, "unload", str(dst_autopilot)], check=False)
        subprocess.run([launchctl, "load", str(dst_sync)], check=False)
        subprocess.run([launchctl, "load", str(dst_pulse)], check=False)
        if args.autopilot:
            subprocess.run([launchctl, "load", str(dst_autopilot)], check=False)
        print("Loaded launch agents.")


def cmd_launchd_uninstall(args: argparse.Namespace) -> None:
    target_dir = Path.home() / "Library" / "LaunchAgents"
    dst_sync = target_dir / "com.myos.sync.plist"
    dst_pulse = target_dir / "com.myos.pulse.plist"
    dst_autopilot = target_dir / "com.myos.autopilot.plist"
    print("Launchd uninstall plan:")
    print(f"- remove {dst_sync}")
    print(f"- remove {dst_pulse}")
    print(f"- remove {dst_autopilot}")
    if not args.apply:
        print("Dry run only. Re-run with --apply to execute.")
        return
    if dst_sync.exists():
        subprocess.run(["launchctl", "unload", str(dst_sync)], check=False, capture_output=True, text=True)
        dst_sync.unlink()
    if dst_pulse.exists():
        subprocess.run(["launchctl", "unload", str(dst_pulse)], check=False, capture_output=True, text=True)
        dst_pulse.unlink()
    if dst_autopilot.exists():
        subprocess.run(["launchctl", "unload", str(dst_autopilot)], check=False, capture_output=True, text=True)
        dst_autopilot.unlink()
    print("Launch agents removed.")


def cmd_activate(args: argparse.Namespace, deps: LaunchdRuntimeDependencies) -> None:
    if args.env_file:
        loaded = deps.load_env_file(args.env_file)
        print(f"Loaded {loaded} vars from {args.env_file}")
    print("Activation flow: onboard -> go-live -> optional launchd install")
    deps.onboard_command(argparse.Namespace())
    print()
    deps.go_live_command(
        argparse.Namespace(
            connector=args.connector,
            env_file=args.env_file,
            external_limit=args.external_limit,
        )
    )
    if args.install_launchd:
        print()
        cmd_launchd_install(
            argparse.Namespace(
                apply=True,
                load=args.load_launchd,
                env_file=args.env_file,
                interval_sec=1800,
                meeting_hours=0.0,
                autopilot=False,
                autopilot_interval_sec=900,
            )
        )


def cmd_start(args: argparse.Namespace, deps: LaunchdRuntimeDependencies) -> None:
    print("Starting MYOS runtime: activate -> launchd status -> sanity")
    cmd_activate(
        argparse.Namespace(
            env_file=args.env_file,
            connector=args.connector,
            external_limit=args.external_limit,
            install_launchd=args.install_launchd,
            load_launchd=args.load_launchd,
        ),
        deps,
    )
    print()
    deps.launchd_status_command(argparse.Namespace())
    print()
    deps.sanity_command(argparse.Namespace(strict=False, report_dir=args.report_dir))


def cmd_stop(args: argparse.Namespace, deps: LaunchdRuntimeDependencies) -> None:
    print("Stopping MYOS runtime: unload/remove launchd -> status")
    cmd_launchd_uninstall(argparse.Namespace(apply=True))
    print()
    deps.launchd_status_command(argparse.Namespace())


def cmd_live(args: argparse.Namespace, deps: LaunchdRuntimeDependencies) -> None:
    cmd_activate(
        argparse.Namespace(
            env_file=args.env_file,
            connector="all",
            external_limit=100,
            install_launchd=args.install_launchd,
            load_launchd=args.load_launchd,
        ),
        deps,
    )

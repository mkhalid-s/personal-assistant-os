"""launchd unit generation for MYOS on macOS.

Two invocation modes are supported so the same generator serves both
developer workflows and end-user installs:

- **Dev mode** — chosen when this file is imported from a source
  checkout (a ``pyproject.toml`` sibling two directories up) AND that
  checkout has a ``.venv/bin/activate`` script next to it. The
  generated plist runs ``cd <repo> && source .venv/bin/activate &&
  myos <cmd>`` so contributors keep the working ``pip install -e .``
  loop with no extra flags.

- **Installed mode** — chosen for every other case (``pipx install``,
  bare wheel install, fresh CI checkout without a venv, etc). The
  generated plist runs an absolute path to the ``myos`` binary
  (resolved via ``shutil.which`` at install time) with no shell
  prelude, and stamps ``MYOS_DATA_DIR`` + ``MYOS_ENV_FILE`` into the
  plist's ``EnvironmentVariables`` block so the child inherits the
  paths the installer just decided on. This is the branch the one-line
  ``install.sh`` bootstrap depends on — a pipx install has no repo
  root and no venv script the plist could ``source``.

Path resolution (log files, env file, working dir) always flows
through :mod:`personal_assistant.data_dirs` so a single
``MYOS_DATA_DIR`` override changes behavior everywhere.
"""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from . import data_dirs


@dataclass(frozen=True)
class LaunchdRuntimeDependencies:
    load_env_file: Callable[[str], int]
    onboard_command: Callable[[argparse.Namespace], None]
    go_live_command: Callable[[argparse.Namespace], None]
    launchd_status_command: Callable[[argparse.Namespace], None]
    sanity_command: Callable[[argparse.Namespace], None]


_LAUNCHD_LABELS = ("com.myos.sync", "com.myos.pulse", "com.myos.autopilot", "com.myos.scheduler")


def _launchd_plist_paths() -> dict[str, Path]:
    """Destination plist path per agent label.

    Single source of truth for install/uninstall/stop/start so the
    lifecycle verbs (PAOS-022: stop must unload without deleting) can
    never drift from the paths the installer writes.
    """
    target_dir = Path.home() / "Library" / "LaunchAgents"
    return {label: target_dir / f"{label}.plist" for label in _LAUNCHD_LABELS}


def _launchctl() -> str | None:
    return shutil.which("launchctl")


def _agent_loaded(launchctl: str, label: str) -> bool:
    proc = subprocess.run([launchctl, "list", label], capture_output=True, text=True, check=False)
    return proc.returncode == 0


def _launchctl_verb(launchctl: str, verb: str, path: Path, label: str) -> bool:
    """Run ``launchctl <verb> <path>`` and report the per-label outcome
    (PAOS-045): ``loaded``/``unloaded`` on success, ``FAILED (rc=N)``
    otherwise, so a silently-failing launchctl is no longer invisible."""
    proc = subprocess.run([launchctl, verb, str(path)], capture_output=True, text=True, check=False)
    if proc.returncode == 0:
        print(f"- {label}: {verb}ed")
        return True
    print(f"- {label}: FAILED (rc={proc.returncode})")
    return False


def _dev_repo_root() -> Path | None:
    """Return the checkout root if we're in dev mode AND a usable
    ``.venv`` is available, else ``None``.

    Both conditions are required: a checkout without a venv should
    behave like an installed environment so the generated plist is
    directly runnable (no ``source .venv/bin/activate`` pointing at a
    directory that does not exist).
    """
    repo = data_dirs._repo_root_if_dev()
    if repo is None:
        return None
    if not (repo / ".venv" / "bin" / "activate").is_file():
        return None
    return repo


def _wrap_myos_invocation(subcmd: str) -> str:
    """Return the shell string that launchd should execute to run
    ``myos <subcmd>`` under the currently detected mode.

    ``subcmd`` is the args part only (e.g. ``"sync --connector all
    --env-file /foo/.env"``); this helper prepends the right launcher
    (venv-activation chain in dev mode, absolute binary path in
    installed mode) so the plist template does not care which branch
    it's in.
    """
    repo = _dev_repo_root()
    if repo is not None:
        project_q = shlex.quote(str(repo))
        return f"cd {project_q} && source .venv/bin/activate && myos {subcmd}"
    myos_bin = shutil.which("myos") or "myos"
    return f"{shlex.quote(myos_bin)} {subcmd}"


def _environment_variables_plist(env_file: Path) -> str:
    """Return the ``<key>EnvironmentVariables</key>...`` block for
    installed-mode plists, or an empty string in dev mode.

    Dev mode inherits its env from the ``source .venv/bin/activate``
    prelude; installed mode has no prelude so we bake ``MYOS_DATA_DIR``
    and ``MYOS_ENV_FILE`` directly into the plist. This makes the
    generated agent immune to the user's shell env drifting after
    install — if you ``myos install`` today with data-dir A, the agent
    keeps hitting A even if you later ``export MYOS_DATA_DIR=B`` in
    zsh, which is the right behavior for a background service.
    """
    if _dev_repo_root() is not None:
        return ""
    pairs = {
        "MYOS_DATA_DIR": str(data_dirs.resolve_data_dir()),
        "MYOS_ENV_FILE": str(env_file),
    }
    lines = ["  <key>EnvironmentVariables</key>", "  <dict>"]
    for key, value in pairs.items():
        lines.append(f"    <key>{xml_escape(key)}</key>")
        lines.append(f"    <string>{xml_escape(value)}</string>")
    lines.append("  </dict>")
    return "\n".join(lines) + "\n"


def cmd_launchd_install(args: argparse.Namespace) -> None:
    """Generate + optionally load MYOS's launchd agents (macOS).

    Emits four agents under ``~/Library/LaunchAgents`` — ``sync``,
    ``pulse``, ``autopilot`` (opt-in), ``scheduler`` (opt-in) — and
    routes their stdout/stderr to ``<data_dir>/logs/*.log``. The
    working paths (env file, log dir, DB) all resolve through
    :mod:`personal_assistant.data_dirs`, so a single ``MYOS_DATA_DIR``
    override reroutes the entire agent surface.
    """
    env_file_path = Path(args.env_file).expanduser().resolve() if args.env_file else data_dirs.resolve_env_file()
    env_q = shlex.quote(str(env_file_path))
    log_dir = data_dirs.resolve_log_dir()
    scheduler_interval = int(getattr(args, "scheduler_interval_sec", 60))
    sync_cmd = _wrap_myos_invocation(f"sync --connector all --env-file {env_q}")
    pulse_cmd = _wrap_myos_invocation(
        f"pulse --env-file {env_q} --interval-sec {int(args.interval_sec)} --meeting-hours {float(args.meeting_hours)}"
    )
    autopilot_cmd = _wrap_myos_invocation(
        f"autopilot --env-file {env_q} --interval-sec {int(args.autopilot_interval_sec)}"
    )
    scheduler_cmd = _wrap_myos_invocation("scheduler tick")

    env_vars_block = _environment_variables_plist(env_file_path)

    log_path = {name: log_dir / f"{name}.log" for name in ("sync", "pulse", "autopilot", "scheduler")}
    err_path = {name: log_dir / f"{name}.err.log" for name in ("sync", "pulse", "autopilot", "scheduler")}

    target_dir = Path.home() / "Library" / "LaunchAgents"
    paths = _launchd_plist_paths()
    dst_sync = paths["com.myos.sync"]
    dst_pulse = paths["com.myos.pulse"]
    dst_autopilot = paths["com.myos.autopilot"]
    dst_scheduler = paths["com.myos.scheduler"]

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
{env_vars_block}  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>{args.interval_sec}</integer>
  <key>StandardOutPath</key>
  <string>{xml_escape(str(log_path["sync"]))}</string>
  <key>StandardErrorPath</key>
  <string>{xml_escape(str(err_path["sync"]))}</string>
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
{env_vars_block}  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{xml_escape(str(log_path["pulse"]))}</string>
  <key>StandardErrorPath</key>
  <string>{xml_escape(str(err_path["pulse"]))}</string>
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
{env_vars_block}  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{xml_escape(str(log_path["autopilot"]))}</string>
  <key>StandardErrorPath</key>
  <string>{xml_escape(str(err_path["autopilot"]))}</string>
</dict>
</plist>
"""
    scheduler_plist = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
  <key>Label</key>
  <string>com.myos.scheduler</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>{xml_escape(scheduler_cmd)}</string>
  </array>
{env_vars_block}  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>{scheduler_interval}</integer>
  <key>StandardOutPath</key>
  <string>{xml_escape(str(log_path["scheduler"]))}</string>
  <key>StandardErrorPath</key>
  <string>{xml_escape(str(err_path["scheduler"]))}</string>
</dict>
</plist>
"""

    scheduler_enabled = bool(getattr(args, "scheduler", False))
    mode_label = "dev-venv" if _dev_repo_root() is not None else "installed"
    print("Launchd plan:")
    print(f"- mode: {mode_label}")
    print(f"- data dir: {data_dirs.resolve_data_dir()}")
    print(f"- log dir: {log_dir}")
    print(f"- write {dst_sync}")
    print(f"- write {dst_pulse}")
    if args.autopilot:
        print(f"- write {dst_autopilot}")
    if scheduler_enabled:
        print(f"- write {dst_scheduler} (StartInterval={scheduler_interval}s)")
    print(f"- env file for sync: {env_q}")
    print(f"- env file for pulse: {env_q}")
    if args.autopilot:
        print(f"- env file for autopilot: {env_q}")
    if scheduler_enabled:
        print(f"- scheduler runs: `{scheduler_cmd}`")
    print(f"- load agents: {args.load}")
    if not args.apply:
        print("Dry run only. Re-run with --apply to execute.")
        return

    log_dir.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir(parents=True, exist_ok=True)
    dst_sync.write_text(sync_plist)
    dst_pulse.write_text(pulse_plist)
    if args.autopilot:
        dst_autopilot.write_text(autopilot_plist)
    if scheduler_enabled:
        dst_scheduler.write_text(scheduler_plist)
    print("Copied launchd files.")
    if args.load:
        launchctl = _launchctl()
        if not launchctl:
            print("launchctl unavailable; copied files but skipped loading launch agents.")
            return
        wanted = [dst_sync, dst_pulse]
        if args.autopilot:
            wanted.append(dst_autopilot)
        if scheduler_enabled:
            wanted.append(dst_scheduler)
        load_results: list[tuple[str, bool]] = []
        for path in wanted:
            label = path.stem
            # Reload semantics: only unload agents that are actually loaded so a
            # first install doesn't print noise for a no-op unload (PAOS-045).
            if _agent_loaded(launchctl, label):
                _launchctl_verb(launchctl, "unload", path, label)
            load_results.append((label, _launchctl_verb(launchctl, "load", path, label)))
        failed_labels = [label for label, ok in load_results if not ok]
        if failed_labels:
            print(f"Warning: {len(failed_labels)} launch agent(s) failed to load: {', '.join(failed_labels)}.")
            print("Inspect <data_dir>/logs/*.err.log and re-run `myos launchd-install --apply --load`.")
            raise SystemExit(1)
        print("Loaded launch agents.")


def cmd_launchd_uninstall(args: argparse.Namespace) -> None:
    paths = _launchd_plist_paths()
    print("Launchd uninstall plan:")
    for label in _LAUNCHD_LABELS:
        print(f"- remove {paths[label]}")
    if not args.apply:
        print("Dry run only. Re-run with --apply to execute.")
        return
    launchctl = _launchctl()

    def unload(path: Path, label: str) -> None:
        # Tolerant by design: uninstall must succeed even when launchctl is
        # missing or the agent was never loaded; rc is captured, never fatal.
        if launchctl and _agent_loaded(launchctl, label):
            subprocess.run([launchctl, "unload", str(path)], check=False, capture_output=True, text=True)

    for label in _LAUNCHD_LABELS:
        path = paths[label]
        if path.exists():
            unload(path, label)
            path.unlink()
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
        # Deprecation notice: `myos install` is the new one-shot entry
        # point and also handles the Linux systemd path. The
        # ``activate --install-launchd`` flag stays for backward compat
        # but omits the scheduler (an install-time-only feature) and
        # will move to a warning-only alias in a future release.
        print("Note: `myos activate --install-launchd` is deprecated; prefer `myos install`.")
        cmd_launchd_install(
            argparse.Namespace(
                apply=True,
                load=args.load_launchd,
                env_file=args.env_file,
                interval_sec=1800,
                meeting_hours=0.0,
                autopilot=False,
                autopilot_interval_sec=900,
                scheduler=False,
                scheduler_interval_sec=60,
            )
        )
    # PAOS-022: stop no longer deletes plists, so start/activate must restore
    # the runtime by loading any installed-but-unloaded agents.
    restored = _load_unloaded_agents()
    if restored:
        print(f"Loaded {restored} previously installed launch agent(s).")


def _load_unloaded_agents() -> int:
    """``launchctl load`` every MYOS agent plist that exists on disk but is
    not currently loaded. Returns the count of newly loaded agents."""
    launchctl = _launchctl()
    if not launchctl:
        return 0
    loaded = 0
    for label in _LAUNCHD_LABELS:
        path = _launchd_plist_paths()[label]
        if path.exists() and not _agent_loaded(launchctl, label) and _launchctl_verb(launchctl, "load", path, label):
            loaded += 1
    return loaded


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
    # PAOS-022: stop is a runtime pause, not an uninstall — unload the loaded
    # agents but keep their plists so a later `myos start` can reload them.
    print("Stopping MYOS runtime: unload launchd agents (plists kept) -> status")
    launchctl = _launchctl()
    if not launchctl:
        print("launchctl unavailable; nothing to unload.")
    else:
        for label in _LAUNCHD_LABELS:
            path = _launchd_plist_paths()[label]
            if path.exists() and _agent_loaded(launchctl, label):
                _launchctl_verb(launchctl, "unload", path, label)
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

"""Tests for ``personal_assistant.cli_install`` — the one-shot install
bootstrap that ``scripts/install.sh`` calls after pipx finishes.

Covers:

- ``myos install --dry-run`` prints the plan without touching disk or
  spawning a service manager.
- Real install (``--dry-run=False``) creates the data + log dirs and
  seeds ``.env.myos`` from the repo template when missing; leaves an
  existing file alone (idempotent second-run).
- Platform branching: macOS delegates to ``cli_launchd.cmd_launchd_install``,
  Linux writes systemd-user unit files (and does NOT touch launchd).
- ``myos uninstall --dry-run`` prints the reverse plan; ``--purge``
  deletes the data dir but only when both apply AND purge are set.

We do not actually invoke ``launchctl`` or ``systemctl``; both are
patched to no-ops so tests run identically on any OS.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from personal_assistant import cli_install, cli_launchd  # noqa: E402


def _capture(func, *args, **kwargs) -> str:
    """Run ``func`` while capturing stdout; return the captured string."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        func(*args, **kwargs)
    return buf.getvalue()


class InstallDryRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._saved = {k: os.environ.pop(k, None) for k in ("MYOS_DATA_DIR", "MYOS_ENV_FILE")}
        os.environ["MYOS_DATA_DIR"] = self._tmp.name

    def tearDown(self) -> None:
        os.environ.pop("MYOS_DATA_DIR", None)
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v

    def test_dry_run_touches_nothing_on_disk(self) -> None:
        args = argparse.Namespace(dry_run=True, scheduler_interval_sec=60, load=False)
        out = _capture(cli_install.cmd_install, args)
        self.assertIn("MYOS install plan", out)
        self.assertIn(f"data dir: {self._tmp.name}", out)
        self.assertIn("(dry-run)", out)
        # Data dir override is a TemporaryDirectory that already exists;
        # what we care about is that no CHILD entries were created.
        self.assertEqual(list(Path(self._tmp.name).iterdir()), [])


class InstallSeedsEnvFileTest(unittest.TestCase):
    """Real install path: seeding ``.env.myos`` is the only side effect
    we can safely exercise cross-platform (launchd/systemd are patched
    to no-ops). Prove first-run seeds, second-run is a no-op.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._saved = {k: os.environ.pop(k, None) for k in ("MYOS_DATA_DIR", "MYOS_ENV_FILE")}
        os.environ["MYOS_DATA_DIR"] = self._tmp.name
        # Stub both platform installers so no real launchd/systemd
        # calls happen. Assertions cover only the seed-file behavior.
        self._patches = [
            mock.patch.object(cli_launchd, "cmd_launchd_install", lambda *a, **kw: None),
            mock.patch.object(cli_install, "_install_linux_scheduler", lambda **kw: None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        os.environ.pop("MYOS_DATA_DIR", None)
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v

    def _run_install(self) -> str:
        args = argparse.Namespace(dry_run=False, scheduler_interval_sec=60, load=False)
        return _capture(cli_install.cmd_install, args)

    def test_first_run_seeds_env_file_from_repo_template(self) -> None:
        # We run from a repo checkout, so ``.env.example`` exists and
        # its content should be copied verbatim into the target.
        out = self._run_install()
        env_target = Path(self._tmp.name) / ".env.myos"
        self.assertTrue(env_target.is_file())
        self.assertIn("env file seeded from", out)
        self.assertIn("Personal Assistant OS example configuration", env_target.read_text())

    def test_second_run_leaves_existing_env_file_intact(self) -> None:
        env_target = Path(self._tmp.name) / ".env.myos"
        env_target.write_text("USER_CUSTOMIZED=1\n")
        out = self._run_install()
        self.assertEqual(env_target.read_text(), "USER_CUSTOMIZED=1\n")
        self.assertIn("env file kept in place", out)


class SystemdUnitEmissionTest(unittest.TestCase):
    """The Linux branch renders a systemd-user timer + service pair.
    Assert on the file contents rather than actually writing them.
    """

    def test_service_unit_contains_env_and_absolute_bin(self) -> None:
        body = cli_install._systemd_scheduler_unit(
            myos_bin="/home/u/.local/bin/myos",
            env_file=Path("/tmp/env"),
            data_dir=Path("/tmp/data"),
        )
        self.assertIn("Type=oneshot", body)
        self.assertIn("Environment=MYOS_DATA_DIR=/tmp/data", body)
        self.assertIn("Environment=MYOS_ENV_FILE=/tmp/env", body)
        self.assertIn("ExecStart=/home/u/.local/bin/myos scheduler tick", body)

    def test_timer_unit_uses_configured_interval(self) -> None:
        body = cli_install._systemd_scheduler_timer(interval_sec=30)
        self.assertIn("OnBootSec=30s", body)
        self.assertIn("OnUnitActiveSec=30s", body)
        self.assertIn("Unit=myos-scheduler.service", body)
        self.assertIn("WantedBy=timers.target", body)


class InstallDelegatesToPlatformTest(unittest.TestCase):
    """Prove ``cmd_install`` fans out to the right platform installer."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._saved = {k: os.environ.pop(k, None) for k in ("MYOS_DATA_DIR",)}
        os.environ["MYOS_DATA_DIR"] = self._tmp.name

    def tearDown(self) -> None:
        os.environ.pop("MYOS_DATA_DIR", None)
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v

    def test_darwin_calls_launchd_install_with_scheduler(self) -> None:
        args = argparse.Namespace(dry_run=False, scheduler_interval_sec=60, load=False)
        with (
            mock.patch.object(cli_install.sys, "platform", "darwin"),
            mock.patch.object(cli_launchd, "cmd_launchd_install") as launchd,
        ):
            _capture(cli_install.cmd_install, args)
        launchd.assert_called_once()
        launchd_args = launchd.call_args.args[0]
        self.assertTrue(launchd_args.apply)
        self.assertTrue(launchd_args.scheduler)
        self.assertEqual(launchd_args.scheduler_interval_sec, 60)

    def test_linux_calls_systemd_installer_not_launchd(self) -> None:
        args = argparse.Namespace(dry_run=False, scheduler_interval_sec=45, load=False)
        with (
            mock.patch.object(cli_install.sys, "platform", "linux"),
            mock.patch.object(cli_launchd, "cmd_launchd_install") as launchd,
            mock.patch.object(cli_install, "_install_linux_scheduler") as systemd,
        ):
            _capture(cli_install.cmd_install, args)
        launchd.assert_not_called()
        systemd.assert_called_once()
        # Interval flows through verbatim.
        self.assertEqual(systemd.call_args.kwargs["interval_sec"], 45)

    def test_unknown_platform_prints_manual_hint(self) -> None:
        args = argparse.Namespace(dry_run=False, scheduler_interval_sec=60, load=False)
        with (
            mock.patch.object(cli_install.sys, "platform", "win32"),
            mock.patch.object(cli_launchd, "cmd_launchd_install") as launchd,
            mock.patch.object(cli_install, "_install_linux_scheduler") as systemd,
        ):
            out = _capture(cli_install.cmd_install, args)
        launchd.assert_not_called()
        systemd.assert_not_called()
        self.assertIn("no scheduler installer for platform 'win32'", out)


class UninstallTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._saved = os.environ.pop("MYOS_DATA_DIR", None)
        os.environ["MYOS_DATA_DIR"] = self._tmp.name

    def tearDown(self) -> None:
        os.environ.pop("MYOS_DATA_DIR", None)
        if self._saved is not None:
            os.environ["MYOS_DATA_DIR"] = self._saved

    def test_dry_run_lists_agent_removal_without_touching_disk(self) -> None:
        args = argparse.Namespace(dry_run=True, purge=False)
        with mock.patch.object(cli_install.sys, "platform", "darwin"):
            out = _capture(cli_install.cmd_uninstall, args)
        self.assertIn("MYOS uninstall plan", out)
        self.assertIn("remove launchd agents", out)
        self.assertIn("keep data dir", out)
        self.assertIn("(dry-run)", out)
        # Data dir still exists (was created by tempfile).
        self.assertTrue(Path(self._tmp.name).is_dir())

    def test_purge_flag_deletes_data_dir_when_applied(self) -> None:
        # Seed a marker file so we can prove the dir went away.
        (Path(self._tmp.name) / "marker.txt").write_text("x")
        args = argparse.Namespace(dry_run=False, purge=True)
        with (
            mock.patch.object(cli_install.sys, "platform", "darwin"),
            mock.patch.object(cli_launchd, "cmd_launchd_uninstall", lambda *a, **kw: None),
        ):
            out = _capture(cli_install.cmd_uninstall, args)
        self.assertIn("PURGE data dir", out)
        self.assertIn(f"Purging {self._tmp.name}", out)
        self.assertFalse(Path(self._tmp.name).exists())

    def test_purge_dry_run_still_keeps_data_dir(self) -> None:
        args = argparse.Namespace(dry_run=True, purge=True)
        with mock.patch.object(cli_install.sys, "platform", "darwin"):
            _capture(cli_install.cmd_uninstall, args)
        self.assertTrue(Path(self._tmp.name).is_dir())

    def test_linux_uninstall_targets_systemd_not_launchd(self) -> None:
        args = argparse.Namespace(dry_run=False, purge=False)
        with (
            mock.patch.object(cli_install.sys, "platform", "linux"),
            mock.patch.object(cli_launchd, "cmd_launchd_uninstall") as launchd,
            mock.patch.object(cli_install, "_uninstall_linux_scheduler") as systemd,
        ):
            _capture(cli_install.cmd_uninstall, args)
        launchd.assert_not_called()
        systemd.assert_called_once_with(apply=True)


class RegistrySpecTest(unittest.TestCase):
    """Both new subcommands must appear in the shared command registry
    so the router / help surface / audit tooling can see them.
    """

    def test_install_and_uninstall_specs_are_registered(self) -> None:
        from personal_assistant import command_registry

        names = {spec.command for spec in command_registry.COMMAND_SPECS}
        self.assertIn("install", names)
        self.assertIn("uninstall", names)

    def test_specs_declare_os_service_side_effect(self) -> None:
        from personal_assistant import command_registry

        by_name = {spec.command: spec for spec in command_registry.COMMAND_SPECS}
        for name in ("install", "uninstall"):
            self.assertIn("os_service_write", by_name[name].side_effects, name)


if __name__ == "__main__":
    unittest.main()

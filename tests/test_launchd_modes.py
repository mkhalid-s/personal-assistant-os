"""Tests for the two launchd emission modes in ``cli_launchd``.

The generator picks between two invocation shapes at install time:

- **Dev-venv mode** — repo checkout + ``.venv/bin/activate`` present.
  Emits ``cd <repo> && source .venv/bin/activate && myos <cmd>``
  and NO ``EnvironmentVariables`` block (the activate script owns
  PATH + env for the child).

- **Installed mode** — everything else (pipx, bare wheel, CI without a
  venv). Emits ``<absolute myos path> <cmd>`` with ``MYOS_DATA_DIR``
  and ``MYOS_ENV_FILE`` stamped into the plist so the agent stays
  pinned to the installer-chosen paths even if the user's shell env
  drifts.

We drive the generator via its public ``cmd_launchd_install`` entry
point in a dry-run configuration (``args.apply=False``) so no plist
files are written and no ``launchctl`` calls are made, then assert on
the emitted stdout (which prints the exact shell command that would
run) plus the underlying builder helpers.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from personal_assistant import cli_launchd, data_dirs  # noqa: E402


def _install_args(**overrides: object) -> argparse.Namespace:
    """Build the argparse namespace ``cmd_launchd_install`` expects.

    Kept as a helper so future new flags only need to be added in one
    place. Every test starts from the same dry-run defaults.
    """
    defaults: dict[str, object] = {
        "apply": False,
        "load": False,
        "env_file": None,
        "interval_sec": 1800,
        "meeting_hours": 0.0,
        "autopilot": False,
        "autopilot_interval_sec": 900,
        "scheduler": True,
        "scheduler_interval_sec": 60,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _run_install(**overrides: object) -> str:
    """Invoke ``cmd_launchd_install`` in dry-run and return stdout."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_launchd.cmd_launchd_install(_install_args(**overrides))
    return buf.getvalue()


class DevVenvModeTest(unittest.TestCase):
    """Simulate a repo checkout with .venv/bin/activate present."""

    def setUp(self) -> None:
        self._patcher = mock.patch.object(cli_launchd, "_dev_repo_root", return_value=Path("/fake/repo"))
        self._patcher.start()
        # Clean env-file override so the resolver runs its default path.
        self._env_backup = {k: os.environ.pop(k, None) for k in ("MYOS_DATA_DIR", "MYOS_ENV_FILE")}

    def tearDown(self) -> None:
        self._patcher.stop()
        for k, v in self._env_backup.items():
            if v is not None:
                os.environ[k] = v

    def test_wrap_myos_uses_venv_activation_chain(self) -> None:
        cmd = cli_launchd._wrap_myos_invocation("scheduler tick")
        # ``shlex.quote`` only adds quotes for shell-unsafe strings, so
        # ``/fake/repo`` is emitted bare. Assert on both the ``cd`` and
        # ``activate`` fragments to prove the shape without pinning
        # quoting behavior we don't control.
        self.assertIn("cd /fake/repo", cmd)
        self.assertIn("source .venv/bin/activate", cmd)
        self.assertTrue(cmd.endswith("&& myos scheduler tick"), cmd)

    def test_environment_variables_block_is_empty(self) -> None:
        # Dev mode inherits env from the activate script; the plist
        # should NOT carry MYOS_DATA_DIR / MYOS_ENV_FILE overrides.
        block = cli_launchd._environment_variables_plist(Path("/tmp/x/.env.myos"))
        self.assertEqual(block, "")

    def test_install_stdout_reports_dev_venv_mode(self) -> None:
        out = _run_install()
        self.assertIn("mode: dev-venv", out)
        self.assertIn("source .venv/bin/activate", out)
        self.assertIn("Dry run only", out)


class InstalledModeTest(unittest.TestCase):
    """Simulate a pipx install: no repo, no venv. ``_dev_repo_root``
    returns ``None`` and we control the resolved ``myos`` binary path.
    """

    def setUp(self) -> None:
        self._patches = [
            mock.patch.object(cli_launchd, "_dev_repo_root", return_value=None),
            mock.patch.object(cli_launchd.shutil, "which", return_value="/home/u/.local/bin/myos"),
        ]
        for p in self._patches:
            p.start()
        # Pin data-dir to a tempy path so plist paths are predictable
        # AND we do not depend on the caller's real ~/Library layout.
        self._env_backup = {k: os.environ.pop(k, None) for k in ("MYOS_DATA_DIR", "MYOS_ENV_FILE")}
        os.environ["MYOS_DATA_DIR"] = "/tmp/myos-installed-test"

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        os.environ.pop("MYOS_DATA_DIR", None)
        for k, v in self._env_backup.items():
            if v is not None:
                os.environ[k] = v

    def test_wrap_myos_uses_absolute_binary_no_activate(self) -> None:
        cmd = cli_launchd._wrap_myos_invocation("scheduler tick")
        self.assertNotIn(".venv", cmd)
        self.assertNotIn("source ", cmd)
        self.assertNotIn("cd ", cmd)
        # Absolute path + shlex-quoted (single quotes on POSIX).
        self.assertIn("/home/u/.local/bin/myos", cmd)
        self.assertTrue(cmd.endswith("scheduler tick"), cmd)

    def test_environment_variables_block_stamps_data_and_env(self) -> None:
        block = cli_launchd._environment_variables_plist(Path("/tmp/some/.env.myos"))
        self.assertIn("<key>EnvironmentVariables</key>", block)
        self.assertIn("<key>MYOS_DATA_DIR</key>", block)
        self.assertIn("<string>/tmp/myos-installed-test</string>", block)
        self.assertIn("<key>MYOS_ENV_FILE</key>", block)
        self.assertIn("<string>/tmp/some/.env.myos</string>", block)

    def test_install_stdout_reports_installed_mode(self) -> None:
        out = _run_install()
        self.assertIn("mode: installed", out)
        self.assertIn("data dir: /tmp/myos-installed-test", out)
        self.assertIn("log dir: /tmp/myos-installed-test/logs", out)
        self.assertIn("/home/u/.local/bin/myos", out)
        self.assertIn("Dry run only", out)

    def test_env_file_default_uses_data_dir(self) -> None:
        # No --env-file passed and no MYOS_ENV_FILE override → the
        # default should sit inside the resolved data dir.
        out = _run_install()
        self.assertIn("/tmp/myos-installed-test/.env.myos", out)


class InstalledModeWhichFallbackTest(unittest.TestCase):
    """When ``shutil.which('myos')`` returns None (rare, mid-install
    race) the generator must still produce a valid plist by falling
    back to the bare ``myos`` name. PATH resolution then becomes the
    user's responsibility, but the plist is at least well-formed.
    """

    def setUp(self) -> None:
        self._patches = [
            mock.patch.object(cli_launchd, "_dev_repo_root", return_value=None),
            mock.patch.object(cli_launchd.shutil, "which", return_value=None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()

    def test_wrap_myos_falls_back_to_bare_binary_name(self) -> None:
        cmd = cli_launchd._wrap_myos_invocation("scheduler tick")
        # No cd/source/activate; just "myos scheduler tick" (or the
        # shlex-quoted equivalent, which is still the bare name).
        self.assertNotIn(".venv", cmd)
        self.assertTrue(cmd.endswith("myos scheduler tick") or cmd.endswith("'myos' scheduler tick"), cmd)


class DevRepoRootDetectionTest(unittest.TestCase):
    """``_dev_repo_root`` must require BOTH the repo marker AND a
    ``.venv/bin/activate`` — a repo checkout without a venv should be
    treated as an installed environment so the generated plist does
    not point ``source`` at a non-existent script.
    """

    def test_returns_none_when_no_venv_even_in_repo(self) -> None:
        # data_dirs._repo_root_if_dev returns our repo, but we force
        # the .venv sibling to be absent.
        fake_repo = Path("/fake/repo/no-venv")
        with mock.patch.object(data_dirs, "_repo_root_if_dev", return_value=fake_repo):
            self.assertIsNone(cli_launchd._dev_repo_root())

    def test_returns_repo_when_marker_and_venv_present(self) -> None:
        # Use the real repo path (we ARE in a checkout) but force
        # a fake .venv/bin/activate to exist by patching Path.is_file.
        real_repo = Path(__file__).resolve().parents[1]
        with (
            mock.patch.object(data_dirs, "_repo_root_if_dev", return_value=real_repo),
            mock.patch.object(Path, "is_file", lambda self: str(self).endswith(".venv/bin/activate")),
        ):
            got = cli_launchd._dev_repo_root()
        self.assertEqual(got, real_repo)


if __name__ == "__main__":
    unittest.main()

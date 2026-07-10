"""Tests for ``personal_assistant.data_dirs`` — runtime data-dir resolution.

Covers the three precedence branches of ``resolve_data_dir``:

- ``MYOS_DATA_DIR`` env override (with ``~`` expansion)
- Dev-mode fallback (``<repo>/data`` when a ``pyproject.toml`` sibling
  is detected two directories up)
- Platform default (macOS Application Support vs Linux XDG)

Plus the ``resolve_env_file`` and ``resolve_log_dir`` helpers that
layer on top.

The tests deliberately do not create any directories or files —
``resolve_data_dir`` is contractually side-effect-free, so any
future regression that starts mkdir-ing on read will be caught by a
watchful reviewer + the fact that these tests pass without any
tempdir setup.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from personal_assistant import data_dirs  # noqa: E402


def _clean_env() -> dict[str, str | None]:
    """Return a mutable snapshot of the env vars this module inspects,
    so tests can restore them exactly (``patch.dict`` with ``clear=True``
    would also erase unrelated CI-provided vars like ``HOME``).
    """
    return {
        "MYOS_DATA_DIR": os.environ.get("MYOS_DATA_DIR"),
        "MYOS_ENV_FILE": os.environ.get("MYOS_ENV_FILE"),
        "XDG_DATA_HOME": os.environ.get("XDG_DATA_HOME"),
    }


class ResolveDataDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = _clean_env()
        # Every test starts with a clean slate — later tests set only
        # what they need. This avoids leakage from ambient dev env.
        for key in self._saved:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_env_override_absolute_wins(self) -> None:
        os.environ["MYOS_DATA_DIR"] = "/tmp/myos-custom"
        self.assertEqual(data_dirs.resolve_data_dir(), Path("/tmp/myos-custom"))

    def test_env_override_expands_tilde(self) -> None:
        os.environ["MYOS_DATA_DIR"] = "~/custom-myos"
        self.assertEqual(data_dirs.resolve_data_dir(), Path.home() / "custom-myos")

    def test_env_override_ignores_empty_string(self) -> None:
        # An empty MYOS_DATA_DIR must NOT win — it would resolve to the
        # process CWD and clobber the user's home dir. Whitespace-only
        # is treated identically for the same reason.
        os.environ["MYOS_DATA_DIR"] = "   "
        # We're running from a repo checkout, so the dev-mode branch fires.
        got = data_dirs.resolve_data_dir()
        self.assertTrue(str(got).endswith(f"{os.sep}data"), got)

    def test_dev_mode_returns_repo_data(self) -> None:
        # This test file itself lives inside the repo checkout, so the
        # dev-mode detector must resolve to <repo>/data. We assert the
        # `pyproject.toml` sibling exists to make the intent explicit.
        got = data_dirs.resolve_data_dir()
        self.assertEqual(got.name, "data")
        self.assertTrue((got.parent / "pyproject.toml").is_file(), got.parent)

    def test_platform_default_macos_when_not_dev(self) -> None:
        # Simulate an installed environment by patching the dev detector
        # to None. This is the branch a pipx install lands on.
        with (
            mock.patch.object(data_dirs, "_repo_root_if_dev", return_value=None),
            mock.patch.object(data_dirs.sys, "platform", "darwin"),
        ):
            got = data_dirs.resolve_data_dir()
        self.assertEqual(got, Path.home() / "Library" / "Application Support" / "myos")

    def test_platform_default_linux_uses_local_share_when_no_xdg(self) -> None:
        with (
            mock.patch.object(data_dirs, "_repo_root_if_dev", return_value=None),
            mock.patch.object(data_dirs.sys, "platform", "linux"),
        ):
            got = data_dirs.resolve_data_dir()
        self.assertEqual(got, Path.home() / ".local" / "share" / "myos")

    def test_platform_default_linux_honors_xdg_data_home(self) -> None:
        os.environ["XDG_DATA_HOME"] = "/tmp/xdg-share"
        with (
            mock.patch.object(data_dirs, "_repo_root_if_dev", return_value=None),
            mock.patch.object(data_dirs.sys, "platform", "linux"),
        ):
            got = data_dirs.resolve_data_dir()
        self.assertEqual(got, Path("/tmp/xdg-share") / "myos")

    def test_platform_default_linux_expands_xdg_tilde(self) -> None:
        os.environ["XDG_DATA_HOME"] = "~/custom-xdg"
        with (
            mock.patch.object(data_dirs, "_repo_root_if_dev", return_value=None),
            mock.patch.object(data_dirs.sys, "platform", "linux"),
        ):
            got = data_dirs.resolve_data_dir()
        self.assertEqual(got, Path.home() / "custom-xdg" / "myos")

    def test_resolve_never_creates_directories(self) -> None:
        # Contract: ``resolve_data_dir`` is a pure computation. Callers
        # that want the dir on disk have to mkdir explicitly. This
        # guards against a future "helpful" mkdir slipping in.
        os.environ["MYOS_DATA_DIR"] = "/tmp/definitely-not-created-by-this-test-abc123xyz"
        got = data_dirs.resolve_data_dir()
        self.assertFalse(got.exists(), f"resolve_data_dir must not create {got}")


class ResolveEnvFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = _clean_env()
        for key in self._saved:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_env_override_wins(self) -> None:
        os.environ["MYOS_ENV_FILE"] = "/tmp/some.env"
        self.assertEqual(data_dirs.resolve_env_file(), Path("/tmp/some.env"))

    def test_env_override_expands_tilde(self) -> None:
        os.environ["MYOS_ENV_FILE"] = "~/secrets/.env.myos"
        self.assertEqual(data_dirs.resolve_env_file(), Path.home() / "secrets" / ".env.myos")

    def test_default_is_data_dir_slash_env_myos(self) -> None:
        self.assertEqual(data_dirs.resolve_env_file(), data_dirs.resolve_data_dir() / ".env.myos")


class ResolveLogDirTest(unittest.TestCase):
    def test_is_data_dir_slash_logs(self) -> None:
        self.assertEqual(data_dirs.resolve_log_dir(), data_dirs.resolve_data_dir() / "logs")


class DbPathIntegrationTest(unittest.TestCase):
    """Prove the plumbing from ``data_dirs`` to ``db.resolve_db_path``
    actually flows — otherwise a future refactor that inlines the
    default back into ``db.py`` would silently regress the pipx path.
    """

    def setUp(self) -> None:
        self._saved = _clean_env()
        self._saved_db = os.environ.pop("MYOS_DB_PATH", None)
        for key in self._saved:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if self._saved_db is not None:
            os.environ["MYOS_DB_PATH"] = self._saved_db

    def test_db_default_derives_from_data_dir(self) -> None:
        from personal_assistant import db

        os.environ["MYOS_DATA_DIR"] = "/tmp/db-integration-check"
        self.assertEqual(db.resolve_db_path(), Path("/tmp/db-integration-check") / "assistant.db")

    def test_db_env_still_wins_over_data_dir(self) -> None:
        from personal_assistant import db

        os.environ["MYOS_DATA_DIR"] = "/tmp/should-be-ignored"
        os.environ["MYOS_DB_PATH"] = "/tmp/explicit.db"
        self.assertEqual(db.resolve_db_path(), Path("/tmp/explicit.db"))


if __name__ == "__main__":
    unittest.main()

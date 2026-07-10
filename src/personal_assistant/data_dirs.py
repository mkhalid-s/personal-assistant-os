"""Runtime data-directory resolution for MYOS.

This module owns the "where does MYOS keep its state" decision so that
one answer is used consistently by ``db.py`` (SQLite file), launchd /
systemd unit generation (log paths + working directory), and the
``myos install`` bootstrap. Prior to this module every call site
reached for ``Path(__file__).resolve().parents[2] / "data"`` — that
worked when the package was imported from a repo checkout with
``.venv`` next to it, but broke the moment the package was installed
via ``pipx`` (no repo, no ``.venv``, no ``data/`` sibling).

Resolution precedence for ``resolve_data_dir()``:

1. ``MYOS_DATA_DIR`` environment variable (absolute or ``~``-expanded)
   — the operator escape hatch, honored in every mode.
2. Dev-mode fallback: ``<repo>/data`` when this module is imported from
   a source checkout, detected by a ``pyproject.toml`` sibling two
   directories up (i.e. ``src/personal_assistant/data_dirs.py`` ->
   parents[2] is the repo root). Preserves the existing developer
   workflow (`pip install -e .` + `data/` in the repo).
3. Platform default: macOS
   ``~/Library/Application Support/myos``, Linux
   ``$XDG_DATA_HOME/myos`` (default ``~/.local/share/myos``). This is
   the branch a ``pipx install`` lands on.

``resolve_env_file()`` and ``resolve_log_dir()`` layer on top so the
same precedence flows through to the ``.env.myos`` file location and
launchd/systemd log paths without every call site re-deriving it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "myos"

# The dev-mode marker: if either of these files sits at parents[2] of
# this file, we're running from a source checkout, not an installed
# wheel. ``pyproject.toml`` is the primary; ``setup.py`` is kept as a
# defensive secondary so a future ``setup.py``-only tree still works.
_DEV_MARKERS = ("pyproject.toml", "setup.py")


def _repo_root_if_dev() -> Path | None:
    """Return the repo root path when this module is imported from a
    source checkout, else ``None`` (installed-mode).

    A ``pipx install`` copies the package into
    ``~/.local/pipx/venvs/<name>/lib/pythonX.Y/site-packages/personal_assistant/``,
    so ``parents[2]`` is the venv's ``lib`` directory with no
    ``pyproject.toml`` — the detector cleanly separates the two cases
    without any environment plumbing.
    """
    candidate = Path(__file__).resolve().parents[2]
    for marker in _DEV_MARKERS:
        if (candidate / marker).is_file():
            return candidate
    return None


def _platform_data_dir() -> Path:
    """Return the OS-native per-user data directory for MYOS.

    macOS: ``~/Library/Application Support/myos`` (Apple's convention
    for per-user app state that isn't user documents).

    Linux: honors ``XDG_DATA_HOME`` per the XDG Base Directory spec,
    falling back to ``~/.local/share/myos``.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser() / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def resolve_data_dir() -> Path:
    """Return the effective runtime data directory. Never creates it.

    See module docstring for precedence rules. Callers that need the
    directory to exist should ``mkdir(parents=True, exist_ok=True)``
    the returned path themselves — this helper stays side-effect-free
    so unit tests can call it freely.
    """
    override = os.environ.get("MYOS_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    repo = _repo_root_if_dev()
    if repo is not None:
        return repo / "data"
    return _platform_data_dir()


def resolve_env_file() -> Path:
    """Return the effective ``.env.myos`` path.

    Precedence: ``MYOS_ENV_FILE`` env var > ``<data_dir>/.env.myos``.
    Kept separate from ``resolve_data_dir`` because operators sometimes
    keep the env file in a secrets manager mount that differs from
    their DB location.
    """
    override = os.environ.get("MYOS_ENV_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return resolve_data_dir() / ".env.myos"


def resolve_log_dir() -> Path:
    """Return the directory for launchd/systemd stdout+stderr logs.

    Currently always ``<data_dir>/logs``. Kept as a helper so callers
    stop hardcoding repo-relative paths and so a future override
    (``MYOS_LOG_DIR``) can slot in without changing call sites.
    """
    return resolve_data_dir() / "logs"

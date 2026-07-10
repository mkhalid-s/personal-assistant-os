#!/usr/bin/env bash
# MYOS one-shot installer.
#
# Design: install MYOS with a single command on any macOS or Linux
# host that already has Python 3.10+. We deliberately do NOT bundle
# our own Python — the target audience is developers who already have
# a modern Python on their box, and a bundled interpreter would balloon
# the installer to tens of megabytes for negligible UX win.
#
# What this script does, in order:
#   1. Detect OS + Python 3.10+ (fail early with a clear message).
#   2. Bootstrap pipx if missing (`python3 -m pip install --user pipx`
#      + `pipx ensurepath`), then re-check PATH so the new pipx bin
#      is reachable within THIS shell invocation.
#   3. Install (or reinstall / upgrade) the MYOS package into a
#      pipx-managed isolated venv:
#        - default source: git+https://github.com/mkhalid-s/personal-assistant-os@main
#        - override with:  --source pypi   (once we publish)
#                          --source git@<ref>
#                          --source local:<path-to-checkout>
#                          --source wheel:<path-to-wheel>
#   4. Call `myos install` so the platform-appropriate background
#      scheduler (launchd on macOS, systemd --user on Linux) is
#      registered and the data dir + .env.myos are seeded.
#
# The uninstaller mirror lives at ./scripts/uninstall.sh.
#
# Exit codes:
#   0  success
#   1  usage / argument error
#   2  Python version below 3.10 or python3 not on PATH
#   3  pipx bootstrap failed
#   4  pipx install of the wheel failed
#   5  `myos install` failed after wheel install succeeded
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/mkhalid-s/personal-assistant-os/main/scripts/install.sh | bash
#   ./scripts/install.sh                          # default: git+main
#   ./scripts/install.sh --source git@v0.1.0      # pin to a tag
#   ./scripts/install.sh --source local:.         # install from cwd
#   ./scripts/install.sh --dry-run                # show plan only
#   ./scripts/install.sh --skip-post-install      # wheel only, no `myos install`

set -euo pipefail

REPO_URL="https://github.com/mkhalid-s/personal-assistant-os"
PACKAGE_NAME="personal-assistant-os"
DEFAULT_REF="main"

# Default source lives at `git@main` until the first PyPI publish
# (see .github/workflows/publish.yml + the P3 slice of the packaging
# plan). Once `personal-assistant-os` exists on pypi.org, flip this to
# `pypi` — the shell script UX and CI smoke both already accept the
# new value with no further changes.
DRY_RUN=0
SKIP_POST=0
SOURCE_SPEC="git@${DEFAULT_REF}"

usage() {
  sed -n '1,55p' "$0" | sed -n '/^# /p' | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

log()  { printf '[myos-install] %s\n' "$*" >&2; }
warn() { printf '[myos-install][warn] %s\n' "$*" >&2; }
die()  { local code="$1"; shift; printf '[myos-install][fatal] %s\n' "$*" >&2; exit "$code"; }

# --- argument parsing --------------------------------------------------------

while [ $# -gt 0 ]; do
  case "$1" in
    --source)     shift; SOURCE_SPEC="${1:?--source requires a value}";;
    --source=*)   SOURCE_SPEC="${1#--source=}";;
    --dry-run)    DRY_RUN=1;;
    --skip-post-install) SKIP_POST=1;;
    -h|--help)    usage 0;;
    *) die 1 "unknown argument: $1 (run with --help)";;
  esac
  shift
done

# --- environment probing -----------------------------------------------------

detect_os() {
  case "$(uname -s 2>/dev/null || echo unknown)" in
    Darwin)  echo "macos" ;;
    Linux)   echo "linux" ;;
    *) die 1 "unsupported OS: $(uname -s). MYOS supports macOS and Linux." ;;
  esac
}

require_python() {
  # We need python3 >= 3.10. The system-provided `python3` on modern
  # macOS (14+) and Ubuntu (22.04+) is fine; older LTS releases (18.04)
  # ship 3.6 and are explicitly not supported.
  if ! command -v python3 >/dev/null 2>&1; then
    die 2 "python3 not found on PATH. Install Python 3.10+ and re-run."
  fi
  local ver
  ver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  local major="${ver%.*}"
  local minor="${ver#*.}"
  if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 10 ]; }; then
    die 2 "Python ${ver} is too old. MYOS requires 3.10+."
  fi
  log "Python ${ver} detected."
}

# --- pipx bootstrap ----------------------------------------------------------

ensure_pipx() {
  if command -v pipx >/dev/null 2>&1; then
    log "pipx already on PATH: $(command -v pipx)"
    return
  fi
  log "pipx not found; bootstrapping via python3 -m pip install --user pipx"
  if [ "$DRY_RUN" -eq 1 ]; then
    log "(dry-run) would run: python3 -m pip install --user pipx"
    log "(dry-run) would run: python3 -m pipx ensurepath"
    return
  fi
  if ! python3 -m pip install --user --upgrade pipx; then
    die 3 "failed to install pipx via pip"
  fi
  # `pipx ensurepath` prints its own instructions; refresh PATH so this
  # shell can find the newly installed pipx binary without a re-login.
  python3 -m pipx ensurepath >/dev/null 2>&1 || true
  local pipx_bin
  pipx_bin="$(python3 -c 'import site; print(site.getuserbase())')/bin"
  case ":$PATH:" in
    *":$pipx_bin:"*) : ;;
    *) export PATH="$pipx_bin:$PATH" ;;
  esac
  if ! command -v pipx >/dev/null 2>&1; then
    die 3 "pipx installed but not reachable. Add ${pipx_bin} to PATH and re-run."
  fi
}

# --- source-spec resolution --------------------------------------------------

# Translate our friendly --source spec into whatever pipx wants.
resolve_pipx_target() {
  local spec="$1"
  case "$spec" in
    pypi)            echo "$PACKAGE_NAME" ;;
    git@*)           echo "git+${REPO_URL}@${spec#git@}" ;;
    local:*)         echo "${spec#local:}" ;;
    wheel:*)         echo "${spec#wheel:}" ;;
    git+*|http*|/*|.|./*|*.whl)
                     echo "$spec" ;;
    *) die 1 "unrecognized --source spec: $spec (see --help)" ;;
  esac
}

install_myos() {
  local target
  target="$(resolve_pipx_target "$SOURCE_SPEC")"
  log "Installing MYOS from: ${target}"
  if [ "$DRY_RUN" -eq 1 ]; then
    log "(dry-run) would run: pipx install --force ${target}"
    return
  fi
  if ! pipx install --force "${target}"; then
    die 4 "pipx install failed for ${target}"
  fi
}

# --- post-install ------------------------------------------------------------

run_post_install() {
  if [ "$SKIP_POST" -eq 1 ]; then
    log "--skip-post-install set; leaving background scheduler + data dir to you."
    return
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    log "(dry-run) would run: myos install"
    # We deliberately do NOT call myos in dry-run because the pipx
    # install step above was itself dry-run — the binary is not
    # guaranteed to exist. Emitting the intent is enough for a smoke.
    return
  fi
  log "Running: myos install"
  if ! myos install; then
    die 5 "myos install failed. Check the plan and re-run manually with: myos install"
  fi
}

# --- main --------------------------------------------------------------------

main() {
  local os
  os="$(detect_os)"
  log "OS: ${os}, source: ${SOURCE_SPEC}, dry-run: ${DRY_RUN}, skip-post: ${SKIP_POST}"
  require_python
  ensure_pipx
  install_myos
  run_post_install
  log "Done. Verify with: myos --help"
  log "Uninstall with: bash <(curl -fsSL ${REPO_URL}/raw/main/scripts/uninstall.sh)"
}

main

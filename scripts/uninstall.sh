#!/usr/bin/env bash
# MYOS one-shot uninstaller.
#
# Order matters: reverse of install.sh.
#   1. `myos uninstall` — unloads launchd (macOS) or systemd --user
#      (Linux) agents. Optionally purges the data dir when --purge
#      is passed (data dir preserved by default so a reinstall keeps
#      the user's local knowledge base).
#   2. `pipx uninstall personal-assistant-os` — removes the wheel +
#      isolated venv, freeing the `myos` binary from PATH.
#
# `myos uninstall` runs BEFORE pipx uninstall because after pipx
# removes the venv the `myos` binary is gone and step 1 would fail.
#
# Exit codes:
#   0  success
#   1  usage / argument error
#   6  `myos uninstall` failed but pipx still ran
#   7  pipx uninstall failed
#
# Usage:
#   ./scripts/uninstall.sh                # remove agents, keep data
#   ./scripts/uninstall.sh --purge        # also delete data dir
#   ./scripts/uninstall.sh --dry-run      # show plan only

set -euo pipefail

PACKAGE_NAME="personal-assistant-os"
DRY_RUN=0
PURGE=0

usage() {
  sed -n '1,28p' "$0" | sed -n '/^# /p' | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

log()  { printf '[myos-uninstall] %s\n' "$*" >&2; }
warn() { printf '[myos-uninstall][warn] %s\n' "$*" >&2; }
die()  { local code="$1"; shift; printf '[myos-uninstall][fatal] %s\n' "$*" >&2; exit "$code"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --purge)   PURGE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage 0 ;;
    *) die 1 "unknown argument: $1 (run with --help)" ;;
  esac
  shift
done

remove_agents() {
  if ! command -v myos >/dev/null 2>&1; then
    warn "myos binary not on PATH — background agents may still be registered."
    warn "If so: remove ~/Library/LaunchAgents/com.myos.*.plist manually (macOS) or"
    warn "  ~/.config/systemd/user/myos-scheduler.* (Linux), then re-run this script."
    return
  fi
  local args=("uninstall")
  [ "$PURGE" -eq 1 ]   && args+=("--purge")
  [ "$DRY_RUN" -eq 1 ] && args+=("--dry-run")
  log "Running: myos ${args[*]}"
  if ! myos "${args[@]}"; then
    warn "myos uninstall exited non-zero; will still attempt pipx uninstall below."
    return 6
  fi
}

remove_wheel() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log "(dry-run) would run: pipx uninstall ${PACKAGE_NAME}"
    return
  fi
  if ! command -v pipx >/dev/null 2>&1; then
    warn "pipx not on PATH; skipping wheel removal."
    return
  fi
  if pipx list 2>/dev/null | grep -q "package ${PACKAGE_NAME}"; then
    log "Running: pipx uninstall ${PACKAGE_NAME}"
    if ! pipx uninstall "${PACKAGE_NAME}"; then
      die 7 "pipx uninstall ${PACKAGE_NAME} failed"
    fi
  else
    log "pipx has no ${PACKAGE_NAME} package registered; nothing to remove."
  fi
}

main() {
  log "Uninstall plan: purge=${PURGE}, dry-run=${DRY_RUN}"
  local rc=0
  remove_agents || rc=$?
  remove_wheel
  if [ "$rc" -ne 0 ]; then
    exit "$rc"
  fi
  log "Done."
}

main

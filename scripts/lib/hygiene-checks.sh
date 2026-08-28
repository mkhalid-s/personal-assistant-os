#!/usr/bin/env bash
# Public hygiene check functions — sourced by CI scripts to share logic.
# Each function exits 1 on failure and prints a clear diagnostic.
# Requires: git on PATH.

# Trailing-whitespace and CRLF check over the given commit range.
# Usage: hygiene_check_whitespace <git-range>
hygiene_check_whitespace() {
  local range="$1"
  git diff --check "$range"
}

# Scan the working tree for patterns that must not appear in a public repo.
# Usage: hygiene_check_private_refs
hygiene_check_private_refs() {
  local patterns=("Guide""wire" "GW Bed""rock" "/Users/""mshaikh/Documents/""GW")
  local found=0
  for pattern in "${patterns[@]}"; do
    if git grep -n -F "$pattern" -- ':!*.lock'; then
      echo "Found private reference: $pattern"
      found=1
    fi
  done
  return "$found"
}

# Fail if any commit in the range carries a Co-authored-by trailer.
# Usage: hygiene_check_coauthor_trailers <git-range>
hygiene_check_coauthor_trailers() {
  local range="$1"
  local trailer_pattern='^Co-authored-''by:'
  local msgs
  msgs="$(git log --format=%B "$range")"
  if printf '%s\n' "$msgs" | grep -qi "$trailer_pattern"; then
    echo "Found co-author trailer in commit messages"
    return 1
  fi
}

# Fail if any tracked file is a local-only artifact (env files, db, logs, etc).
# Usage: hygiene_check_tracked_artifacts
hygiene_check_tracked_artifacts() {
  local tracked
  tracked="$(git ls-files)"
  if printf '%s\n' "$tracked" | grep -E '(^|/)(\.env|\.DS_Store|\.cursor|\.claude)(/|$)|\.(db|sqlite|sqlite3|log)$'; then
    echo "Tracked local-only artifact"
    return 1
  fi
}

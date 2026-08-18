#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

git fetch origin refs/heads/main:refs/remotes/origin/main
if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
  git fetch --unshallow
fi

range="HEAD~1..HEAD"
if [ -n "${BUILDKITE_PULL_REQUEST:-}" ] && [ "${BUILDKITE_PULL_REQUEST}" != "false" ]; then
  range="origin/main..HEAD"
fi

git diff --check "$range"

patterns=("Guide""wire" "GW Bed""rock" "/Users/""mshaikh/Documents/""GW")
for pattern in "${patterns[@]}"; do
  if git grep -n -F "$pattern" -- ':!*.lock'; then
    echo "Found private reference: $pattern"
    exit 1
  fi
done

trailer_pattern='^Co-authored-''by:'
msgs="$(git log --format=%B "$range")"
if printf '%s\n' "$msgs" | grep -qi "$trailer_pattern"; then
  echo "Found co-author trailer in commit messages"
  exit 1
fi

tracked="$(git ls-files)"
if printf '%s\n' "$tracked" | grep -E '(^|/)(\.env|\.DS_Store|\.cursor|\.claude)(/|$)|\.(db|sqlite|sqlite3|log)$'; then
  echo "Tracked local-only artifact"
  exit 1
fi

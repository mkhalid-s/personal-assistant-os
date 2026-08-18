#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

git fetch --depth=1 origin main

git diff --check HEAD~1..HEAD

patterns=("Guide""wire" "GW Bed""rock" "/Users/""mshaikh/Documents/""GW")
for pattern in "${patterns[@]}"; do
  if git grep -n -F "$pattern" -- ':!*.lock'; then
    echo "Found private reference: $pattern"
    exit 1
  fi
done

range="HEAD~1..HEAD"
if [ -n "${BUILDKITE_PULL_REQUEST:-}" ] && [ "${BUILDKITE_PULL_REQUEST}" != "false" ]; then
  range="origin/main..HEAD"
fi
trailer_pattern='^Co-authored-by:'
! git log --format=%B "$range" | grep -i "$trailer_pattern"

! git ls-files | grep -E '(^|/)(\.env|\.DS_Store|\.cursor|\.claude)(/|$)|\.(db|sqlite|sqlite3|log)$'

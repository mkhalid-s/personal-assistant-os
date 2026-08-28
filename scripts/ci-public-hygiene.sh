#!/usr/bin/env bash
# Buildkite public hygiene gate — mirrors the GH Actions hygiene job.
# Shared check logic lives in scripts/lib/hygiene-checks.sh; update there,
# not here, when check behaviour needs to change.
set -euo pipefail

cd "$(dirname "$0")/.."

# shellcheck source=scripts/lib/hygiene-checks.sh
source "$(dirname "$0")/lib/hygiene-checks.sh"

git fetch origin refs/heads/main:refs/remotes/origin/main
if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
  git fetch --unshallow
fi

range="HEAD~1..HEAD"
if [ -n "${BUILDKITE_PULL_REQUEST:-}" ] && [ "${BUILDKITE_PULL_REQUEST}" != "false" ]; then
  range="origin/main..HEAD"
fi

hygiene_check_whitespace           "$range"
hygiene_check_private_refs
hygiene_check_coauthor_trailers    "$range"
hygiene_check_tracked_artifacts

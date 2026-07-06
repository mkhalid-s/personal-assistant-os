# Zero Proof Loop Demo

This demo proves the MYOS control-plane loop with GitLawb Zero as the coding worker:

```text
intent -> retrieved context -> Zero worktree patch -> review packet -> approval -> receipt -> learning
```

Use a disposable target repository. Do not run the first proof against a repo with work you care about.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

export MYOS_DEMO_DIR="$PWD/data/zero-proof"
export MYOS_DB_PATH="$MYOS_DEMO_DIR/assistant.db"
mkdir -p "$MYOS_DEMO_DIR"

export MYOS_AGENT_EXEC_ZERO_STREAM="zero exec"
zero --version
myos doctor --strict
myos migrations verify --strict
```

`myos doctor` should show `zero_stream_executor` as `PASS` when the configured Zero command advertises stream-json support. If it is `INFO`, fix the Zero install or `MYOS_AGENT_EXEC_ZERO_STREAM` before running the proof.

Create a disposable git repo:

```bash
export DEMO_REPO="$MYOS_DEMO_DIR/repo"
mkdir -p "$DEMO_REPO"
git -C "$DEMO_REPO" init -b main
git -C "$DEMO_REPO" config user.email "demo@example.com"
git -C "$DEMO_REPO" config user.name "MYOS Demo"
printf 'seed\n' > "$DEMO_REPO/README.md"
git -C "$DEMO_REPO" add -A
git -C "$DEMO_REPO" commit -m "Initial demo repo"
```

## Run The Loop

```bash
myos intent create "Use Zero to make a small docs-only change in the disposable demo repo" \
  --constraint "Do not commit, push, open PRs, or mutate external systems" \
  --success "Zero produces a reviewable approval-gated patch"

myos factory policy set --mode semi_autonomous --scope-type intent --scope-id 1

myos factory start \
  --intent 1 \
  --mode semi_autonomous \
  --pack software_delivery \
  --executor zero \
  --repo "$DEMO_REPO" \
  --timeout 600 \
  --max-turns 3 \
  --verify-command "git diff --check"

myos factory status --id 1
myos factory review --id 1
myos approve --list
```

At this point the disposable repo should still be unchanged. The review and approval-list output should show a Zero executor artifact, changed files, patch size stats, run/session references, compact permission/warning/error signals, suggested verification commands, an approval action id, a safe MYOS retry command, and a command like:

```bash
myos approve --action <action_id> --execute
```

If Zero is missing, times out, returns an incomplete status, or emits an unsupported stream schema, MYOS keeps the run reviewable and creates a `zero_executor` follow-up inbox item. Inspect it with:

```bash
myos factory status --id 1
myos factory review --id 1
myos inbox list
```

If Zero produces a very large diff, MYOS keeps it as a review-only draft instead of enqueueing a truncated patch. Narrow the task scope and rerun before applying changes.

Apply only if the proposed patch is acceptable and the repo is disposable:

```bash
myos approve --action <action_id> --execute
myos execution-receipt list
myos execution-receipt show --id <receipt_id>
myos factory learn --id 1 --outcome success --notes "Zero proof patch reviewed and applied in disposable repo."
myos factory retrospective --id 1
```

## Notes

- Keep Zero at its default MYOS `--auto low` posture unless policy and the repo risk justify more autonomy.
- MYOS approval is still required for patch application, commits, pushes, PRs, and connector mutations.
- `myos code "fix tests" --backend zero` is a quick direct handoff. Use the factory path above when you want intent, retrieval, review packet, receipt, and learning records.
- If a Zero run is interrupted, inspect temporary git worktrees before retrying with `git -C "$DEMO_REPO" worktree list` and remove stale disposable worktrees only after confirming they are no longer needed.

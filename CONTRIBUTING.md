# Contributing

Thanks for your interest in Personal Assistant OS (MYOS). This project ships as small, reviewable **surgical slices** — one bounded change per commit, each independently validated by the same gates CI enforces.

If you are looking for a deeper walkthrough of the codebase, module boundaries, and how to add a new CLI command, read `DEVELOPING.md`.

## Ground rules

- **Local-first, privacy-first.** MYOS keeps user data in a local SQLite database. Do not add code paths that phone home, silently sync data off-device, or bypass the redaction filters in `personal_assistant.privacy`.
- **Bounded autonomy.** Any change that lets the agent take an external action must remain approval-gated and auditable. Read `docs/BOUNDED_AUTONOMY.md` before you touch approval, patch, or executor paths.
- **Small commits.** A commit should do one clearly named thing. If the plan is bigger than one commit, split it into a sequence of ordered slices in the PR description.

## Development environment

MYOS targets Python 3.10, 3.11, and 3.12 on macOS and Linux. There are no runtime dependencies beyond the standard library and a small optional set declared in `pyproject.toml`.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

That installs the `myos` console entrypoint. Verify:

```bash
myos --help
myos doctor --strict
```

## Local dev loop

Run the full test suite exactly the way CI runs it — under the strict resource-warning gate:

```bash
PYTHONPATH=src python -W error::ResourceWarning -m unittest discover -s tests
```

Run the strict release readiness gate:

```bash
PYTHONPATH=src python -m personal_assistant.cli release-check --strict
```

Both must be green before you push.

Every registered CLI command is also smoke-tested for parser wiring by `tests/test_backlog.py` — that test is what catches "new command added, forgot to plumb into `build_parser`". If it fails, fix the parser, not the test.

## Commit hygiene

- Author and committer identity must match the personal identity used by the rest of the history (`git log -1 --format='%an <%ae>'`). Do not push work-account commits.
- **No auto-added trailers.** Do not commit `Co-authored-by:` trailers from AI tools. The CI hygiene job scans commit messages in each PR range and fails the build if a trailer is present. If your editor or hook auto-appends one, rewrite the commit with `git commit-tree` before pushing.
- Commit messages should describe the *why*, not the *what*. First line ≤72 characters, ending with a period. Body wrapped at 72 characters explains the motivation, tradeoffs, and any operator-facing behavior change.
- Do not commit local artifacts (`.env`, `.DS_Store`, `.cursor/`, `.claude/`, `*.db`, `*.log`) — the release-check hygiene gate refuses them. `myos doctor --strict` will also complain if it finds any staged.

## Pull requests

Before opening a PR:

1. Rebase on `main`.
2. Run the local dev loop above.
3. Verify `git diff --check main..HEAD` is clean (no whitespace errors).
4. Update `CHANGELOG.md` under `## Unreleased` and, if the change is on the bounded-autonomy path, update `docs/BOUNDED_AUTONOMY.md`.
5. If the change adds or modifies a CLI command, update `command_registry.COMMAND_SPECS` in the same commit — the command contract audit will fail otherwise.

Every PR runs three CI jobs:

- **Tests** — the full unittest suite on Python 3.10, 3.11, and 3.12 under `-W error::ResourceWarning`.
- **Public Hygiene** — whitespace check, private-reference scan, commit-message trailer scan, and local-artifact scan.
- **Release Readiness** — installs the wheel, smokes `myos --help`, and runs `myos release-check --strict` covering schema, dependency license, required files, packaging entrypoint, command contract, public hygiene, local artifacts, and the factory smoke run.

All three must pass. Do not force-merge past a failing gate.

## Reporting issues

Open a GitHub issue describing:

- What you were doing (exact `myos` command and arguments — redact user data).
- What you expected.
- What actually happened, including any output from `myos doctor --strict` and `myos migrations verify --strict`.
- Environment: OS, Python version, `myos --version` if available.

Please redact anything sensitive before pasting logs. MYOS runs privacy filters when persisting data; those filters do not run on text you paste into an issue.

## Code of conduct

Be kind, be specific, be honest about tradeoffs. Assume good faith and prefer teaching over blocking. This project is small and local-first — every contribution is welcome.

## License

By contributing you agree that your contributions are licensed under the Apache License 2.0. See `LICENSE`.

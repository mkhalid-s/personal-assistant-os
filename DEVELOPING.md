# Developing MYOS

This document is the internals guide for contributors. For contribution flow, PR expectations, and commit hygiene, read `CONTRIBUTING.md`. For strategy and roadmap read `ARCHITECTURE.md`, `ROADMAP.md`, and `docs/BOUNDED_AUTONOMY.md`.

## Repository shape

```
src/personal_assistant/
├── cli.py                      # argparse top-level, thin dispatcher
├── cli_agent.py                # delegate/approve/execution-receipt handlers
├── cli_autonomy.py             # autonomy loop CLI
├── cli_autopilot.py            # autopilot CLI
├── cli_diagnostics.py          # diagnostics/observability CLI
├── cli_factory.py              # factory workflow CLI
├── cli_health.py               # doctor / release-check / cutover / tune
├── cli_knowledge.py            # entity/claim/relationship CLI
├── cli_launchd.py              # macOS launchd install/lifecycle
├── cli_local_data.py           # backup/restore/migrations verify
├── cli_operations.py           # worker/orchestrate/run-day
├── cli_planning.py             # intent/plan/review-packet handlers
├── cli_review.py               # weekly-review, digests, dashboard
├── cli_runtime.py              # runtime presentation helpers
├── cli_setup_live.py           # first-run setup
├── cli_workflow.py             # goal/loop/agent-run
├── command_registry.py         # single source of truth for command metadata
├── db.py                       # SQLite connection, schema, migrations
├── migrations.py               # schema_migrations helper
├── privacy.py                  # redaction filters + retention policy
├── observability.py            # execution traces, correlation IDs, rollups
├── autonomy.py                 # action classification (safe/needs_approval/blocked)
├── autonomy_loop.py            # durable bounded loop
├── autopilot.py                # proactive local cycles
├── execution.py                # approval + execute + apply_patch + integrity
├── factory.py                  # review-first workflow packs and stages
├── zero_executor.py            # external `zero` streaming coding executor
├── planner.py, plans.py        # planning/review-packet primitives
├── intents.py                  # first-class intents
├── agentcore.py                # agent tasks + proposals
├── assistant.py                # backend-agnostic run_turn loop
├── router.py                   # tiny-model + rules routing
├── retrieval.py                # hybrid retrieval (FTS + graph + pseudo-embed)
├── graphrag.py, graph.py       # graph expansion + traversal
├── entities.py, relationships.py, claims.py, em.py   # deterministic extraction
├── inbox.py, queries.py        # inbox + work item queries
├── context.py                  # context assembly for the assistant
├── extraction.py               # note kind classification, entity mining
├── dashboard.py                # aggregated status views
├── pulse.py                    # periodic heartbeat / rollups
├── model_setup.py, models.py   # tiny-model install helpers
├── locks.py                    # process locks used by autopilot/worker
├── voice.py, watch.py          # voice loop, watched folders
├── connectors/                 # jira/github/confluence/aha adapters
├── ingest/                     # audio/image/text ingestion
└── providers/                  # backend adapters (claude, cursor, agent_cli, sdk)

tests/                          # unittest suite (~250 tests)
docs/                           # long-form design docs
examples/                       # end-to-end demo walkthroughs
```

## Layering

Roughly (top depends on bottom, never the other way):

```
CLI (cli.py + cli_*.py)
  └─> Application (autopilot, autonomy_loop, assistant, factory, dashboard, pulse)
        └─> Agent & Executors (execution, zero_executor, agentcore, planner, plans)
              └─> Retrieval / GraphRAG (retrieval, graphrag, graph, entities, ...)
                    └─> Domain (intents, inbox, queries, context, extraction)
                          └─> Persistence + Cross-cutting (db, migrations, privacy, observability, autonomy)
```

The command registry (`command_registry.py`) and `router.py` are cross-cutting and can be read from anywhere.

Do not add upward imports. If a lower layer needs a decision that lives above it, pass it in as an argument.

## Persistence

- The SQLite schema is defined imperatively in `db.py`. Every non-trivial change adds a new `if current < N:` block, an `ALTER TABLE`/`CREATE TABLE`, and an `INSERT OR IGNORE INTO schema_migrations` row.
- Bump `EXPECTED_SCHEMA_VERSION` in the same commit.
- Update the migration list assertion in `tests/test_cli.py::test_backup_restore_and_migration_verify` and any test that references the previous expected version.
- Prefer additive migrations (new nullable columns) so existing rows stay usable. Backfills belong in a follow-up slice with its own tests.

## Bounded autonomy invariants

The following must remain true after every change. Break one and your PR will not merge:

1. **`autonomy.classify_action` is the single authority** for whether an action is safe / needs approval / blocked. Never inline classification in a CLI handler.
2. **`execution._execute_agent_action` is the single execution chokepoint.** All executors — CLI approve, autopilot, factory, agent SDK — route through it.
3. **`apply_patch` refuses protected paths, tree escapes, and symlink hunks.** See `_PROTECTED_PATH_SEGMENTS` and the symlink guard in `execution.py`.
4. **Approval integrity is verified before execution.** `approve_and_execute` pins `payload_hash` and `approved_at` at approval and refuses drift or TTL expiry. Do not add code paths that skip this check for `payload_hash != NULL` rows.
5. **Redaction runs before persistence.** Anything user-derived (notes, connector payloads, executor stderr) must pass through `personal_assistant.privacy` before it lands in a table or is echoed into a receipt/artifact.
6. **External mutations are always approval-gated** unless the connector adapter explicitly proves it is a read.
7. **CI hygiene: no `Co-authored-by:` trailers, no private references, no local artifacts** — the hygiene job enforces all three on every push.

## How to add a new CLI command

Every new command touches three places. Do all three in one commit; the release-check `command_contract` gate refuses drift.

### 1. Handler function

Put it in an existing `cli_*.py` module if it thematically fits (e.g., new approval-related command → `cli_agent.py`). Otherwise create a new `cli_*.py` module. Every handler takes a single `argparse.Namespace` and returns `None`. Open connections with `get_connection()` and close them on every exit path (see `cli_operations.cmd_worker` for the canonical `try/finally` shape).

```python
def cmd_my_new_command(args: argparse.Namespace) -> None:
    conn = get_connection()
    try:
        # ... work ...
        print(...)
    finally:
        conn.close()
```

### 2. `command_registry.COMMAND_SPECS`

Add a `CommandSpec` entry with the correct `tier`, `safety`, `intent`, `summary`, `examples`, and side-effect metadata. The audit rules that fail `release-check --strict` if you get it wrong:

- `safety` must be one of `SAFETY_LEVELS`.
- `tier` must be one of `TIERS`.
- Every `side_effect` must be one of `SIDE_EFFECT_TYPES`.
- Every example must begin with `myos `.
- `safety ∈ {approval_gated, external_write}` or `side_effects` includes `database_restore` implies `requires_confirmation=True`.
- `summary` must be non-empty.
- The command name in the registry must match the top-level parser command.

### 3. `cli.build_parser`

Add a `sub.add_parser(<name>, help=...)` block with the right arguments and either `.set_defaults(func=cmd_my_new_command)` for a flat command or nested `.add_subparsers(dest=..., required=True)` for a group. `tests/test_backlog.py` will fail if the wiring is missing.

Then wire it through the thin dispatcher: if the handler lives in `cli_my_module.py`, add a one-line indirection in `cli.py` (`def cmd_my_new_command(args): cli_my_module.cmd_my_new_command(args)`) so `build_parser` binds the top-level symbol.

### 4. Validation checklist

```bash
PYTHONPATH=src python -W error::ResourceWarning -m unittest discover -s tests
PYTHONPATH=src python -m personal_assistant.cli release-check --strict
```

Both must be green. `release-check` runs `command_contract` and `factory_smoke` gates that specifically catch registry drift and workflow regressions.

## How to add a new schema migration

1. In `db.py` `initialize_schema`, add the next `if current < N:` block. Prefer `ALTER TABLE ... ADD COLUMN <col> <type>` (nullable) over destructive DDL.
2. Register the migration name: `INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (N, 'add_<what>')`.
3. Bump `EXPECTED_SCHEMA_VERSION = N` at the top of `db.py`.
4. Update `tests/test_cli.py`:
   - `test_backup_restore_and_migration_verify` asserts every migration is listed and the expected version matches.
   - Any other test that mentions the previous expected version.
5. Update `tests/test_observability.py::test_schema_trace_lifecycle_and_cleanup_rollup` if it asserts a specific `expected_version`.
6. If the migration changes an approval-critical table (`agent_actions`, `action_execution_receipts`, `agent_tasks`), also update `docs/BOUNDED_AUTONOMY.md` with the new guarantee.

## How to add a new executor backend

An executor produces `agent_actions` proposals from an intent. It must:

- Return structured results in a stable schema (see `zero_executor.ZeroRunResult` for the shape).
- Enforce a wall-clock timeout at the subprocess boundary.
- Redact stderr and stdout through `privacy.apply_privacy_filters` before persisting.
- Attach a review-packet artifact via `plans.attach_executor_artifact` with a stable schema string so downstream tooling can evolve without breaking.
- Never call `execute_action` directly — always propose via `agentcore.enqueue_proposal` so the approval + integrity + audit chain runs.

Doctor probe: add a preflight check in `cli_health._<backend>_preflight()` and wire it into `_optional_checks` so `myos doctor` reports whether the backend is installed and correctly configured.

## Debugging tips

- `myos doctor` prints DB health, connector reachability, tiny-model status, and any optional executor preflight.
- `myos migrations verify --strict` fails if the schema is behind the expected version.
- `myos release-check --strict` runs every guard the CI release-readiness job runs.
- `MYOS_TRACE=1` on a command enables execution trace recording via `observability`; inspect with `myos trace list/show`.
- `MYOS_ALLOW_DESTRUCTIVE=1` (interactive TTY only) is the emergency override for `autonomy.BLOCKED` actions. Avoid in tests.
- Reset local state: delete `data/assistant.db*` and re-run any `myos` command; migrations reinitialize from scratch.

## Testing conventions

- Use `unittest.TestCase`. Avoid pytest-only fixtures — the suite runs under stdlib.
- Set up a fresh temp SQLite database per test class or method (`_fresh_db_conn` helpers already exist in `test_assistant.py` and `test_remediation.py`).
- Always register cleanup: `self.addCleanup(conn.close)` — the CI runs under `-W error::ResourceWarning`, so a leaked connection fails the build.
- For subprocess-based tests, always pass a `timeout=` — a hung subprocess will hang CI.
- Prefer small, focused tests. If a test needs more than ~50 lines of setup, extract a helper.

## Where the safety-critical code lives

If you touch any of these files, expect an extra-careful review:

- `execution.py` — `_execute_agent_action`, `approve_and_execute`, apply_patch guards, approval integrity.
- `autonomy.py` — action classification. Adding a new BLOCKED action-type here is a soft breaking change.
- `privacy.py` — redaction regexes and retention policy.
- `db.py` — schema migrations. A wrong migration is nearly impossible to undo cleanly.
- `factory.py` — `_prepare_zero_software_action` and the worktree lifecycle.
- `.github/workflows/ci.yml` — the hygiene gate is the only thing keeping private references and AI-tool trailers out of the public repo.

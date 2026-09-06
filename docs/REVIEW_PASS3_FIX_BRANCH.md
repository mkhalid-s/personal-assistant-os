# Pass-3 Independent Verification — `fix/code-review-findings`

- **Date:** 2026-09-06
- **Reviewed:** branch `fix/code-review-findings` (worktree `.worktrees/fix-code-review`), 29 commits ahead of `main` @ `99a00f0`, tip `bd9bb18`. 46 files, +2913/−227.
- **Method:** adversarial re-review of the full diff against the 53-finding audit (PAOS-001..053), interaction-hotspot analysis, and independent re-runs (full test suite and ruff) plus scratch-DB experiments for the migration/retention/FTS paths.
- **Claims verified:** 430 tests passed / 241 subtests (rerun locally, 94.6s) and ruff clean — both executor claims reproduced.

## VERDICT

**fix-first** — the remediation is substantively correct (all 3 Highs and 16/18 mediums verified), but PAOS-018's retention sweep crashes with a real FK violation in production settings (proven empirically, masked by its test), and PAOS-007's diff-redaction exemption was wired into only one of the two `apply_patch` producers. Both are small, surgical fixes.

## FINDING VERIFICATION

| Finding | Status | Evidence / note |
|---|---|---|
| 001 H (migrations 42/43) | ok | `db.py:1787-1815` — row-presence repair + `PRAGMA table_info` guard; empirically tested (fresh, v41→44, missing-row with/without column, 8-way concurrent race: all pass) |
| 002 H (stranded executions) | ok, caveats | `execution.py:262-329` (reaper: reset→failed, follow_up, event, no auto-retry); wired at `execution.py:912`, `autonomy_loop.py:588`, `cli_autopilot.py:44` — see R4 for clock-basis flaw |
| 003 H (pipx paths) | ok | `autopilot.py:417`, `dashboard.py:78`, `cli_runtime.py:40`, `cli_setup_live.py:104`, `cli_review.py:375`, `cli_health.py:135-151` (doctor dev-only downgrade), `cli_health.py:304` |
| 004 (crc32 person refs) | ok | `context.py:273` — note: refs minted pre-fix under `hash()` don't migrate (one-time split of old synthetic nodes; acceptable) |
| 005 (learn() scoping) | ok | `factory.py:1330-1337` — scoped via `factory_artifacts` |
| 006 (query redaction) | ok | write `graphrag.py:314-324`, read `factory.py:485` (filtered lookup); no other raw-query readers |
| 007 + 015 (diff exempt / display) | issue | factory path ok (`factory.py:905-911`); preview + `repo_root` on all four approval surfaces ok (`execution.py:1071-1082`, `cli_agent.py:297-306, 678-687, 596-605`) — **but `assistant.py:154-159` apply_patch enqueue lacks `skip_keys` → R2** |
| 008 (suggestions purge) | ok | `privacy.py:252-256`, tested with FK ON |
| 010 (pragma order) | ok | `db.py:46-49` |
| 009 / 030 / 031 / 032 | ok | migration 44 `db.py:1817-1829`; fast-path `db.py:83-104`; `type='table'` `db.py:1912`; policy hoist `privacy.py:98-106, 128-158` |
| 011 (claim-before-dispatch) | ok | `cli_reminders.py:222-244` — crash-safe (see hotspot c) |
| 012 (chokepoint) | ok | `autopilot.py:75-86` |
| 013 (receipts) | ok | `autonomy_loop.py:184-189`, `autopilot.py:287-291` |
| 014 (noop mapping) | ok | `execution.py:216-227`; all three prefixes match producers verbatim (`execution.py:570, 618, 623`); `test_cli.py` expectation change is semantically right |
| 016 (post-claim re-verify) | ok | `execution.py:975-1003` — refuses to `failed`, does not strand |
| 017 (result redaction) | issue | approve path ok (`execution.py:1005-1021`); autonomy_loop ok — **autopilot's `_execute_safe_autopilot_actions` still persists raw result (`autopilot.py:299-311`) → R3** |
| 018 (retention) | **regression** | `observability.py:217-274` — FK crash, see R1 |
| 019 (renew_lock) | ok | `locks.py:46-57`, wired in run_day/go_live/autopilot/pulse |

Lows sampled (24 of 34): 020, 021, 022, 023, 024, 025, 026, 027, 030, 031, 032, 035, 036, 038, 039, 040, 041, 042, 044, 045, 046, 047, 048, 049, 050, 051, 052, 053 verified ok. **PAOS-028/029: no distinct change found in the diff or commit messages** — possibly subsumed by the 024/046 except-narrowings, but unverifiable against the audit brief.

## HOTSPOT ANALYSIS (a–h)

- **(a) db.py migration machinery** — No path skips a needed migration or re-runs a destructive one; empirically verified (fresh / v41 upgrade / ledger-missing-42 with and without the column / 8-process concurrent first-run all correct). The 42/43 guard narrows the pre-existing ALTER TOCTOU; whole-chain-in-one-transaction + `busy_timeout` serialization held under an 8-process race. The gapless fast-path is correct: `_ensure_fts5` inserts ledger rows 17/19 when it heals (`db.py:1870-1871`), so a no-FTS5-build DB re-engages the fast path after healing. Residual R8 below: a ledger gap at 17/19 *with the FTS table present* never heals → fast path permanently off for that DB (perf only; the full chain stays idempotent).
- **(b) execution.py** — Reaper vs live execution: yes, it can race (R4). Post-claim integrity refusal sets `status='failed'` — cannot re-strand rows. Result redaction order (redact→truncate) safe on `str`; noop mapping exact.
- **(c) scheduler** — Not silently lost: neither `mark_fired` nor `notify()` commits internally; claim + dispatch result + `reminder_missed` row commit atomically (`cli_reminders.py:241`). A crash before that commit rolls back to `pending` → re-fire next tick (at-least-once). The only loss window is an OS-level send that lands before the commit — unavoidable. `finally` releases the lock; the fallback fires on dispatch failure inside the same transaction.
- **(d) skip_keys** — Only `factory.py:911` passes `skip_keys`, only `{"diff"}`; all 13 other `enqueue_proposal` callers use full redaction. Approval surfaces show the diff (015). Exception: R2 — the assistant.py producer was missed.
- **(e) retention FKs** — R1 confirmed: `route_feedback.event_log_id INTEGER NOT NULL REFERENCES event_log(id)` (`db.py:1412, 1421`, no CASCADE) — `event_log` is deleted *first* in `_OPERATIONAL_RETENTION_TABLES` while `route_feedback` still references those rows. `execution_traces.route_event_id` (`db.py:1487`) is the same hazard when traces outlive the sweep. Children `retrieval_run_sources` / `route_eval_cases` correctly precede parents.
- **(f) token matcher** — All seven probe names (`delete_all_users`, `drop_schema`, `truncate_logs`, `wipe_disk`, `force_delete`, `prod_wipe`, `close_all_sessions`) still BLOCK against `_DESTRUCTIVE_HINTS`. New bypass class (R5): camelCase concatenations (`DeleteAllUsers` → single token) and inflections (`deleted_all_users` → `deleted` ≠ `delete`) fall from BLOCKED to CONFIRM (still approval-gated, never auto-run).
- **(g) launchd** — Clean: stop unloads-only (`cli_launchd.py:429-456`); start→activate reloads only not-loaded agents (`_load_unloaded_agents` guards with `_agent_loaded`) → no double-load, no KeepAlive fight; uninstall still unloads + unlinks all four labels.
- **(h) inbox list** — Consistently registered (`cli.py:1034`, `command_registry.py:551`, handler `cli_workflow.py:62`), read-only, no PII beyond capture-time filtering; two tests. Nit: `--limit -5` → SQLite `LIMIT -5` = unlimited.

## REGRESSIONS

1. **R1 — High** — `observability.py:245-266`, call sites `cli_autopilot.py:47` (every autopilot cycle) and `cli_local_data.py:174` (`myos cleanup`). With `PRAGMA foreign_keys=ON` (set by `get_connection`, `db.py:49`), the first DELETE (`event_log`) raises `IntegrityError: FOREIGN KEY constraint failed` once any `route_feedback` row references an event older than the cutoff — empirically reproduced on a scratch DB. Since `route_feedback` rows are never swept elsewhere, **every autopilot cycle will crash permanently once history crosses the window**. Fix: delete `route_feedback` (and any other `event_log` children) before `event_log`.
2. **R2 — High/Medium** — `assistant.py:154-159`: `apply_patch` proposals from `delegate_to_agent` (reachable via `cli_agent.py:110, 242`) are enqueued **without** `skip_keys={"diff"}`, so `redact_obj` rewrites diff bytes (any `user@example.com` in a test fixture, key-shaped string, etc.) — the patch applied to the user's repo silently differs from what the agent produced and what the preview implies. Incomplete PAOS-007.
3. **R3 — Medium** — `autopilot.py:299-311`: the safe-action path persists the raw executor result into `agent_actions.result` and `agent_observations.content` unredacted/untruncated (compare `autonomy_loop.py:176-181`, which redacts + truncates). PAOS-017's provider-stderr leak persists on this path (the receipt copy is redacted; these two copies are not).
4. **R4 — Medium** — `execution.py:289-291`: the reaper's clock is `COALESCE(approved_at, executed_at, created_at)`, but safe-action claims (`autonomy_loop.py:164-167`, `autopilot.py:~296`) stamp no timestamp — an action created hours before its claim is reap-bait while a live execution runs; the reaper also `recovered.append`s and emits the "crashed run" follow_up/event even when its CAS UPDATE matched 0 rows (`execution.py:303-323`, no rowcount check — same class as PAOS-050). Outcome: false "stranded" artifacts + contradictory audit trail; bounded because there is no auto-retry and the terminal UPDATE wins.
5. **R5 — Low** — PAOS-033 camelCase/inflection bypass (hotspot f); deliberate tradeoff, worth a hint-normalization follow-up.
6. **R6 — Deviation** — PAOS-037 residual: `cli_agent.py:1069-1083` — requests *without* `action_id` still honor the self-attested `safety.approved` for `--execute`; documented as intended in `test_cli.py:3374-3377`. Confirm no deployed `MYOS_ACTION_COMMAND` wrapper passes `--execute` for id-less provider requests.
7. **R8 — Low** — db.py fast-path / FTS ledger-gap residue (hotspot a).
8. **R9 — Low** — `dashboard.py:212` prints the tokened URL, but `BaseHTTPRequestHandler` default logging echoes every request line (`GET /?token=…`) to stderr — the token accumulates in terminal logs. Override `log_message` or log a redacted path.
9. **R10 — Low** — `cli.py:1780` `remind` subparsers `required=False` + no top-level positional: the legacy `myos remind "text" --at 15:00` form now argparse-errors instead of creating (documented in `_REMIND_USAGE`; intentional per the fix note, but a CLI behavior break).

## TEST WEAKNESSES

1. **`OperationalRetentionTest` (`tests/test_code_review_fixes.py:1444`)** — builds on `_memory_conn()`, which leaves `PRAGMA foreign_keys` **OFF**, while every production connection (`get_connection`) turns it **ON**. The suite seeds exactly the old-route_feedback→old-event_log shape that crashes in production and asserts success — it *masks R1*, the highest-severity regression in this branch. One-line fix (`_memory_conn(foreign_keys=True)`) would have caught it.
2. **`PragmaOrderTest` (`tests/test_code_review_fixes.py:103-112`)** — asserts character offsets of pragma strings inside the `db.py` source text: tests the file's text, not behavior (would pass even if the pragmas were dead code). Brittle and non-behavioral.
3. **`LocksTest.test_lock_stale_constant_is_four_hours` (`tests/test_code_review_fixes.py:510-513`)** — pure constant restatement; tautological and mechanically updated alongside any threshold change, so it provides no regression protection (the neighboring behavioral lock tests do the real work).

## SCOPE

Clean. 46 files match the finding list + tests + the five docs/infra files; the only added file is `tests/test_code_review_fixes.py` (65 tests — count verified). No secrets (the only `ghp_`/email strings are synthetic test fixtures), no debug prints, no stray files (`git status` clean in the worktree).

## DECISIONS NEEDED BEFORE MERGING

1. **R1 blocks**: one-statement fix — move `route_feedback` (and consider `execution_traces`) deletion before `event_log` in `apply_operational_retention`, and flip the test to `foreign_keys=True`.
2. **R2**: add `skip_keys=frozenset({"diff"})` to `assistant.py:154` (or route both producers through one helper) — otherwise delegate-mode patches get `[REDACTED_*]` literals applied into code.
3. **Confirm PAOS-028/029** against the original audit text — no distinct change found.
4. **R6**: confirm operationally that `MYOS_ACTION_COMMAND` wrappers don't combine `--execute` with id-less provider requests.
5. R3/R4/R5/R8–R10 are follow-up-grade; none blocks on its own.

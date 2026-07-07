# Bounded Autonomy Foundation

MYOS grows autonomy through small, auditable layers rather than a broad uncontrolled agent loop. The current foundation is:

1. Route the user request.
2. Retrieve local context when useful.
3. Plan or execute safe local work.
4. Gate external or risky actions behind approval.
5. Record receipts and privacy-safe learning metadata.

## Router Feedback Application

Feedback is applied only by exact request hash. MYOS records a `text_hash` in `smart_route` events, stores router corrections in `route_feedback`, and activates matching rows in `route_overrides`. The raw request text is not stored in the override path.

This means a correction affects the same future phrasing only. Related but different requests still use deterministic routing and the optional tiny local router model.

Useful commands:

```bash
myos router feedback --event 123 --expected-intent daily_brief
myos router overrides
```

## Command Registry And Tool Awareness

Purpose: give the router and tiny local model a structured map of what MYOS can actually do.

Implemented scope:

- Define command metadata for top-level and important nested commands in `command_registry.py`.
- Include safety tier, command tier, required arguments, examples, and whether confirmation is needed.
- Feed the registry into `myos help`, router command visibility, and the router model prompt.
- Keep the registry static and cheap to load. Do not introspect argparse on every request.
- Verify release readiness with a command contract audit that detects parser/registry drift, malformed examples, missing summaries, invalid safety metadata, and risky commands without confirmation.
- Guarantee database connection hygiene: long-lived CLI handlers (starting with the workflow worker) close their SQLite connection on every exit path, and CI runs the unittest suite under `-W error::ResourceWarning` so any future unclosed connection, file, or subprocess is a build failure rather than silent noise.
- Bind approvals to the payload they approved: `approve_and_execute` pins a canonical SHA-256 of the payload and the approval timestamp on the `agent_actions` row at the moment of approval. Before executing MYOS refuses the action if the current payload no longer matches the pinned hash (`payload_hash_mismatch`) or if the approval is older than `MYOS_APPROVAL_TTL_SECONDS` (default 24h — `approval_ttl_exceeded`). Refusals record a failed execution receipt and an `approval_integrity_block` event so tampering and long-stale approvals surface as auditable outcomes. `myos execution-receipt list/show` prints the integrity status, TTL remaining, and reason alongside the verification receipt so operators see the whole approval-to-execution chain in one place.
- Bound Zero executor wall-clock and cleanup: Zero runs in a disposable worktree with an enforced subprocess timeout, cleaned up on every exit path (success, error, or timeout). Both the action payload metadata and the review-packet artifact carry `timed_out`, `timeout_seconds`, `stderr_bytes`, and `stderr_truncated` so operators can see wall-clock termination and stderr volume without inspecting raw executor output. `MYOS_ZERO_TIMEOUT_SECONDS` acts as a global cap when the factory context does not specify its own `timeout`, and `myos doctor` reports the effective cap and its source.
- Enforce a runtime CLI inventory contract: `tests/test_backlog.py` walks every registered command and asserts (1) the command exists in `build_parser`, (2) `--help` renders under argparse without error, and (3) each command eventually binds a `func` handler through any depth of nested required subparsers. Complements the metadata-focused command contract audit in `release-check --strict` by catching parser wiring regressions the metadata audit cannot see.
- Enforce static type + lint safety on the CLI command contract source of truth: CI runs `ruff check` fatally on the safety-critical modules (`command_registry`, `privacy`, `execution`, `db`, `zero_executor`) and `mypy --strict` fatally on `command_registry.py`. Advisory `ruff check`/`ruff format --check` on the whole tree and `mypy --strict` on the remaining safety-critical modules make follow-up cleanup slices measurable; `pip-audit` runs as an advisory dependency vulnerability scan.
- Expose the approval queue and execution history to automated supervisors: `myos approve --list --json` and `myos execution-receipt list/show --json` emit stable-schema JSON envelopes (`myos.approve.list.v1`, `myos.execution_receipt.list.v1`, `myos.execution_receipt.show.v1`) carrying the same underlying rows the text output produces. The receipt payloads always include the `approval_integrity` envelope (hash + TTL verification) and the `verification` block (suggested-but-not-run commands with reason), so an external dashboard or supervising agent can reconcile the bounded-autonomy loop without ever parsing text or reading raw payload bodies.
- Expose the durable autonomy loop itself to automated supervisors: `myos loop status --json` (`myos.loop.status.v1`) and `myos loop ledger --json` (`myos.loop.ledger.v1`) emit stable-schema JSON envelopes carrying task state (status, cycles, pending approvals) and per-cycle audit trail (decision, actions proposed, safe executed, pending, blocked). The loop is the durable primitive — factory runs are transient per-cycle runners — so these two commands are the canonical channel a supervising process uses to answer "is my agent making progress, and what did it just decide?" without parsing text.

Useful command:

```bash
myos router commands --tier workflow
```

Acceptance criteria:

- The router can recommend a valid command family for common requests.
- The tiny model prompt receives a compact command catalog rather than a free-form list.
- Safety metadata remains explicit and test-covered.

## Lightweight Observability Kernel

Purpose: trace command and agent work without creating a logging warehouse.

Implemented scope:

- Add correlation IDs for CLI command, route, agent/factory run, receipt, and response records.
- Store small metadata rows: status, duration, linked IDs, safety tier, and capped summaries.
- Avoid raw stdout/stderr or private payload persistence by default.
- Add retention policy and cleanup from the first implementation.

Useful commands:

```bash
myos trace list
myos trace cleanup --retention-days 30 --max-rows 5000
myos trace rollups
```

Acceptance criteria:

- Normal commands add at most a few small SQLite writes.
- Trace data links existing records without duplicating payloads.
- Cleanup can remove old detailed traces while preserving aggregate counts.

## Policy-Aware Autonomy Decisions

Purpose: use router intent, command safety metadata, trace history, and approval policy to recommend the safest execution mode for a request.

Implemented scope:

- Add a small autonomy decision function that returns `allowed`, `needs_approval`, or `blocked`.
- Use command registry safety and existing autonomy policy rather than a new permission system.
- Show the decision in `myos do` and factory starts before work begins.
- Keep decisions explainable and test-covered.

Useful commands:

```bash
myos do "draft a Jira update"
myos factory start --intent 1
```

The decision line is intentionally advisory for ordinary local work. Destructive or unknown command classifications remain blocked by the hard autonomy guards, while external and approval-gated workflows stay review-first.

## Policy Decision Feedback And Calibration

Purpose: learn where the autonomy decision explanation is too noisy, too permissive, or too conservative without weakening the execution guard.

Implemented scope:

- Record privacy-safe feedback on autonomy decisions.
- Add a local eval fixture for representative command safety decisions.
- Keep all external mutations approval-gated regardless of feedback.

Useful commands:

```bash
myos autonomy eval
myos autonomy feedback --trace 123 --expected-decision needs_approval
```

Feedback is calibration metadata only. It does not override the hard autonomy guards or make external mutations automatic.

## Decision-Aware Recommendations

Purpose: use policy decisions to guide users toward safer next steps before they start work.

Implemented scope:

- Suggest safer alternatives when a command or route needs approval.
- Show the closest approval/review command for the current decision.
- Keep suggestions read-only and deterministic.

Useful commands:

```bash
myos do "draft a Jira update"
myos factory start --intent 1
```

Recommendations are printed as guidance only. They never execute the suggested command automatically and they do not change approval policy.

## Recommendation Feedback And Ranking

Purpose: learn which deterministic recommendations are useful without using a model or weakening policy.

Implemented scope:

- Record privacy-safe feedback on recommendation usefulness.
- Rank already-deterministic recommendations by usefulness score.
- Store note hashes and lengths rather than raw feedback text.
- Keep all recommendation execution manual.

Useful commands:

```bash
myos autonomy recommendation-feedback --label inspect_recent_traces --command "myos trace list" --useful yes
myos autonomy recommendations
```

Recommendation ranking changes only print order. It does not invent new commands, automatically run suggested commands, or alter autonomy decisions.

## Durable Autonomous Task Loop

Purpose: give MYOS a resumable local autonomy loop without creating a new execution path.

Implemented scope:

- Start or resume one bounded task cycle at a time.
- Store loop state in existing agent task, run, action, and observation tables.
- Execute only safe local actions automatically.
- Leave approval-gated work in the existing approval queue.
- Link loop commands to lightweight execution traces.

Useful commands:

```bash
myos loop start "Handle the blocked launch dependency" --backend cursor
myos loop status
myos loop resume --task 1
myos approve --list
```

The loop is durable and resumable, but not a background daemon. Each invocation runs one bounded cycle, records what happened, and prints the next review command when approval is needed.

## Goal-Driven Autonomy Scheduler

Purpose: let MYOS pick one eligible standing goal and run exactly one bounded autonomy loop decision.

Implemented scope:

- Select active goals whose cadence window is due.
- Start a loop for a selected goal that has no existing loop.
- Resume a selected goal's loop when it is clear of pending approvals.
- Skip and record a scheduler observation when the loop is waiting on approvals.
- Append scheduler events and link loop task traces without adding new schema.

Useful commands:

```bash
myos loop goals
myos loop run-goal --backend cursor
myos loop run-goal --goal 1
myos approve --list
```

The scheduler is one-shot and local. It is not a daemon, does not process multiple goals in a batch, and does not bypass approval-gated actions.

## Autonomy Run Ledger

Purpose: make each autonomous run, skip, pause, and completion easier to inspect after the fact.

Implemented scope:

- Store one compact row per loop and goal scheduler decision.
- Link decisions to assistant goals, agent tasks, agent runs, and trace correlation IDs.
- Expose read-only inspection through `myos loop ledger`.
- Keep payloads capped and privacy-filtered.

Useful commands:

```bash
myos loop ledger
myos loop ledger --goal 1
myos loop ledger --task 1 --status skipped
```

The ledger is not a verbose transcript store. It records counts, IDs, provider names, and short reasons so autonomy behavior can be audited without persisting raw prompts or large outputs.

## Autopilot Goal Wrapper

Purpose: let an explicit autopilot invocation run one goal scheduler decision as a tiny wrapper around the existing durable loop.

Implemented scope:

- Add an opt-in `myos autopilot --once --loop-goal` path and optional `--loop-goal-id`.
- Call the existing goal scheduler once and report the ledger entry.
- Reuse the existing autopilot lock and `autopilot_runs` summary records.
- Keep daemon behavior, approval gates, and multi-goal batching out of scope.

Useful commands:

```bash
myos autopilot --once --loop-goal
myos autopilot --once --loop-goal --loop-goal-id 1
myos loop ledger --limit 1
```

This wrapper is intentionally explicit. It does not make standing goals run in the background; it only gives the normal autopilot entry point a one-shot bridge into the durable goal scheduler.

## Autonomy Command Module Split

Purpose: reduce `cli.py` size by moving autonomy and loop command handlers behind small module boundaries.

Implemented scope:

- Extract command handlers without changing behavior.
- Preserve parser shape, tests, and public command output.
- Keep the slice mechanical and validation-heavy.

The parser remains in `cli.py`, while autonomy and loop command bodies live in `cli_autonomy.py`. This keeps public commands stable and starts reducing the blast radius of future autonomy changes.

## Autopilot Command Module Split

Purpose: continue reducing `cli.py` size by extracting autopilot command orchestration into a focused command module.

Implemented scope:

- Move autopilot command handlers without changing behavior.
- Preserve lock usage, digest behavior, goal-wrapper behavior, and public output.
- Keep validation focused on regression safety.

The parser remains in `cli.py`, while the main autopilot cycle and goal-wrapper orchestration live in `cli_autopilot.py`. CLI-only sync, ingest, triage, and watch-scan functions are passed in as explicit dependencies to avoid circular imports.

## Factory Command Module Split

Purpose: continue reducing `cli.py` size by moving factory command presentation and command dispatch into a focused module.

Implemented scope:

- Move factory command handlers without changing behavior.
- Preserve autonomy decision printing, recommendation output, factory policy commands, and public CLI text.
- Keep the parser in `cli.py` and validate with focused factory regressions.

The parser remains in `cli.py`, while factory command dispatch and presentation live in `cli_factory.py`. This keeps factory behavior stable while reducing the size and risk of future changes to the main CLI entry point.

## Command Mapper Maintenance

Purpose: keep local models aware of the current CLI surface without exposing private user data.

Implemented scope:

- Use `command_registry.py` as the source of truth for local-model command awareness.
- Include command names, subcommands, required args, examples, tiers, intents, confirmation needs, and safety metadata.
- Pass the full local-model-safe command mapper to the optional router model while preserving the older compact catalog field.
- Keep parser coverage tests in place so future command slices update the mapper alongside CLI changes.

## Agent Command Module Split

Purpose: continue reducing `cli.py` size by moving agent task, action, learning, and status command presentation into a focused module.

Implemented scope:

- Move agent-facing command handlers without changing behavior.
- Preserve approval output, execution receipt behavior, and public CLI text.
- Keep the parser in `cli.py` and validate with focused agent/action regressions.

The parser remains in `cli.py`, while agent delegation, action listing/execution, approvals, receipts, learning, coaching, status, and local agent-role presentation live in `cli_agent.py`.

## Intent And Plan Command Module Split

Purpose: continue reducing `cli.py` size by moving intent, plan, evidence, and review-packet command presentation into a focused module.

Implemented scope:

- Move intent and planning command handlers without changing behavior.
- Preserve evidence attachment output, review packet output, and public CLI text.
- Keep the parser in `cli.py` and validate with focused intent/plan regressions.

The parser remains in `cli.py`, while intent, plan, evidence attachment, external evidence sync, and review-packet presentation live in `cli_planning.py`.

## Entity And Relationship Command Module Split

Purpose: continue reducing `cli.py` size by moving entity, relationship, and graph retrieval presentation into focused modules.

Implemented scope:

- Move entity and relationship command handlers without changing behavior.
- Preserve extraction/list output for entity, relationship, and claim commands.
- Keep the parser in `cli.py` and validate with focused entity/relationship/graph regressions.

The parser remains in `cli.py`, while deterministic entity, relationship, and claim extraction/list presentation live in `cli_knowledge.py`.

## Router, Model, And Trace Command Module Split

Purpose: continue reducing `cli.py` size by moving router calibration, model setup, trace inspection, and retrieval/graph presentation into focused modules.

Implemented scope:

- Move router/model/trace command handlers without changing behavior.
- Preserve router command mapper output, model setup output, trace cleanup/rollup output, and retrieval command text.
- Keep the parser in `cli.py` and validate with focused router/model/trace/retrieval regressions.

The parser remains in `cli.py`, while router calibration, model setup/status, trace inspection, graph linking, context lookup, why output, and retrieval-run presentation live in `cli_diagnostics.py`.

## Inbox, Sync, And Connector Command Module Split

Purpose: continue reducing `cli.py` size by moving inbox, sync, connector ingestion, and day-planning presentation into focused modules.

Implemented scope:

- Move sync, ingest, triage, inbox processing, and daily work-list command handlers without changing behavior.
- Preserve connector output, local-write behavior, provenance indexing, and public CLI text.
- Keep the parser in `cli.py` and validate with focused CLI workflow regressions.

The parser remains in `cli.py`, while capture, triage, today, risk radar, sync, external ingestion, and inbox processing presentation live in `cli_workflow.py`.

## Final CLI Thin-Entry Cleanup

Purpose: finish reducing `cli.py` by reviewing remaining command bodies and extracting only clearly cohesive groups that do not create circular dependencies.

Implemented scope:

- Review remaining parser-adjacent helpers, operational setup commands, and daily convenience commands.
- Keep risky orchestration code stable unless a clean dependency boundary is obvious.
- Run final full validation and release hygiene before committing.

The parser and dependency-heavy orchestration commands remain in `cli.py`. Daily/review presentation now lives in `cli_review.py`, including close-day, morning brief, at-risk/waiting/delegation views, executive brief, stop-doing review, reports, metrics, evidence review, commitment resolution, weekly review, renegotiation, and next-action guidance.

## Operational Orchestration Module Split

Purpose: only after this refactor is committed, consider moving dependency-heavy operational commands with explicit dependency injection.

Implemented scope:

- Evaluate `run-day`, `setup-live`, `go-live`, `launchd-install`, and `doctor` for clean dependency boundaries.
- Prefer no extraction if it would increase coupling or obscure safety checks.
- Preserve all existing command text and release gates.

The first safe operational slice moves run-day, go-live, workflow queue, workflow run listing, workflow orchestration, and worker execution into `cli_operations.py`. The module receives environment loading through an explicit dependency object, while OS-service and broad health commands remain in `cli.py`.

## Operational Setup And Health Boundary Review

Purpose: review the remaining operational commands that were intentionally left in `cli.py` because they touch setup, launch agents, dashboard serving, restore, and system health.

Implemented scope:

- Evaluate `setup-live`, `activate`, `start`, `stop`, `launchd-*`, `doctor`, `sanity`, `dashboard`, backup, and restore for clean boundaries.
- Keep OS-service writes and safety/restore checks obvious at the entrypoint unless extraction improves clarity.
- Preserve all existing command text and validation gates.

The first safe health slice moves doctor, sanity, snapshot, cutover readiness, UAT quality, and tuning recommendations into `cli_health.py`. Setup, restore, dashboard serving, and launchd commands remain in `cli.py` because they perform broad system checks, OS-service writes, file restores, or long-running serving.

## Operational Setup And Runtime Boundary Review

Purpose: evaluate the remaining setup/runtime commands that still sit in the parser entrypoint and decide whether any can move without hiding safety-critical behavior.

Implemented scope:

- Review `setup-live`, `activate`, `start`, `stop`, `launchd-*`, `dashboard`, backup, restore, `runbook`, and cleanup.
- Prefer keeping restore and OS-service writes in the entrypoint unless extraction makes the safety checks clearer.
- Preserve all existing command text, dry-run defaults, and validation gates.

The first safe runtime slice moves dashboard presentation, launchd status, runbook output, and the health/ui aliases into `cli_runtime.py`. Setup, activation, start/stop, launchd install/uninstall, cleanup, backup, restore, and dashboard serving safety remain parser-adjacent where their side effects are easiest to audit.

## Setup And Local Data Safety Boundary Review

Purpose: review the remaining side-effecting setup and local-data operations for dependency injection opportunities without obscuring safety checks.

Implemented scope:

- Evaluate `setup-live`, backup, restore, cleanup, and config initialization.
- Keep restore verification, dry-run behavior, local artifact checks, and filesystem writes obvious.
- Preserve all existing command text and validation gates.

Local data maintenance now lives in `cli_local_data.py`, including migration verification, backup, restore, config initialization, and cleanup. Restore still verifies the source database before copying, creates a pre-restore backup, clears SQLite sidecars, and verifies schema after restore. `setup-live` stays in `cli.py` because it coordinates env templating, router setup, watch dirs, standing goals, and optional launchd installation.

## Setup Live Dependency Boundary Review

Purpose: review `setup-live` for explicit dependency injection without weakening dry-run behavior, file-permission handling, or launchd safety.

Implemented scope:

- Evaluate `_env_template`, `_setup_live_paths`, readiness checks, env-line upserts, and launchd handoff.
- Preserve dry-run as the default and keep env file permissions explicit.
- Keep router model setup and launchd installation visibly gated.

Setup-live planning, readiness checks, env templating, router model env upserts, and DB bootstrap now live in `cli_setup_live.py`. The launchd handoff is explicitly injected from `cli.py`, so `--install-launchd` remains the visible gate before any LaunchAgents are written or loaded.

## Launchd Runtime Safety Boundary Review

Purpose: review launchd install/uninstall, activation, start, and stop commands for a clearer OS-service boundary without hiding dry-run defaults or file writes.

Implemented scope:

- Evaluate `launchd-install`, `launchd-uninstall`, `activate`, `start`, `stop`, and `live`.
- Keep LaunchAgents writes, `launchctl` calls, and dry-run behavior obvious.
- Preserve all existing command text and validation gates.

Launchd install/uninstall and runtime lifecycle presentation now live in `cli_launchd.py`. The module keeps LaunchAgent write targets, `launchctl` calls, dry-run exits, and `--apply` gates together, while `cli.py` injects non-launchd collaborators for env loading, onboarding, go-live, status, and sanity checks.

## Runtime Command Mapper Boundary Review

Purpose: review the command mapper and runtime lifecycle metadata so local models can choose setup, launchd, and recovery commands without learning side effects by trial and error.

Implemented scope:

- Review command metadata for setup-live, launchd install/uninstall, activate, start, stop, live, backup, restore, and health checks.
- Ensure local models can distinguish dry-run, filesystem-write, OS-service, restore, and long-running command classes.
- Preserve human approval gates and avoid adding runtime overhead to normal command execution.

The local-model command mapper now includes static side-effect metadata, dry-run defaults, and long-running markers for setup, launchd, backup, restore, dashboard, runtime, and workflow commands. This keeps model routing informed without inspecting implementation code or adding runtime work to normal command execution.

## Safety-Aware Runtime Recommendation Review

Purpose: use command mapper side-effect metadata to improve local recommendations for setup, runtime, restore, and recovery flows while preserving review-first behavior.

Implemented scope:

- Review router and recommendation logic that consumes command metadata.
- Prefer safer dry-run or diagnostic commands before side-effecting runtime commands.
- Keep destructive, restore, OS-service, and external-write actions approval-gated.

Runtime recommendations now consume static command metadata. Setup-live points to readiness checks first, launchd and OS-service changes point to dry-runs/status/runbook steps, restore points to backup and migration verification, and long-running commands point to bounded health checks. Start/stop are explicitly marked approval-required OS-service commands in the mapper.

## Approval Packet Runtime Context Review

Purpose: enrich approval and review surfaces with command side-effect context so humans can quickly understand why a proposed runtime action is gated.

Implemented scope:

- Review approval, action, execution receipt, and factory review packet outputs.
- Surface side-effect classes, dry-run defaults, and safer diagnostics in approval context.
- Preserve raw-data privacy and avoid storing command arguments beyond existing hashes/traces.

Approval queues, action listings, execution receipt views, and factory review output now show side-effect classes, review gates, dry-run status, and safer next commands derived from action type and redacted payload metadata. This keeps human review fast while preserving existing execution gates and raw-data privacy.

## Approval Context Persistence Review

Purpose: decide whether side-effect context should be persisted as structured receipt metadata or remain presentation-only.

Implemented scope:

- Review action execution receipt storage and retrospective learning inputs.
- Preserve privacy by storing only static side-effect classes and hashes if persistence is needed.
- Avoid schema churn unless persisted context materially improves autonomous learning and review quality.

Execution receipts now persist compact approval context inside existing receipt request metadata: side-effect classes, dry-run status, and approval reason. This avoids schema churn and does not add new raw command arguments beyond the receipt payload already stored. Factory learning retrospectives summarize receipt side effects so future runs can learn whether blocked, failed, or risky actions cluster around specific side-effect classes.

## Learning-Aware Approval Recommendation Review

Purpose: use persisted receipt side-effect learning to improve approval recommendations without allowing history to bypass safety gates.

Implemented scope:

- Review recommendation ranking, factory insights, and retrospective learning summaries.
- Prefer recommendations that match historically risky side-effect classes.
- Keep learned recommendations advisory only; approval requirements must continue to come from static policy and command metadata.

Recommendation candidates now carry static side-effect classes. Ranking combines explicit recommendation feedback with a bounded retrospective signal from persisted factory learning, boosting recommendations whose side-effect classes previously appeared in blocked, failed, partial, or failed factory runs. The learning signal only changes recommendation ordering and explanatory text; command decisions and approval requirements still come from static policy and command metadata.

## Approval Recommendation Feedback Loop Review

Purpose: close the loop between surfaced approval recommendations and operator feedback without storing raw recommendation notes.

Implemented scope:

- Review `autonomy recommendation-feedback`, recommendation summaries, and learned side-effect ranking together.
- Preserve hashed-note privacy and avoid storing raw approval details.
- Keep feedback advisory and separate from hard execution approval gates.

Recommendation feedback summaries now show explicit usefulness scores alongside inferred side-effect classes and advisory learned-risk scores. Feedback notes remain hash-and-length only, recommendation side effects are inferred from stable labels and command metadata, and the signal remains ranking-only; static policy still owns execution approval gates.

## Recommendation Feedback Surface Review

Purpose: make it easier for operators to give useful recommendation feedback from the places where recommendations are printed.

Implemented scope:

- Review printed recommendation labels in factory, autonomy loop, smart do, and runtime flows.
- Ensure each recommendation includes enough label/command context to submit privacy-safe feedback.
- Avoid adding prompt noise to common happy paths.

Shared autonomy recommendations already include label and command context. The remaining hand-written autonomy loop recommendations now include stable labels, so operators can submit privacy-safe `autonomy recommendation-feedback` without guessing which recommendation was printed. The change stays inline with existing recommendation text and avoids adding extra prompt noise.

## Recommendation Feedback Surface Audit

Purpose: audit non-autonomy recommendation-like output for places that should either gain labels or remain intentionally outside the feedback loop.

Implemented scope:

- Review morning, close-day, runbook, and operational status outputs for recommendation-like text.
- Add labels only where feedback would improve future ranking.
- Keep static instructional output unlabelled when feedback would not affect recommendations.

Morning, close-day, runbook, setup, and operational status outputs were reviewed for recommendation-like text. Static instructions remain unlabelled because they do not feed recommendation ranking. Actionable autonomy/factory/smart-do recommendations already carry stable labels and commands, and autonomy loop recommendation labels were preserved as the feedback surface for pending approvals and loop-status guidance.

## Feedback-Aware Daily Recommendation Review

Purpose: decide whether daily next-action and review-draft outputs should feed the recommendation feedback loop.

Implemented scope:

- Review `next-action`, `now`, `morning`, and `brief` outputs for repeated recommendation patterns.
- Add labels only where operator feedback can improve future daily ranking.
- Keep one-off generated content and static runbook text outside the feedback loop.

Daily `next-action` and `now` outputs now include stable feedback labels and command context for the selected recommendation. Morning and brief remain summary surfaces without labels because they present multiple facts and one-off generated context rather than a single ranked recommendation. This keeps feedback focused on the decision point that can improve future daily ranking.

## Daily Recommendation Feedback Ranking Review

Purpose: use privacy-safe feedback on daily next-action labels to tune future daily recommendation ordering without changing underlying work-item safety.

Implemented scope:

- Review daily next-action selection between risk reduction, owner nudges, tiny wins, and focus blocks.
- Use existing recommendation feedback summaries as advisory ranking input.
- Keep work-item data and raw feedback notes private.

Daily next-action selection now builds candidate recommendations and applies a bounded usefulness score from privacy-safe recommendation feedback. Feedback can tune close daily choices such as owner nudges versus risk reduction in meeting-heavy mode, while baseline ranking still protects risk-first behavior in normal maker mode. Feedback notes remain hash-and-length only.

## Daily Recommendation Feedback Explainability Review

Purpose: show enough context for why a daily recommendation won without exposing raw feedback notes or noisy scoring details.

Implemented scope:

- Review next-action output after feedback-aware ranking.
- Surface concise ranking context only when feedback affected selection.
- Keep default daily output compact.

Daily next-action output now remains unchanged when the baseline daily recommendation wins. When bounded feedback changes the selected recommendation, MYOS prints a compact ranking context line that shows only the prior label, selected label, and bounded score. Raw feedback notes remain private and are never surfaced in daily output.

## Daily Recommendation Feedback Decay Review

Purpose: prevent old daily recommendation feedback from over-steering future daily choices.

Implemented scope:

- Review whether daily feedback should be weighted by recency.
- Keep the ranking signal bounded and deterministic.
- Avoid storing or exposing raw feedback notes.

Daily feedback ranking now uses a bounded 30-day lookback window and the existing score clamp. Stale feedback remains stored for audit and summaries, but it no longer steers daily next-action selection after the lookback window. The implementation does not add raw-note storage or new schema.

## Daily Recommendation Feedback Command Scope Review

Purpose: make sure daily feedback for `myos next-action` and `myos now` stays command-specific and does not unintentionally cross-train different daily surfaces.

Implemented scope:

- Review command-specific feedback keys for daily recommendation ranking.
- Validate that `next-action` and `now` can learn independently.
- Keep feedback labels stable and privacy-safe.

Daily feedback ranking already keys feedback by stable label and command. Regression coverage now verifies that positive feedback for `myos next-action` does not tune `myos now`, and that `myos now` can learn independently with its own command-scoped feedback. Raw feedback notes remain private.

## Daily Recommendation Feedback Summary Review

Purpose: make daily feedback learning visible in existing feedback summaries without adding new private data.

Implemented scope:

- Review `autonomy recommendation-feedback --summary` output for daily labels.
- Ensure command, label, side-effect, and score context remain compact.
- Keep raw notes private and avoid new schema.

Recommendation feedback summaries now surface daily recommendation rows with `surface=daily` and `recent_score_30d` context. This makes the daily learning signal visible alongside the all-time score, command, label, side-effect, and learning-score fields while keeping raw feedback notes private.

## Daily Recommendation Feedback Summary Ordering Review

Purpose: ensure daily feedback summary rows remain easy to inspect as daily feedback grows.

Implemented scope:

- Review ordering of summary rows when both daily and non-daily feedback exists.
- Keep useful score ordering predictable and compact.
- Avoid new schema or raw-note exposure.

Recommendation feedback summaries now order rows by displayed 30-day recent score first, then all-time score, last feedback time, label, and command. This keeps active daily learning visible even when older general feedback has a higher all-time score, without adding schema or exposing raw feedback notes.

## Daily Recommendation Feedback Summary Limit Review

Purpose: make sure useful daily feedback rows are not accidentally hidden by small summary limits.

Implemented scope:

- Review the interaction between summary ordering and `--limit`.
- Keep default summary output compact.
- Avoid new storage or raw-note exposure.

Recommendation feedback summaries now apply a compact minimum display floor and reserve a protected slot for the highest-ranked positive daily feedback row when it would otherwise be hidden by a tiny limit. This keeps active daily learning visible during inspection without increasing the default summary size, adding schema, or exposing raw feedback notes.

## Daily Recommendation Feedback Negative Signal Review

Purpose: make sure negative daily feedback lowers ranking without hiding useful audit context.

Implemented scope:

- Review how not-useful daily feedback affects ranking, summary scores, and explainability.
- Keep score bounds and recency behavior deterministic.
- Preserve hashed-note privacy.

Not-useful daily feedback now has an explicit regression path: it lowers the bounded daily ranking score, can change the selected daily recommendation, and surfaces signed score context without exposing raw notes. Ranking explanations now show both selected and baseline bounded feedback scores, so negative feedback is understandable when it changes a winner. Compact summaries also protect active signed daily feedback rows, including negative rows, for audit visibility.

## Daily Recommendation Feedback Conflict Review

Purpose: make mixed useful and not-useful daily feedback easy to reason about when signals offset each other.

Implemented scope:

- Review behavior when daily labels have both positive and negative recent feedback.
- Keep net score behavior bounded and deterministic.
- Preserve compact output and hashed-note privacy.

Mixed daily feedback now keeps deterministic net-score behavior while surfacing compact conflict context. Recommendation summaries include recent useful and not-useful counts internally and print `mixed_recent=yes` when a daily row has both recent useful and not-useful feedback. Protected daily summary slots now preserve active mixed rows even when their net recent score is zero, and raw notes remain hashed-only.

## Daily Recommendation Feedback Cleanup Review

Purpose: review whether the daily feedback helpers should be consolidated after the ranking, summary, limit, and conflict slices.

Implemented scope:

- Review duplicated daily feedback constants and summary helper logic.
- Keep behavior and tests unchanged unless simplification is clearly safer.
- Avoid schema changes and raw-note exposure.

Daily feedback constants are now centralized in the autonomy layer and reused by both daily ranking and recommendation feedback summaries. The shared helper defines daily recommendation scope for `myos next-action` and `myos now`, while the CLI uses the same bounded recency window and score clamp. Behavior remains unchanged and raw notes remain hashed-only.

## Daily Recommendation Feedback Documentation Review

Purpose: document the daily feedback loop clearly enough for a local operator to understand how to give feedback and interpret summaries.

Implemented scope:

- Review user-facing docs for daily feedback commands and summary fields.
- Add concise usage guidance if missing.
- Preserve privacy guarantees and avoid changing runtime behavior.

The README now documents daily recommendation feedback for `myos next-action` and `myos now`, including how to submit useful/not-useful feedback, why command scope matters, how the 30-day bounded score window works, and how to inspect `myos autonomy recommendations` summary fields such as `surface=daily`, `recent_score_30d`, and `mixed_recent=yes`. The expert command catalog also lists recommendation feedback and summary commands.

## Daily Recommendation Feedback Public Hygiene Review

Purpose: make sure the expanded daily feedback docs and tests remain safe for a public repository.

Implemented scope:

- Review daily feedback examples for private names, secrets, or personal data.
- Keep examples generic and reproducible.
- Preserve runtime behavior.

Daily recommendation feedback documentation and regression fixtures were reviewed for public repository hygiene. Examples remain generic, raw feedback notes stay hashed-only, and a person-like test owner was replaced with a synthetic owner value. Runtime behavior is unchanged.

## Daily Recommendation Feedback End-to-End Review

Purpose: verify the full operator path from daily recommendation label to feedback submission to summary interpretation.

Implemented scope:

- Review the end-to-end daily feedback workflow across README, CLI output, and tests.
- Add only missing coverage or docs that reduce operator ambiguity.
- Preserve runtime behavior and privacy guarantees.

The daily feedback workflow now has clearer operator guidance: the README tells users to copy the bracketed `label` and `command` values from `myos next-action` or `myos now` output before submitting feedback. CLI regression coverage also verifies the feedback acknowledgement and privacy message before checking ranking and summary effects.

## Daily Recommendation Feedback Command Help Review

Purpose: verify CLI help text makes recommendation feedback discoverable without requiring the README.

Implemented scope:

- Review `myos autonomy` help for recommendation feedback and summary actions.
- Add concise help text only if discoverability is weak.
- Preserve runtime behavior and privacy guarantees.

The `myos autonomy recommendation-feedback --help` output now includes the daily bracket-copy workflow and examples for `daily_reduce_risk`, `myos next-action`, and `myos now`. The `myos autonomy recommendations --help` output now mentions summary fields such as `surface`, `recent_score_30d`, `side_effects`, and `mixed_recent`, plus the compact daily visibility behavior for tiny limits.

## Daily Recommendation Feedback Final Review

Purpose: do a final review of the completed daily feedback loop before moving to a different bounded-autonomy area.

Implemented scope:

- Review implementation, docs, and tests for daily recommendation feedback as one cohesive feature.
- Prefer no code changes unless a clear bug or gap remains.
- Preserve runtime behavior and privacy guarantees.

Daily recommendation feedback was reviewed as a cohesive feature across `myos next-action`, `myos now`, feedback submission, summary output, README guidance, CLI help text, and regression coverage. No runtime behavior changes were needed: feedback remains command-scoped, bounded to a 30-day score window, clamped, private by hash/length-only notes, and explainable only when it affects a daily winner.

## Bounded Recommendation Surface Review

Purpose: review all recommendation-like feedback surfaces now that daily recommendation feedback is complete.

Implemented scope:

- Compare daily, autonomy loop, approval, and factory recommendation labels and summaries.
- Keep feedback surfaces compact, deterministic, and privacy-safe.
- Prefer no runtime changes unless a clear consistency gap remains.

Recommendation-like feedback surfaces were reviewed across daily recommendations, autonomy loop output, approval review, and factory recommendations. The review found one consistency gap: `myos loop status` printed an actionable approval recommendation without the stable `review_approvals` feedback label used by loop start/resume and goal-cycle output. The status output now carries the same label, preserving compact and deterministic feedback surfaces.

## Recommendation Feedback Summary Label Coverage Review

Purpose: verify that labeled recommendation surfaces map cleanly into feedback summaries and side-effect context.

Implemented scope:

- Review recommendation labels against summary side-effect inference.
- Add coverage only for labels that can be submitted through `recommendation-feedback`.
- Preserve privacy guarantees and avoid schema changes.

Recommendation feedback summary coverage now exercises submit-able labels across approval review, factory review, runtime dry-run guidance, and loop-status inspection. The regression verifies that side-effect inference remains accurate for impactful labels such as `review_approvals`, `review_factory`, and `dry_run_runtime_change`, while read-only inspection labels such as `inspect_loop_status` remain side-effect-free. Raw feedback notes remain hash/length-only.

## Recommendation Feedback Summary Command Context Review

Purpose: verify that command-specific recommendation feedback remains clear outside the daily surfaces.

Implemented scope:

- Review how command text appears in summary rows for loop, approval, factory, and runtime recommendations.
- Add coverage only if command context can be ambiguous.
- Preserve privacy guarantees and avoid schema changes.

Recommendation feedback summary behavior already grouped rows by label and command and printed command context. Regression coverage now asserts exact command preservation for approval, factory, runtime, and loop-status feedback rows, and CLI coverage verifies that the summary prints command context alongside side-effect context for approval feedback. No schema or runtime behavior change was required.

## Recommendation Feedback Summary Privacy Review

Purpose: review recommendation summary output for any remaining raw-note or private-command leakage risks.

Implemented scope:

- Review summary rows and tests for raw note exposure.
- Keep command context visible while preserving hashed-note privacy.
- Avoid schema changes.

Recommendation feedback summaries were reviewed for privacy boundaries. Command context remains visible because it is part of the stable recommendation key and operator-facing context, but summary rows and CLI output now have explicit coverage proving raw note text, `note_hash`, and `note_length` are not surfaced. The persistence layer continues to store only hashes and lengths for feedback notes.

## Recommendation Feedback Summary Help Review

Purpose: make summary privacy and command-context behavior discoverable from CLI help.

Implemented scope:

- Review `myos autonomy recommendations --help` for privacy language.
- Add concise help text only if raw-note privacy is unclear.
- Avoid runtime behavior or schema changes.

The `myos autonomy recommendations --help` output now explicitly states that command context is shown while raw notes, `note_hash`, and `note_length` are not shown. This makes the recommendation summary privacy boundary discoverable from the CLI without changing runtime behavior or schema.

## Recommendation Feedback Final Review

Purpose: do a final review of recommendation feedback surfaces before returning to broader bounded-autonomy work.

Implemented scope:

- Review feedback submission, summaries, help text, privacy tests, and side-effect coverage together.
- Prefer no runtime changes unless a clear bug remains.
- Preserve command context and hashed-note privacy.

The recommendation feedback loop now has a final contract review across feedback submission, ranking, summaries, CLI help, daily feedback, side-effect context, and privacy behavior. No runtime changes were needed. CLI coverage now explicitly verifies that summary output keeps command context and side-effect context visible while hiding raw notes, `note_hash`, and `note_length`.

## Bounded Autonomy Roadmap Review

Purpose: choose the next high-leverage bounded-autonomy slice now that the recommendation feedback arc is complete.

Implemented scope:

- Review the current autonomy roadmap and implemented command-module boundaries.
- Pick one small next slice that improves autonomous operation, auditability, or safety.
- Avoid broad refactors unless a clear boundary and validation path exist.

The roadmap review selected the autonomy run ledger as the next high-leverage surface. The durable loop and goal scheduler already write compact audit rows, but pending-approval ledger rows needed the same direct review command shown by loop status. This keeps the next slice focused on auditability and operator handoff rather than new autonomy behavior.

## Autonomy Ledger Pending Approval Follow-up Review

Purpose: make pending approvals actionable from the autonomy run ledger.

Implemented scope:

- Review `myos loop ledger` output for pending-approval rows.
- Surface the existing approval-review recommendation when a ledger row has pending approvals.
- Avoid schema changes and keep ledger inspection read-only.

Ledger rows with pending approvals now print `Recommendation: myos approve --list [label=review_approvals]`, matching the loop status surface. This keeps the audit trail actionable without changing scheduler behavior, approval gates, or execution policy.

## Autonomy Ledger Help Review

Purpose: make ledger filters, pending-approval follow-up, and audit boundaries discoverable from CLI help.

Implemented scope:

- Review `myos loop ledger --help` for auditability and privacy language.
- Add concise help text only if pending-review behavior or read-only scope is unclear.
- Avoid runtime behavior or schema changes.

The `myos loop ledger --help` output now describes the ledger as a read-only audit trail, calls out goal/task/status filters, and explains that pending approval rows point operators to `myos approve --list`. This improves discoverability without changing ledger behavior or storage.

## Autonomy Ledger Status Filter Review

Purpose: make ledger status filters easier to use from the CLI.

Implemented scope:

- Review the status values printed by `myos loop ledger`.
- Decide whether `--status` should expose choices or clearer examples.
- Avoid changing persisted ledger rows or scheduler behavior.

The ledger status filter now exposes the known bounded ledger states as parser choices: `blocked`, `completed`, `noop`, `skipped`, `waiting`, and `waiting_approval`. Invalid status filters fail fast at the CLI, while persisted ledger rows and scheduler behavior remain unchanged.

## Autonomy Ledger Status Filter Help Review

Purpose: make status-filter use cases clear without adding noise to normal ledger output.

Implemented scope:

- Review `myos loop ledger --help` after adding status choices.
- Add concise examples only if the choice list is not enough for common filters.
- Avoid runtime behavior or schema changes.

The ledger help now includes compact examples for the most common status filters: `myos loop ledger --status waiting_approval` for review work and `myos loop ledger --status skipped --goal 1` for goal-specific scheduler skips. This keeps examples in help text only and does not change ledger execution, storage, or scheduler behavior.

## Autonomy Ledger Empty Filter Review

Purpose: make empty ledger filter results easier to interpret without changing query behavior.

Implemented scope:

- Review output when `myos loop ledger` filters match no rows.
- Add concise no-result context only if the current output is ambiguous.
- Avoid schema changes or persisted-row changes.

Filtered ledger queries that match no rows now keep the existing `No autonomy ledger entries found.` message and add a compact `filters:` line showing the goal, task, and/or status filters that were applied. Unfiltered empty output stays unchanged, and ledger query behavior and persisted rows are unchanged.

## Autonomy Ledger Empty Filter Help Review

Purpose: make empty filtered ledger results and recovery commands discoverable from help/docs only if needed.

Implemented scope:

- Review whether `myos loop ledger --help` and README need no-result guidance.
- Prefer no change if the new output is self-explanatory.
- Avoid runtime behavior or schema changes.

The empty filtered ledger output is self-explanatory after the prior slice because it prints the applied filter set directly below the no-result message. Existing help and README guidance already document status filters and the pending-approval recovery command, so no CLI behavior or help text changes were needed.

## Autonomy Ledger Final Review

Purpose: review the ledger audit surface as a whole before returning to broader bounded-autonomy work.

Implemented scope:

- Review ledger output, filters, no-result behavior, help, tests, and docs together.
- Prefer no runtime changes unless a clear bug remains.
- Preserve read-only ledger semantics and compact audit output.

The autonomy ledger audit surface now has a final contract review across row output, pending-approval handoff, status filtering, no-result behavior, help text, README guidance, and tests. No runtime behavior change was needed. Regression coverage now explicitly protects both unfiltered empty output and filtered empty output with applied-filter context.

## Goal Scheduler Review Handoff Review

Purpose: review goal scheduler handoff output after the ledger audit surface improvements.

Implemented scope:

- Review `myos loop goals`, `myos loop run-goal`, and autopilot goal wrapper output for clear next steps.
- Prefer output/test/help updates over scheduler behavior changes.
- Preserve one-shot scheduler semantics and approval gates.

`myos loop goals` now prints a direct next-step recommendation for each eligible goal. Goals that are clear to run point to `myos loop run-goal --goal <id>`, while goals blocked on pending approvals point to `myos approve --list` with the stable `review_approvals` feedback label. This keeps the scheduler handoff actionable without changing one-shot scheduling, eligibility, or approval gates.

## Goal Scheduler Handoff Help Review

Purpose: make the goal scheduler handoff behavior discoverable from CLI help and docs.

Implemented scope:

- Review `myos loop goals --help` and `myos loop run-goal --help` for next-step clarity.
- Add concise help/docs only if the new handoff output is not discoverable enough.
- Avoid scheduler behavior or schema changes.

`myos loop goals --help` now explains that eligible-goal listing prints the next handoff command and includes a `run-goal` example. `myos loop run-goal --help` now states that it runs exactly one eligible goal loop and stops for review, with pending approvals still handed off to `myos approve --list`. This improves discoverability without changing scheduler behavior or schema.

## Goal Scheduler Empty State Review

Purpose: make no-goal and no-eligible-goal scheduler outputs easy to act on.

Implemented scope:

- Review `myos loop goals` and `myos loop run-goal` when no goals are eligible.
- Add concise next-step guidance only if the empty state is ambiguous.
- Preserve one-shot scheduler semantics and approval gates.

No-eligible-goal scheduler outputs now include `Recommendation: review assistant goals -> myos goal list` on both `myos loop goals` and no-op `myos loop run-goal` paths. This keeps empty states actionable without changing goal eligibility, scheduler state, or approval gates.

## Goal Scheduler Empty State Help Review

Purpose: make empty-state recovery discoverable from help/docs without adding prompt noise.

Implemented scope:

- Review `myos loop goals --help`, `myos loop run-goal --help`, and README after the empty-state recommendation.
- Add concise help/docs only if `myos goal list` recovery is not discoverable enough.
- Preserve scheduler behavior and approval gates.

`myos loop goals --help` now states that when no goals are eligible, operators can review standing goals with `myos goal list`. The README already documented the same recovery path, so this keeps empty-state recovery discoverable without changing scheduler behavior.

## Goal Scheduler Final Review

Purpose: review the goal scheduler handoff and empty-state surface as a whole.

Implemented scope:

- Review `loop goals`, `loop run-goal`, autopilot goal wrapper output, help, tests, and docs together.
- Prefer no runtime changes unless a clear handoff bug remains.
- Preserve one-shot scheduler semantics and approval gates.

The goal scheduler handoff surface now has a final contract review across eligible-goal listing, direct scheduler runs, autopilot goal-wrapper runs, skipped pending-approval paths, no-op empty states, help text, README guidance, and tests. No runtime behavior change was needed. Regression coverage now protects the autopilot goal-wrapper no-op path so the `myos goal list` recovery recommendation and ledger handoff stay visible there too.

## Autonomy Handoff Surface Review

Purpose: review handoff recommendations across loop, ledger, goal scheduler, and autopilot surfaces for consistency.

Implemented scope:

- Review printed handoff recommendations and labels across autonomy surfaces.
- Prefer test/docs updates unless a clear consistency bug remains.
- Preserve approval gates and one-shot execution semantics.

Autonomy handoff output was reviewed across loop status, loop run/resume, goal scheduler, ledger, autopilot goal wrapper, and regular autopilot approval queues. The one consistency gap was regular autopilot approval handoff output, which pointed to `myos approve --list` without the stable `review_approvals` feedback label. It now prints `Run: myos approve --list [label=review_approvals]`, matching the rest of the approval-review surfaces without changing approval gates or execution behavior.

## Autonomy Handoff Help Review

Purpose: make handoff labels and approval-review paths discoverable without increasing output noise.

Implemented scope:

- Review help/docs around autonomy handoff surfaces after label consistency.
- Add concise docs/tests only if the operator feedback path remains unclear.
- Preserve approval gates and one-shot execution semantics.

`myos autonomy recommendation-feedback --help` now includes an approval-handoff example: `--label review_approvals --command "myos approve --list"`. This makes the stable label now printed by loop, ledger, goal, and autopilot approval handoffs discoverable from the feedback command itself without changing approval gates or handoff behavior.

## Autonomy Handoff Final Review

Purpose: do a final review of autonomy handoff labels, help, docs, and feedback paths.

Implemented scope:

- Review loop, ledger, goal scheduler, autopilot, and recommendation-feedback handoff surfaces together.
- Prefer no runtime changes unless a clear consistency bug remains.
- Preserve approval gates and one-shot execution semantics.

Autonomy handoff surfaces now have a final contract review across loop start/resume/status, goal scheduler, autopilot goal wrapper, regular autopilot approval queues, ledger rows, and recommendation-feedback help. No runtime behavior change was needed. Regression coverage now explicitly protects the `review_approvals` feedback label on regular autopilot and goal-wrapper approval handoffs, while help documents how to submit feedback for the approval-review command.

## Bounded Autonomy Readiness Review

Purpose: review the accumulated bounded-autonomy work before choosing the next feature slice.

Implemented scope:

- Review docs, changelog, tests, and validation status across the recent autonomy surfaces.
- Prefer no runtime changes unless a clear regression or public-readiness gap remains.
- Preserve approval gates, privacy boundaries, and one-shot execution semantics.

Recent bounded-autonomy surfaces were reviewed across runtime recommendations, approval context, recommendation feedback, daily feedback, the autonomy run ledger, goal scheduler handoffs, autopilot handoffs, help text, README guidance, and regression coverage. No runtime behavior change was needed. The current readiness posture is validation-first: all recent slices preserve approval gates, hashed-note privacy, read-only audit surfaces, and one-shot loop/scheduler semantics, with the full test and release gates remaining the deciding checkpoint.

## Bounded Autonomy Public Readiness Review

Purpose: review public-readiness signals for the accumulated autonomy changes before picking another feature slice.

Implemented scope:

- Review README, changelog, release-check output, and public hygiene coverage for autonomy surfaces.
- Prefer docs/test updates unless a clear public-readiness gap remains.
- Preserve local-first privacy boundaries and approval-gated execution.

Public-readiness signals were reviewed across README guidance, changelog coverage, strict release checks, and public hygiene gates. `myos release-check --strict` passes with zero public-hygiene findings. The README now also documents the stable `review_approvals` feedback path for approval handoffs, keeping the public operator workflow aligned with the CLI help and printed autonomy handoff labels.

## Bounded Autonomy Commit Readiness Review

Purpose: review the accumulated autonomy work for a clean surgical commit boundary.

Implemented scope:

- Review git status, diff scope, generated files, and validation evidence.
- Do not commit unless explicitly requested by the user.
- Preserve commit hygiene rules and avoid generated/AI attribution footers.

The accumulated autonomy work was reviewed as a commit boundary before any staging or commit action. The working tree is broad but coherent: autonomy runtime behavior, CLI presentation, focused module extraction, tests, README/changelog guidance, and the autonomy roadmap are all part of the same bounded-autonomy hardening arc. The untracked files are source modules, not generated artifacts; no changes are staged; no unresolved conflict files are present; and whitespace validation is clean. Commit hygiene remains explicit: no commit should be created without user confirmation, and any eventual commit message must use the personal GitHub identity with no co-author or automation attribution footer.

## Bounded Autonomy Staging Plan Review

Purpose: prepare a surgical staging and commit-message plan without committing.

Implemented scope:

- Group the accumulated autonomy changes into a clear staging boundary.
- Draft a concise commit message that matches recent repository style.
- Do not stage or commit unless explicitly requested by the user.

The accumulated diff was reviewed for staging and is best treated as one cohesive commit boundary. The files group into a single bounded-autonomy hardening story:

- Runtime safety and learning: `src/personal_assistant/autonomy.py`, `src/personal_assistant/approval_context.py`, `src/personal_assistant/command_registry.py`, `src/personal_assistant/execution.py`, and `src/personal_assistant/factory.py`.
- CLI surfaces and module extraction: `src/personal_assistant/cli.py`, `src/personal_assistant/cli_agent.py`, `src/personal_assistant/cli_autonomy.py`, `src/personal_assistant/cli_autopilot.py`, `src/personal_assistant/cli_factory.py`, `src/personal_assistant/cli_review.py`, `src/personal_assistant/cli_launchd.py`, and `src/personal_assistant/cli_setup_live.py`.
- Loop and scheduler audit behavior: `src/personal_assistant/autonomy_loop.py` plus related CLI output.
- Regression coverage: `tests/test_autonomy.py`, `tests/test_cli.py`, and `tests/test_command_registry.py`.
- Public operator guidance: `README.md`, `CHANGELOG.md`, and `docs/BOUNDED_AUTONOMY.md`.

No staging or commit action was performed. A future explicit commit request can use one all-files stage for the changed/untracked source, tests, and docs, followed by a message in the recent repository style:

```text
Harden bounded autonomy surfaces
```

The commit message should remain a plain subject with no co-author or automation attribution footer and should be created with the personal GitHub identity.

## Bounded Autonomy Commit Handoff Review

Purpose: prepare the final operator handoff before any optional commit action.

Implemented scope:

- Reconfirm the staging plan, validation status, and clean public-hygiene result.
- Surface the exact commit command only if the user explicitly asks to commit.
- Continue to avoid staging or committing during unattended loop ticks.

The final commit handoff was reviewed without changing the index. The current boundary remains one cohesive bounded-autonomy hardening commit, and the proposed subject remains:

```text
Harden bounded autonomy surfaces
```

Validation evidence is current from the staging-plan review: the attribution scan covered all changed and untracked files, the full unit suite passed, strict release checks reported zero public-hygiene findings, `doctor` passed, whitespace validation passed, and the index was confirmed clean. No exact staging or commit command is included here because unattended loop ticks must not initiate or script commit actions. The next action is an explicit operator decision: ask to commit this boundary, ask to split it, or ask to keep iterating before commit.

## Bounded Autonomy Operator Decision Gate

Purpose: avoid expanding the already validated commit boundary without an explicit operator decision.

Implemented scope:

- Recheck status and validation freshness only.
- Do not add runtime, docs, test, staging, or commit changes unless explicitly requested.
- Keep the documented commit boundary ready for user-directed commit, split, or continued feature work.

The operator decision gate held through unattended loop ticks with no edits, staging, or commits. After explicit direction to continue feature work, the next autonomy slice resumed from the validated boundary.

## Goal Scheduler Feedback Label Review

Purpose: make non-approval goal scheduler handoffs visible to the privacy-safe recommendation feedback loop.

Implemented scope:

- Add stable feedback labels to `myos loop goals` run-goal recommendations and no-eligible-goal review recommendations.
- Add the same goal-review label to no-op goal cycle output, including autopilot goal-wrapper output.
- Document the goal scheduler feedback labels in CLI help and README guidance.
- Preserve one-shot scheduler semantics and approval gates; labels are advisory calibration metadata only.

Goal scheduler recommendations now use `run_goal_cycle` for `myos loop run-goal --goal N` handoffs and `review_goals` for `myos goal list` recovery handoffs. Approval-related scheduler handoffs continue to use `review_approvals`.

## Goal Scheduler Feedback Label Final Review

Purpose: review the new goal scheduler feedback labels across output, help, docs, and summaries.

Implemented scope:

- Verify label consistency across `loop goals`, `loop run-goal`, and autopilot goal-wrapper output.
- Confirm recommendation feedback summaries preserve command context and privacy boundaries for the new labels.
- Prefer tests/docs only unless a concrete label mismatch remains.

The goal scheduler feedback labels were reviewed across CLI output, help text, README guidance, and recommendation feedback summaries. No runtime mismatch was found: `run_goal_cycle`, `review_goals`, and `review_approvals` remain consistent with their printed commands and approval semantics. The final gap was regression coverage for the summary path, so tests now record feedback for `run_goal_cycle` and `review_goals`, assert command context is visible, and guard that raw feedback notes remain hidden.

## Goal Scheduler Feedback Summary Help Review

Purpose: improve discoverability of goal scheduler feedback labels in recommendation summary/help surfaces.

Implemented scope:

- Review `myos autonomy recommendations --help` and related README text for goal scheduler label discoverability.
- Prefer docs/help/test updates only.
- Preserve feedback privacy and do not change scheduler execution behavior.

Recommendation summary help and README guidance now explicitly document that goal scheduler labels such as `run_goal_cycle` and `review_goals` appear with command context in `myos autonomy recommendations`, while raw feedback notes remain hidden. This keeps the feedback entry path and summary inspection path aligned without changing scheduler execution behavior or approval gates.

## Goal Scheduler Feedback Summary Final Review

Purpose: perform a final consistency review of the goal scheduler feedback summary surface.

Implemented scope:

- Verify help, README, tests, and summary output agree on label names and privacy boundaries.
- Prefer no runtime changes unless a concrete mismatch remains.
- Preserve one-shot scheduler behavior and approval-gated execution.

The goal scheduler feedback summary surface was reviewed across `myos autonomy recommendation-feedback --help`, `myos autonomy recommendations --help`, README guidance, regression tests, and the CLI summary contract. No runtime mismatch was found. The labels `run_goal_cycle`, `review_goals`, and `review_approvals` are documented with their command context, summary output keeps raw feedback notes hidden, and scheduler execution remains one-shot and approval-gated.

## Goal Scheduler Feedback Public Hygiene Review

Purpose: review public hygiene and release-readiness posture for the completed goal scheduler feedback work.

Implemented scope:

- Review README, changelog, release-check output, and attribution hygiene for the goal scheduler feedback labels.
- Prefer no runtime changes unless a public-readiness issue remains.
- Preserve privacy-safe recommendation feedback and approval-gated scheduler behavior.

The completed goal scheduler feedback work was reviewed for public hygiene. README guidance uses generic examples, changelog entries describe behavior without private context, strict release checks report zero public-hygiene findings, and the label names remain stable, privacy-safe operator metadata. No runtime changes were needed; `run_goal_cycle`, `review_goals`, and `review_approvals` continue to preserve one-shot scheduler behavior and approval-gated execution.

## Goal Scheduler Feedback Learning Signal Review

Purpose: review whether the new goal scheduler feedback labels provide enough advisory learning signal without changing scheduler execution.

Implemented scope:

- Review recommendation feedback summaries and ranking behavior for `run_goal_cycle` and `review_goals`.
- Prefer tests/docs unless a concrete learning-signal gap remains.
- Preserve one-shot scheduler behavior and never weaken approval gates.

Goal scheduler feedback learning was reviewed across summary side-effect context and recommendation ranking behavior. A concrete advisory-signal gap remained: `run_goal_cycle` feedback could be summarized without a local-write side-effect signal because the broad top-level `loop` command metadata does not describe the specific goal-cycle handoff. `run_goal_cycle` now contributes `local_db_write` as label-specific advisory metadata, while `review_goals` remains read-only/no-side-effect. Regression coverage verifies summary side effects, learning-score ranking, hidden raw notes, and unchanged one-shot scheduler semantics.

## Goal Scheduler Feedback Learning Final Review

Purpose: perform a final review of goal scheduler feedback learning signals before returning to commit readiness.

Implemented scope:

- Verify `run_goal_cycle` and `review_goals` learning metadata, summaries, docs, and tests remain aligned.
- Prefer no runtime changes unless a concrete advisory-signal mismatch remains.
- Preserve one-shot scheduler behavior and approval-gated execution.

Goal scheduler feedback learning signals were reviewed across label metadata, recommendation summaries, README guidance, changelog coverage, and regression tests. No runtime mismatch remained. `run_goal_cycle` carries `local_db_write` as advisory learning metadata for the bounded goal-cycle handoff, `review_goals` remains read-only/no-side-effect, and summary output preserves command context while hiding raw feedback notes. Scheduler execution remains one-shot and approval-gated.

## Goal Scheduler Feedback Commit Readiness Review

Purpose: review the accumulated goal scheduler feedback work for a clean commit boundary.

Implemented scope:

- Review git status, diff scope, validation evidence, and attribution hygiene for the recent goal scheduler feedback slices.
- Do not stage or commit unless explicitly requested by the user.
- Preserve commit hygiene rules and the existing bounded-autonomy commit boundary.

The recent goal scheduler feedback work was reviewed for commit readiness without staging or committing. The changes remain part of the existing bounded-autonomy hardening boundary: scheduler handoff labels, recommendation feedback summaries, advisory learning metadata, docs, changelog, and regression tests all support the same operator-feedback arc. Strict release checks report zero public-hygiene findings, attribution scanning over changed and untracked files is clean, whitespace validation is clean, no unresolved conflict files are present, and the index remains empty. The untracked files are source modules already included in the broader bounded-autonomy boundary.

## Bounded Autonomy Operator Decision Gate

Purpose: pause feature expansion until the operator chooses to commit, split, or continue the already validated boundary.

Implemented scope:

- Recheck status and validation freshness only.
- Do not add runtime, docs, test, staging, or commit changes unless explicitly requested.
- Preserve commit hygiene rules and the current bounded-autonomy boundary.

The operator decision gate was checked with no edits, staging, or commits. After explicit direction to continue feature work, the next bounded-autonomy slice resumed from the validated boundary.

## Goal Scheduler Feedback Surface Review

Purpose: make goal scheduler recommendation feedback easier to inspect as its own summary surface.

Implemented scope:

- Classify `run_goal_cycle` and `review_goals` recommendation feedback as `surface=goal_scheduler`.
- Keep command context, useful/not-useful scores, advisory side-effect metadata, and hidden raw-note privacy unchanged.
- Preserve one-shot scheduler behavior and approval-gated execution.

Goal scheduler feedback now appears as `surface=goal_scheduler` in `myos autonomy recommendations`, separating scheduler calibration from generic recommendations while keeping the same privacy and approval boundaries. Daily recommendations remain `surface=daily`; other recommendation labels remain `surface=general`.

## Next Slice: Goal Scheduler Feedback Surface Final Review

Purpose: review the dedicated goal scheduler feedback surface across output, help, docs, and tests.

Scope:

- Verify `surface=goal_scheduler` appears consistently for `run_goal_cycle` and `review_goals`.
- Prefer docs/tests only unless a concrete surface mismatch remains.
- Preserve privacy-safe feedback summaries and approval-gated scheduler behavior.

## Executable Packaging Readiness Review

Purpose: verify that MYOS is packaged as an executable local CLI before adding standalone binary packaging.

Implemented scope:

- Review `pyproject.toml`, README setup guidance, CI install behavior, and release-check coverage.
- Add a release-readiness check for the `myos` console script entrypoint.
- Document editable install, `pipx install .`, and the current boundary between Python console packaging and future standalone binary packaging.
- Preserve local-first runtime behavior and avoid changing scheduler or approval semantics.

MYOS is currently executable as a Python console application through the package script entrypoint `myos = "personal_assistant.cli:main"`. `myos release-check --strict` now verifies that packaging contract before reporting release readiness. Standalone executable packaging remains a later layer after the Python package boundary is stable.

## Executable Packaging Smoke Final Review

Purpose: review executable packaging readiness across release-check output, docs, CI, and tests.

Implemented scope:

- Verify the `package_entrypoint` release-check row, README install guidance, and CI editable install path remain aligned.
- Prefer tests/docs only unless a concrete packaging mismatch remains.
- Keep standalone binary packaging as a later explicit decision.

Executable packaging readiness was reviewed across release-check output, README setup guidance, CI install behavior, and regression coverage. The Python package boundary is aligned: `pyproject.toml` defines `myos = "personal_assistant.cli:main"`, README documents editable and `pipx` installs, CI installs the package before running tests and `myos release-check --strict`, and release-check reports `PASS package_entrypoint`. No runtime changes were needed. Standalone signed binary packaging remains a later explicit decision.

## Wheel Artifact Smoke Review

Purpose: decide whether wheel artifact build validation should become an automated release-readiness gate.

Implemented scope:

- Review the manual wheel build smoke result, CI workflow, and release-check scope.
- Prefer a lightweight test or CI/docs update over introducing a full binary packaging stack.
- Keep standalone executable packaging separate from Python wheel packaging.

The manual wheel build smoke was promoted into the CI release-readiness job as a lightweight artifact gate before `myos release-check --strict`. The gate builds a local wheel with `python -m pip wheel --no-deps . -w dist/wheel-smoke` and fails if no wheel is produced. README setup guidance now mentions the CI wheel smoke while preserving the boundary that MYOS is currently a Python console application, not a standalone binary.

## Installed Command Smoke Review

Purpose: verify that the installed `myos` command path is covered separately from module execution.

Implemented scope:

- Review CI and tests for coverage of `python -m personal_assistant.cli` versus installed `myos`.
- Prefer a lightweight installed-command smoke over changing packaging architecture.
- Preserve the current Python console application boundary.

CI release readiness now includes a dedicated installed-command smoke after editable install: `myos --help >/dev/null`. This covers the console script path independently from module-based test execution while keeping the strict release gate on `myos release-check --strict`. README setup guidance documents the installed-command smoke alongside the wheel artifact smoke.

## Packaging CI Final Review

Purpose: review the packaging CI gates as a coherent release-readiness path before choosing any standalone executable work.

Implemented scope:

- Verify installed command smoke, wheel artifact smoke, and strict release-check ordering.
- Prefer docs/tests only unless a concrete CI packaging mismatch remains.
- Keep standalone binary packaging behind an explicit operator decision.

The release-readiness job now verifies the packaging path in order: install the package, smoke the installed `myos` command, build a wheel artifact, then run `myos release-check --strict`. The strict gate now runs through the installed command path without a `PYTHONPATH=src` override, so CI covers the console entrypoint rather than only module execution. Standalone binary packaging remains behind an explicit operator decision.

## Standalone Packaging Decision Gate

Purpose: decide whether MYOS should add standalone executable packaging now or continue hardening Python package release readiness.

Implemented scope:

- Review current Python console, wheel, and CI smoke coverage before adding any binary tooling.
- Prefer an operator decision over introducing PyInstaller, zipapp, Homebrew, or signing workflows by default.
- Preserve local-first release safety and public hygiene.

Decision: defer standalone executable packaging for now. The current release boundary is strong enough for the next public-readiness step: `myos` is a Python console application with an entrypoint guard, installed-command smoke, wheel artifact smoke, and strict release-check coverage in CI. Standalone binary tooling such as PyInstaller, zipapp, Homebrew packaging, notarization, or signing should be introduced only after an explicit operator decision with platform, distribution, and signing requirements.

## Packaging Commit Readiness Review

Purpose: review the accumulated packaging changes as a coherent commit boundary.

Implemented scope:

- Review CI, README, release-check, tests, roadmap, and changelog changes for a clean packaging slice.
- Verify validation evidence and public hygiene remain current.
- Do not stage or commit without an explicit user commit request.

Packaging readiness is a coherent commit boundary within the larger accumulated autonomy diff: release-check guards the `myos` entrypoint, README documents executable install and the standalone-binary boundary, CI smokes installed `myos`, builds a wheel, and runs strict release readiness through the installed command path, and tests cover the workflow contracts. Validation evidence is current, public hygiene remains clean, and no files were staged or committed. A future commit should be explicit and should use a concise packaging/release-readiness message without co-author or generated-by trailers.

## Packaging Operator Decision Gate

Purpose: pause packaging expansion and decide whether the next action is commit handoff, more validation, or returning to autonomy capability work.

Implemented scope:

- Reconfirm git status, validation freshness, and public hygiene after packaging commit readiness.
- Do not stage, commit, or add binary packaging without an explicit user request.
- Prefer the next bounded autonomy capability slice if packaging readiness remains complete.

Packaging expansion is complete for now. Release readiness, full validation, wheel smoke, and public hygiene remain current, and no files were staged or committed. Since standalone binary packaging is deferred behind an explicit operator decision, the next loop returns to bounded autonomy capability work instead of adding more packaging surface.

## Local Router Command Mapper Final Review

Purpose: review the local router command mapper as a model-facing autonomy contract.

Implemented scope:

- Verify command mapper output includes commands, subcommands, required arguments, examples, tiers, intents, safety levels, and side-effect metadata.
- Prefer docs/tests only unless a concrete mapper coverage gap remains.
- Preserve privacy-safe metadata and avoid exposing raw user text to local router models.

The command mapper contract now has a final review across implementation, CLI output, README guidance, and tests. `local_model_command_mapper()` exposes schema, tiers, safety levels, side-effect types, and command metadata including subcommands, required arguments, examples, intents, confirmation requirements, dry-run defaults, and long-running flags. `myos router commands` now mirrors the model-safe metadata by printing side effects and runtime flags without raw user text.

## Local Router Command Mapper Public Hygiene Review

Purpose: verify the mapper remains safe for local model prompts and public repository exposure.

Implemented scope:

- Review mapper output and docs for raw user text, private references, or overly specific local data.
- Prefer tests/docs only unless a concrete privacy or public-hygiene gap remains.
- Preserve command metadata usefulness for local routing.

The local router command mapper was reviewed as a public, model-facing metadata surface. The mapper and `myos router commands` output remain command-metadata-only: command names, examples, safety classes, side-effect classes, runtime flags, and routing intents, with no raw user text or stored feedback payload fields. Regression coverage now serializes the mapper and guards against private references and raw payload field names while preserving useful routing metadata.

## Local Router Command Mapper Commit Readiness Review

Purpose: review the accumulated router mapper changes as a coherent commit boundary.

Implemented scope:

- Review command registry, router command output, README, roadmap, changelog, and tests for a clean mapper slice.
- Verify validation evidence and public hygiene remain current.
- Do not stage or commit without an explicit user commit request.

The local router command mapper work is a coherent commit boundary within the larger accumulated autonomy diff: `command_registry.py` exposes model-safe mapper metadata, `myos router commands` mirrors side-effect and runtime flags, README documents the metadata-only surface, tests cover mapper fields, CLI output, and public hygiene, and the roadmap/changelog capture the final review. Validation evidence and public hygiene are current, and no files were staged or committed. A future commit should be explicit and scoped to router mapper/autonomy metadata without co-author or generated-by trailers.

## Local Router Command Mapper Operator Decision Gate

Purpose: pause mapper expansion and decide whether the next action is commit handoff, more validation, or another bounded autonomy capability slice.

Implemented scope:

- Reconfirm git status, validation freshness, and public hygiene after mapper commit readiness.
- Do not stage, commit, or add new mapper scope without an explicit user request.
- Prefer the next bounded autonomy capability slice if mapper readiness remains complete.

Mapper expansion is complete for now. Release readiness, mapper-focused tests, full validation, wheel smoke, and public hygiene remain current, and no files were staged or committed. Since the mapper contract is now model-safe, operator-visible, and commit-ready, the next loop returns to the adjacent bounded autonomy capability that consumes command metadata for safer recommendations.

## Runtime Recommendation Side-Effect Final Review

Purpose: review runtime recommendations that use command side-effect metadata to prefer safer next commands.

Implemented scope:

- Verify diagnostics, dry-runs, backups, and approval-review recommendations are prioritized before risky setup or service changes.
- Prefer docs/tests only unless a concrete recommendation ordering or side-effect metadata gap remains.
- Preserve advisory-only recommendation behavior; do not auto-execute recommended commands.

Runtime recommendations now have final-review coverage for side-effect-aware ordering and advisory-only behavior. Setup and launchd paths recommend dry-run/status/runbook checks before service changes; restore paths recommend backup and migration verification; long-running commands recommend health checks; connector/external paths recommend approval review with `myos approve --list`. Regression coverage verifies these recommendations remain guidance only and do not carry auto-execution fields.

## Runtime Recommendation Public Hygiene Review

Purpose: verify recommendation outputs and summaries remain privacy-safe public surfaces.

Implemented scope:

- Review recommendation output and summaries for raw notes, raw request fields, command payloads, private references, and local paths.
- Prefer tests/docs only unless a concrete privacy or public-hygiene gap remains.
- Preserve advisory side-effect and learning metadata without exposing private content.

Runtime recommendation feedback summaries were reviewed as public-facing advisory surfaces. Summary rows preserve label, command context, surface, side-effect classes, bounded recent scores, mixed-feedback flags, and advisory learning scores, but continue to omit raw notes, note metadata, raw request fields, stored payload fields, private references, and local paths. Regression coverage now serializes recommendation summaries and guards those public-hygiene boundaries while keeping side-effect metadata visible.

## Runtime Recommendation Commit Readiness And Operator Gate

Purpose: close runtime recommendation safety work as a coherent boundary before moving to local production readiness.

Implemented scope:

- Review side-effect ordering, public hygiene, README/help coverage, roadmap, changelog, and tests.
- Verify validation evidence and public hygiene remain current.
- Do not stage or commit without an explicit user request.

Runtime recommendation safety is commit-ready within the larger accumulated autonomy diff: side-effect ordering is covered for dry-runs, backups, approval review, diagnostics, long-running commands, and connector review; recommendation summaries preserve advisory side-effect and learning metadata without exposing raw notes or payload fields; roadmap and changelog entries capture the review. No files were staged or committed.

## Local Production Readiness Final Review

Purpose: verify a clean local install can become a safe daily-running MYOS instance.

Implemented scope:

- Review setup-live, doctor, strict doctor, migration verification, config templates, package install, DB path, permissions, and optional-tool behavior.
- Prefer docs/tests only unless a concrete local production readiness gap remains.
- Preserve offline usefulness and safe missing-credential behavior.

Local production readiness was reviewed across setup guidance, config templates, doctor checks, migration verification, package install checks, and setup-live readiness. `setup-live --check` now treats missing optional connector credentials as informational, matching local-first operation and `myos doctor`; required local readiness checks still cover env file, permissions, watch directory, database file, standing goals, and watch configuration. Tests now cover applying setup in a temporary local data directory and passing readiness check without connector credentials.

## Runtime Service And Scheduler Readiness Review

Purpose: make recurring local operation safe, inspectable, and bounded.

Implemented scope:

- Review launchd install/uninstall/status, runtime start/stop/live flows, goal scheduler one-shot behavior, and dry-run/confirmation boundaries.
- Prefer docs/tests only unless a concrete service or scheduler readiness gap remains.
- Avoid starting long-running daemons during validation unless explicitly requested.

Runtime service and scheduler readiness was reviewed across launchd install/uninstall/status, activate/start/stop/live flows, and goal scheduler handoffs. Launchd install/uninstall remain dry-run by default and require explicit `--apply`; command metadata marks service writes as confirmation-required; runtime recommendation ordering points operators to dry-run/status/runbook checks before service changes; goal scheduler commands run one bounded cycle or print review handoffs rather than silently daemonizing. Validation uses dry-run and one-shot paths only.

## External Connector And Approval Boundary Final Review

Purpose: prove external connector mutations remain approval-gated and auditable.

Implemented scope:

- Review connector sync, risk-scan, action-provider, approval queue, outbox, and execution receipt flows.
- Prefer docs/tests only unless a concrete approval-boundary gap remains.
- Preserve connector dry-run/outbox behavior unless live mutation is explicitly enabled.

External connector and approval boundaries were reviewed across action listing, approval review, action provider execution, connector dry-run outbox writes, blocked invalid connector payloads, execution receipts, factory connector workflows, and release-check factory smoke. Existing coverage verifies connector actions require approval, dry-run connector actions write local outbox records, blocked connector actions create receipts and follow-up work, receipt context includes side effects and review gates, and connector factory workflows use explicit policy before executing dry-run connector paths.

## Recovery, Backup, Restore, And Migration Final Review

Purpose: make local production recoverable.

Implemented scope:

- Review backup, restore, migration verification, pre-restore backup, SQLite integrity checks, and recovery docs.
- Prefer docs/tests only unless a concrete recovery gap remains.
- Preserve refusal behavior for invalid restore inputs and verify schema after restore.

Recovery readiness was reviewed across backup, restore, migration verification, SQLite integrity checks, and recovery docs. Restore continues to refuse missing or invalid SQLite inputs, creates a pre-restore backup of the current database before replacing it, removes stale WAL/SHM sidecars, and verifies schema migrations after restore. Tests now explicitly cover pre-restore backup creation and refusal of invalid restore input.

## Audit, Observability, Retention, And Learning Final Review

Purpose: ensure operators can inspect what the agent did, what failed, what was approved, and what was learned.

Implemented scope:

- Review traces, loop ledger, execution receipts, approval lists, recommendation summaries, retention cleanup, and failure follow-up visibility.
- Prefer docs/tests only unless a concrete auditability gap remains.
- Preserve privacy-safe audit surfaces and avoid raw payload exposure by default.

Audit and observability were reviewed across command traces, trace cleanup/rollups, autonomy loop ledger, approval/action lists, execution receipts, failure follow-up creation, recommendation summaries, and retention cleanup. Trace cleanup now has regression coverage showing detailed old traces roll up into aggregate retained counts, while loop and connector tests cover linked traces, pending approvals, receipts, side-effect context, and follow-up visibility. Public-facing summaries continue to omit raw feedback notes and payload internals by default.

## Production Runbook And Public Documentation Final Review

Purpose: make public docs accurate for local-first production use without overclaiming maturity.

Implemented scope:

- Review README, architecture, roadmap, recovery docs, migrations docs, runbook output, and changelog.
- Prefer docs/tests only unless command output materially overclaims or misses an operator-critical step.
- Avoid claiming GraphRAG or enterprise stability beyond the implemented local-first capability.

Production runbook and public docs were reviewed for local-first accuracy and overclaiming risk. README, architecture, and roadmap continue to state MVP status and GraphRAG limitations. The README now includes a concise local production checklist for setup-live, strict doctor, migration verification, backup, one-shot autopilot, approval review, receipts, trace cleanup, and optional launchd dry-run review. The `myos runbook --short` output now includes the same operator-critical setup, backup, approval, receipt, and audit commands.

## CI And Release Workflow Alignment Review

Purpose: align release workflow gates with current CI packaging and readiness checks.

Implemented scope:

- Review CI workflow, release workflow, installed-command smoke, wheel smoke, test gates, dependency checks, and strict release-check.
- Prefer workflow/docs/tests only unless a concrete release gate drift remains.
- Preserve no-publish-by-default behavior unless explicitly tagged/released by the operator.

CI and release workflow gates were aligned. The release workflow now upgrades pip, installs the package, smokes the installed `myos` command, runs tests, builds a lightweight wheel artifact, runs strict dependency/license checks, runs strict doctor and migration verification, and finishes with strict release readiness. The workflow remains validate-only and does not publish artifacts by default. Regression coverage now checks that the release workflow includes these gates and avoids `PYTHONPATH` overrides or upload steps.

## Final Validation Soak

Purpose: run the full validation battery before commit grouping.

Implemented scope:

- Run full tests, strict doctor, strict migrations, strict release-check, strict dependency-check, whitespace checks, wheel build, venv install smoke, attribution scan, private-reference scan, and local artifact scan.
- Investigate and fix any failures surgically.
- Preserve current git state until the commit operator gate.

Final validation soak passed: 229 tests, strict doctor, strict migrations, strict release-check, strict dependency-check, `git diff --check`, wheel build, isolated editable venv install with `myos --help`, installed-command release-check, explicit private-reference scan, attribution scan, and tracked local artifact scan. No files were staged or committed during validation.

## Commit Grouping And Operator Gate

Purpose: prepare surgical commit grouping and wait for explicit operator approval before committing.

Implemented scope:

- Review git status and diff at the final-leg boundary.
- Identify coherent commit groups and any relevant changes from the other checkout that should be ported into this public repo.
- Do not commit unless explicitly approved by the operator.

The final-leg changes were reviewed as one coherent local-production readiness boundary: bounded autonomy recommendation safety, local setup/runtime readiness, connector approval boundaries, recovery, observability, production docs, CI/release gates, and validation evidence. The other checkout was checked for relevant public-ready changes; license metadata, `.gitignore`, README hardening, and Bedrock wording were already represented in this repo, while the sanitized README-referenced launchd plist templates were ported into `deploy/launchd/` and covered by tests/release hygiene scans. The operator explicitly approved adding and committing relevant cross-checkout work into this public repo.

## Next Slice: Push, CI, And Release Operator Gate

Purpose: push and monitor CI only after explicit operator approval.

Scope:

- Push the committed branch only if approved.
- Monitor CI and fix failures if they occur.
- Tag or create a release only if separately approved.

## GitLawb Zero Coding Executor Safety Contract Review

Purpose: review the committed GitLawb Zero coding executor as an autonomy boundary for repo-scoped code work.

Implemented scope:

- Validate that `myos code` and `factory start --executor zero` route generated code through isolated worktrees and approval-gated `apply_patch` proposals.
- Clarify the operator config split between text-mode GitLawb Zero delegation and structured streaming GitLawb Zero factory execution.
- Preserve explicit approval review before any local repo patch is applied.

GitLawb Zero coding delegation is now reviewed as a bounded autonomy surface rather than an automatic coder. The simple `myos code ... --backend zero` path uses `MYOS_AGENT_EXEC_ZERO` and proposes diffs from an isolated worktree. The factory executor path uses the structured streaming adapter, configurable with `MYOS_AGENT_EXEC_ZERO_STREAM`, and still records an agent run plus an approval-required action. Approval review now explicitly covers local repository mutations as well as external systems. This `zero` backend is distinct from the Agent Zero framework.

The canonical proof loop is `myos factory start --mode semi_autonomous --pack software_delivery --executor zero --repo <disposable_repo>`. That path binds a coding task to an intent, retrieval evidence, review packet, Zero executor artifact, proposed `apply_patch` action, execution receipt, and factory learning record. `myos factory status`, `myos factory review`, and `myos approve --list` surface the git-derived Zero changed files, patch size stats, run/session reference, isolated-worktree status, compact permission counts, privacy-filtered warning/error signals, suggested verification commands, failed-run follow-up inbox item when needed, safe MYOS retry command, and exact `myos approve --action <id> --execute` command, while the source repo remains unchanged until the MYOS approval command runs. After approval execution, `myos execution-receipt list/show` records the suggested verification commands as `not_run` audit evidence so operators can see what still needs local verification without MYOS auto-running shell commands. Oversized Zero diffs are kept as review-only draft actions instead of truncated `apply_patch` proposals. Raw permission event payloads and transient worktree paths are not stored in review packets or approval action metadata. See `examples/demo-zero-proof.md` for the disposable-repo smoke test.

Public hygiene now includes `examples/` in the release scan so the Zero proof runbook is checked with README, docs, source, tests, deploy templates, and workflows before release readiness passes.

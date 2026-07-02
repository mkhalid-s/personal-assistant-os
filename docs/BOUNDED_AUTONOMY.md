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

## Next Slice: Agent Command Module Split

Purpose: continue reducing `cli.py` size by moving agent task, action, learning, and status command presentation into a focused module.

Scope:

- Move agent-facing command handlers without changing behavior.
- Preserve approval output, execution receipt behavior, and public CLI text.
- Keep the parser in `cli.py` and validate with focused agent/action regressions.

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

## Next Slice: Decision-Aware Recommendations

Purpose: use policy decisions to guide users toward safer next steps before they start work.

Scope:

- Suggest safer alternatives when a command or route needs approval.
- Show the closest approval/review command for the current decision.
- Keep suggestions read-only and deterministic.

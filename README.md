# Personal Assistant OS

[![CI](https://github.com/mkhalid-s/personal-assistant-os/actions/workflows/ci.yml/badge.svg)](https://github.com/mkhalid-s/personal-assistant-os/actions/workflows/ci.yml)

Local-first CLI assistant for planning work, remembering context, triaging tasks, and safely proposing agentic actions. It keeps the user's working memory in a local SQLite store, retrieves relevant context on demand, and gates external mutations behind explicit approval.

## Current Status

This repository is an MVP public baseline. It is useful as a local CLI assistant with reliability checks, durable plans, review packets, retrieval traces, policy-aware factory runs, and daily operating loops, but it is not yet a production-stable application or a graph database application.

The current graph support is SQLite-based and lightweight: `knowledge_nodes`, `knowledge_edges`, deterministic `entities`/`entity_aliases`, typed `relationships`, `claims`, manual links, conversation-derived relationship hints, persisted retrieval traces for cited graph expansion, entity-aware retrieval expansion, bounded multi-hop work-item traversal, claim-backed retrieval, and fixture-based retrieval evals. See `ARCHITECTURE.md` and `ROADMAP.md`.

## Project Direction

The long-term direction is a local-first AI control plane:

```text
Intent -> Context -> Plan -> Agent Work -> Review -> Approval -> Execution -> Audit -> Learning
```

The design is inspired by AI-native software-factory ideas: intent-first workflows, living documentation, graph-backed context, approval-gated agents, and full audit trails. This project applies those ideas to a personal, open-source, local-first assistant OS.

## What It Does

- Captures notes, tasks, commitments, decisions, risks, and daily logs.
- Syncs optional external context from configured connectors such as Jira, GitHub, Confluence, and Aha.
- Ingests text, audio transcripts, images, meeting notes, and watched folders.
- Builds searchable memory with provenance, deterministic entity and relationship extraction, graph links, hybrid retrieval, persisted retrieval traces, retrieval eval fixtures, and graph-aware "why" explanations.
- Runs assistant workflows through chat, voice, autopilot, one-shot smart routing, morning briefs, durable plans, review packets, policy-aware factory runs, provider-backed role runs with local fallback, risk scans, delegation, approvals, connector dry-run outbox workflows, and weekly reviews.
- Redacts common PII and secrets before persistence and keeps private runtime data out of git.

## Design Docs

- `ARCHITECTURE.md`: current architecture, target operating loop, and GraphRAG direction.
- `ROADMAP.md`: surgical roadmap from MVP to stable app, intent layer, GraphRAG, and product hardening.
- `docs/BOUNDED_AUTONOMY.md`: bounded autonomy direction, router feedback application, command registry, and lightweight observability plans.
- `CHANGELOG.md`: release notes for the current checkpoint and future tagged releases.

## Open Source Stack

Core runtime:

- Python 3.10+
- SQLite via the Python standard library
- `setuptools` packaging
- `unittest` test suite
- `anthropic` Python SDK for the default hosted reasoning backend

Optional local tools:

- `sounddevice` and `faster-whisper` for voice/audio transcription workflows
- `tesseract` for OCR if you use image ingestion
- macOS `launchd` for always-on local scheduling

External services are optional. Connectors only run when their environment variables are configured, and approved-action execution is off by default.

## Safety Model

The assistant is designed to propose before it mutates external systems.

- Local capture and bookkeeping can run directly.
- External updates are normalized as connector mutations and drafted into an approval queue/outbox by default.
- Destructive or broad actions are blocked by policy.
- Executed, failed, blocked, and no-op actions write execution receipts; failed or blocked receipts create follow-up inbox items.
- Conversation logs, action payloads, and indexed text pass through privacy filters.
- Runtime data lives under `data/`, which is ignored by git.

## Setup

```bash
cd personal-assistant-os
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

This installs the `myos` console command from the package entrypoint:

```bash
myos doctor
myos release-check --strict
```

For an isolated local CLI install, use `pipx` from the repository root:

```bash
pipx install .
```

CI also smokes the installed `myos` command with `myos --help`, then performs a lightweight wheel artifact build with `python -m pip wheel --no-deps .` before the strict release-readiness gate.

MYOS is currently packaged as a Python console application, not a standalone signed binary. Standalone executable packaging can be layered later with tools such as zipapp, PyInstaller, or a Homebrew formula after the Python package boundary is stable.

Install optional voice dependencies only if needed:

```bash
python -m pip install -e '.[voice]'
```

## Configuration

Set only what you use. Missing connectors are skipped safely.

```bash
# Start from the tracked safe template:
cp .env.example data/.env.myos

# Optional custom DB path
export MYOS_DB_PATH="/path/to/personal-assistant-os/data/assistant.db"

# Optional external connectors
export JIRA_BASE_URL="https://example.atlassian.net"
export JIRA_USER_EMAIL="you@example.com"
export JIRA_API_TOKEN="<token>"

export GITHUB_TOKEN="<token>"
export GITHUB_OWNER="<org-or-user>"
export GITHUB_REPO="<repo>"

export CONFLUENCE_BASE_URL="https://example.atlassian.net"
export CONFLUENCE_USER_EMAIL="you@example.com"
export CONFLUENCE_API_TOKEN="<token>"

export AHA_BASE_URL="https://example.aha.io"
export AHA_API_TOKEN="<token>"

# Optional connector hardening
export MYOS_CONNECTOR_RETRIES="3"
export MYOS_CONNECTOR_BACKOFF_SEC="1.2"
export MYOS_CONNECTOR_TIMEOUT_SEC="25"

# Optional reasoning providers.
export MYOS_AGENT_BACKEND="cursor"  # claude|claude-sdk|claude-code-sdk|cursor|zero|claude-code|copilot|command
export MYOS_AI_COMMAND="/path/to/your-ai-wrapper"
export MYOS_AGENT_CMD_CURSOR="agent --print --trust --mode ask --output-format text"
export MYOS_AGENT_CMD_ZERO="zero exec --output-format text --auto low --no-notify"
export MYOS_AGENT_EXEC_ZERO="zero exec --output-format text --auto low --no-notify"
export MYOS_AGENT_EXEC_ZERO_STREAM="zero exec"
export MYOS_AGENT_CMD_CLAUDE_CODE="claude -p"
export MYOS_CLAUDE_MODEL="claude-opus-4-8"
export MYOS_SDK_LOAD_SETTINGS="0"

# Optional tiny local router model for intent finding.
# Dry-run first: myos model setup --router
export MYOS_ROUTER_BACKEND="ollama"
export MYOS_ROUTER_MODEL="qwen2.5:0.5b"
export MYOS_ROUTER_COMMAND="python3 /path/to/data/router/router_ollama.py"
export MYOS_ROUTER_TIMEOUT_SEC="8"
export MYOS_ROUTER_MIN_CONFIDENCE="0.70"

# Optional notification hook. Command receives assistant digest JSON on stdin.
export MYOS_NOTIFY_COMMAND="/path/to/notify-wrapper"

# Built-in safe default: write approved external actions into data/outbox.
export MYOS_ACTION_PROVIDER="builtin"
export MYOS_ACTION_COMMAND="myos action-provider"

# Optional live connector mutations. Keep unset for dry-run outbox behavior.
export MYOS_CONNECTOR_LIVE="0"
```

Do not commit local `.env` files, SQLite databases, logs, generated reports, or agent/tool settings. The repository `.gitignore` is configured to keep those local artifacts out of source control.

For a no-network first run, follow `examples/demo-local.md`. For a disposable coding-agent proof loop with GitLawb Zero, follow `examples/demo-zero-proof.md`.

## Local Production Checklist

MYOS is local-first and operator-driven. A safe daily setup is:

```bash
myos setup-live --check
myos setup-live --apply
myos doctor --strict
myos migrations verify --strict
myos backup
myos router eval
myos autonomy eval
myos autopilot --once --no-sync
myos approve --list
myos execution-receipt list
myos trace cleanup --retention-days 30 --max-rows 5000
```

Keep launch agents optional until the one-shot path is healthy:

```bash
myos launchd-install --autopilot
myos launchd-status
```

The launchd install command is a dry run until `--apply` is supplied.

## Smart Daily Surface

Most daily use should start with one of these surfaces instead of memorizing the full command catalog:

- `myos chat`: interactive assistant with routed intent awareness and approval-gated actions.
- `myos voice`: voice-first assistant using the same routed chat loop.
- `myos autopilot --factory`: proactive loop that selects a factory workflow pack from detected signals.
- `myos do "plan my day and draft follow-ups"`: one-shot natural-language router for CLI users.
- `myos factory start --pack software_delivery --executor zero`: auditable coding proof path that binds Zero output to intent, retrieval, review packets, approvals, receipts, and learning.
- `myos code "fix the failing tests" --backend zero`: quick coding-agent handoff that runs in an isolated worktree and proposes a patch for approval.
- `myos approve --list`: review anything that could mutate your local repo or external systems.

The `zero` backend here refers to [GitLawb Zero](https://github.com/gitlawb/zero), the coding agent CLI. It is distinct from the [Agent Zero framework](https://github.com/agent0ai/agent-zero). Use the factory path when you need the full MYOS loop; use `myos code` for a direct one-off patch proposal. `myos doctor` reports `zero_stream_executor` as an optional preflight for the structured factory path.

Use `myos help daily`, `myos help workflows`, `myos help expert`, or `myos help diagnostic` to see a smaller tiered command list.

## Tiny Local Router Model

MYOS can use a very small local model as a fallback for intent routing when deterministic confidence is low. This is optional and never downloaded during `pip install`.

Recommended first setup:

```bash
myos model recommend --purpose router
myos model setup --router
myos model setup --router --runtime ollama --model qwen2.5:0.5b --apply
```

Lower-memory fallback:

```bash
myos model setup --router --runtime ollama --model smollm2:360m --apply
```

The setup command keeps MYOS runtime-agnostic by writing a local JSON command wrapper under `data/router/` and printing env vars such as `MYOS_ROUTER_COMMAND`. The router still falls back to deterministic rules if the model runtime is unavailable, times out, or returns invalid JSON.

Router quality can be measured locally:

```bash
myos router eval
myos router eval --model-shadow
myos router feedback --event 123 --expected-intent daily_brief --note "Expected daily planning"
myos router overrides
myos router commands --tier workflow
```

`myos router eval` uses packaged, non-private fixtures and records only route metadata, confidence, and text hashes. Feedback records correction metadata against a `smart_route` event and stores note hashes/lengths, not raw request text.
Exact feedback corrections are applied only to the same future request hash, so unrelated phrasing still uses deterministic routing and optional model fallback.
`myos router commands` shows the static command registry that the router and tiny local model use for bounded tool awareness, including tier and safety metadata. Internally, the router also passes a local-model-safe command mapper with command names, subcommands, required args, examples, and safety metadata whenever a configured router model is asked to route a request.

### Execution traces stay lightweight

```bash
myos trace list
myos trace cleanup --retention-days 30 --max-rows 5000
myos trace rollups
```

MYOS records small execution trace rows for CLI commands and links them to route events, factory runs, agent tasks, or execution receipts when those records exist. Traces store correlation IDs, command path, status, duration, safety metadata, linked IDs, capped summaries, and hashes. They do not store raw stdout/stderr or private command text by default. Cleanup rolls old detailed rows into aggregate counts before deleting them.

### Autonomy decisions are explicit

`myos do "..."` and `myos factory start ...` print an autonomy decision before doing work:

```bash
Autonomy: decision=needs_approval tier=confirm safety=approval_gated reason=...
```

The decision uses command registry safety metadata and the existing `autonomy_level` policy. Local/read-only work can proceed, approval-gated and external-write work stays review-first, and destructive/unknown classifications remain blocked by the hard autonomy guards.

Local router models receive a metadata-only command map. Inspect the same privacy-safe surface with:

```bash
myos router commands
```

The command map includes command names, subcommands, required arguments, examples, tiers, intents, safety levels, side-effect classes, dry-run defaults, and long-running flags. It does not include raw user text.

Autonomy decisions can be calibrated locally:

```bash
myos autonomy eval
myos autonomy feedback --trace 123 --expected-decision needs_approval --note "Keep external sync approval-gated"
```

`myos autonomy eval` uses packaged, non-private safety fixtures. Feedback stores the trace link, expected/actual decision metadata, note hash, and note length, not raw notes or command arguments.

When a decision needs review, MYOS also prints deterministic recommendations such as `myos approve --list` or `myos factory review --id <run_id>`. These are suggestions only; MYOS never executes the recommended command automatically.

### Daily Recommendation Feedback

`myos next-action` and `myos now` print stable feedback labels on their selected daily recommendation:

```bash
myos next-action --meeting-hours 7
myos now
```

Copy the `label` and `command` values from the bracketed output, for example `[label=daily_reduce_risk command="myos next-action"]`.

If the recommendation was useful, record that locally:

```bash
myos autonomy recommendation-feedback \
  --label daily_reduce_risk \
  --command "myos next-action" \
  --useful yes \
  --note "Risk reduction was the better daily recommendation."
```

Use `--useful no` when the selected daily recommendation was not useful. Feedback is command-specific, so feedback for `myos next-action` does not tune `myos now` unless you submit feedback with `--command "myos now"`.

Daily ranking uses only a bounded 30-day score window, clamped to `-3..+3`. Raw feedback notes are not stored; MYOS stores note hashes and lengths for audit/privacy. To inspect learning without exposing notes:

```bash
myos autonomy recommendations
```

Daily rows show `surface=daily`, `recent_score_30d`, signed useful/not-useful counts, and `mixed_recent=yes` when recent useful and not-useful feedback offset each other. If feedback changes a daily winner, MYOS prints a compact ranking context with the selected and baseline bounded scores.

## Quick Start

```bash
myos capture "Follow up with platform team about auth token expiry by Friday"
myos do "what should I work on today?"
myos autopilot --once --factory
myos triage
myos today --meeting-hours 4
myos sync --connector all
myos transcribe /path/to/meeting.m4a --text "Decision: move freeze to Wednesday. Follow up by Friday."
myos ingest-image /path/to/whiteboard.png --text "Task: add canary checks. Risk: platform dependency."
myos inbox-process
myos at-risk
myos why --item 1 --graph
myos close-day --mode hybrid --note "Meeting-heavy coordination day"
```

## Expert Command Catalog

The commands below remain available for scripting, debugging, and precise control. For day-to-day use, prefer the smart surface above.

Common daily commands:

- `myos capture <text> [--kind note|task|commitment|decision|risk] [--due YYYY-MM-DD] [--owner NAME]`
- `myos triage`
- `myos morning [--limit N] [--risk-threshold N]`
- `myos today [--meeting-hours FLOAT]`
- `myos brief [--meeting-hours FLOAT] [--top N] [--risk-threshold N]`
- `myos risk-radar`
- `myos at-risk [--threshold N] [--limit N]`
- `myos waiting-on [--limit N]`
- `myos next-action [--meeting-hours FLOAT] [--risk-threshold N]`
- `myos close-day [--mode maker|hybrid|meeting-heavy|recovery] [--note TEXT]`
- `myos weekly-review [--days N] [--risk-threshold N] [--risk-alert N]`

Ingestion and context:

- `myos sync [--connector all|jira|github|confluence|aha]`
- `myos ingest-external [--limit N] [--min-risk N]`
- `myos transcribe <audio_file> [--text TRANSCRIPT]`
- `myos ingest-image <image_file> [--text OCR_TEXT]`
- `myos watch-dir add <path> [--label TEXT]`
- `myos watch-scan [--limit N]`
- `myos context <query> [--limit N] [--graph]`
- `myos retrieval-run [list|show --id N]`
- `myos claim extract --text TEXT [--source-type TYPE] [--source-id ID]`
- `myos claim list [--source-type TYPE] [--limit N]`
- `myos related --item N [--limit N]`
- `myos why --item N [--graph]`
- `myos reindex`

Assistant and automation:

- `myos chat`
- `myos voice [--text-reply]`
- `myos delegate <objective> [--context TEXT] [--constraint TEXT] [--mode safe|balanced|aggressive]`
- `myos plan create --intent N [--title TEXT] [--assumption TEXT]`
- `myos plan show --id N`
- `myos evidence attach --intent N --retrieval-run N`
- `myos evidence sync-external --intent N [--connector all|jira|github|confluence|aha]`
- `myos review-packet --plan N [--retrieval-run N]`
- `myos agent-run --intent N --role planner|researcher|executor|reviewer|critic|summarizer [--plan N]`
- `myos factory start --intent N [--mode review_first|semi_autonomous|full_autonomous] [--pack intent_execution|daily_ops|software_delivery|connector_ops]`
- `myos factory status --id N`
- `myos factory review --id N`
- `myos factory approve --id N [--execute]`
- `myos factory learn --id N --outcome success|partial|failed [--notes TEXT]`
- `myos factory insights [--intent N] [--pack intent_execution|daily_ops|software_delivery|connector_ops]`
- `myos factory policy set --mode review_first|semi_autonomous|full_autonomous [--scope-type global|intent|goal] [--scope-id ID] [--connector NAME] [--action-type TYPE]`
- `myos act [--task N] [--action N] [--list] [--approve] [--execute]`
- `myos approve [--list] [--action N] [--execute]`
- `myos execution-receipt [list|show --id N]`
- `myos action-provider [--execute]` for explicit connector adapters; without `--execute`, it writes `data/outbox` drafts.
- `myos model recommend|setup|status` for optional tiny local router model setup.
- `myos router eval|feedback|overrides|commands` for privacy-safe router quality, learned exact-match corrections, and command awareness.
- `myos autonomy recommendation-feedback|recommendations` for privacy-safe recommendation usefulness feedback and summaries.
- `myos trace list|cleanup|rollups` for lightweight execution observability with retention budgets.
- `myos autopilot [--env-file PATH] [--once] [--interval-sec N] [--factory]`
- `myos autopilot-status [--limit N]`
- `myos digest [--id N] [--title-only]`
- `myos self-review`

Setup and operations:

- `myos config-init [--path ./.env.myos] [--force]`
- `myos setup-live [--apply] [--check] [--data-dir PATH] [--env-file PATH] [--db-path PATH] [--watch-dir PATH] [--force] [--install-launchd] [--load-launchd] [--autopilot-interval-sec N]`
- `myos doctor [--strict]`
- `myos backup [--output PATH]`
- `myos restore --from PATH`
- `myos migrations [verify|list] [--strict]`
- `myos dependency-check [--strict]`
- `myos performance-baseline [--query TEXT] [--limit N]`
- `myos release-check [--strict] [--verbose]`
- `myos health`
- `myos dashboard [--host 127.0.0.1] [--port 8787] [--report-dir PATH]`
- `myos sanity [--strict] [--report-dir PATH]`
- `myos cleanup [--days N] [--limit N]`
- `myos policy [--set KEY=VALUE]`
- `myos launchd-install [--apply] [--load] [--env-file PATH] [--interval-sec N] [--meeting-hours FLOAT]`
- `myos launchd-uninstall [--apply]`
- `myos launchd-status`

## Agentic Workflows

### Conversational Assistant

```bash
myos chat
myos chat --backend cursor
myos chat --backend claude-code
myos chat --backend claude-code-sdk
myos voice
myos voice --text-reply
myos doctor
```

The assistant can answer from local memory, retrieve relevant context, capture new tasks, and draft external updates for approval. Cursor chat defaults to read-only ask mode; Claude Code CLI and SDK backends are explicit opt-ins and still preserve MYOS approval gates.

### Durable Autonomy Loop

```bash
myos loop start "Handle the blocked launch dependency" --backend cursor
myos loop status
myos loop resume --task 1
myos loop goals
myos loop run-goal --goal 1 --backend cursor
myos loop ledger --goal 1
myos approve --list
```

The loop runs one bounded cycle at a time. It stores durable task state in the existing agent task/run/action tables, executes only safe local actions, links execution traces, and pauses on approval-gated work until you explicitly review it. Goal-driven runs pick one due active goal, start or resume its loop, and skip cleanly when that goal is waiting on approvals. `myos loop goals` prints the next `run-goal` or approval-review command for each eligible goal with stable feedback labels; when no goals are eligible, review standing goals with `myos goal list`. The ledger gives a compact history of why each autonomy decision ran, paused, skipped, or no-oped, and pending approval rows point back to `myos approve --list`; use `myos loop ledger --status waiting_approval` to focus review work.

### Delegation and Approval

```bash
myos delegate "Handle a blocked launch dependency" \
  --context "Need owner confirmation and timeline renegotiation"
myos act --task 1 --list
myos act --action 1 --execute
myos learn --task 1 --outcome success --notes "Owner confirmed reduced scope"
myos coach "blocked launch dependency"
myos agent-status --task 1
```

### Recommendation Feedback

```bash
myos autonomy recommendation-feedback \
  --label inspect_recent_traces \
  --command "myos trace list" \
  --useful yes
myos autonomy recommendations
```

Recommendation feedback is privacy-safe calibration only. MYOS stores labels, command text, usefulness, and note hashes to rank already-deterministic guidance; it never executes a recommendation automatically or weakens approval gates.

Approval handoffs use the stable `review_approvals` label. If `myos loop`, `myos autopilot`, or a ledger row points you to `myos approve --list`, you can submit feedback with `--label review_approvals --command "myos approve --list"`.

Goal scheduler handoffs use `run_goal_cycle` for `myos loop run-goal --goal N` and `review_goals` for `myos goal list`, so scheduler guidance can be calibrated without changing approval gates.

`myos autonomy recommendations` shows these labels as `surface=goal_scheduler` with their command context, compact scores, and advisory side-effect context while keeping raw feedback notes hidden.

### Autopilot

```bash
myos autopilot --env-file ./data/.env.myos --once
myos autopilot --once --loop-goal
myos autopilot --once --loop-goal --loop-goal-id 1
myos autopilot --env-file ./data/.env.myos --interval-sec 900
myos approve --list
myos digest
```

Autopilot runs the pipeline, detects important changes, creates delegated assistant tasks, executes safe local actions, and leaves risky or external actions in the approval queue. The `--loop-goal` option is one-shot only: it routes an explicit autopilot invocation into the goal scheduler, reports the latest ledger row, and stops.

## Privacy and Retention

```bash
myos policy
myos policy --set retention_media_days=45
myos cleanup
```

Useful policy keys include:

- `retention_media_days`
- `retention_evidence_days`
- `retention_conversation_days`
- `redact_emails`
- `redact_phones`
- `redact_secrets`
- `redact_cards`
- `log_conversations`
- `autonomy_level`

## Launchd Auto-Start on macOS

Template plist files are in `deploy/launchd/`.

Before loading them, replace `/path/to/personal-assistant-os` with your local checkout path, then run:

```bash
launchctl unload ~/Library/LaunchAgents/com.myos.sync.plist 2>/dev/null || true
launchctl unload ~/Library/LaunchAgents/com.myos.pulse.plist 2>/dev/null || true
cp deploy/launchd/com.myos.sync.plist ~/Library/LaunchAgents/
cp deploy/launchd/com.myos.pulse.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.myos.sync.plist
launchctl load ~/Library/LaunchAgents/com.myos.pulse.plist
```

The CLI setup commands can also generate and install launchd configuration for a local checkout.

## Testing

```bash
source .venv/bin/activate
python -m unittest discover -s tests -p "test_*.py" -v
```

## License

Licensed under the Apache License, Version 2.0. See `LICENSE`.

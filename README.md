# Personal Assistant OS (MYOS)

[![CI](https://github.com/mkhalid-s/personal-assistant-os/actions/workflows/ci.yml/badge.svg)](https://github.com/mkhalid-s/personal-assistant-os/actions/workflows/ci.yml)

**MYOS is a local-first CLI AI control plane** — a personal engineer that captures your work, retrieves relevant context, plans and proposes actions, and executes only what you explicitly approve. It runs entirely on your machine against a local SQLite database; external services (Jira, GitHub, Confluence, Aha, a hosted reasoning model) are opt-in and skipped safely when unconfigured.

```text
Capture -> Triage -> Plan -> Agent Work -> Review -> Approval -> Execution -> Audit -> Learning
```

Every stage leaves durable, queryable evidence. Every external mutation is approval-gated with payload-hash pinning and TTL expiry — MYOS proposes, you decide, MYOS executes exactly what you approved.

## Why MYOS Exists

Most "AI assistant" tools are either fully autonomous (opaque, risky, hard to trust with real systems) or fully manual (you still do all the work, the model just chats). MYOS is built on a different premise: **an assistant should act like a disciplined team member, not a black box or a toy.**

That means:

- It **remembers** — durable local memory across sessions, not a fresh context window every time.
- It **plans before acting** — every proposed action is reviewable before it touches anything external.
- It **never surprises you** — approval integrity is enforced with a pinned payload hash and a TTL, so an approved action can't silently change or run stale days later.
- It **explains itself** — every retrieved fact has a citation; every executed action has a receipt; every autonomy decision prints its reasoning.
- It **degrades safely** — no configured connector, no problem; no API key, local reasoning fallback; no embedding model installed, hash-based retrieval still works.

## Architecture

MYOS is organized in layers, each with a narrow, well-tested responsibility:

```text
┌──────────────────────────────────────────────────────────────────┐
│ Interfaces          CLI (myos), chat, voice, dashboard            │
├──────────────────────────────────────────────────────────────────┤
│ Autonomy Loop       capture -> triage -> plan -> propose          │
│                     -> approve -> execute -> audit -> learn       │
├──────────────────────────────────────────────────────────────────┤
│ Personas            role-scoped retrieval + action policies       │
│                     (chief-of-staff, researcher, coach,           │
│                      reviewer, operator, engineer)                │
├──────────────────────────────────────────────────────────────────┤
│ Providers           pluggable reasoning backends                  │
│                     (claude, claude-sdk, claude-code, cursor,     │
│                      copilot, zero, command)                      │
├──────────────────────────────────────────────────────────────────┤
│ Retrieval           GraphRAG (graphrag.py) + planner analogies    │
│                     FTS5 candidate selection, entity/graph        │
│                     expansion, embedding-backed reranking         │
├──────────────────────────────────────────────────────────────────┤
│ Execution Safety    approval integrity, payload-hash pinning,     │
│ Core                TTL expiry, destructive-action guards,        │
│                     persona-scoped approval gate                  │
├──────────────────────────────────────────────────────────────────┤
│ Connectors          Jira, GitHub, Confluence, Aha                 │
│                     (read sync; Jira/GitHub/Confluence/Aha        │
│                      write via approval-gated comments)           │
├──────────────────────────────────────────────────────────────────┤
│ Data Layer          SQLite, migration-based schema (44+           │
│                     migrations), FTS5 index, embedding cache,     │
│                     event log, knowledge graph tables             │
└──────────────────────────────────────────────────────────────────┘
```

### Data layer

SQLite is the canonical store (`db.py`). Every schema change is a numbered, idempotent migration applied on connect — there is no separate "migrate" step to forget. The schema holds work items, external items, media metadata, text chunks (FTS5-indexed), an embedding cache, knowledge graph nodes/edges, deterministic entities/aliases/relationships, claims, intents, plans, review packets, conversations, agent tasks/actions/runs, execution receipts, retrieval traces, and an append-only event log.

### Retrieval layer

Two retrieval paths feed different parts of the system:

- **`graphrag.retrieve()`** — the factory/intent path. FTS5 candidate selection (BM25-ranked, OR-token semantics) followed by hybrid lexical+semantic reranking, entity-alias matching, claim scoring, and bounded multi-hop graph expansion over `knowledge_edges`. Every hit carries a citation and, when graph-expanded, an explained relationship path.
- **`planner._agent_analogies()`** — the autonomy-loop path. Scores candidates from `work_items`, `agent_observations`, `intents`, `external_items`, and `people` directly.

Both paths share the same pluggable `EmbeddingBackend` seam (`retrieval.py`). By default this is a deterministic zlib-hash pseudo-embedding — zero dependencies, fully offline, useful for lexical-adjacent recall but not real semantic similarity. Install the `embed` extra (`fastembed`, ONNX-based, no torch) to get real embeddings: they're computed once at write time, persisted in `embedding_cache`, and reused for reranking without recomputing on every query.

### Execution safety core

`execution.py` is the single approve → execute path used by every caller (CLI, chat, voice, autopilot, factory). Every action passes through:

1. **Approval integrity** — a payload hash is pinned at approval time and re-verified at execution time (`myos.approval_integrity.v1`). If the payload changed after approval, execution is refused.
2. **TTL enforcement** — approvals older than `MYOS_APPROVAL_TTL_SECONDS` (default 24h) are refused; you re-approve to run them.
3. **Destructive-action guard** — no autonomy level can run a blocked or destructive action automatically.
4. **Persona guard** — if the owning task declares a persona, the action type must be in that persona's `allowed_actions`, checked *before* approval so a disallowed action is never even approved.
5. **Execution receipt** — every terminal outcome (`executed`, `blocked`, `failed`, `noop`) writes an immutable receipt; failed/blocked receipts spawn a follow-up inbox item so failures never silently disappear.

### Connectors

Four read-sync connectors (Jira, GitHub, Confluence, Aha) share a common retry/backoff/redaction base. Write support (posting comments) is live for all four, routed through the same approval-gated outbox as every other external mutation — nothing bypasses the safety core.

### Providers

Reasoning backends are pluggable via `MYOS_AGENT_BACKEND`: `claude` (Anthropic API), `claude-sdk`, `claude-code` (Claude Code CLI), `cursor`, `copilot`, `zero` (GitLawb Zero coding agent, with a structured stream-JSON executor for the software-delivery factory pack), or `command` (any custom wrapper). No backend is required for local capture, triage, and retrieval — reasoning is only needed for planning, chat, and delegated work.

### Personas

Personas are durable, user-visible manifests (`personas.py`) — six built in (`chief-of-staff`, `researcher`, `coach`, `reviewer`, `operator`, `engineer`), and you can define your own. A persona narrows retrieval scope, reasoning backend preference, and the set of action types a workflow may propose. **A persona can only narrow — it never grants a capability the global policy denies, and it cannot approve or execute an action itself.**

## Installation

```bash
# Standard install
pip install personal-assistant-os

# With real semantic embeddings (fastembed, ONNX — no torch, ~130MB model)
pip install personal-assistant-os[embed]

# With voice input (sounddevice + faster-whisper)
pip install personal-assistant-os[voice]

# Both
pip install personal-assistant-os[embed,voice]
```

Or the one-shot installer (macOS/Linux, installs via pipx and registers a background scheduler):

```bash
curl -fsSL https://raw.githubusercontent.com/mkhalid-s/personal-assistant-os/main/scripts/install.sh | bash
```

Preview without changing anything: append `-s -- --dry-run`.

### Development install

```bash
git clone https://github.com/mkhalid-s/personal-assistant-os
cd personal-assistant-os
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[embed,voice]'
myos doctor
```

## Quick Start

```bash
myos capture "Follow up with platform team about auth token expiry by Friday"
myos do "what should I work on today?"
myos autopilot --once --factory
myos triage
myos approve --list
```

That's capture → routed intent → one autonomy cycle → triage view → review anything awaiting your approval. Nothing external is touched until you approve it.

## Key Commands

| Command | What it does |
|---|---|
| `myos capture <text>` | Record a note, task, commitment, decision, or risk |
| `myos triage` | Review what needs attention right now |
| `myos today` / `myos brief` / `myos morning` | Daily planning surfaces |
| `myos do "<natural language>"` | One-shot router — turns free text into the right command |
| `myos chat` / `myos voice` | Interactive assistant with routed intent + approval-gated actions |
| `myos delegate <objective>` | Hand off a durable task to the autonomy loop |
| `myos loop start "<objective>"` | Start a bounded, resumable autonomy cycle |
| `myos autopilot --once --factory` | One proactive cycle: detect signals, propose, execute safe actions |
| `myos approve --list` | Review everything awaiting your explicit approval |
| `myos act --action N --execute` | Approve and execute a specific proposed action |
| `myos execution-receipt list` | Audit trail of every executed/blocked/failed action |
| `myos rollback --receipt N` | Propose the compensating (inverse) action for a past execution |
| `myos context <query> --graph` | Retrieve relevant memory with graph-expanded citations |
| `myos why --item N --graph` | Explain why an item matters via its relationship graph |
| `myos embed backfill` / `myos embed status` | Compute/inspect semantic embeddings for existing memory |
| `myos persona list` / `myos persona show <name>` | Inspect role-scoped action policies |
| `myos sync --connector all` | Pull from configured Jira/GitHub/Confluence/Aha |
| `myos doctor --strict` | Full local health check |
| `myos backup` / `myos restore --from <path>` | Database backup and restore |

Run `myos help daily`, `myos help workflows`, `myos help expert`, or `myos help diagnostic` for a scoped command list — there are 35+ top-level commands, most of which you'll never need day-to-day.

## Safety Model

**Nothing external happens without your explicit approval.** The full guarantee:

- Local capture and read-only retrieval run directly — no gate needed.
- Any action that would mutate an external system (Jira comment, GitHub comment, etc.) is drafted into an approval queue by default (`MYOS_CONNECTOR_LIVE=0`).
- Approving an action **pins its payload hash**. If the payload is mutated afterward (accidentally or otherwise), execution is refused — you're always executing exactly what you approved.
- Approvals expire after a configurable TTL (default 24h) so a stale approval can't fire on an old, possibly-outdated payload without a fresh review.
- Personas can narrow which action types a workflow is allowed to propose, enforced at both proposal time (autonomy loop) and approval time (`approve_and_execute`) — closing the gap where a manual `myos act` call could bypass persona scoping.
- Every terminal execution outcome writes an immutable receipt; failures and blocks automatically create a follow-up inbox item.
- A structured compensating-action contract (`myos.action.compensation.v1`) lets you propose the inverse of a past execution through the same approval queue — rollback is never a silent, unreviewed operation.
- Privacy filters redact common PII/secret patterns before anything is persisted or indexed.

See `ARCHITECTURE.md` for the full JSON schema contracts (`myos.approval_integrity.v1`, `myos.execution_receipt.v1`, `myos.action.compensation.v1`) that make this auditable end-to-end.

## Configuration

```bash
cp .env.example data/.env.myos
```

Key environment variables (all optional — missing connectors/providers are skipped safely):

```bash
# Connectors
JIRA_BASE_URL / JIRA_USER_EMAIL / JIRA_API_TOKEN
GITHUB_TOKEN / GITHUB_OWNER / GITHUB_REPO
CONFLUENCE_BASE_URL / CONFLUENCE_USER_EMAIL / CONFLUENCE_API_TOKEN
AHA_BASE_URL / AHA_API_KEY

# Reasoning backend
MYOS_AGENT_BACKEND=claude   # claude|claude-sdk|claude-code|cursor|zero|copilot|command

# Live connector mutations — keep 0 for dry-run outbox behavior
MYOS_CONNECTOR_LIVE=0

# Approval TTL (seconds)
MYOS_APPROVAL_TTL_SECONDS=86400
```

See the full list in `.env.example`.

## Development

```bash
source .venv/bin/activate
PYTHONPATH=src python -m unittest discover -s tests -p "test_*.py" -v
```

558 tests across the suite. `db.py`, `command_registry.py`, `privacy.py`, `approval_context.py`, `autonomy.py`, `inbox.py`, `agentcore.py`, `zero_executor.py`, and `execution.py` are held to `mypy --strict` in CI — these are the modules where a type regression could silently corrupt state or mis-classify an approval.

```bash
ruff check src/personal_assistant tests
ruff format --check src/personal_assistant tests
mypy --strict src/personal_assistant/execution.py   # + the other safety-critical modules
```

## Roadmap

**Done:**
- Core SQLite schema, migrations, backup/restore, and CI release gates
- Approval/execution safety core with hash pinning, TTL, and persona-scoped guards
- Live write adapters for all four connectors (Jira, GitHub, Confluence, Aha)
- FTS5-backed retrieval candidate selection (`graphrag._direct_hits`)
- Pluggable embedding backend seam with a real `fastembed` implementation, persisted embedding cache, write-time embedding hooks, and semantic reranking in both retrieval paths
- Compensating-action (rollback) contract and CLI

**Next — the learning loop:**
- Outcome feedback written on every execution receipt, feeding a pattern detector for recurring failure modes
- Self-adjusting persona `allowed_actions` and goal priority based on observed outcomes
- Semantic recall of past decisions injected into the planning prompt

**Deferred:**
- Graph database backend (Kuzu/DuckDB) for relationship traversal beyond the current SQLite-first graph tables
- Standalone binary packaging
- Multi-machine sync

See `ARCHITECTURE.md` and `ROADMAP.md` for the detailed staged plan, and `docs/BOUNDED_AUTONOMY.md` for the safety-hardening history.

## License

Licensed under the Apache License, Version 2.0. See `LICENSE`.

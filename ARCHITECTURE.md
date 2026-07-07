# Architecture

Personal Assistant OS is intended to become a local-first AI control plane: a system that turns intent into planned, reviewable, approval-gated work while preserving context, provenance, and auditability.

The current implementation is an MVP. It has a local SQLite store, CLI workflows, reliability checks, connector ingestion, durable intents/plans/review packets, lightweight graph tables, privacy filters, retrieval traces, local agent-role runs, and approval queues. It is not yet a full GraphRAG system, graph database application, or production-grade software factory.

## Operating Loop

The target operating loop is:

```text
Intent -> Context -> Plan -> Agent Work -> Review -> Approval -> Execution -> Audit -> Learning
```

Each stage should leave durable evidence:

- `Intent`: what outcome the user wants, why it matters, and what constraints apply.
- `Context`: notes, conversations, files, tickets, PRs, meetings, decisions, risks, and people related to the intent.
- `Plan`: proposed steps, dependencies, assumptions, risks, and validation checks.
- `Agent Work`: local reasoning, retrieval, drafting, code or document changes, and external-action proposals.
- `Review`: human-readable diff, rationale, sources, confidence, and rollback notes.
- `Approval`: explicit user authorization for any external mutation or risky local action.
- `Execution`: actual action execution through a constrained provider.
- `Audit`: immutable event trail of what happened, what evidence was used, and who approved it.
- `Learning`: outcome feedback that updates future retrieval, planning, and prioritization.

## Current Components

### CLI Surface

`myos` is the main interface. It supports capture, triage, sync, retrieval, daily planning, durable plans, review packets, agent-role runs, risk scans, assistant delegation, approval queues, autopilot, reports, policies, backup/restore, migration verification, and local launchd setup.

### Local Store

SQLite is the canonical store. It holds work items, external items, media metadata, text chunks, graph tables, claims, intents, plans, review packets, conversations, observations, insights, policies, agent tasks, proposed actions, execution receipts, retrieval traces, and event logs.

### Ingestion

Current ingestion sources include manual capture, connector sync, audio transcript text, image OCR text, watched folders, and assistant conversations.

### Retrieval

Retrieval currently combines:

- SQLite FTS-indexed text chunks.
- Lexical scoring.
- Deterministic hash-based pseudo-embeddings.
- Lightweight graph relationships in `knowledge_nodes` and `knowledge_edges`.

This is useful for a local MVP, but it is not equivalent to production embedding retrieval, vector search, reranking, or GraphRAG.

### Agent Control Plane

Agent outputs flow through proposed actions and approval gates. The policy layer classifies actions as safe, confirm-required, or blocked. External mutations are drafted and require approval before execution.

### Audit and Privacy

Events, proposed actions, execution receipts, provider calls, conversation turns, and context observations are persisted. Failed or blocked execution receipts create follow-up inbox items so failures do not disappear. Privacy filters redact common PII and secret patterns before data is stored or indexed.

## Target Architecture

The next stable architecture should make these layers explicit:

```text
Interfaces
  CLI, voice, dashboard, future API

Intent Layer
  goals, intents, outcomes, constraints, success criteria

Knowledge Layer
  documents, meetings, tickets, PRs, people, projects, decisions, risks

Graph Layer
  typed entities, typed relationships, graph traversal, explanations

Retrieval Layer
  FTS, embeddings, vector index, graph expansion, reranking, citations

Agent Layer
  planner, researcher, executor, reviewer, critic, summarizer

Control Layer
  policy, approvals, destructive guards, execution providers

Audit Layer
  event log, provenance, retrieved evidence, approvals, outcomes
```

## GraphRAG Direction

GraphRAG should be added deliberately, not implied by the current graph tables.

### What Exists Now

- `knowledge_nodes` and `knowledge_edges` tables in SQLite.
- `entities` and `entity_aliases` tables populated by conservative deterministic extraction.
- `relationships` table populated by conservative deterministic typed edge extraction.
- `claims` table for deterministic fact storage.
- Manual links between work items.
- Conversation-derived co-mention edges.
- `related`, `context`, and `why` commands that expose some relationship context.
- `retrieval_runs` and `retrieval_run_sources` tables that persist selected sources, scores, citations, and graph paths for graph-backed retrieval.
- `retrieval-run` CLI inspection for persisted retrieval traces.
- A SQLite-first retrieval trace contract in `graphrag.py` surfaced through `myos context --graph`, `myos why --graph`, chat logging, and review packets, returning cited chunks and relationship explanations.
- Entity-aware retrieval expansion from matched aliases through typed relationships.
- Fixture-based GraphRAG eval cases for common assistant questions such as blockers, mitigation, and approval evidence.

### What Is Missing

- Robust entity canonicalization beyond conservative identifier and labeled-name extraction.
- Broader typed relationship extraction from documents, conversations, tickets, and PRs.
- Real embeddings and vector search.
- Stronger reranking and citation quality checks.
- Graph summaries or community-level memory.
- A graph database backend or embedded graph engine.

### Backend Options

- SQLite-first: simplest and keeps the local-first footprint small. Good for early GraphRAG.
- Kuzu: embedded graph database, good fit for local-first graph traversal without a server.
- Neo4j: mature graph database, better for larger deployments, but adds server operations.

The recommended path is SQLite-first GraphRAG primitives, then optional Kuzu when graph traversal becomes a bottleneck or a core product feature.

## Action-Lifecycle Contract

Every external mutation MYOS performs travels the same path:

```text
proposed -> approved (payload hash pinned + TTL started)
        -> executed / blocked / failed (execution receipt written)
        -> [P2] compensating action drafted for rollback (approval-gated)
```

Each step is observable through a machine-readable JSON envelope printed by
the corresponding `--json` CLI surface. The schemas below are stable — the
`schema` string is the contract, and a rename becomes a build failure via
the `JsonEnvelopeSurfaceTest` regression suite. Fields marked *optional*
may be omitted when unpopulated; fields marked *stable* must appear on
every payload in that schema.

### `myos.approval_integrity.v1`

Pinned at approval time, verified at execution time, and surfaced beside
every approval-queue entry and every execution receipt. It answers
*"is this approved payload still the same one I approved, and is it still
fresh?"* — the two questions that separate a legitimate execution from a
race, tamper, or stale re-run.

| field | type | stable | description |
| --- | --- | --- | --- |
| `schema` | `"myos.approval_integrity.v1"` | yes | Contract discriminator. |
| `ok` | bool | yes | `true` if payload hash still matches and TTL not exceeded. |
| `reason` | string | yes | Empty when `ok=true`; `"payload_hash_mismatch"` or `"approval_ttl_exceeded"` otherwise. |
| `payload_hash_verified` | bool | yes | Whether the pinned hash re-hashed to the same value. |
| `approved_age_seconds` | int | optional | Seconds since approval; omitted for rows without `approved_at`. |
| `approval_ttl_seconds` | int | optional | Configured TTL (env `MYOS_APPROVAL_TTL_SECONDS`, default 86400). |
| `ttl_remaining_seconds` | int \| null | optional | Seconds left before expiry, `null` when TTL disabled, `0` when expired. |

Emitted in-place by `verify_approval_integrity(row)` and embedded under
`approval_integrity` in every execution receipt written by
`_record_execution_receipt`. `myos approve --list --json` and
`myos execution-receipt list/show --json` both surface it verbatim.

**Operator-facing derived view** — `myos.approval_integrity_view.v1` —
maps that raw envelope to one of six states (`not_yet_approved`, `fresh`,
`nearing_expiry` for the last 10% of the TTL window, `expired`,
`tampered`, `invalid`) so supervisors can trigger re-approval prompts
without reimplementing the timing math.

### `myos.execution_receipt.show.v1` / `myos.execution_receipt.list.v1`

Written on every terminal execution outcome (`executed`, `blocked`,
`failed`, `noop`) — the durable audit record of "MYOS proposed X, the
operator approved it (or the policy classified it as safe), and here is
exactly what happened." The show envelope is the strict superset; list
entries drop presentation-only fields (`title`, `result`, `rollback_note`,
`outbox`) to keep row sizes bounded for regex-free UI paging.

| field | type | stable | description |
| --- | --- | --- | --- |
| `schema` | `"myos.execution_receipt.show.v1"` \| `"myos.execution_receipt.list.v1"` | yes | Contract discriminator. |
| `id` | int | yes | Receipt row id in `action_execution_receipts`. |
| `agent_action_id` | int \| null | yes | The `agent_actions` row this receipt closes out. |
| `action_type` | string | yes | One of the registered action types (e.g. `local_note`, `jira_comment`). |
| `final_status` | string | yes | Terminal status: `executed`, `blocked`, `failed`, or `noop`. |
| `approved` | bool | yes | Whether the row was approved (vs safe-list auto-run). |
| `follow_up_required` | bool | yes | `true` for `failed` and `blocked`; drives inbox follow-up creation. |
| `follow_up_inbox_id` | int \| null | yes | The `inbox_items` row automatically created when `follow_up_required=true`. |
| `created_at` | string | yes | ISO 8601 UTC timestamp of the receipt insertion. |
| `approval_integrity` | `myos.approval_integrity.v1` \| null | yes | Verbatim integrity envelope pinned at execution time. |
| `verification` | `myos.verification_receipt.v1` \| null | yes | Suggested operator-side verification commands; MYOS never auto-runs them. |
| `approval_context` | object \| null | yes | Compact review context recorded at approval (side-effect class, dry-run flag, connector target). |
| `outbox` | object \| null | show-only | The `action_outbox` row, if the action drafted a connector mutation. |
| `title` | string | show-only | Human-readable title of the closed-out action. |
| `result` | string | show-only | Provider result string (privacy-filtered). |
| `rollback_note` | string | show-only | Operator-drafted rollback instructions from the payload. |

`myos.verification_receipt.v1` carries `{schema, status="not_run", reason,
commands: [str, ...]}` and is intentionally never auto-executed: MYOS
records the operator's suggested verification commands but leaves the
choice to run them where it belongs — with the operator.

### `myos.action.compensation.v1` *(P2 target schema)*

Every action that mutates external state should also record its inverse
operation as JSON at the time of execution, so a later
`myos rollback --receipt N` can propose that inverse through the exact
same approval queue that authorized the original action. The schema
below is the target contract; the P2.1 slice adds the
`compensating_action_json` column to `action_execution_receipts`, the
adapter hooks that populate it, and the `myos rollback` command that
proposes it.

| field | type | stable | description |
| --- | --- | --- | --- |
| `schema` | `"myos.action.compensation.v1"` | yes | Contract discriminator. |
| `strategy` | string | yes | One of `delete_on_create`, `close_on_open`, `revert_on_update`, or `no_op`. |
| `action_type` | string | yes | Target action type the compensating proposal will be filed as (e.g. `jira_comment_delete`). |
| `payload` | object | yes | Full privacy-filtered payload the compensating action should carry (identical shape to a normal `agent_actions.payload_json`). |
| `target` | object | yes | `{provider, target_type, target_ref}` for the external resource the compensation touches. |
| `preconditions` | array\<string\> | optional | Human-readable checks the operator should confirm before approving the rollback (e.g. "comment still exists"). |
| `rollback_note` | string | optional | Free-form note explaining what the compensation will undo and any residual side effects. |
| `dry_run_supported` | bool | optional | Whether the compensating adapter can render a preview without touching the external system. |

`no_op` explicitly documents actions whose side effects cannot be
reversed by MYOS (e.g. an email that has already left the outbox). The
receipt still records `myos.action.compensation.v1` with
`strategy="no_op"` so operators are not silently left without a rollback
signal — a follow-up inbox item still fires so the human loop knows the
action is one-way.

### Consumer contract

Downstream tooling — supervisor scripts, dashboards, external monitors —
can rely on the schemas above with these guarantees:

- The `schema` string never changes for a given contract version;
  breaking changes bump the version suffix (e.g. `.v2`).
- Fields marked *stable* always appear on every payload in that schema,
  even when the field's value is `null` or `""`.
- `--json` on any surface that produces one of these envelopes always
  writes to `stdout`; on the exit-1 error path it writes a schema-stable
  error envelope (same `schema` string, `error` field populated) so
  callers never see a bare traceback.
- The `JsonEnvelopeSurfaceTest` locks the `schema` strings and the
  tripwire fields, so any accidental rename is caught in CI before it
  reaches downstream consumers.

## Stability Principles

- The assistant must be useful without external services.
- Every external mutation must be approval-gated.
- Every generated answer should be explainable from retrieved evidence.
- Every workflow should be repeatable from a clean install.
- Every persisted artifact should have retention and privacy behavior.
- Every release should pass tests, scans, and setup validation.

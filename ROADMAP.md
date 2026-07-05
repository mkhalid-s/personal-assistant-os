# Roadmap

This roadmap turns the current MVP into a stable, repeatable local-first assistant OS. It is intentionally staged so the project does not overclaim GraphRAG, production stability, or enterprise-grade control-plane behavior before those pieces exist.

## Current State

The public repository currently provides:

- Local SQLite-backed CLI assistant workflows.
- Capture, triage, retrieval, sync, reports, policies, and approval queues.
- Optional connectors for Jira, GitHub, Confluence, and Aha.
- Conversation logging, privacy redaction, and lightweight context observations.
- First-class intents, plans, review packets, retrieval evidence attachment, local agent-role runs, execution receipts, and daily operating loops.
- SQLite-first graph tables, deterministic entity/relationship/claim extraction, entity-aware retrieval expansion, and persisted retrieval traces.
- Backup/restore, migration verification, dependency checks, performance baselines, CI, and tag release validation.
- Tests covering CLI flows, autonomy policy, context, redaction, connectors, GraphRAG primitives, and remediation behavior.

It does not yet provide:

- Full production GraphRAG with real embeddings, stronger reranking, and graph summaries.
- A graph database backend.
- Production embeddings or vector search.
- A complete autonomous execution workflow with external mutation receipts and mature rollback automation.

## Phase 0: Public Baseline

Goal: keep the public repo clean and honest.

- Maintain Apache 2.0 license and public README.
- Keep `.env`, databases, logs, local agent settings, and generated reports ignored.
- Keep current history free of company email and co-author trailers.
- Document current limitations clearly.
- Maintain CI for tests and public-readiness scans.

Exit criteria:

- Fresh clone can install and run tests.
- README does not imply GraphRAG or production stability.
- Pushes are validated by automated checks.

## Phase 1: Stable Local Application

Goal: make the current app repeatable from a clean checkout.

Work items:

- Add a `myos doctor --strict` checklist for Python version, package install, DB path, permissions, optional tools, and configured connectors.
- Add a sample `.env.example` with safe placeholders.
- Add a deterministic demo workflow using only local data.
- Add CI with unit tests, diff whitespace checks, and public hygiene scans.
- Add dependency license checks.
- Add migration backup guidance and recovery commands.
- Add structured error messages for connector/auth/setup failures.

Exit criteria:

- A new user can clone, install, run demo commands, and understand failures without reading source code.
- Tests run in CI on every push.
- The app is useful offline with no external connectors.

## Phase 2: Intent Layer

Goal: make user intent a first-class object instead of just free-text tasks.

New concepts:

- `intents`: outcome, context, constraints, success criteria, priority, owner, status.
- `plans`: steps, assumptions, dependencies, risks, validation gates.
- `evidence`: notes, files, tickets, PRs, meetings, decisions, and retrieved context tied to an intent.
- `decisions`: durable choices with rationale and supersession history.
- `risks`: impact, likelihood, mitigation, owner, deadline.

Implemented command shape:

```bash
myos intent create "Ship customer escalation dashboard by Friday" \
  --constraint "No external mutation without approval" \
  --success "Dashboard passes smoke test and owner signs off"
myos intent list
myos intent show --id 1
myos plan create --intent 1
myos evidence attach --intent 1 --retrieval-run 1
myos review-packet --plan 1 --retrieval-run 1
```

Exit criteria:

- Every meaningful workflow can be tied to an intent.
- Plans and actions can cite the evidence behind them.
- Intent status can be audited from creation through outcome.

## Phase 3: SQLite-First GraphRAG

Goal: add real GraphRAG primitives without introducing a graph server too early.

Data model additions:

- `entities`: canonical people, projects, systems, documents, tickets, PRs, decisions, risks, requirements, APIs, services. Initial deterministic extraction exists for high-confidence identifiers and labeled names.
- `entity_aliases`: alternate names and IDs. Initial SQLite persistence exists.
- `relationships`: typed edges with source, confidence, timestamps, and provenance. Initial deterministic extraction exists for explicit relationship phrases.
- `claims`: extracted facts with source and confidence. Initial deterministic storage exists.
- `retrieval_runs`: query, selected sources, graph expansion, rerank scores, and final citations. Initial SQLite persistence exists for GraphRAG CLI surfaces.

Retrieval pipeline:

```text
Query
  -> identify entities
  -> lexical/vector candidate retrieval
  -> graph neighborhood expansion
  -> rerank by relevance, recency, authority, and graph distance
  -> return answer with citations and relationship explanation
```

Work items:

- Add SQLite-first retrieval trace tests before adding graph storage.
- Extend deterministic entity extraction, then add provider-assisted extraction behind approval/policy.
- Extend typed edge extraction across notes, conversations, tickets, and PRs.
- Extend graph-backed why explanations beyond work items into plans, reviews, and assistant answers.
- Extend retrieval traces from GraphRAG CLI surfaces and inspection into assistant answers and review packets. Initial persistence exists.
- Expand retrieval eval fixtures beyond the initial blocker, mitigation, and approval-evidence cases.

Exit criteria:

- Answers can cite source chunks and explain relationship paths.
- Graph traversal improves retrieval quality in tests.
- The system still runs locally with SQLite only.

## Phase 4: Real Embeddings and Optional Graph Backend

Goal: improve retrieval quality while keeping local-first defaults.

Options:

- Local embeddings by default where practical.
- Provider embeddings as an opt-in backend.
- SQLite vector extension or local vector index for small deployments.
- Kuzu as the first optional embedded graph database backend.
- Neo4j only for larger/server deployments.

Exit criteria:

- Retrieval quality is measurable with evals.
- Backend choice is explicit in config.
- SQLite remains supported as the baseline.

## Phase 5: Agent Control Plane

Goal: make agentic execution more repeatable, reviewable, and safe.

Work items:

- Split agent roles: planner, researcher, executor, reviewer, critic, summarizer.
- Require review packets before execution: intent, plan, evidence, risks, actions, rollback notes.
- Add an external coding executor backend that can delegate software engineering tasks to `zero exec` while MYOS owns intent, memory, approvals, review packets, and audit.
- Add policy gates per action type and per connector.
- Add execution receipts for every external mutation.
- Add outcome learning tied back to intents and plans.

Exit criteria:

- Agents can work on bounded tasks without bypassing policy.
- Every external action has approval and execution evidence.
- Failed actions create follow-up work instead of disappearing.

### Zero Coding Executor Integration

Goal: reuse `Gitlawb/zero` as a high-quality coding worker for repository-scoped tasks without replacing MYOS's control plane. MYOS should decide what work is eligible, gather local context, start the run, consume structured events, create review artifacts, and keep acceptance/merge/commit/external mutation approval-gated.

Research notes:

- `zero exec` already exposes the right subprocess boundary: `--cwd`, `--worktree`, `--worktree-dir`, `--input-format stream-json`, `--output-format stream-json`, `--max-turns`, `--auto`, `--self-correct`, `--use-spec`, `--enabled-tools`, `--disabled-tools`, `--resume`, and `--fork`.
- Zero stream JSON uses schema version `2` and emits run lifecycle, reasoning/text, tool calls, permission requests/decisions, tool results, usage, final output, warnings, errors, and run-end events.
- Zero worktrees are detached git worktrees created outside the source repo by default, with optional naming and base directory controls.
- Zero has distinct exit codes for success, usage error, provider error, incomplete work, and interrupted runs. MYOS should persist those as execution receipts instead of flattening them into text.
- Zero can list visible tools without constructing a provider, so MYOS can preflight installed Zero capabilities before launching a live model run.

Initial command shape:

```bash
myos code "Fix the failing tests" --repo /path/to/repo --backend zero --worktree
myos factory start --pack software_delivery --executor zero
```

Adapter contract:

| MYOS Responsibility | Zero Responsibility |
| --- | --- |
| Route the user request as a coding task and bind it to an intent, plan, or factory run. | Inspect and edit the target repository using its coding-agent tool loop. |
| Retrieve relevant local memory, decisions, risks, tickets, and prior review packets. | Use repo-local context, tools, model/provider configuration, sandbox policy, and optional self-correction. |
| Build a bounded prompt with success criteria, constraints, validation commands, and approval policy. | Emit stream JSON events and leave code changes uncommitted for MYOS review. |
| Parse stream JSON into traces, observations, changed-file summaries, usage rows, and execution receipts. | Report final answer, changed files, tool results, permissions, warnings, and terminal status. |
| Create MYOS review packets and require explicit approval before merge, commit, PR, or external connector mutation. | Optionally run in an isolated worktree and execute safe local verification commands under its own policy. |

Event mapping:

- `run_start`: create or link a MYOS `agent_runs` row with Zero version, provider, model, run ID, session ID, repo path, worktree path, and correlation ID.
- `tool_call` and `tool_result`: append trace events; persist `changedFiles`, status, truncated/redacted flags, and compact outputs when privacy filters allow.
- `permission_request` and `permission_decision`: mirror into MYOS autonomy traces and approval context so double-permission decisions are visible.
- `usage`: store token/cost metadata when present.
- `final`: attach the final summary to the agent run and seed the review packet.
- `error` and `run_end`: write an execution receipt with exit code, terminal status, recoverability, and follow-up inbox item when blocked, incomplete, or failed.

Guardrails:

- Prefer subprocess integration over vendoring Zero code for the first implementation.
- Pin or record the Zero binary version and stream JSON schema version for every run.
- Default to `--auto low`; allow `--auto medium` only when MYOS policy allows semi-autonomous local coding work.
- Do not pass `--skip-permissions-unsafe` from MYOS.
- Prefer `--worktree` for non-trivial edits; keep commits, PR creation, merge, and external mutations in MYOS approval flow.
- Store raw Zero output only behind an explicit debug flag; default persistence should be redacted summaries, event metadata, changed files, and receipts.
- Treat Zero permission grants as executor-local only. They do not approve MYOS-level external actions.

Implementation batches:

1. Discovery and preflight: add `myos doctor` checks for `zero`, `zero exec --help`, stream JSON support, and provider readiness guidance.
2. Minimal adapter: run `zero exec --cwd <repo> --output-format stream-json` from MYOS, parse events, and persist a read-only trace plus final summary.
3. Worktree mode: add `--worktree`, capture worktree path, changed files, terminal status, and suggested verification commands.
4. Review packet integration: attach Zero final output, changed files, tool summaries, verification status, risks, rollback notes, and approval instructions.
5. Factory integration: allow the `software_delivery` workflow pack to use Zero for the executor role while planner/reviewer/critic remain MYOS-owned.
6. Eval coverage: add offline fixtures for stream JSON parsing, failed/incomplete runs, permission events, worktree path handling, and review packet generation.

Acceptance criteria:

- A coding task can run through Zero from MYOS and produce a durable MYOS review packet without committing changes.
- Failed, incomplete, blocked, and interrupted Zero runs create clear receipts and follow-up work.
- MYOS can re-run or resume a task with a recorded Zero session/worktree reference.
- No external connector mutation, commit, PR, or merge happens without MYOS approval.
- The integration remains optional: MYOS still works when Zero is not installed.

## Phase 6: Product Hardening

Goal: make the OS dependable for daily use.

Work items:

- Add dashboard views for intents, risks, approvals, graph context, and audit trails.
- Add backup/restore and migration verification.
- Add release notes and versioned migrations.
- Add structured logging and health reports.
- Add security review checklist.
- Add packaging/release workflow.

Exit criteria:

- A user can run it daily without hand-editing internals.
- Data can be backed up, restored, and migrated safely.
- Releases are reproducible.

## External Inspiration Triage

The most relevant inspiration from `Gitlawb/zero` and `agent0ai/agent-zero` should be borrowed as product patterns, contracts, and safety practices rather than copied wholesale. Any direct code reuse must preserve upstream license notices and fit MYOS's Apache-2.0 public baseline.

| Priority | Source | Borrow | Reimplement In MYOS | Defer |
| --- | --- | --- | --- | --- |
| P0 | `Gitlawb/zero` | Schema-versioned headless run protocol with JSONL events for run lifecycle, text, tool calls, permission requests, usage, final output, and errors. | Add `myos do --output-format stream-json` and extend it to include retrieval sources, autonomy decisions, proposed actions, approvals, execution receipts, and trace IDs. | Full editor/CI ecosystem integration until the local CLI contract is stable. |
| P0 | `Gitlawb/zero` | Mature coding-agent execution through `zero exec`, including repository tools, worktree mode, tool filters, self-correction, and machine-readable terminal status. | Add an optional Zero coding executor backend where MYOS owns task routing, retrieved context, review packets, approvals, and audit while Zero performs bounded repo edits. | Vendoring Zero internals or letting Zero commits/PRs bypass MYOS approval. |
| P0 | `Gitlawb/zero` | Explicit permission and sandbox UX: visible side effects, write-root boundaries, network/destructive action gates, and structured denial reasons. | Keep MYOS policy-first, but make approval decisions and blocked-action reasons more machine-readable across CLI, trace, and review packet surfaces. | Broad filesystem sandboxing unless MYOS starts executing arbitrary shell work directly. |
| P0 | `Gitlawb/zero` | Offline agent eval methodology with fixtures, expected/forbidden changed files, verification commands, trace-event checks, and model metadata. | Expand MYOS evals beyond retrieval and autonomy fixtures into repeatable agent-workflow suites for planning, review packets, approvals, and self-correction behavior. | Public pass-rate claims until task suites and model stamps are reproducible. |
| P1 | `Gitlawb/zero` | Specialist manifests with scoped tools and project/user precedence. | Add markdown role manifests for planner, researcher, reviewer, critic, summarizer, and connector-specific reviewers, all bounded by existing approval policy. | Nested specialist spawning and autonomous specialist creation until the control layer is mature. |
| P1 | `agent0ai/agent-zero` | Project isolation model: instructions, workspace, memory, variables, secrets, and model presets scoped to a project. | Add `myos project` records that bind goals, memory, connector config aliases, local paths, instructions, and default agent backend without leaking context globally. | Multi-tenant workspace management and hosted project administration. |
| P1 | `agent0ai/agent-zero` | Memory curation practices: searchable memories, clear provenance, edit/delete flows, and guidance for stale or harmful memories. | Add memory inspection and cleanup commands for observations, insights, claims, conversation summaries, and retrieval traces with privacy-safe previews. | Rich dashboard editing until the CLI curation loop is useful. |
| P2 | `Gitlawb/zero` | Spec-first and isolated worktree workflows for risky code changes. | Add optional worktree-backed factory runs that produce review packets and verification receipts before any merge or external mutation. | Automatic branch creation, commits, or PRs without explicit user approval. |
| P2 | `Gitlawb/zero` | Provider/model registry, setup wizard, health checks, and capability metadata. | Replace scattered provider setup guidance with `myos providers`, `myos models`, and stricter `doctor` checks for configured backends and local model fallbacks. | Supporting dozens of providers before core provider abstractions are stable. |
| P2 | `agent0ai/agent-zero` | Skills, profiles, plugins, and hooks as user-extensible behavior. | Start with small local markdown skills and lifecycle hooks that are visible in traces and governed by policy. | Plugin hub, Web UI plugin runtime, and arbitrary extension loading. |
| P3 | `agent0ai/agent-zero` | Human-observable workbench concepts: live activity, intervention points, recoverable snapshots, and project-level time travel. | Translate into CLI-first audit views, rollback notes, backup/restore, and review packet diffs. | Docker desktop, browser canvas, LibreOffice integration, and full GUI coworking. |

## Non-Goals For Now

- Do not claim enterprise-grade regulated SDLC coverage.
- Do not require a hosted graph database.
- Do not auto-execute external mutations without approval.
- Do not optimize for multi-tenant SaaS before the local OS is stable.
- Do not market the current graph tables as GraphRAG.

## Near-Term Surgical Plan

The next implementation batch should be small:

1. Add `.env.example` and a local-only demo script.
2. Add CI for tests and scans.
3. Add `intents` and `intent_evidence` tables with CLI create/list/show commands.
4. Add `ARCHITECTURE.md` and keep README current-state honest.
5. Add GraphRAG design tests before adding new graph storage.

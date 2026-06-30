# Roadmap

This roadmap turns the current MVP into a stable, repeatable local-first assistant OS. It is intentionally staged so the project does not overclaim GraphRAG, production stability, or enterprise-grade control-plane behavior before those pieces exist.

## Current State

The public repository currently provides:

- Local SQLite-backed CLI assistant workflows.
- Capture, triage, retrieval, sync, reports, policies, and approval queues.
- Optional connectors for Jira, GitHub, Confluence, and Aha.
- Conversation logging, privacy redaction, and lightweight context observations.
- Basic graph tables and relationship commands.
- Tests covering CLI flows, autonomy policy, context, redaction, connectors, and remediation behavior.

It does not yet provide:

- Real GraphRAG.
- A graph database backend.
- Production embeddings or vector search.
- CI/CD and release automation.
- A fully repeatable first-run setup story.
- A complete intent-to-execution workflow model.

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

Candidate commands:

```bash
myos intent create "Ship customer escalation dashboard by Friday" \
  --constraint "No external mutation without approval" \
  --success "Dashboard passes smoke test and owner signs off"
myos intent list
myos intent show --id 1
myos plan --intent 1
myos evidence add --intent 1 --text "Customer needs daily status visibility"
myos review-plan --intent 1
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
- `claims`: extracted facts with source spans and confidence.
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
- Extend retrieval traces from GraphRAG CLI surfaces and inspection into assistant answers and review packets.
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
- Add policy gates per action type and per connector.
- Add execution receipts for every external mutation.
- Add outcome learning tied back to intents and plans.

Exit criteria:

- Agents can work on bounded tasks without bypassing policy.
- Every external action has approval and execution evidence.
- Failed actions create follow-up work instead of disappearing.

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

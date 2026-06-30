# Architecture

Personal Assistant OS is intended to become a local-first AI control plane: a system that turns intent into planned, reviewable, approval-gated work while preserving context, provenance, and auditability.

The current implementation is an MVP. It has a local SQLite store, CLI workflows, connector ingestion, lightweight graph tables, privacy filters, retrieval helpers, and approval queues. It is not yet a full GraphRAG system, graph database application, or production-grade software factory.

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

`myos` is the main interface. It supports capture, triage, sync, retrieval, daily planning, risk scans, assistant delegation, approval queues, autopilot, reports, policies, and local launchd setup.

### Local Store

SQLite is the canonical store. It holds work items, external items, media metadata, text chunks, graph tables, conversations, observations, insights, policies, agent tasks, proposed actions, and event logs.

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

Events, proposed actions, provider calls, conversation turns, and context observations are persisted. Privacy filters redact common PII and secret patterns before data is stored or indexed.

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
- Manual links between work items.
- Conversation-derived co-mention edges.
- `related`, `context`, and `why` commands that expose some relationship context.
- `retrieval_runs` and `retrieval_run_sources` tables that persist selected sources, scores, citations, and graph paths for graph-backed retrieval.
- A SQLite-first retrieval trace contract in `graphrag.py` surfaced through `myos context --graph` and `myos why --graph`, returning cited chunks and one-hop relationship explanations.
- Fixture-based GraphRAG eval cases for common assistant questions such as blockers, mitigation, and approval evidence.

### What Is Missing

- Robust entity canonicalization beyond conservative identifier and labeled-name extraction.
- Broader typed relationship extraction from documents, conversations, tickets, and PRs.
- Entity-aware retrieval that expands from canonical entities to broader neighborhoods.
- Real embeddings and vector search.
- Reranking and citation quality checks.
- Graph summaries or community-level memory.
- A graph database backend or embedded graph engine.

### Backend Options

- SQLite-first: simplest and keeps the local-first footprint small. Good for early GraphRAG.
- Kuzu: embedded graph database, good fit for local-first graph traversal without a server.
- Neo4j: mature graph database, better for larger deployments, but adds server operations.

The recommended path is SQLite-first GraphRAG primitives, then optional Kuzu when graph traversal becomes a bottleneck or a core product feature.

## Stability Principles

- The assistant must be useful without external services.
- Every external mutation must be approval-gated.
- Every generated answer should be explainable from retrieved evidence.
- Every workflow should be repeatable from a clean install.
- Every persisted artifact should have retention and privacy behavior.
- Every release should pass tests, scans, and setup validation.

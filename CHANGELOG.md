# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

### Added

- Local reliability kernel: backup, restore, migration verification, release readiness checks, dependency checks, and performance baselines.
- First-class intents, durable plans, review packets, retrieval evidence attachment, and execution receipts.
- SQLite-first GraphRAG depth: deterministic entities, relationships, claims, entity-aware retrieval expansion, retrieval traces, and graph eval coverage.
- Local agent-role control plane for planner, researcher, executor, reviewer, critic, and summarizer runs.
- Daily operating loops for morning briefs, close-day summaries, weekly review signals, and connector evidence mapping.
- Release hardening docs, recovery notes, CI release-readiness gate, and tag/manual release workflow validation.

### Changed

- External CLI execution now uses a generic command backend rather than a tool-specific provider surface.
- Failed or blocked execution receipts create and retain links to follow-up inbox work.

### Validation

- Full unit suite passes locally.
- `myos release-check --strict` passes locally.

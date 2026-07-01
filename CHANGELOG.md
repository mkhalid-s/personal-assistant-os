# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

### Added

- Local reliability kernel: backup, restore, migration verification, release readiness checks, dependency checks, and performance baselines.
- First-class intents, durable plans, review packets, retrieval evidence attachment, and execution receipts.
- SQLite-first GraphRAG depth: deterministic entities, relationships, claims, entity-aware retrieval expansion, retrieval traces, and graph eval coverage.
- Local agent-role control plane for planner, researcher, executor, reviewer, critic, and summarizer runs.
- Review-first AI factory workflow runs with durable stages, artifacts, autonomy policies, workflow packs, and learning retrospectives.
- Policy-aware semi/full autonomous factory execution with safe local receipts, connector draft actions, provider-backed role runs, learning insights, and proactive autopilot factory steps.
- Bounded multi-hop SQLite GraphRAG expansion and claim-backed retrieval traces.
- Connector mutation hardening with normalized Jira/GitHub/Confluence/Aha payloads, dry-run outbox defaults, explicit connector adapters, rollback notes, and receipt visibility.
- Smart command UX with tiered command inventory, natural-language `myos do`, routed chat/voice metadata, and autopilot factory workflow selection.
- Managed tiny local router model setup via `myos model recommend/setup/status`, optional `setup-live --router-model`, and `MYOS_ROUTER_COMMAND` fallback routing.
- Router quality loop with packaged route eval fixtures, `myos router eval`, model shadow comparison, and privacy-safe feedback records.
- Bounded autonomy foundation with exact-hash router feedback application, active route override visibility, and next-slice plans for command registry and lightweight observability.
- Command registry and router tool awareness with static command metadata, safety tiers, `myos router commands`, and compact tiny-model command context.
- Lightweight observability kernel with bounded execution traces, correlation IDs, linked route/factory/action receipt metadata, and trace rollups/cleanup.
- Policy-aware autonomy decisions with explicit `allowed`, `needs_approval`, and `blocked` explanations before smart routes and factory starts.
- Policy decision feedback and calibration with local autonomy eval fixtures and privacy-safe feedback linked to execution traces.
- Daily operating loops for morning briefs, close-day summaries, weekly review signals, and connector evidence mapping.
- Release hardening docs, recovery notes, CI release-readiness gate, and tag/manual release workflow validation.

### Changed

- External CLI execution now uses a generic command backend rather than a tool-specific provider surface.
- Failed or blocked execution receipts create and retain links to follow-up inbox work.
- README command guidance now starts with chat, voice, autopilot, smart routing, and approvals before the expert command catalog.

### Validation

- Full unit suite passes locally.
- `myos release-check --strict` passes locally, including review-first, semi-autonomous, and connector dry-run factory smoke gates.

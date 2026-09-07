# Surgical Plan — From MVP to Pluggable Agent OS

Synthesis of the September 2026 codebase review: gaps, production-readiness,
the plugin/flow architecture, and the "universal agent harness / team teammate"
direction. This document operationalizes `ROADMAP.md` — it does not replace it.
Each phase below maps to a ROADMAP phase in §7.

**Positioning decided during the review:** MYOS is an *agent harness* — memory,
policy, approvals, receipts, review packets, audit — with a frozen safety kernel.
"Personal assistant" is pack #0; "software factory" is pack #1 (70% built in
`factory.py`); a git-native "team teammate" is the next pack. Versatility comes
from registries + manifests, never from loosening the kernel.

---

## 0. Frozen kernel invariants (apply to every phase, no exceptions)

These are the merge-blockers from `DEVELOPING.md`, restated as the plugin/pack
contract. Every phase below is designed to preserve them:

1. `autonomy.classify_action` is the single classification authority; unknown
   action types classify CONFIRM (`autonomy.py:226`).
2. `_execute_agent_action` (`execution.py`) is the single execution chokepoint.
   Plugins, packs, and spawned agents *propose* via `agentcore.enqueue_proposal`;
   nothing else ever executes.
3. Approval integrity (payload-hash pin + TTL, `execution.py:102-144`) is never
   bypassed. Stale approvals re-review.
4. Redaction (`privacy.apply_privacy_filters`) runs before persistence, for
   plugin/pack/executor output too.
5. Policy can only **tighten**: personas, packs, and plugin manifests may narrow
   action allowlists and raise tiers (CONFIRM→BLOCKED); nothing may lower a tier
   or widen an allowlist. Mirrors the "personas narrow, never widen" rule
   (`ARCHITECTURE.md:56`).
6. Hooks are **veto-only**. A hook may block an action; it may never approve,
   auto-run, or mutate a payload past its classification.
7. Additive migrations only; pre-migration backup before any new migration
   (added in P0).

---

## 1. Target end-state

```
┌────────────────────────────────────────────────────────────────────┐
│ COORDINATOR   task graph · sub-agent spawn/monitor · budgets       │
├────────────────────────────────────────────────────────────────────┤
│ PACKS (plugins)  = flow manifest + role manifests + skills         │
│   personal_assistant (#0) · software_factory (#1) · git_teammate   │
│   future: ops_agent, research_factory, support_desk …              │
├────────────────────────────────────────────────────────────────────┤
│ ROUTER   intent → domain classification → pack bind → role         │
├────────────────────────────────────────────────────────────────────┤
│ KERNEL (FROZEN)  classify → enqueue_proposal → approve_and_execute │
│                  → receipt → audit; hooks veto-only                │
├────────────────────────────────────────────────────────────────────┤
│ REGISTRIES   connectors · backends · actions · ingest · commands   │
│ DISCOVERY     entry-points ("myos.plugins") + ~/.myos/plugins      │
│               + markdown skills (data, not code)                   │
└────────────────────────────────────────────────────────────────────┘
```

"Identify the task and become that kind of agent" = router classifies the
domain → coordinator binds the matching pack → pack's role manifest instantiates
(instructions, retrieval scope, backend, allowlist) → proposals flow through the
same spine. "Additional team member" = the `git_teammate` pack operating on a
project repo under the trust ramp (§ Phase 6).

---

## 2. Phase dependency graph

```
P0 Reliability & Release ──┐
                           ├─► P1 Registry Refactor ─► P3 Plugin Substrate ─► P4 Packs & Flows ─► P6 Teammate
P2 Daily Surface ──────────┘            │                  ▲
                                        └──────────────────┘ (registries are the substrate)
P5 Retrieval & Learning ─── standalone; feeds P6 learning; gate for "versatile" claim
```

- P1 is the keystone: it is simultaneously the de-sprawl refactor and ~80% of
  the plugin system.
- P2 is independent of P1 and can proceed in parallel.
- P5 can start any time after P0; its embeddings work gates P6's "learns the
  codebase" claim.
- P6 requires P4 (packs) and P5 (retrieval quality).

Rough solo effort: P0 1–2 wks · P1 2–3 · P2 2 · P3 3–4 · P4 3–4 · P5 3–4 ·
P6 4–6. Sequence P0→P1→P2→P3→P4→P6 with P5 woven in ≈ 4–6 months.

---

## Phase 0 — Reliability & release hygiene (1–2 weeks)

**Goal:** the system survives daily use: no unbounded growth, no silent LLM
hangs, a tagged release, a proven upgrade path.

Work items:

1. **DB maintenance** — add `PRAGMA wal_checkpoint(TRUNCATE)` plus a
   size-gated `VACUUM` (e.g. only when freelist > threshold) to the pulse
   cycle (`pulse.py`). Add a manual `myos db maintenance` command in
   `cli_local_data.py` + `command_registry.COMMAND_SPECS` entry. Nothing in
   src today calls VACUUM/checkpoint; WAL grows unbounded.
2. **Pre-migration backup** — in `db.initialize_schema`, when `current <
   EXPECTED_SCHEMA_VERSION`, copy the DB file to
   `data/backups/pre-migration-<version>-<ts>.db` (keep last N=5) *before*
   applying new migrations. Expand `docs/RECOVERY.md` (currently 8 lines) with
   the restore procedure.
3. **Provider hardening** (`providers/claude.py` — the only Anthropic import):
   - Explicit request timeout: env `MYOS_LLM_TIMEOUT_SECONDS` (default 120).
   - Retry policy: configure the SDK client `max_retries`, wrap turns with
     exponential backoff for 429/5xx/connection errors; cap total turn wall
     clock.
   - Configurable `max_tokens` (currently hardcoded 16000/4000 at
     `claude.py:382,653`) via policy/env.
   - Circuit breaker: N consecutive failures → mark backend unavailable for a
     TTL; chat surfaces a graceful degradation message instead of hanging.
4. **Lock TTL configurability** — `locks.py:19` hardcodes a 1-hour stale
   reclaim; a legit >1h factory run can be double-started. Make the window a
   policy/env value; document the tradeoff.
5. **Broad-except audit** — triage all 52 `except Exception` sites. Replace
   silent swallows with logged + trace-recorded errors (`observability`);
   keep and annotate justified ones (several already carry `# noqa: BLE001`
   rationale comments).
6. **Tag the first release** (`v0.1.0`) and add an **upgrade test**: CI
   restores a versioned DB fixture (schema 42, schema 43), runs migrations,
   and smoke-tests `myos today`/`doctor`. This is the "upgrade compatibility
   proven across real user databases" gap named in `ROADMAP.md`.

**Acceptance:** `PYTHONPATH=src python -m unittest discover -s tests` green;
`myos release-check --strict` and `myos doctor --strict` green on a fresh
install; pulse runs checkpoint/VACUUM; a pre-migration backup file appears on
a forced migration; release tagged with upgrade CI job green.

---

## Phase 1 — Registry refactor: kill the hardcode (2–3 weeks, no behavior change)

**Goal:** every name-list that is currently duplicated by hand becomes a
lookup. This is the keystone — it fixes connector sprawl *and* is the plugin
substrate. Pure refactor; golden-snapshot tests prove zero user-visible diff.

The hardcode inventory being eliminated (from the review):

| What | Where today |
| --- | --- |
| Connector import map | `connectors/__init__.py:1-14` |
| Duplicated connector dicts | `cli_operations.py:41-46, 189-194`; `cli.py:416-425`; `cli_workflow.py:225-230`; `pulse.py:21` |
| Hardcoded argparse `choices` (8 sites) | `cli.py:1109, 1224, 1239, 1314, 1325, 1378, 1519, 1677` |
| Legal mutation targets/operations | `execution.py:153-154` (`CONNECTOR_TARGETS`/`CONNECTOR_OPERATIONS`) |
| Per-connector target-ref parsing | `execution.py:247-264` |
| Live-send adapter dispatch | `execution.py:723-728` |
| Rollback provider allowlist + compensation branches | `rollback.py:125, 131-198` |
| Backend if/elif chain + availability list | `providers/__init__.py:131-169, 175` |
| Workflow packs + fixed roles | `factory.py:19`, `factory.py:508` (KeyError on unknown role) |
| Watchable extensions | `cli_workflow.py:420-421` |

Work items:

1. **Create `registries.py`** — a small generic `Registry[T]`
   (register/get/names/all) with capability metadata. No discovery yet
   (that's P3); built-ins register explicitly.
2. **ConnectorRegistry** — migrate all 12 call sites above to lookups.
   Argparse `choices` become registry-derived (help text may reflow — accept
   and snapshot).
3. **ActionRegistry** — one home for action types: tier (AUTO/CONFIRM/BLOCKED
   — default CONFIRM for unknown, preserving `autonomy.py:226`), allowed
   connector targets/operations, live-adapter reference, compensation strategy
   (absorbs `rollback.py`'s map). `autonomy._ACTION_TIER` and
   `execution.py:153-154` become views over it.
4. **BackendRegistry** — replace the `get_backend` if/elif; `available_backends()`
   iterates the registry. Keep the fail-safe fallback: unknown name →
   command backend if `MYOS_AI_COMMAND` set, else claude.
5. **IngestRegistry** — `source_type → extractor + extensions`; register
   text/audio/image; watch-scan iterates registered extensions.
6. **Parity/golden tests** — snapshot registry contents against the old
   hardcoded lists; snapshot representative CLI outputs (`--json` envelopes)
   before/after. Extend the `command_contract` release gate to assert no
   duplicated registry lists creep back (grep-based CI check for
   `frozenset({"jira"` etc.).

**Acceptance:** full suite + `release-check --strict` green; a repo-grep
proves no duplicated connector/backend name lists remain outside the
registry; `--json` envelope snapshots identical to pre-refactor.

---

## Phase 2 — Daily surface & operability (2 weeks, parallelizable after P0)

**Goal:** a new user gets value in 10 minutes; a daily user needs one command.
This is `ROADMAP.md` near-term item 3 ("reduce the daily product surface").

Work items:

1. **`myos today`** — the focused surface: today brief, inbox triage,
   pending approvals (with integrity state), reminders, suggested next
   actions. `--json` envelope per the consumer contract in
   `ARCHITECTURE.md`. The 179-command catalog remains as expert mode.
2. **`myos init` onboarding wizard** — extend `cli_setup_live.py`: DB
   location, backend choice with availability probe, connector env-var
   guidance (from the connector registry's `required_env`), deterministic
   offline demo seeding, and a first-run `today`.
3. **Linux scheduling parity** — abstract `cli_launchd.py` behind a scheduler
   interface with launchd (macOS) and systemd user-unit (Linux) backends;
   `install.sh` picks per platform. Keep Windows an explicit documented
   non-goal.
4. **Quickstart docs** — README 5-minute path (clone → install → init →
   today → demo); expand `docs/RECOVERY.md` (done in P0, verify here).

**Acceptance:** a fresh clone to first `today` runs in <10 minutes without
reading source; CI gains a fresh-install smoke job; systemd install verified
on Linux CI or documented manual test.

---

## Phase 3 — Plugin substrate: manifests, discovery, skills, hooks (3–4 weeks)

**Goal:** third parties (and us) add connectors/backends/skills/flows without
touching `src/`. Deliberately matches `ROADMAP.md`'s P2 triage row: "small
local markdown skills and lifecycle hooks that are visible in traces and
governed by policy" — plugin hub and arbitrary extension loading stay
deferred.

Work items:

1. **Manifest spec** — `plugin.toml` per plugin:
   `[plugin] name, version, capabilities[], required_env[], policy_hints`;
   per-capability tables (connector/backend/commands/skills/flows/hooks)
   including declared action types **with requested tiers**.
2. **Discovery** — two channels:
   - Installed packages: setuptools entry-point group `myos.plugins` (code
     plugins, importable modules registering into the P1 registries).
   - `~/.myos/plugins/<name>/` drop-ins: manifest + markdown skills + flow
     files only — **no code loading from local dirs** initially (safer; the
     roadmap defers arbitrary extension loading).
3. **Load & govern** — deterministic load order; one failing plugin never
   blocks startup (isolated, logged, surfaced in doctor). Enable/disable
   state DB-backed behind `myos plugins list/enable/disable/audit`. Per-plugin
   `doctor` checks (required_env present, backend reachable). Registry clamp:
   requested tier CONFIRM-or-lower-than-declared → clamped to CONFIRM; plugin
   may declare BLOCKED for its own types.
4. **Markdown skills** — persona-instruction-style files with an action
   allowlist; loaded as *data*; rendered into persona/role prompts; every
   skill use visible in traces.
5. **Hook bus** — lifecycle events: `before_propose`, `after_classify`,
   `before_execute`, `after_receipt`, `on_failure`. `before_execute` is
   veto-only (invariant 6). Hooks registered via manifest (external command,
   JSON on stdin/stdout — same pattern as `MYOS_NOTIFY_COMMAND`) or entry
   point. Every invocation written to `execution_traces` via `observability`.
6. **MCP evaluation spike (timeboxed)** — MYOS as MCP *client*: tools
   discovered from an MCP server enter `classify_tool` as CONFIRM proposals.
   Adopt only if the spike proves simpler than manifest-declared command
   hooks for the tool case; defer MYOS-as-MCP-server until the command
   surface stabilizes.

**Acceptance:** a sample out-of-tree plugin (e.g., a Linear connector) works
end-to-end — syncs, proposes gated mutations, appears in doctor and plugins
CLI — with zero changes under `src/`; disabling it leaves zero residue;
`docs/BOUNDED_AUTONOMY.md` gains the hook-guarantee section (required by
`DEVELOPING.md:152` when touching approval-critical paths).

---

## Phase 4 — Packs & flows: generalize the factory (3–4 weeks)

**Goal:** "become any type of agent" becomes data. The factory stops being a
hardcoded pack and becomes a pack *interpreter*.

Work items:

1. **Flow manifest** — YAML/JSON declaring: stages, roles, per-role backend,
   allowed action types (validated against the ActionRegistry), required
   review-packet sections, executor policy, and retrieval scopes. The
   `factory_policies` table (`factory.py:26-115`) remains the enforcement
   point — a flow can never exceed what policy allows.
2. **Extract `software_delivery`** — move `WORKFLOW_PACKS` (`factory.py:19`)
   and the fixed role tuple (`factory.py:508`) into the first data-driven
   pack. Golden tests prove identical behavior pre/post extraction.
3. **Role manifests as markdown** — planner/researcher/executor/reviewer/
   critic (+ connector-specific reviewers) as user-editable files
   (ROADMAP P1 triage row), bounded by persona allowlist machinery
   (`personas.filter_actions`).
4. **Router domain binding** — extend `router.py` from command-routing to
   domain classification → pack binding; persist the bound pack on the
   intent; `myos packs list/run`. Tiny-model + rules path already exists.
5. **Prove the abstraction with exactly one more pack** — recommend
   `git_teammate` (P6) since it serves the team vision; a simple `ops_agent`
   is the alternative. Extract-don't-speculate: no third pack until the
   second ships.

**Acceptance:** `software_delivery` runs identically from a manifest
(golden tests); adding a trivial demo pack (manifest + one markdown role)
requires zero `src/` changes; unknown role in a manifest fails validation
with a structured error, not a KeyError.

---

## Phase 5 — Retrieval & learning (3–4 weeks, woven in after P0)

**Goal:** agents that "know the project" and improve from outcomes. This gates
the versatility and teammate claims — a teammate that misremembers decisions
is worse than none.

Work items:

1. **Expand retrieval evals first** (ROADMAP near-term item 5) — grow the
   fixture set until it's a meaningful regression gate *before* touching the
   engine.
2. **Real local embeddings** — add an embedding backend seam (local-first,
   e.g. sqlite-vec + a small local model; provider embeddings opt-in);
   hybrid FTS + vector + graph scoring. Ship behind config, gated on the eval
   suite showing measurable improvement — no regression ships.
3. **GraphRAG depth** (ROADMAP Phase 3) — provider-assisted entity/relationship
   extraction behind the approval/policy path; extend typed edges across
   tickets/PRs; fetch Confluence page bodies (`confluence.py:44` currently
   stores `body=""`).
4. **Minimal learning loop** — execution receipts + review verdicts + user
   corrections produce insight rows with provenance; confirmed outcomes boost
   the retrieval weight of the claims/decisions behind them; weekly review
   surfaces "what the assistant learned." Every learned item traceable to
   receipts/verdicts (audit invariant).
5. **Memory curation CLI** — inspect/correct/delete stale or harmful memories
   with privacy-safe previews (ROADMAP P1 memory-curation row).

**Acceptance:** eval suite green with measurable lift from embeddings; a
rejected-review pattern demonstrably changes later proposals; curation
commands covered by tests.

---

## Phase 6 — Teammate direction: git-native pack + trust ramp (4–6 weeks)

**Goal:** the agent operates as an additional team member on a real repo:
assigned work → branch → PR → CI → human merge, learning as it goes — every
step inside the frozen kernel.

Work items:

1. **Project scoping (prerequisite)** — `projects` table binding goals,
   personas, memory, connector aliases, repo paths, instructions per project
   (ROADMAP P1 agent-zero row). Migration + CLI + scoping enforcement in
   retrieval/context assembly. Kills cross-project memory contamination.
2. **`git_teammate` pack**:
   - **Bot identity**: dedicated committer identity + bot token (GitHub App
     or PAT), never the user's credentials.
   - **Branch/worktree lifecycle**: build on the existing Zero worktree path
     (`factory.py:736-975`) and `apply_patch` guards.
   - **PR-as-review-packet**: opening a PR *is* the review artifact — attach
     the review-packet schema (intent, plan, evidence, risks, rollback) to
     the PR description; approval-integrity maps to "stale PR needs
     re-review" (TTL semantics already exist, `execution.py:39`).
   - **CI-as-receipt**: CI results recorded as execution-receipt evidence on
     the agent run; failed CI creates follow-up inbox items (existing
     receipt→inbox machinery).
   - **Assignment model**: `myos assign` / issue-label pickup; the autonomy
     loop claims assigned work within policy.
   - **Standup surface**: per-project digest posted to a team channel via
     the notify hook.
3. **Trust ramp (explicit operator config per project/skill)**:
   - *Probation*: read + draft only; everything queues for approval.
   - *PR author*: opens PRs, never merges; approvals happen on the PR.
   - *Lane owner*: auto-merge for policy-listed low-risk paths (docs/tests);
     BLOCKED-classified actions always need a human.
   - Promotion is a recorded operator decision; demotion is automatic on
     rejected-review/failed-CI streaks.
4. **Teammate learning** — feed rejected-review reasons and CI failures into
   the P5 loop as weighted memory.

**Acceptance:** demo journey — assign a ticket → agent branches in a
worktree → opens a PR with a review packet → CI runs → human merges — with
zero pushes/merges outside policy and a receipt for every external action;
one agent instance per project (multi-agent/multi-human coordination stays a
documented non-goal until a single teammate proves out).

---

## 3. Cross-cutting work streams (every phase)

- **Tests**: stdlib `unittest`, `-W error::ResourceWarning`, fresh temp DB per
  test, subprocess timeouts (`DEVELOPING.md:175-181`). Every phase ships its
  tests in the same commit.
- **Gates**: `release-check --strict` (command contract, factory smoke),
  `doctor --strict`, `migrations verify --strict` extend alongside features.
- **Migrations**: additive only, pre-migration backup (P0), update
  `tests/test_cli.py` migration assertions per `DEVELOPING.md:143-152`.
- **Docs**: any approval-critical change updates `docs/BOUNDED_AUTONOMY.md`;
  new CLI commands follow the 3-place rule (`DEVELOPING.md:98-141`).
- **Docs honesty**: README/ARCHITECTURE claims stay behind shipped capability
  (the ROADMAP Phase 0 rule).

## 4. Immediate action items (this week)

1. P0.1 — DB maintenance in pulse (`wal_checkpoint` + gated `VACUUM`).
2. P0.2 — pre-migration backup in `db.initialize_schema` + RECOVERY.md.
3. P0.3 — provider timeout/retry/circuit-breaker in `providers/claude.py`.
4. P1.1 — create `registries.py` skeleton + golden-snapshot tests capturing
   today's hardcoded lists (write the snapshots *before* refactoring).
5. P0.6 — tag `v0.1.0` once the above land.

## 5. Decision log / open questions

| # | Decision | Recommendation |
| --- | --- | --- |
| 1 | Second pack after `software_factory` | `git_teammate` (serves the team vision); `ops_agent` is the fallback |
| 2 | MCP adoption timing | Timeboxed client-side spike in P3; adopt only if simpler than manifest hooks |
| 3 | Windows support | Documented non-goal until real demand |
| 4 | Agent instance topology | One agent per project/DB; no shared multi-tenant instance yet |
| 5 | Plugin code from local dirs | Not loaded (entry-points only) until sandboxing story exists |
| 6 | `initialize_schema` on every connection | Optimize with fast schema-version check path — optional P0 item |
| 7 | Cost/token observability | Build as its own ledger slice — see `docs/COST_OBSERVABILITY.md` (Slices 1–3 slot into P0–P2; `project_id` reserved for P6 chargeback). Today only `zero` runs capture usage; the default Claude backend discards `response.usage` |

## 6. What we are explicitly NOT doing (preserved from ROADMAP.md)

- No auto-executing external mutations without approval.
- No hosted/graph database requirement; SQLite-first.
- No multi-tenant SaaS posture before the local OS is stable.
- No remote plugin hub, plugin network access, or arbitrary extension loading.
- No marketing of current graph tables as GraphRAG; no eval-pass-rate claims
  until fixtures are reproducible.

## 7. Mapping to ROADMAP.md

| This plan | ROADMAP.md |
| --- | --- |
| P0 | Phase 1 (stable local app) + Phase 6 (backup/health/release) |
| P1 | Prerequisite enabler (new — the hardcode debt the roadmap implies but doesn't itemize) |
| P2 | Phase 1 + near-term surgical item 3 (daily surface) |
| P3 | "External Inspiration Triage" P2 row (skills/plugins/hooks) |
| P4 | Phase 5 (agent control plane) + P1 role-manifest row |
| P5 | Phase 3 (GraphRAG) + Phase 4 (embeddings) + P1 memory-curation row |
| P6 | Phase 5 (Zero integration → general executor/teammate) + P1 project row |

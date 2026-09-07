# External Benchmark Review — MYOS vs. the 2026 agentic-OS field

Date: 2026-09-07. Comparators researched the same day from their GitHub
repositories: **ECC** (affaan-m/ecc), **OpenClaw**, **AIOS** (agiresearch),
**QwenPaw** (agentscope-ai), **OpenFang** (RightNow-AI). Honorable mentions
noted but not deep-dived: Letta, Agno AgentOS, Khoj, Leon, Goose, ZeroClaw.

---

## 1. Verdict

MYOS's safety kernel is at or above the state of the art. Its product surface
is well behind the field's leaders. The field has converged on five patterns
MYOS lacks or only has on paper: a registry/driver substrate, a messaging
gateway, a markdown skill ecosystem, kernel-level cost metering with budget
ceilings, and defense-in-depth around the agent loop itself. MYOS leads in
approval integrity (hash-pinned, TTL'd approvals with receipts), redaction
before persistence, policy monotonicity, and test discipline.

Do not outbuild the ecosystems — out-govern them. The winning niche is
"the assistant with the provable safety kernel": every external action has a
hash-pinned approval, a receipt, and an audit trail.

## 2. The comparators

| Project | What it is | Stack | Extension model | Safety model |
| --- | --- | --- | --- | --- |
| **ECC** | Governance/skills/memory layer installed *into* coding harnesses (Claude Code, Codex, Cursor…). 68 agents, 286 skills, 94 commands, hooks, "instincts" (confidence-scored learned patterns), Memory Vault, AgentShield scanner (102 rules, CI gate), Plan Canvas approval UI. ~251k stars self-reported. | TS/Shell/Python | Adapters per harness; marketplace plugins; markdown skills | Explicit hook-consent at install; secret-blocking Cursor hooks; AgentShield exit-2 CI gate; "memory is unreviewed context, not executable policy" |
| **OpenClaw** | Most popular personal-agent gateway. One local process bridging 13+ messaging channels, Control UI, CLI/TUI, companion nodes (voice/camera/screen). | Node/TS | Skills + plugins + ClawHub marketplace | Pairing-based trust for inbound DMs; optional sandboxing |
| **AIOS** | Academic agent OS (COLM 2025). LLM as kernel service, agent scheduler (FIFO/RR/priority, agent groups), context/memory/storage/tool managers, syscall-style SDK, semantic file system, A-MEM memory. | Python (+ Rust scaffold) | Multi-framework agent onboarding; MCP computer-use | Research-grade; isolation via managed resources |
| **QwenPaw** | Closest 1:1 comparator: personal AI assistant architected as an Agent OS. 34.9k stars. Workspace (Resources/Governance/Sandbox), Drivers layer over MCP/A2A/ACP with encrypted credential vault, Loop Engineering (Sense→Plan→Act→Review), ReMe markdown memory, cron + heartbeat. | Python | Drivers (MCP/A2A/ACP), skills with scanner + lineage verification, plugin security review | Per-call policy gate on driver writes; approval gates before irreversible actions; fail-closed |
| **OpenFang** | Rust "Agent OS": 7 scheduled autonomous "Hands" (manifest + playbook + guardrails, activate/pause/status lifecycle), 40 channels, dashboard, OpenAI-compatible API, FangHub marketplace, single 32 MB binary. | Rust | Hands, 53 native tools + MCP + A2A, SKILL.md-native (ClawHub-compatible) | 16 systems: Merkle hash-chained audit, signed manifests, WASM sandbox, taint tracking, prompt-injection scanner, loop guard, rate limiter, budget ceilings in-kernel |

## 3. Where MYOS is ahead

1. **Approval integrity.** `verify_approval_integrity` payload-hash pin + TTL
   + stale-approval re-review (`execution.py:102-144`) + execution receipts +
   failure→inbox is stronger than anything documented in the five. OpenClaw's
   pairing governs *who may talk*; QwenPaw's gates govern *who may act*;
   MYOS governs *that the thing you approved is bit-for-bit the thing that
   runs, before a deadline*.
2. **Redaction before persistence** (`privacy.apply_privacy_filters`) — rarer
   in the field than it should be.
3. **Policy can only tighten** as a written invariant — QwenPaw is
   fail-closed in spirit; MYOS codifies it.
4. **Test discipline** — 363 tests, `release-check --strict`, migration
   verification. Most of the field is under-tested.
5. **The operating loop with review packets** — Intent→…→Audit→Learning with
   durable evidence per stage matches ECC's plan→test→implement→review→
   verify→remember→improve and QwenPaw's loop engineering; the receipt/audit
   half is deeper in MYOS.

## 4. Gaps, mapped to SURGICAL_PLAN amendments

### A. Gateway / always-on surface (biggest product gap — no phase owns it)
Every leader is daemon-first: OpenClaw's gateway + Control UI, QwenPaw's
channels + heartbeat, OpenFang's dashboard + OpenAI-compatible API. MYOS is
CLI + launchd; `dashboard.py` is a read-only HTML snapshot.
**Amendment:** cheap step in P2 — push briefs and approval requests through
the existing `MYOS_NOTIFY_COMMAND` seam to one messaging channel (e.g.
Telegram bot); make the dashboard read-write for approvals ("approve from
phone"). Real step: a post-P4 gateway phase (long-running local process
exposing the read API + approval callbacks; CLI becomes one client).

### B. Skill format compatibility (P3.4)
ECC ships 286 skills; ClawHub and FangHub are marketplaces; OpenFang reads
SKILL.md natively. A de-facto markdown skill standard (SKILL.md + frontmatter)
exists. **Amendment:** make P3.4 markdown skills SKILL.md-compatible so MYOS
consumes the existing ecosystem; scan loaded skills for prompt injection
(OpenFang has a dedicated scanner; MYOS renders skills into prompts).

### C. MCP is table stakes, not a spike (P3.6)
All five comparators are MCP-native. **Amendment:** promote the P3.6
"timeboxed evaluation spike" to a committed MCP-client item; discovered tools
enter `classify_tool` as CONFIRM proposals (design already right — commit to
it). Keep MYOS-as-MCP-server deferred.

### D. Cost: meter and enforce at the kernel, earlier (P0/P6)
OpenFang enforces budget ceilings in-kernel. MYOS's ledger exists only on
`feat/llm-usage-ledger`; the default backend discards usage on main.
**Amendment:** merge the ledger before v0.1.0; then add budget ceilings as
policy (exceeded budget → BLOCKED/confirm — fits "policy can only tighten").

### E. Defense-in-depth for the loop itself
OpenFang's checklist, translated to MYOS:
- **Loop guard** — circuit breaker on repeated failed actions in
  autonomy/autopilot cycles (none today).
- **Merkle hash-chain the audit event log** — each event stores prev-hash;
  cheap, and makes the audit story tamper-evident.
- **Manifest integrity for P3 plugins** — checksums + capability audit;
  drop-ins are unsigned today (OpenFang signs with Ed25519).
- **AgentShield-lite in doctor** — scan own env/config/plugin manifests for
  leaked secrets. ECC built an entire product on this; a doctor check is the
  1% version.

### F. Autonomy as named Hands, not cron (P4)
OpenFang's Hand = manifest + playbook + guardrails + activate/pause/status
lifecycle + dashboard metrics. **Amendment:** in P4, package autonomy goals
as Hand-like managed objects with per-goal guardrails, pause/resume, and
metrics. This is the demo surface that makes the "OS" claim tangible.

### G. Memory doctrine (P5)
ECC: "Memory is unreviewed context, not executable policy," instincts with
confidence scores; QwenPaw's ReMe keeps a human in the loop. MYOS P5.4
already only reweights retrieval. **Amendment:** write the doctrine into
`docs/BOUNDED_AUTONOMY.md` as an explicit invariant (learned memories never
escalate permissions), add confidence-scoped recall, surface confidence in
the P5.5 curation CLI.

### H. Teammate pack (P6) — validated by ECC
- Adopt ECC's explicit **test** stage (TDD-by-default) in factory/teammate
  packs and a **verify** step after review.
- Seed P4 role manifests with **per-language reviewer** roles (most of ECC's
  68 agents are reviewers).
- AgentShield's exit-code-2 CI gate → run a local policy/secret scanner in
  CI and record it as receipt evidence (fits CI-as-receipt).
- PR-as-review-packet and the trust ramp stand as planned; OpenClaw's
  pairing codes are a reusable pattern for inbound channel trust later.

### I. Command surface — keep the plan
ECC ships 94 slash commands and survives because of on-demand loading plus a
front door. Validates P2's `myos today` + expert-mode catalog for the
~118-command registry.

## 5. Positioning note

ECC did not build an OS that runs agents; it built a governance/skills layer
that rides existing harnesses and became the biggest repo in the space. MYOS
is the same thesis from the opposite direction (own kernel, adopt packs).
Both converge on: the durable value is governance, memory, and receipts —
not model access. That is strong external validation of the SURGICAL_PLAN
positioning ("MYOS is an agent harness… versatility comes from registries +
manifests, never from loosening the kernel") and of P1-as-keystone.

## 6. Consolidated plan deltas

| Phase | Amendment |
| --- | --- |
| P0 | Merge cost ledger pre-v0.1.0; add loop guard; (optional) Merkle-chain audit log |
| P2 | Briefs/approvals out via `MYOS_NOTIFY_COMMAND` to one channel; read-write approvals in dashboard |
| P3 | Commit to MCP client (drop "spike"); SKILL.md-compatible skills + injection scan; manifest checksums; secrets scan in doctor |
| P4 | Autonomy goals as Hand-like objects (lifecycle + metrics) |
| P5 | Confidence-scoped instincts; "memory is unreviewed context" invariant in BOUNDED_AUTONOMY.md |
| P6 | TDD-by-default; CI policy-scanner as receipt evidence |
| New (post-P4) | Gateway/daemon + HTTP API as the product-surface phase |

## Sources

- https://github.com/affaan-m/ecc
- https://github.com/openclaw/openclaw
- https://github.com/agiresearch/AIOS
- https://github.com/agentscope-ai/QwenPaw
- https://github.com/rightnow-ai/openfang
- https://github.com/topics/agentic-os
- https://www.vellum.ai/blog/best-open-source-personal-ai-assistants
- https://contabo.com/blog/best-open-source-ai-agent-frameworks/

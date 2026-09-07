# Cost & Token Observability — Research and Design

How MYOS finds, records, and governs LLM token consumption and dollar cost.
This document is the research backing for a new work slice that slots into the
`docs/SURGICAL_PLAN.md` sequence (Slices 1–3 below fit naturally inside the
P0–P2 window).

---

## 1. Current-state audit: what MYOS captures today

**Answer in one line: almost nothing — the only token/cost capture in the
entire codebase is for external `zero` executor runs, and the default
Anthropic backend discards usage entirely.**

| Capture point | Status | Evidence |
| --- | --- | --- |
| `zero` executor runs | **Captured** — `usage` stream events parsed (`promptTokens`, `completionTokens`, `totalTokens`, `costUsd`) into `ZeroRunResult.usage` | `zero_executor.py:167-170` |
| Zero usage persistence | Attached to the agent-run result dict (embedded in a JSON blob), never a queryable column | `factory.py:727` |
| `claude` backend (default) | **Discarded** — `response = stream.get_final_message()` carries a full `usage` object every iteration of the ≤12-turn tool loop (`claude.py:393-399`); it is never read | `providers/claude.py:368-443` |
| `claude_sdk` backend | No usage handling at all | `providers/claude_sdk.py` |
| `agent_cli` / `cursor` / `copilot` backends | Log purpose/status/latency only | `providers/agent_cli.py:177` |
| `ai_provider_calls` table | Has provider/purpose/status/latency/request_json/response_json — **no token columns**; written by `planner.py:305` and `agent_cli.py:177` | migration 12 (`db.py`) |
| `agent_runs` / `execution_traces` | No usage columns; `execution_traces.metadata_json` is the natural join point but nothing writes usage there | `db.py` |
| Pricing data | **None anywhere.** No model price map, no cost computation. `costUsd` from Zero is stored as an opaque self-reported string | — |
| Router tiny-model | Local runtime (ollama/llama-cpp) → $0 marginal cost, tokens unmeasured | `models.py:26` (`RUNTIMES = ("ollama", "llama-cpp", "command")`) |

**Consequence:** a user running chat/autopilot/factory through the default
Claude backend has zero visibility into spend — no per-run cost, no daily
rollup, no budget. The single most expensive subsystem is also the least
observable one. This becomes blocking at `SURGICAL_PLAN.md` P6 (teammate),
where per-project budgets and chargeback ("the invoice for your additional
team member") are requirements.

---

## 2. Research: how providers report usage and price

### 2.1 Anthropic (the default backend) — four distinct billing categories

The Messages API `usage` object (returned on every response, including via
`stream.get_final_message()`):

| Field | Billed at |
| --- | --- |
| `input_tokens` | base input rate |
| `output_tokens` | output rate (~5× input on most models) |
| `cache_creation_input_tokens` | **1.25×** input rate (5-minute TTL) or **2×** (1-hour TTL) |
| `cache_read_input_tokens` | **0.1×** input rate (90% discount) |

Two facts make this critical for MYOS specifically:

1. **MYOS already uses prompt caching** — `stream_kwargs["cache_control"] =
   {"type": "ephemeral"}` (`claude.py:391`). Long chats/agent loops will show
   most input tokens as `cache_read`. A naive `tokens × rate` estimate would
   overstate cost by roughly an order of magnitude; any cost model must treat
   the four categories separately.
2. **The tool loop multiplies turns** — `run_turn` iterates up to 12 times
   (`claude.py:393`), each with its own cumulative usage. Capture must sum
   across iterations per turn.

Cost formula:

```
cost = input_tokens            × rate_input
     + output_tokens           × rate_output
     + cache_creation_tokens   × rate_input × 1.25   (5m TTL; 2.0 for 1h)
     + cache_read_tokens       × rate_input × 0.1
```

Pre-flight counting: Anthropic exposes an exact `POST /v1/messages/count_tokens`
endpoint (`client.beta.messages.count_tokens` in the SDK) that accepts the same
payload shape as message creation. `tiktoken` is only a rough proxy for Claude
models — different tokenizer.

### 2.2 OpenAI-compatible surfaces

`usage.prompt_tokens` / `completion_tokens`, with nested detail objects:
`prompt_tokens_details.cached_tokens` (cache hits bill at the cached rate;
hits occur in 128-token increments for prompts ≥1024 tokens) and
`completion_tokens_details.reasoning_tokens`. Relevant for any future
OpenAI-family backend and for subprocess executors that report OpenAI-style
usage.

### 2.3 Estimation fallbacks

- Local heuristic: `len(chars) / 4` — crude but universal; always flagged
  `estimated=1`.
- `tiktoken` (optional dependency): accurate for OpenAI-family, approximate
  elsewhere.
- Anthropic `count_tokens` API: exact, but a network call — opt-in only.

### 2.4 Prior art worth copying

- **LiteLLM's `model_prices` JSON** — a community-maintained per-model price
  map keyed by model id (input/output/cache rates + context window). Proof
  that a static, versioned price file is a workable pattern; MYOS should ship
  its own small file rather than take the dependency.
- **OpenTelemetry GenAI semantic conventions** — attribute naming for
  (`gen_ai.usage.input_tokens`, `output_tokens`, …) if MYOS ever exports
  traces.
- Known pitfall from the field: proxies/gateways sometimes drop cache usage
  fields (LiteLLM issue #27763, Cloudflare gateway reports) — another reason
  to capture at the SDK boundary inside MYOS rather than trusting middleware.

---

## 3. Design: the usage ledger

### 3.1 Principles

1. **Append-only ledger** — usage events are never updated or deleted (except
   by the existing retention policy).
2. **Price snapshot at call time** — each event stores the price version used
   so later price changes never rewrite history.
3. **Tokens even when cost is unknown** — unknown model ⇒ `cost_millicents`
   NULL, tokens still recorded, `doctor`/report flags the gap.
4. **Estimated is a first-class flag** — estimated counts are never silently
   mixed with exact ones in rollups.
5. **Observe-only** — recording usage needs no approval (local persistence,
   invariant-compatible); it changes no action's classification.
6. **No content in usage rows** — counts and ids only; privacy filters not
   even needed (nothing user-derived is stored beyond ids).

### 3.2 Schema (additive migration 44)

```sql
CREATE TABLE llm_usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id TEXT,              -- joins to execution_traces
    surface TEXT,                     -- cli/chat/voice/autopilot/factory
    command TEXT,
    agent_task_id INTEGER,            -- joins to agent_tasks
    agent_run_id INTEGER,             -- joins to agent_runs
    factory_run_id INTEGER,
    persona TEXT, pack TEXT,
    project_id INTEGER,               -- reserved for SURGICAL_PLAN P6 scoping
    backend TEXT NOT NULL,            -- claude / claude-sdk / zero / command / ...
    model TEXT NOT NULL,
    purpose TEXT,                     -- chat / plan / extract / route / reflect
    requests INTEGER NOT NULL DEFAULT 1,  -- tool-loop iterations aggregated
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated INTEGER NOT NULL DEFAULT 0,
    cost_millicents INTEGER,          -- NULL when model price unknown
    self_reported_cost_millicents INTEGER,  -- e.g. zero's costUsd, kept verbatim
    price_version TEXT,
    latency_ms INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_usage_created ON llm_usage_events(created_at);
CREATE INDEX idx_usage_correlation ON llm_usage_events(correlation_id);
CREATE INDEX idx_usage_backend ON llm_usage_events(backend, model, created_at);
```

Integer **millicents** throughout (1_000_000 millicents = $1,000) — no float
drift; divide by 10_000 at display time. `self_reported_cost_*` exists so
external executors' own numbers are preserved verbatim *next to* MYOS's
computed figure (discrepancy is signal, not noise).

### 3.3 Price map as data

Ship `model_prices.json` as package data (same pattern as
`route_eval_fixtures.json`):

```json
{
  "version": "2026-09",
  "currency": "usd",
  "per_mtok": {
    "claude-sonnet-4-5":  {"input": 3000, "output": 15000,
                           "cache_write_5m": 3750, "cache_write_1h": 6000,
                           "cache_read": 300},
    "claude-haiku-4-5":   {"input": 1000, "output": 5000,
                           "cache_write_5m": 1250, "cache_write_1h": 2000,
                           "cache_read": 100}
  }
}
```

(Rates in millicents per MTok; illustrative — refresh from the Anthropic
pricing page at release time.) Unknown model ⇒ cost NULL + a `myos prices
list` warning; `price_version` is stamped on every event.

### 3.4 Capture points (one helper, five call sites)

New `usage.py` with a single entry point:

```python
def record(conn, *, backend, model, purpose, usage: dict,
           correlation_id=None, persona=None, pack=None, **refs) -> None
```

| # | Site | Work |
| --- | --- | --- |
| 1 | `providers/claude.py` `run_turn` | Accumulate `response.usage` per tool-loop iteration (`claude.py:399`); sum + record once per turn with the active persona. Cache-read tokens dominate here — the whole point of §2.1. |
| 2 | `providers/claude_sdk.py` | Same from the SDK result object. |
| 3 | `providers/agent_cli.py` (and cursor/copilot) | Parse usage from the result JSON when present; else estimate (`chars/4`, `estimated=1`) and record before the existing `ai_provider_calls` insert. |
| 4 | `factory.py` Zero path | Persist the already-parsed `result.usage` (`zero_executor.py:167-170`) as an event bound to `factory_run_id`/`agent_run_id`; keep `costUsd` in `self_reported_cost_millicents`. |
| 5 | `router.py` tiny-model calls | Record estimated tokens, `backend="local-tiny"`, cost 0. |

Extension (P5 embeddings): embedding calls record as
`purpose="embed"` events — cheap but non-zero, and they belong in the same
ledger.

### 3.5 Surfaces and rollups

- **`myos usage report`** — `--since/--by backend|model|persona|purpose|day`,
  `--json` envelope `myos.usage.report.v1` (stable per the
  `ARCHITECTURE.md` consumer contract; register in `JsonEnvelopeSurfaceTest`).
- **`myos usage show <correlation_id>`** — per-command/per-run breakdown,
  joinable from traces, receipts, and factory runs.
- **`myos today` budget line** — spend today vs budget.
- **Weekly review** — "what the assistant cost this week and where it went."
- **Execution receipts** — optional `usage` block on `execution_receipt.show.v1`
  (additive field, superset-safe).
- **Dashboard** — cost-by-day chart data from the same rollups.

### 3.6 Budgets and governance (assistant_policies keys)

- `usage_budget_daily_millicents`, `usage_budget_monthly_millicents`,
  `usage_warn_threshold_pct` (default 80), plus per-persona/pack overrides
  (`usage_budget_<persona>_daily_millicents`).
- **Soft gate:** today/digest shows a warning at the threshold.
- **Hard gate (deliberately narrow):** when a budget is exceeded,
  *proposing* loops pause — autopilot/autonomy cycles and factory runs skip
  LLM-backed work and record why. **Already-approved actions always still
  execute** (kernel invariant: budget pressure never blocks an approved
  external mutation). Interactive chat warns and requires confirmation to
  continue the turn.
- This is the mechanism that makes per-agent budgets real for the P6 trust
  ramp: a probation-period teammate gets a small daily budget by policy, and
  runaway loops self-throttle instead of surprising anyone.

### 3.7 Teammate tie-in (P6)

`project_id` is reserved on the ledger now. Once project scoping lands
(`SURGICAL_PLAN.md` P6.1), `myos usage report --project X` is the monthly
chargeback statement for the additional-team-member use case, and the trust
ramp's budget dials read from the same policy keys.

---

## 4. Implementation slices

| Slice | Scope | Fits |
| --- | --- | --- |
| 1 — Ledger + default backend | migration 44, `usage.py`, price map, capture in `claude.py`/`claude_sdk.py`, `myos usage report/show`, `prices list` | Inside SURGICAL_PLAN P0–P1 window (~2–3 days) |
| 2 — Governance + surfaces | budget policies, today/digest/receipt surfaces, weekly-review section | With P2 (`myos today`) (~1–2 days) |
| 3 — Subprocess + estimation + chargeback | zero/factory events, agent_cli estimation, tiny-model events, `--project` rollups | With P4/P6 (~2 days) |

Testing: offline fake responses with synthetic usage objects; golden rollup
math including cache multipliers (a cached-heavy turn must compute *cheaper*
than a naive estimate, not more expensive); budget-gate tests proving
approved actions execute while over budget; price-map validation test
(every packaged model id parses, rates > 0, versioned).

## 5. Acceptance criteria

- Every chat turn / autopilot cycle / factory run with the default backend
  produces exactly one `llm_usage_events` row with exact token counts
  (`estimated=0`).
- `myos usage report --by day` reconciles with the provider's own dashboard
  within tolerance (cache categories separated correctly).
- Over-budget state pauses proposing but never blocks execution of approved
  actions (test-enforced).
- Zero-run events appear with both self-reported and computed cost.

## 6. Sources

- [Prompt caching — Claude Platform Docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) (cache write 1.25×/2×, read 0.1×)
- [Using the Messages API — Claude Platform Docs](https://platform.claude.com/docs/en/build-with-claude/working-with-messages)
- [Streaming — Claude Platform Docs](https://platform.claude.com/docs/en/build-with-claude/streaming) (cumulative `usage` in `message_delta`)
- [Count tokens in a Message — Claude API Reference](https://platform.claude.com/docs/en/api/messages/count_tokens) and [Token counting guide](https://platform.claude.com/docs/en/build-with-claude/token-counting)
- [Reviewing API usage and costs — OpenAI Help Center](https://help.openai.com/articles/10478918)
- [Prompt caching guide — OpenAI API docs](https://developers.openai.com/api/docs/guides/prompt-caching) (`cached_tokens`, 128-token increments)
- [How to count tokens with Tiktoken — OpenAI Cookbook](https://developers.openai.com/cookbook/examples/how_to_count_tokens_with_tiktoken); [openai/tiktoken](https://github.com/openai/tiktoken)
- [LiteLLM routing docs — Anthropic count_tokens](https://docs.litellm.ai/docs/anthropic_count_tokens); [LiteLLM issue #27763](https://github.com/BerriAI/litellm/issues/27763) (proxies dropping cache fields)
- [Count Tokens API on Bedrock — AWS announcement](https://aws.amazon.com/about-aws/whats-new/2025/08/count-tokens-api-anthropics-claude-models-bedrock/)

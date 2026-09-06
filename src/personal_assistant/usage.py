"""LLM usage ledger — append-only token/cost recording (docs/COST_OBSERVABILITY.md).

One row per logical LLM call (a chat turn with its tool-loop iterations is ONE
row with ``requests`` > 1). Recording is observe-only and must never break the
call it observes — every entry point swallows exceptions, following the
``agent_cli._audit`` precedent. Cost is computed from the packaged price map
(``prices.py``); unknown models record tokens with a NULL cost rather than
guessing. Budget policy keys live in ``assistant_policies`` and only ever gate
*unattended proposing* — nothing here touches approval or execution.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import observability, prices

DAILY_BUDGET_KEY = "usage_budget_daily_millicents"
MONTHLY_BUDGET_KEY = "usage_budget_monthly_millicents"
WARN_THRESHOLD_KEY = "usage_warn_threshold_pct"

# Zero executor stream events report camelCase token keys (zero_executor.py);
# Anthropic reports the snake_case billing categories.
_TOKEN_KEYS = {
    "input_tokens": ("input_tokens", "promptTokens"),
    "output_tokens": ("output_tokens", "completionTokens"),
    "cache_write_tokens": ("cache_write_tokens", "cache_creation_input_tokens", "cacheCreationInputTokens"),
    "cache_read_tokens": ("cache_read_tokens", "cache_read_input_tokens", "cacheReadInputTokens"),
    "reasoning_tokens": ("reasoning_tokens",),
}


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _tokens_from_usage(usage: Mapping[str, Any] | None) -> dict[str, int]:
    usage = usage or {}
    tokens: dict[str, int] = {}
    for canonical, aliases in _TOKEN_KEYS.items():
        tokens[canonical] = next((_int(usage[alias]) for alias in aliases if usage.get(alias) is not None), 0)
    return tokens


def _self_reported_cost_millicents(usage: Mapping[str, Any] | None) -> int | None:
    """External executors self-report dollars (``costUsd``); keep it verbatim,
    converted to millicents, next to our own computed figure."""
    usage = usage or {}
    for key in ("costUsd", "cost_usd"):
        raw = usage.get(key)
        if raw is None:
            continue
        try:
            return round(float(raw) * 100_000)
        except (TypeError, ValueError):
            continue
    return None


def record(
    conn,
    *,
    backend: str,
    model: str | None,
    purpose: str = "",
    usage: Mapping[str, Any] | None = None,
    estimated: bool = False,
    latency_ms: int | None = None,
    persona: str | None = None,
    pack: str | None = None,
    surface: str | None = None,
    command: str | None = None,
    agent_task_id: int | None = None,
    agent_run_id: int | None = None,
    factory_run_id: int | None = None,
    project_id: int | None = None,
    correlation_id: str | None = None,
    commit: bool = True,
) -> int | None:
    """Insert one ``llm_usage_events`` row. Returns the row id, or None when
    recording failed (by design: callers never need to handle a failure).

    Commits by default so a usage row survives even if the caller's later work
    rolls back — the tokens were consumed externally either way. Pass
    ``commit=False`` where the caller owns the transaction boundary and has
    in-flight writes that must not be persisted early.
    """
    try:
        tokens = _tokens_from_usage(usage)
        rates, price_version = prices.rate_for(model)
        cost = prices.compute_cost_millicents(rates, tokens)
        correlation = correlation_id or observability.current_correlation_id() or None
        cursor = conn.execute(
            """
            INSERT INTO llm_usage_events (
                correlation_id, surface, command, agent_task_id, agent_run_id,
                factory_run_id, persona, pack, project_id, backend, model,
                purpose, requests, input_tokens, output_tokens,
                cache_write_tokens, cache_read_tokens, reasoning_tokens,
                estimated, cost_millicents, self_reported_cost_millicents,
                price_version, latency_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                correlation,
                surface,
                command,
                agent_task_id,
                agent_run_id,
                factory_run_id,
                persona,
                pack,
                project_id,
                backend,
                model or "unknown",
                purpose,
                max(1, _int(usage.get("requests")) if usage else 1),
                tokens["input_tokens"],
                tokens["output_tokens"],
                tokens["cache_write_tokens"],
                tokens["cache_read_tokens"],
                tokens["reasoning_tokens"],
                1 if estimated else 0,
                cost,
                _self_reported_cost_millicents(usage),
                price_version or None,
                _int(latency_ms) if latency_ms is not None else None,
            ),
        )
        if commit:
            conn.commit()
        return int(cursor.lastrowid) if cursor.lastrowid is not None else None
    except Exception:  # noqa: BLE001 — auditing must never break the observed call
        return None


def budget_status(conn) -> dict[str, object]:
    """Current spend vs the configured daily/monthly budgets.

    Only rows with a known cost count toward spend; a NULL-cost row (unknown
    model price) cannot trip a budget — tokens are still reported through the
    ``usage report`` surface.
    """
    from .privacy import get_policy_map

    policy = get_policy_map(conn)

    def _policy_int(key: str) -> int:
        try:
            return int(str(policy.get(key, "0")).strip())
        except (TypeError, ValueError):
            return 0

    daily_budget = _policy_int(DAILY_BUDGET_KEY)
    monthly_budget = _policy_int(MONTHLY_BUDGET_KEY)
    warn_threshold = min(100, max(1, _policy_int(WARN_THRESHOLD_KEY) or 80))
    daily_spent = int(
        conn.execute(
            "SELECT COALESCE(SUM(cost_millicents), 0) FROM llm_usage_events WHERE created_at >= date('now')"
        ).fetchone()[0]
    )
    monthly_spent = int(
        conn.execute(
            "SELECT COALESCE(SUM(cost_millicents), 0) FROM llm_usage_events "
            "WHERE created_at >= date('now', 'start of month')"
        ).fetchone()[0]
    )

    reasons: list[str] = []
    state = "ok"
    if daily_budget > 0 and daily_spent >= daily_budget:
        reasons.append(f"daily usage budget reached ({daily_spent}/{daily_budget} millicents)")
    if monthly_budget > 0 and monthly_spent >= monthly_budget:
        reasons.append(f"monthly usage budget reached ({monthly_spent}/{monthly_budget} millicents)")
    if reasons:
        state = "exceeded"
    else:
        worst = 0
        if daily_budget > 0:
            worst = max(worst, daily_spent * 100 // daily_budget)
        if monthly_budget > 0:
            worst = max(worst, monthly_spent * 100 // monthly_budget)
        if worst >= warn_threshold:
            reasons.append(f"usage at {worst}% of budget")
            state = "warn"
    return {
        "state": state,
        "reasons": reasons,
        "daily_budget_millicents": daily_budget,
        "daily_spent_millicents": daily_spent,
        "monthly_budget_millicents": monthly_budget,
        "monthly_spent_millicents": monthly_spent,
        "warn_threshold_pct": warn_threshold,
    }


def proposing_allowed(conn) -> tuple[bool, str]:
    """Whether unattended LLM-backed proposing may run right now.

    Deliberately narrow: only the *hard* "exceeded" state gates, and only
    proposing loops (autopilot/autonomy). Interactive chat warns instead, and
    nothing here ever blocks execution of an already-approved action.
    """
    status = budget_status(conn)
    if status["state"] == "exceeded":
        return False, "; ".join(str(reason) for reason in status["reasons"])
    return True, ""


def cleanup_usage_events(conn, *, retention_days: int = 180, max_rows: int = 200_000) -> dict[str, int]:
    """Bound the ledger: drop rows older than retention, then the oldest rows
    beyond max_rows. Follows the observability cleanup contract shape."""
    deleted = 0
    cutoff = conn.execute("SELECT date('now', ?)", (f"-{int(retention_days)} days",)).fetchone()[0]
    cursor = conn.execute("DELETE FROM llm_usage_events WHERE created_at < ?", (cutoff,))
    deleted += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    total = int(conn.execute("SELECT COUNT(*) FROM llm_usage_events").fetchone()[0])
    if total > max_rows:
        cursor = conn.execute(
            "DELETE FROM llm_usage_events WHERE id IN ("
            "SELECT id FROM llm_usage_events ORDER BY id ASC LIMIT ?)",
            (total - max_rows,),
        )
        deleted += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    conn.commit()
    return {"deleted": deleted, "remaining": int(conn.execute("SELECT COUNT(*) FROM llm_usage_events").fetchone()[0])}

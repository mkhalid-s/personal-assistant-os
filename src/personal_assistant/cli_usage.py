"""``myos usage`` / ``myos prices`` CLI handlers — the read surface for the
LLM usage ledger (docs/COST_OBSERVABILITY.md).

Split out of ``cli.py`` per the repo's thin-dispatcher convention. Every
handler wraps its body in ``with connection() as conn:`` so the connection
lifetime is bounded to the invocation.

JSON envelopes:

- ``myos.usage.report.v1`` — grouped rollup for ``myos usage report``.
- ``myos.usage.show.v1``  — raw ledger rows for one correlation id.
- ``myos.usage.cleanup.v1`` — retention outcome for ``myos usage cleanup``.
- ``myos.prices.v1`` — the packaged price map plus coverage warnings.

All surfaces emit a schema-stable error envelope on the exit-1 path so
automation consumers never see a bare traceback.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from . import prices, usage
from .db import connection

USAGE_REPORT_SCHEMA = "myos.usage.report.v1"
USAGE_SHOW_SCHEMA = "myos.usage.show.v1"
USAGE_CLEANUP_SCHEMA = "myos.usage.cleanup.v1"
PRICES_SCHEMA = "myos.prices.v1"

_GROUP_COLUMNS = {
    "backend": "backend",
    "model": "model",
    "persona": "COALESCE(persona, '(none)')",
    "purpose": "COALESCE(purpose, '(none)')",
    "day": "substr(created_at, 1, 10)",
}

_TOKEN_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "reasoning_tokens",
)


def _emit_error(schema: str, error: str, *, details: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"schema": schema, "error": error}
    if details:
        payload.update(details)
    print(json.dumps(payload, ensure_ascii=True))


def _fmt_cost(millicents: int | None) -> str:
    if millicents is None:
        return "unknown"
    return f"${millicents / 100_000:.4f}"


def _since_clause(args: argparse.Namespace) -> tuple[str, list[Any]]:
    if getattr(args, "today", False):
        return "created_at >= date('now')", []
    days = max(1, int(getattr(args, "since", 7) or 7))
    return "created_at >= datetime('now', ?)", [f"-{days} days"]


def _report_rows(conn, group_by: str, where: str, values: list[Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    column = _GROUP_COLUMNS[group_by]
    rows = conn.execute(
        f"""
        SELECT {column} AS grp,
               SUM(requests) AS requests,
               SUM(input_tokens) AS input_tokens,
               SUM(output_tokens) AS output_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens,
               SUM(cache_read_tokens) AS cache_read_tokens,
               SUM(reasoning_tokens) AS reasoning_tokens,
               SUM(estimated) AS estimated_requests,
               SUM(cost_millicents) AS cost_millicents,
               SUM(self_reported_cost_millicents) AS self_reported_cost_millicents,
               SUM(CASE WHEN cost_millicents IS NULL THEN 1 ELSE 0 END) AS unknown_cost_events
        FROM llm_usage_events
        WHERE {where}
        GROUP BY grp
        ORDER BY requests DESC
        """,
        values,
    ).fetchall()
    entries = [
        {
            "group": row["grp"],
            "requests": int(row["requests"] or 0),
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "cache_write_tokens": int(row["cache_write_tokens"] or 0),
            "cache_read_tokens": int(row["cache_read_tokens"] or 0),
            "reasoning_tokens": int(row["reasoning_tokens"] or 0),
            "estimated_requests": int(row["estimated_requests"] or 0),
            "cost_millicents": int(row["cost_millicents"]) if row["cost_millicents"] is not None else None,
            "self_reported_cost_millicents": (
                int(row["self_reported_cost_millicents"])
                if row["self_reported_cost_millicents"] is not None
                else None
            ),
            "unknown_cost_events": int(row["unknown_cost_events"] or 0),
        }
        for row in rows
    ]
    totals = {
        key: sum(entry[key] or 0 for entry in entries if isinstance(entry[key], int))
        for key in (
            "requests",
            "input_tokens",
            "output_tokens",
            "cache_write_tokens",
            "cache_read_tokens",
            "reasoning_tokens",
        )
    }
    totals["estimated_requests"] = sum(entry["estimated_requests"] for entry in entries)
    totals["cost_millicents"] = sum(
        entry["cost_millicents"] for entry in entries if entry["cost_millicents"] is not None
    )
    totals["unknown_cost_events"] = sum(entry["unknown_cost_events"] for entry in entries)
    return entries, totals


def cmd_usage_report(args: argparse.Namespace) -> None:
    group_by = str(getattr(args, "by", "backend"))
    with connection() as conn:
        where, values = _since_clause(args)
        entries, totals = _report_rows(conn, group_by, where, values)
    if getattr(args, "json", False):
        payload = {
            "schema": USAGE_REPORT_SCHEMA,
            "group_by": group_by,
            "today": bool(getattr(args, "today", False)),
            "since_days": None if getattr(args, "today", False) else max(1, int(getattr(args, "since", 7) or 7)),
            "rows": entries,
            "totals": totals,
        }
        print(json.dumps(payload, ensure_ascii=True))
        return
    if not entries:
        print("No LLM usage recorded in the selected window.")
        return
    label = "today" if getattr(args, "today", False) else f"last {max(1, int(getattr(args, 'since', 7) or 7))} days"
    print(f"LLM usage by {group_by} ({label}):")
    header = f"{'group':<32} {'req':>5} {'in':>9} {'out':>9} {'cache_r':>9} {'est':>4} {'cost':>10}"
    print(header)
    for entry in entries:
        print(
            f"{str(entry['group'])[:31]:<32} {entry['requests']:>5} {entry['input_tokens']:>9} "
            f"{entry['output_tokens']:>9} {entry['cache_read_tokens']:>9} {entry['estimated_requests']:>4} "
            f"{_fmt_cost(entry['cost_millicents']):>10}"
        )
    print(
        f"{'TOTAL':<32} {totals['requests']:>5} {totals['input_tokens']:>9} "
        f"{totals['output_tokens']:>9} {totals['cache_read_tokens']:>9} {totals['estimated_requests']:>4} "
        f"{_fmt_cost(totals['cost_millicents']):>10}"
    )
    if totals.get("unknown_cost_events"):
        print(
            f"  note: {totals['unknown_cost_events']} event(s) have unknown price "
            "(model not in myos prices map) — token counts still included."
        )


def _usage_event_entry(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "correlation_id": row["correlation_id"],
        "surface": row["surface"],
        "command": row["command"],
        "backend": row["backend"],
        "model": row["model"],
        "purpose": row["purpose"],
        "requests": int(row["requests"]),
        "input_tokens": int(row["input_tokens"]),
        "output_tokens": int(row["output_tokens"]),
        "cache_write_tokens": int(row["cache_write_tokens"]),
        "cache_read_tokens": int(row["cache_read_tokens"]),
        "reasoning_tokens": int(row["reasoning_tokens"]),
        "estimated": bool(row["estimated"]),
        "cost_millicents": row["cost_millicents"],
        "self_reported_cost_millicents": row["self_reported_cost_millicents"],
        "price_version": row["price_version"],
        "latency_ms": row["latency_ms"],
        "persona": row["persona"],
        "created_at": row["created_at"],
    }


def cmd_usage_show(args: argparse.Namespace) -> None:
    correlation = str(getattr(args, "correlation_id", "") or "").strip()
    if not correlation:
        if getattr(args, "json", False):
            _emit_error(USAGE_SHOW_SCHEMA, "invalid_request", details={"message": "correlation_id is required"})
        else:
            print("Usage error: a correlation id is required (see `myos trace list`).")
        raise SystemExit(1)
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM llm_usage_events WHERE correlation_id = ? ORDER BY id ASC",
            (correlation,),
        ).fetchall()
    if not rows:
        if getattr(args, "json", False):
            _emit_error(USAGE_SHOW_SCHEMA, "not_found", details={"correlation_id": correlation})
        else:
            print(f"No usage events found for {correlation}.")
        raise SystemExit(1)
    if getattr(args, "json", False):
        payload = {
            "schema": USAGE_SHOW_SCHEMA,
            "correlation_id": correlation,
            "count": len(rows),
            "events": [_usage_event_entry(row) for row in rows],
        }
        print(json.dumps(payload, ensure_ascii=True))
        return
    print(f"Usage events for {correlation}:")
    for row in rows:
        entry = _usage_event_entry(row)
        est = " (estimated)" if entry["estimated"] else ""
        self_rep = (
            f" self-reported {_fmt_cost(entry['self_reported_cost_millicents'])}"
            if entry["self_reported_cost_millicents"] is not None
            else ""
        )
        print(
            f"- #{entry['id']} [{entry['backend']}/{entry['model']}] purpose={entry['purpose'] or '-'} "
            f"req={entry['requests']} in={entry['input_tokens']} out={entry['output_tokens']} "
            f"cache_r={entry['cache_read_tokens']} cost={_fmt_cost(entry['cost_millicents'])}{self_rep}{est}"
        )


def cmd_usage_cleanup(args: argparse.Namespace) -> None:
    retention_days = max(1, int(getattr(args, "retention_days", 180) or 180))
    max_rows = max(1, int(getattr(args, "max_rows", 200_000) or 200_000))
    with connection() as conn:
        outcome = usage.cleanup_usage_events(conn, retention_days=retention_days, max_rows=max_rows)
    if getattr(args, "json", False):
        payload = {
            "schema": USAGE_CLEANUP_SCHEMA,
            "retention_days": retention_days,
            "max_rows": max_rows,
            **outcome,
        }
        print(json.dumps(payload, ensure_ascii=True))
        return
    print(f"Usage cleanup: deleted {outcome['deleted']} row(s), {outcome['remaining']} remaining.")


def cmd_prices_list(args: argparse.Namespace) -> None:
    price_map = prices.load_price_map(refresh=True)
    version = str(price_map.get("version", ""))
    with connection() as conn:
        uncovered = [
            row["model"]
            for row in conn.execute(
                "SELECT DISTINCT model FROM llm_usage_events WHERE cost_millicents IS NULL ORDER BY model ASC LIMIT 25"
            )
        ]
    models = []
    for model, rates in sorted(price_map.get("per_mtok", {}).items()):
        models.append({"model": model, **{key: int(rates.get(key, 0)) for key in
                                          ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")}})
    if getattr(args, "json", False):
        payload = {
            "schema": PRICES_SCHEMA,
            "version": version,
            "currency": str(price_map.get("currency", "usd")),
            "models": models,
            "uncovered_models": uncovered,
        }
        print(json.dumps(payload, ensure_ascii=True))
        return
    print(f"Price map {version} (millicents per million tokens; 100,000 = $1):")
    for entry in models:
        print(
            f"- {entry['model']}: in={entry['input']} out={entry['output']} "
            f"cache_w={entry['cache_write_5m']}/{entry['cache_write_1h']} cache_r={entry['cache_read']}"
        )
    if uncovered:
        print("Models seen in the ledger without a price (cost recorded as unknown):")
        for model in uncovered:
            print(f"- {model}")
    else:
        print("All ledger models have a known price.")


def cmd_usage_dispatch(args: argparse.Namespace) -> None:
    """Dispatch entry for ``myos usage …`` subcommands."""
    action = getattr(args, "usage_action", None) or "report"
    handler = {
        "report": cmd_usage_report,
        "show": cmd_usage_show,
        "cleanup": cmd_usage_cleanup,
    }.get(action)
    if handler is None:
        print(f"Unknown usage action: {action}")
        raise SystemExit(2)
    handler(args)


__all__ = [
    "PRICES_SCHEMA",
    "USAGE_CLEANUP_SCHEMA",
    "USAGE_REPORT_SCHEMA",
    "USAGE_SHOW_SCHEMA",
    "cmd_prices_list",
    "cmd_usage_cleanup",
    "cmd_usage_dispatch",
    "cmd_usage_report",
    "cmd_usage_show",
]

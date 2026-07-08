"""``myos remind`` / ``myos scheduler`` CLI handlers (slices S2 / S4).

Split out of ``cli.py`` so the parser stays thin and the daily-tier
reminder surface can grow (recurrence, English phrasings, chat inline
proposals) without bloating the god-file further. Every handler wraps
its body in ``with connection() as conn:`` so the connection lifetime
is bounded to the invocation — matches the leak-free pattern the rest
of the CLI already follows.

JSON envelopes (published in ``ARCHITECTURE.md`` in a follow-up slice):

- ``myos.reminder.v1`` — single reminder (create/complete/snooze/cancel).
- ``myos.reminder.list.v1`` — list surface for ``myos remind list``.

All write-side surfaces emit a schema-stable error envelope on the
exit-1 path so automation consumers never see a bare traceback.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from . import reminders
from .db import connection

REMINDER_SCHEMA = "myos.reminder.v1"
REMINDER_LIST_SCHEMA = "myos.reminder.list.v1"


def _reminder_entry(row: dict[str, Any]) -> dict[str, Any]:
    """Snapshot of a reminder row for JSON output. Same field set as the
    ``reminders.get`` dict; separated so future presentation-only fields
    (age, human-readable ETA) can be added here without touching the
    storage helper."""
    return dict(row)


def _print_reminder_text(row: dict[str, Any]) -> None:
    print(f"Reminder #{row['id']} [{row['status']}] kind={row['kind']} at={row['scheduled_at']}\n  text: {row['text']}")
    if row.get("source_ref"):
        print(f"  source: {row['source_ref']}")
    if row.get("snoozed_until"):
        print(f"  previously scheduled: {row['snoozed_until']}")
    if row.get("fired_at"):
        print(f"  fired: {row['fired_at']}")
    if row.get("completed_at"):
        print(f"  completed: {row['completed_at']}")


def _emit_error(schema: str, error: str, *, details: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"schema": schema, "error": error}
    if details:
        payload.update(details)
    print(json.dumps(payload, ensure_ascii=True))


def cmd_remind_create(args: argparse.Namespace) -> None:
    """``myos remind "text" --at <when> [--kind …] [--json]``."""
    with connection() as conn:
        try:
            rid = reminders.create(
                conn,
                args.text,
                args.at,
                kind=getattr(args, "kind", None),
                source_ref=getattr(args, "source_ref", None) or None,
            )
        except reminders.ReminderError as exc:
            conn.commit()
            if getattr(args, "json", False):
                _emit_error(REMINDER_SCHEMA, "invalid_request", details={"message": str(exc)})
            else:
                print(f"Reminder rejected: {exc}")
            raise SystemExit(1) from exc
        row = reminders.get(conn, rid)
        conn.commit()
    assert row is not None  # freshly inserted row is always readable
    if getattr(args, "json", False):
        print(json.dumps({"schema": REMINDER_SCHEMA, "reminder": _reminder_entry(row)}, ensure_ascii=True))
        return
    _print_reminder_text(row)


def cmd_remind_list(args: argparse.Namespace) -> None:
    """``myos remind list [--due-only] [--limit N] [--json]``."""
    with connection() as conn:
        rows = reminders.list_pending(
            conn,
            limit=int(getattr(args, "limit", 50) or 50),
            due_only=bool(getattr(args, "due_only", False)),
        )
    if getattr(args, "json", False):
        payload = {
            "schema": REMINDER_LIST_SCHEMA,
            "count": len(rows),
            "limit": int(getattr(args, "limit", 50) or 50),
            "filter": {"due_only": bool(getattr(args, "due_only", False))},
            "entries": [_reminder_entry(row) for row in rows],
        }
        print(json.dumps(payload, ensure_ascii=True))
        return
    if not rows:
        print("No pending reminders." if not getattr(args, "due_only", False) else "No due reminders.")
        return
    print("Reminders (pending):" if not getattr(args, "due_only", False) else "Reminders (due):")
    for row in rows:
        print(f"- #{row['id']} [{row['kind']}] at {row['scheduled_at']}: {row['text']}")


def cmd_remind_complete(args: argparse.Namespace) -> None:
    with connection() as conn:
        row = reminders.mark_done(conn, int(args.id))
        conn.commit()
    if row is None:
        if getattr(args, "json", False):
            _emit_error(REMINDER_SCHEMA, "not_found", details={"id": int(args.id)})
        else:
            print(f"Reminder #{args.id} not found or already terminal.")
        raise SystemExit(1)
    if getattr(args, "json", False):
        print(json.dumps({"schema": REMINDER_SCHEMA, "reminder": _reminder_entry(row)}, ensure_ascii=True))
        return
    _print_reminder_text(row)


def cmd_remind_snooze(args: argparse.Namespace) -> None:
    with connection() as conn:
        try:
            delta = reminders.parse_duration(args.for_)
        except reminders.ReminderError as exc:
            if getattr(args, "json", False):
                _emit_error(REMINDER_SCHEMA, "invalid_request", details={"message": str(exc)})
            else:
                print(f"Snooze rejected: {exc}")
            raise SystemExit(1) from exc
        row = reminders.snooze(conn, int(args.id), for_delta=delta)
        conn.commit()
    if row is None:
        if getattr(args, "json", False):
            _emit_error(REMINDER_SCHEMA, "not_found", details={"id": int(args.id)})
        else:
            print(f"Reminder #{args.id} not found or not in a snoozeable state.")
        raise SystemExit(1)
    if getattr(args, "json", False):
        print(json.dumps({"schema": REMINDER_SCHEMA, "reminder": _reminder_entry(row)}, ensure_ascii=True))
        return
    _print_reminder_text(row)


def cmd_remind_cancel(args: argparse.Namespace) -> None:
    with connection() as conn:
        row = reminders.cancel(conn, int(args.id))
        conn.commit()
    if row is None:
        if getattr(args, "json", False):
            _emit_error(REMINDER_SCHEMA, "not_found", details={"id": int(args.id)})
        else:
            print(f"Reminder #{args.id} not found or already terminal.")
        raise SystemExit(1)
    if getattr(args, "json", False):
        print(json.dumps({"schema": REMINDER_SCHEMA, "reminder": _reminder_entry(row)}, ensure_ascii=True))
        return
    _print_reminder_text(row)


def cmd_remind_dispatch(args: argparse.Namespace) -> None:
    """Dispatch entry for ``myos remind …`` subcommands.

    argparse routes to this function via ``set_defaults(func=…)`` when
    the top-level ``remind`` parser dispatches — the sub-action lives on
    ``args.remind_action`` and defaults to ``create`` so ``myos remind
    "text" --at 15:00`` behaves as a create.
    """
    action = getattr(args, "remind_action", None) or "create"
    handler = {
        "create": cmd_remind_create,
        "list": cmd_remind_list,
        "complete": cmd_remind_complete,
        "snooze": cmd_remind_snooze,
        "cancel": cmd_remind_cancel,
    }.get(action)
    if handler is None:
        print(f"Unknown remind action: {action}")
        raise SystemExit(2)
    handler(args)


__all__ = [
    "REMINDER_LIST_SCHEMA",
    "REMINDER_SCHEMA",
    "cmd_remind_cancel",
    "cmd_remind_complete",
    "cmd_remind_create",
    "cmd_remind_dispatch",
    "cmd_remind_list",
    "cmd_remind_snooze",
]

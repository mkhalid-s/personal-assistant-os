"""Durable wall-clock reminders (slice S1).

MYOS-native reminders live in their own ``reminders`` table (schema
migration 39) so the scheduler tick can walk a bounded, single-column
index (``idx_reminders_status_time``) instead of every ``work_items`` /
``inbox_items`` row when it fires on the launchd 60s cadence.

This module is intentionally dependency-light (``db`` + ``privacy`` only)
so the CLI surface (``cli_reminders``), the notification pipeline
(``notify``), and the eventual router / chat integrations can import it
freely without creating an import cycle.

Time semantics
--------------
- ``scheduled_at`` is always stored as ISO 8601 UTC, i.e. the format
  ``YYYY-MM-DDTHH:MM:SS+00:00``. String comparison against
  ``datetime.now(UTC).isoformat()`` is order-preserving, so the
  scheduler tick's ``WHERE status = 'pending' AND scheduled_at <= ?``
  works without any timezone math.
- ``parse_when`` accepts three deterministic shapes only (per the S1
  scope): ``HH:MM`` (today, or tomorrow if the wall-clock time already
  passed), ``+Nm`` / ``+Nh`` (relative offset), and ISO 8601 datetime
  (with or without a timezone; naive is treated as local time and
  converted to UTC). English phrases and recurrence are deferred to
  follow-up slices.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import append_event
from .privacy import apply_privacy_filters

REMINDER_KINDS = ("followup", "standup", "meeting", "task")
DEFAULT_KIND = "followup"

STATUS_PENDING = "pending"
STATUS_FIRED = "fired"
STATUS_DONE = "done"
STATUS_SNOOZED = "snoozed"
STATUS_CANCELLED = "cancelled"

_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_OFFSET_RE = re.compile(r"^\+(\d{1,4})([mh])$")
_DURATION_RE = re.compile(r"^(\d{1,4})([mh])$")


class ReminderError(ValueError):
    """Raised by ``parse_when`` / ``parse_duration`` and the write helpers
    on invalid input.

    Deliberately subclasses ``ValueError`` so callers that only care
    about "was the input bad?" can catch that base class and get
    consistent behavior across ``parse_when``, ``create``, and the
    argparse ``type=`` slot.
    """


# ---------------------------------------------------------------- time


def _to_utc_iso(dt: datetime) -> str:
    """Render *dt* as an ISO 8601 UTC string with an explicit ``+00:00``.

    Naive datetimes are treated as local time (matches the CLI user
    expectation that ``--at 15:00`` means "3pm on my clock"). Non-UTC
    aware datetimes are converted to UTC.
    """
    if dt.tzinfo is None:
        dt = dt.astimezone()  # attach local tz
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_when(raw: str, *, now: datetime | None = None) -> str:
    """Parse a user-facing ``--at`` value into an ISO 8601 UTC timestamp.

    Accepts:

    - ``HH:MM`` — today at that wall-clock time in the local timezone.
      If the time has already passed today, rolls forward to tomorrow
      (matches the "remind me at 5pm" mental model when it's 6pm).
    - ``+Nm`` / ``+Nh`` — relative offset from *now*.
    - ISO 8601 datetime — parsed via ``datetime.fromisoformat``. Naive
      values are treated as local time. ``Z`` suffix is accepted as a
      synonym for ``+00:00`` for user convenience.

    *now* is injected for tests so parser edge cases (past ``HH:MM``,
    boundary rollovers) are deterministic. Production callers should
    leave it unset.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ReminderError("reminder time is required")

    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        # Bare-naive ``now`` is ambiguous; treat as local time so tests
        # can pass a natural ``datetime(2026, 7, 8, 15, 0)`` without
        # worrying about the local tz. Convert to UTC-aware for math.
        now = now.astimezone()

    now_local = now.astimezone()

    match = _HHMM_RE.match(raw)
    if match:
        hh = int(match.group(1))
        mm = int(match.group(2))
        candidate = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now_local:
            candidate = candidate + timedelta(days=1)
        return _to_utc_iso(candidate)

    match = _OFFSET_RE.match(raw)
    if match:
        magnitude = int(match.group(1))
        unit = match.group(2)
        delta = timedelta(minutes=magnitude) if unit == "m" else timedelta(hours=magnitude)
        return _to_utc_iso(now + delta)

    # ISO 8601 — accept "Z" as a friendly alias for "+00:00".
    iso_candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(iso_candidate)
    except ValueError as exc:
        raise ReminderError(
            f"unrecognized reminder time {raw!r}: expected HH:MM, +Nm/+Nh, or ISO 8601 datetime"
        ) from exc
    return _to_utc_iso(parsed)


def parse_duration(raw: str) -> timedelta:
    """Parse a snooze duration (``Nm`` / ``Nh`` shape without the plus).

    ``myos remind snooze --for 30m`` should feel natural, so we accept
    ``30m`` / ``2h`` here (no leading ``+``) — the caller has already
    committed to "add this delta to the existing schedule".
    """
    raw = (raw or "").strip()
    if not raw:
        raise ReminderError("snooze duration is required")
    match = _DURATION_RE.match(raw)
    if not match:
        raise ReminderError(f"unrecognized duration {raw!r}: expected Nm or Nh")
    magnitude = int(match.group(1))
    return timedelta(minutes=magnitude) if match.group(2) == "m" else timedelta(hours=magnitude)


# ---------------------------------------------------------------- crud


def _normalize_kind(kind: str | None) -> str:
    kind = (kind or DEFAULT_KIND).strip().lower()
    if kind not in REMINDER_KINDS:
        raise ReminderError(f"unknown reminder kind {kind!r}; expected one of {sorted(REMINDER_KINDS)}")
    return kind


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "text": row["text"],
        "scheduled_at": row["scheduled_at"],
        "status": row["status"],
        "kind": row["kind"],
        "source_ref": row["source_ref"],
        "correlation_id": row["correlation_id"],
        "created_at": row["created_at"],
        "fired_at": row["fired_at"],
        "completed_at": row["completed_at"],
        "snoozed_until": row["snoozed_until"],
    }


def create(
    conn: sqlite3.Connection,
    text: str,
    when: str,
    *,
    kind: str | None = None,
    source_ref: str | None = None,
    correlation_id: str | None = None,
    now: datetime | None = None,
) -> int:
    """Create a new reminder and return its row id.

    ``text`` is redacted through ``apply_privacy_filters`` before insert
    (matches the ``em.py`` chokepoint pattern — every write through this
    module gets the same PII treatment as any other stored user text).
    """
    text = (text or "").strip()
    if not text:
        raise ReminderError("reminder text is required")
    kind = _normalize_kind(kind)
    scheduled_at = parse_when(when, now=now)
    filtered = apply_privacy_filters(conn, text)
    cur = conn.execute(
        """
        INSERT INTO reminders (text, scheduled_at, status, kind, source_ref, correlation_id)
        VALUES (?, ?, 'pending', ?, ?, ?)
        """,
        (filtered, scheduled_at, kind, source_ref, correlation_id),
    )
    rid = int(cur.lastrowid or 0)
    append_event(
        conn,
        "reminder_created",
        "reminder",
        rid,
        json.dumps({"kind": kind, "scheduled_at": scheduled_at, "source_ref": source_ref}),
    )
    return rid


def list_pending(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    due_only: bool = False,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return pending reminders ordered by ``scheduled_at`` ascending.

    ``due_only=True`` filters to rows whose ``scheduled_at`` is already
    in the past (i.e. the tick would fire them on its next run).
    """
    if due_only:
        cutoff = _to_utc_iso(now or datetime.now(UTC))
        rows = conn.execute(
            """
            SELECT * FROM reminders
            WHERE status = 'pending' AND scheduled_at <= ?
            ORDER BY scheduled_at ASC
            LIMIT ?
            """,
            (cutoff, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM reminders
            WHERE status = 'pending'
            ORDER BY scheduled_at ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def list_due(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Scheduler-facing helper: pending reminders whose time has arrived.

    Split from ``list_pending(due_only=True)`` so callers that expect a
    strict scheduler contract (return no more than *limit* rows, always
    the oldest first) don't have to pass a keyword argument.
    """
    return list_pending(conn, limit=limit, due_only=True, now=now)


def get(conn: sqlite3.Connection, reminder_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM reminders WHERE id = ?", (int(reminder_id),)).fetchone()
    return _row_to_dict(row) if row else None


def count_pending(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM reminders WHERE status = 'pending'").fetchone()
    return int(row["c"]) if row else 0


def next_scheduled_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT scheduled_at FROM reminders WHERE status = 'pending' ORDER BY scheduled_at ASC LIMIT 1"
    ).fetchone()
    return row["scheduled_at"] if row else None


def mark_fired(
    conn: sqlite3.Connection,
    reminder_id: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Move a reminder from ``pending`` to ``fired`` and stamp ``fired_at``.

    Returns the resulting row dict, or ``None`` if the reminder id was
    not found or was not ``pending`` (idempotent — a second call is a
    no-op so a crashed scheduler tick can safely retry).
    """
    fired_at = _to_utc_iso(now or datetime.now(UTC))
    cur = conn.execute(
        "UPDATE reminders SET status = 'fired', fired_at = ? WHERE id = ? AND status = 'pending'",
        (fired_at, int(reminder_id)),
    )
    if cur.rowcount == 0:
        return None
    append_event(
        conn,
        "reminder_fired",
        "reminder",
        int(reminder_id),
        json.dumps({"fired_at": fired_at}),
    )
    return get(conn, reminder_id)


def mark_done(
    conn: sqlite3.Connection,
    reminder_id: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    completed_at = _to_utc_iso(now or datetime.now(UTC))
    cur = conn.execute(
        """
        UPDATE reminders SET status = 'done', completed_at = ?
        WHERE id = ? AND status IN ('pending', 'fired', 'snoozed')
        """,
        (completed_at, int(reminder_id)),
    )
    if cur.rowcount == 0:
        return None
    append_event(
        conn,
        "reminder_completed",
        "reminder",
        int(reminder_id),
        json.dumps({"completed_at": completed_at}),
    )
    return get(conn, reminder_id)


def snooze(
    conn: sqlite3.Connection,
    reminder_id: int,
    *,
    for_delta: timedelta | None = None,
    until: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Push a reminder's ``scheduled_at`` further into the future.

    Callers pass exactly one of ``for_delta`` (relative) or ``until``
    (absolute). The row goes back to ``status='pending'`` — snooze is a
    reschedule, not a terminal state — with ``snoozed_until`` recording
    the previous fire attempt for audit.
    """
    if (for_delta is None) == (until is None):
        raise ReminderError("snooze requires exactly one of for_delta or until")
    row = get(conn, reminder_id)
    if row is None:
        return None
    if row["status"] not in {STATUS_PENDING, STATUS_FIRED, STATUS_SNOOZED}:
        return None
    base = now or datetime.now(UTC)
    if for_delta is not None:
        new_at_dt = base + for_delta
    else:
        assert until is not None
        new_at_dt = until
    new_at = _to_utc_iso(new_at_dt)
    prior_scheduled = row["scheduled_at"]
    conn.execute(
        """
        UPDATE reminders
        SET status = 'pending', scheduled_at = ?, snoozed_until = ?, fired_at = NULL
        WHERE id = ?
        """,
        (new_at, prior_scheduled, int(reminder_id)),
    )
    append_event(
        conn,
        "reminder_snoozed",
        "reminder",
        int(reminder_id),
        json.dumps({"prior_scheduled_at": prior_scheduled, "new_scheduled_at": new_at}),
    )
    return get(conn, reminder_id)


def cancel(conn: sqlite3.Connection, reminder_id: int) -> dict[str, Any] | None:
    cur = conn.execute(
        """
        UPDATE reminders SET status = 'cancelled'
        WHERE id = ? AND status IN ('pending', 'fired', 'snoozed')
        """,
        (int(reminder_id),),
    )
    if cur.rowcount == 0:
        return None
    append_event(conn, "reminder_cancelled", "reminder", int(reminder_id), "{}")
    return get(conn, reminder_id)


__all__ = [
    "DEFAULT_KIND",
    "REMINDER_KINDS",
    "ReminderError",
    "STATUS_CANCELLED",
    "STATUS_DONE",
    "STATUS_FIRED",
    "STATUS_PENDING",
    "STATUS_SNOOZED",
    "cancel",
    "count_pending",
    "create",
    "get",
    "list_due",
    "list_pending",
    "mark_done",
    "mark_fired",
    "next_scheduled_at",
    "parse_duration",
    "parse_when",
    "snooze",
]

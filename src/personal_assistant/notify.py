"""Notification pipeline (slice S3).

MYOS was documenting ``MYOS_NOTIFY_COMMAND`` in ``.env.example`` but
never actually invoking it. This module closes that gap by exposing a
single ``notify()`` entry point that dispatches through three channels
in priority order:

1. **Custom hook** — if ``MYOS_NOTIFY_COMMAND`` is set, the ``myos.notify.v1``
   JSON envelope is written to that command's stdin. Matches the README
   contract and lets users route to Slack, iOS push, ntfy.sh, etc.
2. **macOS native** — on ``darwin``, falls back to
   ``osascript -e 'display notification …'`` which shows a native
   Notification Center banner. No install required.
3. **Terminal** — everywhere else, prints the envelope to stdout so
   headless / CI / non-macOS Linux users still see something.

The three channels are *fallbacks*, not fan-out: the first available
channel handles the notification. Every dispatch, regardless of channel
outcome, writes an ``event_log`` row for audit and an ``inbox_items``
row (``kind='reminder'`` on success, ``kind='reminder_missed'`` on
failure) so a broken notification hook can never silently swallow a
scheduled reminder. That guarantee is the reason the scheduler tick
can be aggressive about firing due reminders without worrying about
push-channel outages.

The module is deliberately dependency-light (``db`` + ``privacy`` +
stdlib) so it can be called from both ``cli_reminders`` (scheduler
tick) and eventually from ``autopilot`` / ``cli_review`` (digest
delivery) without any import cycles.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

from .db import append_event
from .privacy import apply_privacy_filters

# ``datetime.UTC`` alias — added natively in CPython 3.11; we alias
# ``timezone.utc`` here so this module also runs on 3.10 (the project's
# lower Python bound per pyproject.toml).
UTC = timezone.utc

NOTIFY_SCHEMA = "myos.notify.v1"

CHANNEL_CUSTOM = "custom_command"
CHANNEL_OSASCRIPT = "osascript"
CHANNEL_NOTIFY_SEND = "notify_send"
CHANNEL_STDOUT = "stdout"

_HOOK_TIMEOUT_SECONDS = 5

_ALLOWED_URGENCY = frozenset({"low", "normal", "high"})
_ALLOWED_KIND = frozenset({"reminder", "digest", "approval", "risk", "generic"})


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sanitize_urgency(urgency: str | None) -> str:
    urgency = (urgency or "normal").strip().lower()
    return urgency if urgency in _ALLOWED_URGENCY else "normal"


def _sanitize_kind(kind: str | None) -> str:
    kind = (kind or "generic").strip().lower()
    return kind if kind in _ALLOWED_KIND else "generic"


def build_envelope(
    *,
    title: str,
    body: str,
    kind: str = "generic",
    urgency: str = "normal",
    correlation_id: str | None = None,
    source_ref: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Assemble a stable ``myos.notify.v1`` envelope.

    Split from ``notify()`` so tests can build fixture envelopes without
    hitting the dispatcher, and so future callers (chat inline
    notifications, autopilot digest) can serialize the envelope
    upstream without duplicating the schema.
    """
    return {
        "schema": NOTIFY_SCHEMA,
        "ts": now or _now_iso(),
        "title": title,
        "body": body,
        "kind": _sanitize_kind(kind),
        "urgency": _sanitize_urgency(urgency),
        "correlation_id": correlation_id,
        "source_ref": source_ref,
    }


def _dispatch_custom(envelope: dict[str, Any]) -> tuple[bool, str | None]:
    """Run the ``MYOS_NOTIFY_COMMAND`` hook with the envelope on stdin.

    Returns ``(dispatched, error)``. On non-zero exit or timeout we
    return ``(False, error)`` so the caller can record a
    ``reminder_missed`` audit row and (if configured) fall through to a
    lower-priority channel. We *never* re-raise: a broken hook must
    not crash the scheduler tick.
    """
    raw = os.getenv("MYOS_NOTIFY_COMMAND", "").strip()
    if not raw:
        return False, "MYOS_NOTIFY_COMMAND unset"
    try:
        parts = shlex.split(raw)
    except ValueError as exc:
        return False, f"shlex parse: {exc}"
    try:
        proc = subprocess.run(  # noqa: S603 - user-configured hook, intentional
            parts,
            input=json.dumps(envelope, ensure_ascii=True),
            capture_output=True,
            text=True,
            timeout=_HOOK_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        return False, f"hook not found: {exc}"
    except subprocess.TimeoutExpired:
        return False, f"hook timed out after {_HOOK_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return False, f"hook os error: {exc}"
    if proc.returncode != 0:
        stderr_snip = (proc.stderr or "").strip().splitlines()[:2]
        return False, f"hook exit={proc.returncode} stderr={' | '.join(stderr_snip)[:200]}"
    return True, None


def _dispatch_osascript(envelope: dict[str, Any]) -> tuple[bool, str | None]:
    """macOS Notification Center fallback via ``osascript``.

    Escapes double quotes in title/body so ``display notification`` is
    given a well-formed AppleScript expression. Only runs on darwin;
    the caller guards this.
    """
    title = (envelope.get("title") or "MYOS Reminder").replace('"', "'").replace("\\", "/")
    body = (envelope.get("body") or "").replace('"', "'").replace("\\", "/")
    script = f'display notification "{body}" with title "{title}"'
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no user shell interpolation
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=_HOOK_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        return False, f"osascript not found: {exc}"
    except subprocess.TimeoutExpired:
        return False, f"osascript timed out after {_HOOK_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return False, f"osascript os error: {exc}"
    if proc.returncode != 0:
        return False, f"osascript exit={proc.returncode} stderr={(proc.stderr or '')[:200]}"
    return True, None


def _dispatch_notify_send(envelope: dict[str, Any]) -> tuple[bool, str | None]:
    """Best-effort Linux desktop fallback via ``notify-send`` if present.

    Only tried when the user is on a non-darwin platform *and* the
    binary is on PATH; otherwise we fall through to stdout. Never
    surfaces an error visibly — a Linux user without notify-send is a
    perfectly reasonable state.
    """
    binary = shutil.which("notify-send")
    if not binary:
        return False, "notify-send not on PATH"
    title = envelope.get("title") or "MYOS"
    body = envelope.get("body") or ""
    urgency_arg = {"low": "low", "normal": "normal", "high": "critical"}.get(
        str(envelope.get("urgency") or "normal"),
        "normal",
    )
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, resolved via shutil.which
            [binary, "--urgency", urgency_arg, str(title), str(body)],
            capture_output=True,
            text=True,
            timeout=_HOOK_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return False, f"notify-send error: {exc}"
    if proc.returncode != 0:
        return False, f"notify-send exit={proc.returncode}"
    return True, None


def _dispatch_stdout(envelope: dict[str, Any]) -> tuple[bool, str | None]:
    """Terminal fallback — always succeeds. Prints the envelope so
    users see something in headless CI or non-macOS shells without any
    OS notification integration."""
    line = json.dumps(envelope, ensure_ascii=True)
    sys.stdout.write(f"[myos.notify] {line}\n")
    sys.stdout.flush()
    return True, None


def _preferred_channels() -> list[str]:
    """Return the ordered list of channels to try, respecting env config.

    Order: custom-hook (if configured) -> osascript (darwin only) ->
    notify-send (linux only) -> stdout (always).

    ``MYOS_NOTIFY_DISABLE_HOOK=1`` skips the custom hook (useful in
    tests where the hook file cleanup would race).
    """
    channels: list[str] = []
    if os.getenv("MYOS_NOTIFY_COMMAND", "").strip() and os.getenv("MYOS_NOTIFY_DISABLE_HOOK", "0") != "1":
        channels.append(CHANNEL_CUSTOM)
    if sys.platform == "darwin" and os.getenv("MYOS_NOTIFY_DISABLE_OSASCRIPT", "0") != "1":
        channels.append(CHANNEL_OSASCRIPT)
    if sys.platform.startswith("linux") and os.getenv("MYOS_NOTIFY_DISABLE_NOTIFY_SEND", "0") != "1":
        channels.append(CHANNEL_NOTIFY_SEND)
    channels.append(CHANNEL_STDOUT)  # always the last resort
    return channels


_DISPATCHERS = {
    CHANNEL_CUSTOM: _dispatch_custom,
    CHANNEL_OSASCRIPT: _dispatch_osascript,
    CHANNEL_NOTIFY_SEND: _dispatch_notify_send,
    CHANNEL_STDOUT: _dispatch_stdout,
}


def _record_inbox_item(conn: sqlite3.Connection, envelope: dict[str, Any], *, missed: bool) -> int | None:
    """Persist an inbox_items row so a scheduled notification is durable
    even when the dispatch channel silently drops it.

    Returns the new inbox_items.id (or ``None`` if the row was
    deduplicated by ``UNIQUE(text, kind, source)``).
    """
    kind_str = "reminder_missed" if missed else "reminder"
    text = f"{envelope.get('title') or 'MYOS'}: {envelope.get('body') or ''}".strip(": ").strip()
    if not text:
        text = envelope.get("schema") or NOTIFY_SCHEMA
    source = f"notify:{envelope.get('kind') or 'generic'}"
    # Redact defensively — the caller should have redacted body already,
    # but the join produces new text so we run the filter once more at
    # this final chokepoint (matches the em.py / agentcore chokepoint
    # rule for anything landing in inbox_items -> FTS).
    text = apply_privacy_filters(conn, text)
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO inbox_items (text, kind, owner, due_date, confidence, source, status)
        VALUES (?, ?, ?, ?, ?, ?, 'new')
        """,
        (text[:2000], kind_str, None, None, 0.8, source),
    )
    if cur.rowcount == 0:
        return None
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def notify(
    conn: sqlite3.Connection,
    *,
    title: str,
    body: str,
    kind: str = "reminder",
    urgency: str = "normal",
    correlation_id: str | None = None,
    source_ref: str | None = None,
) -> dict[str, Any]:
    """Dispatch a notification through the highest-priority available channel.

    The body is redacted through ``apply_privacy_filters`` before it
    ever leaves the process, so hooks that stream to Slack / iOS push
    never receive raw PII. On every call we:

    - Emit an ``event_log`` row of type ``notify_dispatch`` (audit).
    - Insert an ``inbox_items`` row (``kind='reminder'`` on dispatch
      success, ``kind='reminder_missed'`` on failure) so the operator
      loop always has a durable back-reference.
    - Return ``{dispatched, channel, error, envelope, inbox_id}`` so
      the caller (scheduler tick) can surface per-reminder outcomes in
      its own JSON envelope.
    """
    safe_body = apply_privacy_filters(conn, body or "")
    envelope = build_envelope(
        title=title,
        body=safe_body,
        kind=kind,
        urgency=urgency,
        correlation_id=correlation_id,
        source_ref=source_ref,
    )
    channels = _preferred_channels()
    dispatched = False
    channel_used = CHANNEL_STDOUT
    error: str | None = None
    for candidate in channels:
        ok, err = _DISPATCHERS[candidate](envelope)
        if ok:
            dispatched = True
            channel_used = candidate
            break
        # Remember the first non-fallback error so the caller has a
        # single actionable signal even if a lower-priority channel
        # later succeeds — the user still wants to know their hook broke.
        if error is None:
            error = err
    inbox_id = _record_inbox_item(conn, envelope, missed=not dispatched)
    append_event(
        conn,
        "notify_dispatch",
        "notify",
        None,
        json.dumps(
            {
                "channel": channel_used,
                "dispatched": dispatched,
                "kind": envelope["kind"],
                "urgency": envelope["urgency"],
                "correlation_id": correlation_id,
                "source_ref": source_ref,
                "error": error,
                "inbox_id": inbox_id,
            }
        ),
    )
    return {
        "dispatched": dispatched,
        "channel": channel_used,
        "error": error,
        "envelope": envelope,
        "inbox_id": inbox_id,
    }


__all__ = [
    "CHANNEL_CUSTOM",
    "CHANNEL_NOTIFY_SEND",
    "CHANNEL_OSASCRIPT",
    "CHANNEL_STDOUT",
    "NOTIFY_SCHEMA",
    "build_envelope",
    "notify",
]

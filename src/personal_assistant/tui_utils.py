"""Shared formatting helpers for MYOS terminal UI layers (L2/L3/L4).

No rich/textual imports — pure stdlib so this module is always importable
regardless of whether the [tui] extra is installed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone


def format_age(dt_str: str) -> str:
    """Human-readable age from a SQLite CURRENT_TIMESTAMP string.

    SQLite stores '2026-08-31 17:59:34' (space separator, not T).
    Python 3.10 fromisoformat does not accept the space form, so we use strptime.
    Returns '' on any parse error so callers never crash.
    """
    if not dt_str:
        return ""
    try:
        dt = datetime.strptime(dt_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        secs = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m"
        if secs < 86400:
            h, m = divmod(secs // 60, 60)
            return f"{h}h {m}m" if m else f"{h}h"
        return f"{secs // 86400}d"
    except (ValueError, TypeError):
        return ""


def truncate(s: str, n: int, suffix: str = "…") -> str:
    """Truncate s to at most n characters, appending suffix if trimmed."""
    if not isinstance(s, str):
        s = str(s)
    return s if len(s) <= n else s[: n - len(suffix)] + suffix


def condense_payload(payload: dict, max_chars: int = 120) -> str:
    """Compact single-line summary of a payload dict for table cells.

    Joins key=value pairs, skipping None/empty values, capped at max_chars.
    """
    if not isinstance(payload, dict):
        return ""
    parts: list[str] = []
    for k, v in payload.items():
        if v is None or v == "":
            continue
        sv = str(v)
        if len(sv) > 40:
            sv = sv[:37] + "…"
        parts.append(f"{k}={sv}")
    joined = "  ".join(parts)
    return truncate(joined, max_chars)


def status_chip(status: str) -> tuple[str, str]:
    """Return (label, rich-style) for an agent_action status value.

    The style strings are rich markup compatible; callers that don't use
    rich can ignore the second element.
    """
    _MAP = {
        "proposed": ("proposed", "yellow"),
        "approved": ("approved", "green"),
        "executed": ("executed", "dim"),
        "blocked": ("blocked", "bold red"),
        "failed": ("failed", "red"),
        "expired": ("expired", "bold red"),
    }
    return _MAP.get(status, (status or "—", ""))


def integrity_chip(state: str) -> tuple[str, str]:
    """Return (label, rich-style) for an approval_integrity state value."""
    _MAP = {
        "fresh": ("fresh", "green"),
        "nearing_expiry": ("expiring", "yellow"),
        "expired": ("expired", "bold red"),
        "tampered": ("tampered", "bold red"),
        "not_yet_approved": ("pending", ""),
        "invalid": ("invalid", "red"),
        "": ("—", "dim"),
    }
    return _MAP.get(state, (state or "—", "dim"))


def parse_payload(payload_json: str) -> dict:
    """Safe JSON parse; returns {} on any error."""
    try:
        obj = json.loads(payload_json or "{}")
        return obj if isinstance(obj, dict) else {}
    except (ValueError, TypeError):
        return {}

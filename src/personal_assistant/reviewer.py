"""Reviewer-model escalation for auto-approve mode (A3 — inspired by openworker).

When MYOS_AUTO_REVIEWER=<backend_name> is set, each action that would otherwise
auto-execute (requires_approval=0) is first classified by a lightweight model.
The reviewer returns one of three verdicts:

  allow    — safe to auto-execute, proceed normally
  escalate — uncertain, route to human approval queue
  block    — clearly unsafe, mark as blocked

This adds a semantic safety layer on top of the existing regex blocklist in
autonomy.py. The regex blocklist is still the first gate; the reviewer only
sees actions the blocklist already classified as safe or confirm-tier.

Activation: set MYOS_AUTO_REVIEWER=claude-haiku (or any configured backend).
Default: unset — zero behavior change, no API calls.

Fail-open: on any error (provider unavailable, timeout, malformed reply),
the reviewer returns 'allow' and execution continues normally.

Circuit breaker: _execute_safe_actions() stops calling the reviewer after
3 consecutive escalations in one cycle to prevent queue flooding.
"""

from __future__ import annotations

import json
import os
import sqlite3

from .privacy import redact_obj

_REVIEWER_PROMPT_PREFIX = """\
You are a safety reviewer for an autonomous agent system.
Classify the proposed action below as exactly one of: allow / escalate / block.

  allow    = clearly safe to auto-execute; no human review needed
  escalate = uncertain or ambiguous; a human should review before executing
  block    = clearly unsafe or destructive; do not execute

Reply with exactly one word on a single line: allow, escalate, or block.
Do not explain your reasoning.

"""


def _build_prompt(action_type: str, payload_summary: str) -> str:
    # Concatenate rather than str.format() — payload_summary is JSON and
    # contains { } characters that would trigger a KeyError with .format().
    return _REVIEWER_PROMPT_PREFIX + f"Action type: {action_type}\n" + f"Payload (redacted): {payload_summary}\n"


_VALID_VERDICTS = frozenset({"allow", "escalate", "block"})


def reviewer_backend_name() -> str:
    """Return the configured reviewer backend, or '' when disabled."""
    return os.getenv("MYOS_AUTO_REVIEWER", "").strip().lower()


def classify_action_safety(
    conn: sqlite3.Connection,
    action_type: str,
    payload: dict,
    backend_name: str,
) -> str:
    """Ask the reviewer model whether this action is safe to auto-execute.

    Returns 'allow', 'escalate', or 'block'.
    Always returns 'allow' on any error (fail-open).
    """
    try:
        from . import providers

        backend = providers.get_backend(backend_name)
        ok, _ = backend.available()
        if not ok:
            return "allow"

        payload_summary = json.dumps(redact_obj(conn, payload), ensure_ascii=True)[:400]
        prompt = _build_prompt(action_type, payload_summary)
        result = backend.reason(
            conn,
            {
                "purpose": "safety_review",
                "objective": prompt,
                "context": "",
                "analogies": [],
            },
        )
        reply = str(result.get("reply") or "").strip().lower()
        # Take the first word — the model may include punctuation.
        first_word = reply.split()[0].rstrip(".,;:") if reply else ""
        return first_word if first_word in _VALID_VERDICTS else "allow"
    except Exception:  # noqa: BLE001 — reviewer errors must never block execution
        return "allow"

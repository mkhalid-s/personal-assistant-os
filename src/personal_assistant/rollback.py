"""Rollback automation (slice P2.1).

This module implements the ``myos.action.compensation.v1`` contract
documented in ``ARCHITECTURE.md``. It has three responsibilities:

1. **Derive** a compensating-action envelope for a completed execution
   receipt. Every terminal ``executed`` receipt gets a compensation
   record — either a concrete inverse operation (``delete_on_create``,
   ``close_on_open``, ``revert_on_update``) or an explicit ``no_op``
   with a rollback note when the mutation cannot be reversed.

2. **Persist** the compensation envelope onto the receipt row via the
   ``compensating_action_json`` column so ``myos rollback --receipt N``
   can read it later without recomputing.

3. **Propose** the compensation as a *new* ``agent_actions`` row through
   the standard approval queue. Rollback never bypasses approval — the
   compensating mutation is subject to the exact same policy and TTL
   guarantees as the original mutation was.

The module is intentionally dependency-light (db + json + agentcore),
mirroring the ``agentcore`` layering rules, so it can be imported from
both ``execution`` (recording side) and ``cli_agent`` (propose side)
without creating an import cycle.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import agentcore
from .db import append_event
from .privacy import apply_privacy_filters, redact_obj

COMPENSATION_SCHEMA = "myos.action.compensation.v1"

# Strategy vocabulary published in ARCHITECTURE.md § Action-Lifecycle Contract.
STRATEGY_DELETE_ON_CREATE = "delete_on_create"
STRATEGY_CLOSE_ON_OPEN = "close_on_open"
STRATEGY_REVERT_ON_UPDATE = "revert_on_update"
STRATEGY_NO_OP = "no_op"

_ALL_STRATEGIES = frozenset(
    {
        STRATEGY_DELETE_ON_CREATE,
        STRATEGY_CLOSE_ON_OPEN,
        STRATEGY_REVERT_ON_UPDATE,
        STRATEGY_NO_OP,
    }
)

# Compensating action type for a connector-side rollback. The compensating
# action always flows through the *same* draft_external_update handler so
# the connector-mutation guard, dry-run gating, and outbox recording all
# still apply verbatim; only the ``operation`` inside the payload changes.
_ROLLBACK_ACTION_TYPE = "draft_external_update"


def _base_envelope(
    *,
    strategy: str,
    action_type: str,
    payload: dict[str, Any],
    target: dict[str, Any],
    rollback_note: str = "",
    preconditions: list[str] | None = None,
    dry_run_supported: bool = True,
) -> dict[str, Any]:
    """Build a schema-stable compensation envelope. ``strategy`` must be one of
    the four published values; unknown strategies are coerced to ``no_op``
    with a diagnostic note so downstream consumers never see a payload
    outside the vocabulary."""
    if strategy not in _ALL_STRATEGIES:
        rollback_note = (rollback_note + f" (unknown strategy '{strategy}' coerced to no_op)").strip()
        strategy = STRATEGY_NO_OP
    envelope: dict[str, Any] = {
        "schema": COMPENSATION_SCHEMA,
        "strategy": strategy,
        "action_type": action_type,
        "payload": payload,
        "target": target,
        "dry_run_supported": bool(dry_run_supported),
    }
    if preconditions:
        envelope["preconditions"] = list(preconditions)
    if rollback_note:
        envelope["rollback_note"] = rollback_note
    return envelope


def _no_op_envelope(action_type: str, rollback_note: str) -> dict[str, Any]:
    """Explicit no-op compensation. We still record this so operators see
    'this action cannot be rolled back' as a first-class signal instead of
    an empty column."""
    return _base_envelope(
        strategy=STRATEGY_NO_OP,
        action_type=action_type,
        payload={},
        target={},
        rollback_note=rollback_note,
        dry_run_supported=False,
    )


def _connector_target(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract the (provider, target_type, target_ref) triple published in the
    receipt's ``approval_context.target`` field. Falls back to empty strings
    so the schema stays stable even for partially-shaped payloads."""
    provider = str(payload.get("provider") or payload.get("connector") or payload.get("target") or "").strip().lower()
    target_type = str(payload.get("target_type") or payload.get("operation") or "").strip().lower()
    target_ref = str(payload.get("target_ref") or payload.get("external_id") or "").strip()
    return {"provider": provider, "target_type": target_type, "target_ref": target_ref}


def _connector_compensation(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Derive a compensating envelope for a connector mutation payload. Only
    the shapes MYOS actually emits through ``normalize_connector_mutation``
    are compensated; anything else returns ``None`` so the caller can fall
    through to the no-op branch with a helpful note."""
    target = _connector_target(payload)
    provider = target["provider"]
    operation = target["target_type"]  # already lower-cased above
    if provider not in {"jira", "github", "confluence", "aha"}:
        return None
    correction_body = (
        f"[MYOS rollback] Reverting prior {provider} {operation or 'update'} on {target['target_ref'] or 'target'}. "
        "Refer to the linked execution receipt for the original text and rationale."
    )
    if operation == "comment":
        # Comments are additive. The safe compensation is a follow-up
        # correction comment (delete-on-create semantics — the original
        # comment remains for audit, the compensating comment marks it
        # withdrawn). A true DELETE would require an extra scope grant we
        # don't request.
        return _base_envelope(
            strategy=STRATEGY_DELETE_ON_CREATE,
            action_type=_ROLLBACK_ACTION_TYPE,
            payload={
                "target": provider,
                "operation": "comment",
                "target_ref": target["target_ref"],
                "body": correction_body,
                "rollback_note": (
                    "Compensating comment retracts the prior draft; the original comment stays "
                    "for audit. Delete manually if the platform allows."
                ),
                "dry_run": True,
            },
            target=target,
            rollback_note="Withdraw prior comment via a compensating correction comment.",
            preconditions=[
                "Original comment/thread still exists on the target.",
                "Operator has permission to comment on the target.",
            ],
            dry_run_supported=True,
        )
    if operation == "status_update":
        return _base_envelope(
            strategy=STRATEGY_REVERT_ON_UPDATE,
            action_type=_ROLLBACK_ACTION_TYPE,
            payload={
                "target": provider,
                "operation": "status_update",
                "target_ref": target["target_ref"],
                "body": correction_body,
                "rollback_note": (
                    "Revert status field to prior value. Prior value is not persisted; "
                    "operator must confirm from the target's history before approving."
                ),
                "dry_run": True,
            },
            target=target,
            rollback_note="Revert status update; prior status must be confirmed by the operator.",
            preconditions=[
                "Operator has verified the prior status value from the target's history.",
                "Target state has not moved forward past the reverted value.",
            ],
            dry_run_supported=True,
        )
    if operation in {"draft_note", "link_back"}:
        return _base_envelope(
            strategy=STRATEGY_DELETE_ON_CREATE,
            action_type=_ROLLBACK_ACTION_TYPE,
            payload={
                "target": provider,
                "operation": operation,
                "target_ref": target["target_ref"],
                "body": correction_body,
                "rollback_note": "Withdraw prior draft/link via compensating update.",
                "dry_run": True,
            },
            target=target,
            rollback_note="Withdraw prior draft/link via compensating update.",
            preconditions=["Operator has confirmed the prior note/link is still present."],
            dry_run_supported=True,
        )
    return None


def derive_compensation(
    *,
    action_type: str,
    payload: dict[str, Any],
    final_status: str,
) -> dict[str, Any]:
    """Return the ``myos.action.compensation.v1`` envelope for a completed
    execution. Always returns a schema-valid envelope — non-executed
    outcomes and non-compensable actions get an explicit ``no_op`` with a
    diagnostic note so ``execution-receipt show --json`` always has a
    compensation field to render."""
    action_type = (action_type or "").strip()
    if final_status != "executed":
        # Blocked / failed / noop receipts have nothing to undo — the
        # follow-up inbox item (added by ``_record_execution_receipt``)
        # already handles operator notification.
        return _no_op_envelope(
            action_type or "unknown",
            f"Nothing to roll back: original execution ended in status='{final_status}'.",
        )
    if action_type in {"draft_external_update"}:
        envelope = _connector_compensation(payload)
        if envelope is not None:
            return envelope
        return _no_op_envelope(
            action_type,
            "Connector payload shape not recognized by the rollback planner; "
            "operator must draft a corrective update manually.",
        )
    if action_type in {"local_note", "create_inbox_item"}:
        return _no_op_envelope(
            action_type,
            "Local capture actions have no external side effect to reverse; "
            "delete the created record manually if desired.",
        )
    if action_type == "apply_patch":
        # A reverse-patch compensation would require re-serializing the
        # inverted diff at execution time; that's a follow-up. Explicitly
        # mark as no_op with a pointer so operators know to `git revert`.
        return _no_op_envelope(
            action_type,
            "Rolling back a local patch requires `git revert` or manual inspection; "
            "the compensating patch is not auto-derived in this slice.",
        )
    return _no_op_envelope(
        action_type or "unknown",
        f"No compensation strategy registered for action_type='{action_type}'.",
    )


def record_compensation(
    conn: sqlite3.Connection,
    *,
    receipt_id: int,
    compensation: dict[str, Any],
) -> None:
    """Persist a compensation envelope onto an existing execution receipt.
    Silently no-ops if the column doesn't exist (migration 38 not applied)
    so importing this module on an older DB never raises."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(action_execution_receipts)").fetchall()}
    if "compensating_action_json" not in columns:
        return
    conn.execute(
        "UPDATE action_execution_receipts SET compensating_action_json = ? WHERE id = ?",
        (json.dumps(compensation, ensure_ascii=True)[:8000], int(receipt_id)),
    )


def load_receipt_row(conn: sqlite3.Connection, receipt_id: int) -> Any:
    """Return the receipt row plus its compensation envelope, or ``None``
    when the receipt does not exist."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(action_execution_receipts)").fetchall()}
    has_compensation = "compensating_action_json" in columns
    select_cols = "id, agent_action_id, agent_task_id, action_type, final_status, approved, rollback_note"
    if has_compensation:
        select_cols += ", compensating_action_json, rollback_action_id"
    row = conn.execute(
        f"SELECT {select_cols} FROM action_execution_receipts WHERE id = ?",  # noqa: S608
        (int(receipt_id),),
    ).fetchone()
    return row


def parse_compensation(row: Any) -> dict[str, Any] | None:
    """Read the compensation envelope from a receipt row, tolerant of the
    pre-migration case (column missing) and invalid JSON (corrupted row)."""
    try:
        raw = row["compensating_action_json"]
    except (IndexError, KeyError):
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


class RollbackError(Exception):
    """Raised when a rollback cannot be proposed (missing receipt, unrollable
    action, or empty compensation envelope). Callers should format a
    schema-stable error envelope for ``myos rollback --json`` consumers."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def propose_rollback(
    conn: sqlite3.Connection,
    *,
    receipt_id: int,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Propose the compensating action into the standard approval queue.

    Returns a dict describing the proposal (or the dry-run preview when
    ``dry_run=True``). Always privacy-filters the compensating payload
    before persistence and event logging, matching the same discipline
    ``enqueue_proposal`` enforces for regular proposals. Raises
    :class:`RollbackError` on unrecoverable states so the CLI can render
    a schema-stable error envelope on exit-1.
    """
    row = load_receipt_row(conn, receipt_id)
    if not row:
        raise RollbackError("not_found", f"Execution receipt #{receipt_id} not found.")
    compensation = parse_compensation(row)
    if compensation is None:
        raise RollbackError(
            "no_compensation",
            (
                f"Receipt #{receipt_id} has no compensation envelope recorded. "
                "Older receipts (created before migration 38) can be replayed manually."
            ),
        )
    strategy = str(compensation.get("strategy") or "")
    if strategy == STRATEGY_NO_OP:
        raise RollbackError(
            "no_op",
            (
                f"Receipt #{receipt_id} was recorded as no_op — "
                f"{compensation.get('rollback_note') or 'no automated rollback available.'}"
            ),
        )
    if strategy not in _ALL_STRATEGIES:
        raise RollbackError("invalid_strategy", f"Unsupported compensation strategy '{strategy}'.")
    action_type = str(compensation.get("action_type") or "").strip()
    if not action_type:
        raise RollbackError("invalid_envelope", "Compensation envelope missing 'action_type'.")
    payload = compensation.get("payload") or {}
    if not isinstance(payload, dict):
        raise RollbackError("invalid_envelope", "Compensation envelope 'payload' must be an object.")
    filtered_payload = redact_obj(conn, dict(payload))
    filtered_payload["rollback_context"] = {
        "receipt_id": int(row["id"]),
        "agent_action_id": int(row["agent_action_id"]) if row["agent_action_id"] is not None else None,
        "original_action_type": str(row["action_type"] or ""),
        "strategy": strategy,
    }
    title_prefix = apply_privacy_filters(conn, f"Rollback: {row['action_type']} (receipt #{row['id']})")[:500]
    target = compensation.get("target") or {}
    preview = {
        "schema": "myos.rollback.preview.v1",
        "receipt_id": int(row["id"]),
        "agent_action_id": int(row["agent_action_id"]) if row["agent_action_id"] is not None else None,
        "strategy": strategy,
        "compensating_action_type": action_type,
        "target": target if isinstance(target, dict) else {},
        "preconditions": list(compensation.get("preconditions") or []),
        "rollback_note": str(compensation.get("rollback_note") or ""),
        "dry_run": bool(dry_run),
        "payload": filtered_payload,
        "title": title_prefix,
    }
    if dry_run:
        preview["proposed_action_id"] = None
        preview["status"] = "preview"
        return preview

    task_id_val = row["agent_task_id"]
    if task_id_val is None:
        raise RollbackError(
            "no_task",
            f"Receipt #{receipt_id} has no owning agent_task; cannot enqueue a rollback proposal.",
        )
    action_id = agentcore.enqueue_proposal(
        conn,
        task_id=int(task_id_val),
        action_type=action_type,
        title=title_prefix,
        payload=filtered_payload,
        requires_approval=1,
    )
    columns = {row_meta["name"] for row_meta in conn.execute("PRAGMA table_info(action_execution_receipts)").fetchall()}
    if "rollback_action_id" in columns:
        conn.execute(
            "UPDATE action_execution_receipts SET rollback_action_id = ? WHERE id = ?",
            (int(action_id), int(row["id"])),
        )
    append_event(
        conn,
        "rollback_proposed",
        "action_execution_receipt",
        int(row["id"]),
        json.dumps(
            {
                "receipt_id": int(row["id"]),
                "compensating_action_id": int(action_id),
                "strategy": strategy,
                "original_action_type": str(row["action_type"] or ""),
            },
            ensure_ascii=True,
        ),
    )
    preview["proposed_action_id"] = int(action_id)
    preview["status"] = "proposed"
    return preview

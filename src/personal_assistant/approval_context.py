from __future__ import annotations

from typing import Any

from . import command_registry

_EXTERNAL_ACTION_TYPES = {
    "draft_external_update",
    "create_issue",
    "update_issue",
    "draft_message",
    "send_message",
}


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def action_review_context(
    action_type: str, payload: dict[str, Any] | None, *, requires_approval: bool = True
) -> dict[str, object]:
    payload = payload if isinstance(payload, dict) else {}
    action_type = (action_type or "").strip()
    command_text = str(payload.get("command") or payload.get("myos_command") or "").strip()
    if command_text.startswith("myos "):
        command_text = command_text.split(" ", 1)[1]
    command_name = command_text.split(" ", 1)[0]
    side_effects: list[str] = []
    safer_commands: list[str] = []

    command_spec = command_registry.find_command(command_name) if command_name else None
    if command_spec:
        side_effects.extend(command_spec.side_effects)
        if command_spec.dry_run_by_default and command_spec.examples:
            safer_commands.append(command_spec.examples[0])

    connector = (
        str(payload.get("connector") or payload.get("target") or payload.get("target_type") or "").strip().lower()
    )
    if connector in {"jira", "github", "confluence", "aha", "external_system"} or action_type in _EXTERNAL_ACTION_TYPES:
        side_effects.append("external_write")
        safer_commands.append("myos approve --list")
        safer_commands.append("myos execution-receipt list")
    elif action_type == "create_inbox_item":
        side_effects.append("local_db_write")
    elif action_type == "apply_patch":
        side_effects.append("local_file_write")
        safer_commands.append("myos trace list")
        safer_commands.append("myos execution-receipt list")
    elif action_type:
        side_effects.append("local_db_write")

    if "database_restore" in side_effects:
        safer_commands.insert(0, "myos backup")
        safer_commands.insert(1, "myos migrations verify --strict")
    if "os_service_write" in side_effects:
        safer_commands.insert(0, "myos launchd-status")
        safer_commands.insert(1, "myos runbook --short")

    dry_run = _truthy(payload.get("dry_run"))
    approval_reason = "approval_required" if requires_approval else "safe_local"
    if "external_write" in side_effects:
        approval_reason = "external_write_requires_approval"
    elif "os_service_write" in side_effects:
        approval_reason = "os_service_change_requires_review"
    elif "database_restore" in side_effects:
        approval_reason = "database_restore_requires_review"

    return {
        "side_effects": _dedupe(side_effects),
        "dry_run": dry_run,
        "approval_reason": approval_reason,
        "safer_commands": _dedupe(safer_commands),
    }


def compact_action_review_context(
    action_type: str, payload: dict[str, Any] | None, *, requires_approval: bool = True
) -> dict[str, object]:
    context = action_review_context(action_type, payload, requires_approval=requires_approval)
    return {
        "side_effects": list(context["side_effects"]),
        "dry_run": bool(context["dry_run"]),
        "approval_reason": str(context["approval_reason"]),
    }


def format_compact_action_review_context(context: dict[str, Any]) -> list[str]:
    side_effects = context.get("side_effects") if isinstance(context, dict) else []
    if not isinstance(side_effects, list):
        side_effects = []
    lines = ["side_effects: " + (", ".join(str(value) for value in side_effects if value) or "none")]
    approval_reason = str(context.get("approval_reason") or "approval_required")
    lines.append(f"review_gate: {approval_reason}")
    if bool(context.get("dry_run")):
        lines.append("dry_run: true")
    return lines


def format_action_review_context(
    action_type: str, payload: dict[str, Any] | None, *, requires_approval: bool = True
) -> list[str]:
    context = action_review_context(action_type, payload, requires_approval=requires_approval)
    lines = ["side_effects: " + (", ".join(context["side_effects"]) or "none")]
    lines.append(f"review_gate: {context['approval_reason']}")
    if context["dry_run"]:
        lines.append("dry_run: true")
    safer = context["safer_commands"]
    if safer:
        lines.append("safer_next: " + "; ".join(safer))
    return lines


def factory_review_context(workflow_pack: str) -> dict[str, object]:
    pack = (workflow_pack or "intent_execution").strip()
    side_effects = ["local_db_write"]
    safer_commands = ["myos approve --list", "myos execution-receipt list"]
    if pack in {"connector_ops", "daily_ops", "software_delivery"}:
        side_effects.append("external_write")
    return {
        "side_effects": side_effects,
        "review_gate": "factory_execution_approval_required",
        "safer_commands": safer_commands,
    }


def format_factory_review_context(workflow_pack: str) -> list[str]:
    context = factory_review_context(workflow_pack)
    return [
        "Side effects: " + ", ".join(context["side_effects"]),
        f"Review gate: {context['review_gate']}",
        "Safer next: " + "; ".join(context["safer_commands"]),
    ]

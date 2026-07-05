from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from . import observability
from .db import append_event
from .privacy import apply_privacy_filters, redact_obj

SCHEMA_VERSION = 2
DEFAULT_AUTO = "low"
DEFAULT_TIMEOUT = 600


@dataclass
class ZeroRunResult:
    status: str
    exit_code: int
    events: list[dict[str, Any]] = field(default_factory=list)
    event_counts: dict[str, int] = field(default_factory=dict)
    run_id: str = ""
    session_id: str = ""
    cwd: str = ""
    provider: str = ""
    model: str = ""
    api_model: str = ""
    final_text: str = ""
    changed_files: list[str] = field(default_factory=list)
    permission_events: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    stderr: str = ""
    protocol_errors: list[str] = field(default_factory=list)
    timed_out: bool = False

    def terminal_ok(self) -> bool:
        return self.status == "success" and self.exit_code == 0 and not self.protocol_errors


def _zero_base_argv() -> list[str]:
    configured = os.getenv("MYOS_AGENT_EXEC_ZERO_STREAM", "").strip()
    if configured:
        return shlex.split(configured)
    return ["zero", "exec"]


def _prompt_input(task_text: str) -> str:
    return json.dumps(
        {"schemaVersion": SCHEMA_VERSION, "type": "prompt", "content": task_text},
        ensure_ascii=True,
    ) + "\n"


def zero_stream_argv(
    *,
    cwd: str,
    auto: str = DEFAULT_AUTO,
    max_turns: int | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    argv = _zero_base_argv()
    argv.extend(
        [
            "--cwd",
            cwd,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--auto",
            auto or DEFAULT_AUTO,
            "--no-notify",
        ]
    )
    if max_turns is not None and int(max_turns) > 0:
        argv.extend(["--max-turns", str(int(max_turns))])
    argv.extend(extra_args or [])
    return argv


def run_zero_stream(
    task_text: str,
    *,
    cwd: str,
    timeout: int = DEFAULT_TIMEOUT,
    auto: str = DEFAULT_AUTO,
    max_turns: int | None = None,
    extra_args: list[str] | None = None,
) -> ZeroRunResult:
    argv = zero_stream_argv(cwd=cwd, auto=auto, max_turns=max_turns, extra_args=extra_args)
    if not argv or not shutil.which(argv[0]):
        return ZeroRunResult(status="missing", exit_code=127, errors=[{"code": "missing_zero", "message": "zero executable not found"}])
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            input=_prompt_input(task_text),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return ZeroRunResult(
            status="timed_out",
            exit_code=124,
            stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
            timed_out=True,
            errors=[{"code": "timed_out", "message": f"zero timed out after {timeout}s"}],
        )
    result = parse_zero_stream(proc.stdout or "", exit_code=int(proc.returncode), stderr=proc.stderr or "")
    if not result.status:
        result.status = status_from_exit(int(proc.returncode))
    # Keep duration available without adding a schema dependency.
    result.usage.setdefault("durationMs", int((time.monotonic() - started) * 1000))
    return result


def parse_zero_stream(stdout: str, *, exit_code: int = 0, stderr: str = "") -> ZeroRunResult:
    result = ZeroRunResult(status="", exit_code=int(exit_code), stderr=stderr)
    for line_no, raw_line in enumerate((stdout or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            result.protocol_errors.append(f"line {line_no}: invalid JSON")
            continue
        if not isinstance(event, dict):
            result.protocol_errors.append(f"line {line_no}: event is not an object")
            continue
        if int(event.get("schemaVersion") or 0) != SCHEMA_VERSION:
            result.protocol_errors.append(f"line {line_no}: unsupported schemaVersion={event.get('schemaVersion')}")
            continue
        event_type = str(event.get("type") or "")
        result.events.append(event)
        result.event_counts[event_type] = result.event_counts.get(event_type, 0) + 1
        result.run_id = result.run_id or str(event.get("runId") or "")
        if event_type == "run_start":
            result.session_id = str(event.get("sessionId") or "")
            result.cwd = str(event.get("cwd") or "")
            result.provider = str(event.get("provider") or "")
            result.model = str(event.get("model") or "")
            result.api_model = str(event.get("apiModel") or "")
        elif event_type == "tool_result":
            for changed in event.get("changedFiles") or []:
                changed_text = str(changed)
                if changed_text and changed_text not in result.changed_files:
                    result.changed_files.append(changed_text)
        elif event_type in {"permission", "permission_request", "permission_decision"}:
            result.permission_events.append(_compact_permission(event))
        elif event_type == "usage":
            for key in ("promptTokens", "completionTokens", "totalTokens", "costUsd"):
                if key in event:
                    result.usage[key] = event[key]
        elif event_type == "warning":
            result.warnings.append(str(event.get("message") or event.get("text") or ""))
        elif event_type == "error":
            result.errors.append({"code": str(event.get("code") or "error"), "message": str(event.get("message") or ""), "recoverable": event.get("recoverable")})
        elif event_type == "final":
            result.final_text = str(event.get("text") or "")
        elif event_type == "run_end":
            result.status = str(event.get("status") or "") or status_from_exit(exit_code)
            if event.get("exitCode") is not None:
                try:
                    result.exit_code = int(event.get("exitCode"))
                except (TypeError, ValueError):
                    result.protocol_errors.append(f"line {line_no}: invalid exitCode")
    if not result.status:
        result.status = status_from_exit(result.exit_code)
    if result.protocol_errors and result.status == "success":
        result.status = "protocol_error"
    return result


def status_from_exit(exit_code: int) -> str:
    return {
        0: "success",
        1: "crashed",
        2: "failed_usage",
        3: "failed_runtime",
        4: "incomplete",
        124: "timed_out",
        127: "missing",
        130: "interrupted",
    }.get(int(exit_code), "failed" if int(exit_code) else "success")


def _compact_permission(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": event.get("type"),
        "id": event.get("id"),
        "name": event.get("name"),
        "action": event.get("action"),
        "permission": event.get("permission"),
        "permissionGranted": event.get("permissionGranted"),
        "permissionMode": event.get("permissionMode"),
        "sideEffect": event.get("sideEffect"),
        "reason": event.get("reason"),
        "decisionReason": event.get("decisionReason"),
    }


def result_payload(result: ZeroRunResult) -> dict[str, Any]:
    return {
        "schema": "myos.zero_executor.result.v1",
        "stream_schema_version": SCHEMA_VERSION,
        "status": result.status,
        "exit_code": result.exit_code,
        "run_id": result.run_id,
        "session_id": result.session_id,
        "cwd": result.cwd,
        "provider": result.provider,
        "model": result.model,
        "api_model": result.api_model,
        "event_counts": result.event_counts,
        "changed_files": result.changed_files,
        "permission_events": result.permission_events[:20],
        "warnings": [w for w in result.warnings if w][:20],
        "errors": result.errors[:20],
        "usage": result.usage,
        "protocol_errors": result.protocol_errors[:20],
        "final_text": result.final_text[:4000],
        "stderr_preview": result.stderr[:1000],
    }


def record_zero_agent_run(
    conn,
    *,
    task_id: int,
    result: ZeroRunResult,
    factory_run_id: int | None = None,
) -> int:
    payload = redact_obj(conn, result_payload(result))
    summary = apply_privacy_filters(conn, result.final_text or f"Zero run {result.status}")[:500]
    conn.execute(
        """
        INSERT INTO agent_runs (agent_task_id, agent_name, provider, status, plan_json, summary, finished_at)
        VALUES (?, 'zero_executor', 'zero', ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            int(task_id),
            "completed" if result.terminal_ok() else result.status,
            json.dumps(payload, ensure_ascii=True),
            summary,
        ),
    )
    agent_run_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    append_event(
        conn,
        "zero_executor_run",
        "agent_run",
        agent_run_id,
        json.dumps(
            {
                "task_id": int(task_id),
                "factory_run_id": int(factory_run_id) if factory_run_id is not None else None,
                "status": result.status,
                "exit_code": result.exit_code,
                "run_id": result.run_id,
                "changed_files": len(result.changed_files),
            },
            ensure_ascii=True,
        ),
    )
    observability.link_current_trace(
        conn,
        agent_task_id=int(task_id),
        factory_run_id=int(factory_run_id) if factory_run_id is not None else None,
    )
    return agent_run_id

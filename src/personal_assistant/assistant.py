"""Orchestrator: pick the active brain, run conversational turns, and harness
external agent CLIs as executor sub-agents.

Both surfaces funnel every proposed change into the existing approval queue via
``agentcore``. Coding delegations run inside a throwaway git worktree so a
harnessed agent's edits are captured as a diff and proposed -- never applied to the
real tree without one-tap approval.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time

from . import agentcore, context, graphrag
from .providers import get_backend, resolve_backend_name


def run_turn(
    conn,
    user_text: str,
    history: list[dict],
    backend_name: str | None = None,
    on_text=None,
    *,
    surface: str = "chat",
    conversation_id: int | None = None,
) -> dict:
    """One conversational turn against the active (or named) brain.

    The result is logged through the Context Intelligence Loop at this single chokepoint
    (every surface — chat, voice, future API — funnels here), so the conversation, its
    observations, and derived relationships are persisted automatically. Logging is
    best-effort: a failure here never breaks the turn, and policy can disable it.
    """
    backend = get_backend(backend_name)
    retrieval_run_ids: list[int] = []
    try:
        hits = graphrag.retrieve(conn, user_text, limit=3, record_run=True, mode=f"{surface}_answer")
        if hits and hits[0].get("retrieval_run_id"):
            retrieval_run_ids.append(int(hits[0]["retrieval_run_id"]))
            conn.commit()
    except Exception:  # noqa: BLE001 - retrieval traces should never block a chat turn
        conn.rollback()
    started = time.monotonic()
    result = backend.run_turn(conn, user_text, history, on_text=on_text)
    if retrieval_run_ids:
        result["retrieval_run_ids"] = retrieval_run_ids
    latency_ms = int((time.monotonic() - started) * 1000)
    try:
        log = context.log_turn(
            conn,
            user_text=user_text,
            assistant_text=result.get("reply", "") or "",
            conversation_id=conversation_id,
            surface=surface,
            backend=result.get("backend") or backend.name,
            proposed_action_ids=result.get("proposed_action_ids", []),
            retrieval_run_ids=retrieval_run_ids,
            latency_ms=latency_ms,
        )
        if log.get("conversation_id"):
            result["conversation_id"] = log["conversation_id"]
    except Exception as exc:  # noqa: BLE001 — logging must never break a conversational turn
        # Roll back any partial, uncommitted log_turn work so we don't strand an open
        # write transaction / WAL lock (review L6), and leave an audit trail rather than
        # silently swallowing a Context-Loop bug that could lie dead for a release (L5).
        try:
            conn.rollback()
            from .db import append_event

            append_event(conn, "context_log_failed", "conversation", None, str(exc)[:500])
            conn.commit()
        except Exception:  # noqa: BLE001 — never let error-handling itself break the turn
            pass
    return result


def delegate_to_agent(conn, target: str, task_text: str, cwd: str | None = None, timeout: int = 600) -> dict:
    """Harness an external agent CLI on a coding task; propose its diff for approval."""
    name = resolve_backend_name(target)
    backend = get_backend(target)
    if not hasattr(backend, "executor_argv"):
        return {"error": f"backend '{name}' has no executor mode; use it as the brain via `myos chat --backend {name}`."}
    argv = backend.executor_argv(task_text)
    if not argv:
        return {"error": f"no executor command configured for '{name}' (set MYOS_AGENT_EXEC_{name.upper()})."}

    root = _git_root(cwd or os.getcwd())
    if not root:
        return {"error": "delegating a coding task needs a git repo for safe worktree isolation. "
                         "Run from inside a git repo (or `git init`), or use the agent as a brain via `myos chat`."}

    diff, log = _run_in_worktree(root, argv, timeout=timeout)
    task_id = agentcore.ensure_turn_task(conn, f"delegate to {name}: {task_text}")

    if not diff.strip():
        action_id = agentcore.enqueue_proposal(
            conn, task_id=task_id, action_type="draft_external_update",
            title=f"{name} output: {task_text[:80]}",
            payload={"target": "outbox", "agent": name, "draft": (log or "(no output)")[:8000]},
        )
        conn.commit()
        return {"proposed_action_ids": [action_id], "diff": "",
                "summary": f"{name} produced no code changes; its output was proposed as action #{action_id}."}

    action_id = agentcore.enqueue_proposal(
        conn, task_id=task_id, action_type="apply_patch",
        title=f"Apply {name} patch: {task_text[:80]}",
        payload={"agent": name, "task": task_text, "repo_root": root, "diff": diff[:200000]},
    )
    conn.commit()
    return {"proposed_action_ids": [action_id], "diff": diff,
            "summary": f"{name} produced a {diff.count(chr(10))}-line diff; proposed as action #{action_id} "
                       f"(approve to apply: `myos approve --action {action_id} --execute`)."}


def _git_root(cwd: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def _run_in_worktree(root: str, argv: list[str], timeout: int) -> tuple[str, str]:
    wt = tempfile.mkdtemp(prefix="myos-wt-")
    try:
        subprocess.run(["git", "-C", root, "worktree", "add", "--detach", wt],
                       capture_output=True, text=True, check=True)
        try:
            proc = subprocess.run(argv, cwd=wt, capture_output=True, text=True, timeout=timeout, check=False)
            log = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        except subprocess.TimeoutExpired:
            log = f"(agent timed out after {timeout}s)"
        subprocess.run(["git", "-C", wt, "add", "-A"], capture_output=True, text=True, check=False)
        diff = subprocess.run(["git", "-C", wt, "diff", "--cached"],
                              capture_output=True, text=True, check=False).stdout
        return diff, log
    finally:
        subprocess.run(["git", "-C", root, "worktree", "remove", "--force", wt],
                       capture_output=True, text=True, check=False)
        shutil.rmtree(wt, ignore_errors=True)

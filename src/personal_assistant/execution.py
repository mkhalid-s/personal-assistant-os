"""The action executor + approval engine — the safety-critical core, extracted
from cli.py (refactor #12) so it lives in a small, testable module.

`approve_and_execute` is the single approve→execute path used by both `cmd_act`
(CLI) and `_handle_proposals` (chat/voice). It returns a structured outcome (no
printing, no SystemExit), which removed the old `argparse.Namespace`-fabrication
coupling between the chat loop and `cmd_act` (review nit).

Every execution passes through `_execute_agent_action`, which enforces the hard
destructive guard (autonomy.BLOCKED) and the apply_patch protected-path guard.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request

from . import autonomy
from .db import append_event, resolve_db_path
from .inbox import insert_inbox_item_dedup
from .privacy import apply_privacy_filters, get_policy_map, redact_obj

# Path *segments* a harnessed-agent patch may NEVER touch. We protect the whole
# MYOS package directory (not drifting individual filenames — review A-1/C-4) so
# relocating safety code can't silently un-protect it. ".git/.claude/hooks" guard
# the repo/agent config. Matched as path-segment prefixes, never bare substrings.
_PROTECTED_PATH_SEGMENTS = ("personal_assistant", ".claude", ".git", "hooks")
_PROTECTED_PATCH_PATTERNS = _PROTECTED_PATH_SEGMENTS  # back-compat alias


def _path_is_protected(path: str) -> bool:
    p = path.strip()
    if p.startswith("./"):  # strip a leading ./ only — NOT leading dots (would mangle .claude)
        p = p[2:]
    if p.startswith("/") or ".." in p.split("/"):
        return True  # absolute or tree-escaping
    segments = p.split("/")
    return any(seg in _PROTECTED_PATH_SEGMENTS for seg in segments) or p.endswith("settings.local.json")


def _patch_target_paths(diff: str) -> list[str]:
    """Extract every path a unified diff would touch — incl. rename/copy headers
    (review C-1), so a crafted `diff --git` line can't hide the real target."""
    paths = []
    for line in diff.splitlines():
        if line.startswith(("+++ ", "--- ", "diff --git ")):
            toks = line.split()[2:] if line.startswith("diff --git ") else [line[4:].strip()]
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            toks = [line.split(" ", 2)[2].strip()]
        else:
            continue
        for tok in toks:
            tok = tok.strip().strip('"')
            if not tok or tok == "/dev/null":
                continue
            if tok.startswith(("a/", "b/")):
                tok = tok[2:]
            paths.append(tok)
    return paths


def _git_numstat_paths(root: str, diff: str) -> list[str]:
    """Authoritative path list from git itself — catches anything our textual
    parser misses (renames/copies/binary). Returns [] if git can't enumerate."""
    proc = subprocess.run(["git", "-C", root, "apply", "--numstat"], input=diff,
                          text=True, capture_output=True)
    if proc.returncode != 0:
        return []
    paths = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            p = parts[2].strip()
            # renames render as "old => new" or "dir/{old => new}/f" — take the new path
            if " => " in p:
                p = re.sub(r"\{[^}]*=> *([^}]*)\}", r"\1", p)
                if " => " in p:
                    p = p.split(" => ", 1)[1]
                p = re.sub(r"//+", "/", p)
            paths.append(p.strip())
    return paths


def _status_from_result(result: str) -> str:
    """Map an executor result string to an agent_actions status (finding #20:
    a blocked action must NOT be recorded as 'executed')."""
    if result.startswith("blocked:"):
        return "blocked"
    if result.startswith(("provider execution failed:", "patch failed")):
        return "failed"
    if result.startswith(("no diff to apply", "marked complete")):
        return "noop"  # nothing happened — don't record as 'executed' (review A5)
    return "executed"


def _execute_agent_action(conn, row) -> str:
    payload = json.loads(row["payload_json"] or "{}")
    action_type = row["action_type"]
    # Hard destructive guard — protects every execution path (cmd_act, autopilot,
    # Agent SDK). No autonomy level can run a blocked/destructive action; only an
    # explicit operator override env var can.
    verdict = autonomy.classify_action(action_type, payload, level=autonomy.level_from_policy(conn))
    # The override is honored ONLY in an interactive TTY (review finding #16) — so it
    # can never silently re-enable destructive actions under autopilot/cron/systemd.
    override = bool(os.getenv("MYOS_ALLOW_DESTRUCTIVE", "").strip()) and sys.stdin.isatty()
    if verdict["tier"] == autonomy.BLOCKED and not override:
        append_event(conn, "autonomy_block", "agent_action", row["id"], verdict["reason"])
        return f"blocked: {verdict['reason']} — destructive action must be executed manually (interactive MYOS_ALLOW_DESTRUCTIVE=1 to override)"
    if verdict["tier"] == autonomy.BLOCKED and override:
        append_event(conn, "autonomy_override", "agent_action", row["id"], f"OVERRIDE {verdict['reason']}")
        print(f"⚠️  MYOS_ALLOW_DESTRUCTIVE override: executing BLOCKED action #{row['id']} ({verdict['reason']})")
    if action_type == "create_inbox_item":
        inbox_id = insert_inbox_item_dedup(
            conn,
            text=str(payload.get("text", row["title"])),
            kind=str(payload.get("kind", "task")),
            owner=None,
            due_date=None,
            confidence=0.8,
            source=str(payload.get("source", "agent")),
        )
        return f"created inbox item #{inbox_id}" if inbox_id is not None else "inbox item already existed"
    if action_type == "apply_patch":
        diff = str(payload.get("diff", ""))
        if not diff.strip():
            return "no diff to apply"
        # Resolve repo root via git (don't trust an arbitrary payload path).
        rp = subprocess.run(
            ["git", "-C", str(payload.get("repo_root", "")) or os.getcwd(), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True,
        )
        if rp.returncode != 0 or not rp.stdout.strip():
            return "blocked: apply_patch repo_root is not a git repository"
        root = rp.stdout.strip()
        # Symlink hunks can escape the tree and aren't visible as a path line (C-2).
        if re.search(r"(?mi)^\s*(new|old|deleted)?\s*(file )?mode 120000\b", diff) or "mode 120000" in diff:
            append_event(conn, "autonomy_block", "agent_action", row["id"], "patch introduces a symlink")
            return "blocked: patch introduces a symlink (mode 120000) — refused"
        # Authoritative path set = textual parse (incl rename/copy headers) UNION git's
        # own enumeration, checked against the protected package dir + tree-escape (A-1/C-1/C-4).
        targets = set(_patch_target_paths(diff)) | set(_git_numstat_paths(root, diff))
        bad = sorted(p for p in targets if _path_is_protected(p))
        if bad:
            append_event(conn, "autonomy_block", "agent_action", row["id"], f"patch touches protected paths: {bad[:5]}")
            return f"blocked: patch touches protected/out-of-tree paths {bad[:5]} — refused to protect the safety policy"
        check = subprocess.run(["git", "-C", root, "apply", "--check"], input=diff, text=True, capture_output=True)
        if check.returncode != 0:
            return f"patch failed pre-check (git apply --check): {(check.stderr or '')[:300]}"
        proc = subprocess.run(["git", "-C", root, "apply", "--index"], input=diff, text=True, capture_output=True)
        if proc.returncode != 0:
            proc = subprocess.run(["git", "-C", root, "apply"], input=diff, text=True, capture_output=True)
        return "patch applied" if proc.returncode == 0 else f"patch failed: {(proc.stderr or '')[:300]}"
    if action_type == "draft_external_update" and os.getenv("MYOS_ACTION_COMMAND", "").strip():
        return _execute_action_provider(conn, row, payload)
    if action_type.startswith("draft_"):
        draft = str(payload.get("draft", "Draft ready for review."))
        return f"draft ready: {draft}"
    return "marked complete; external mutation adapter not configured"


def _execute_action_provider(conn, row, payload: dict[str, object]) -> str:
    command = os.getenv("MYOS_ACTION_COMMAND", "").strip()
    provider = os.getenv("MYOS_ACTION_PROVIDER", "command")
    clean_payload = redact_obj(conn, payload)  # redact leaves, never the JSON envelope (C-3)
    request = {
        "action_id": row["id"],
        "agent_task_id": row["agent_task_id"],
        "action_type": row["action_type"],
        "title": apply_privacy_filters(conn, row["title"]),
        "payload": clean_payload,
        "safety": {
            "approved": row["status"] in ("approved", "executing"),
            "requires_approval": bool(row["requires_approval"]),
        },
    }
    started = time.monotonic()
    status = "error"
    response_json = ""
    error = ""
    try:
        proc = subprocess.run(
            shlex.split(command),
            input=json.dumps(request, ensure_ascii=True),
            capture_output=True,
            text=True,
            timeout=int(get_policy_map(conn).get("action_timeout_sec", "30")),
            check=False,
        )
        if proc.returncode != 0:
            error = (proc.stderr or proc.stdout or f"exit={proc.returncode}")[:1000]
            raise RuntimeError(error)
        status = "ok"
        response_json = (proc.stdout or "{}")[:8000]
        return f"provider executed: {response_json[:300]}"
    except Exception as exc:
        error = str(exc)[:1000]
        return f"provider execution failed: {error}"
    finally:
        conn.execute(
            """
            INSERT INTO action_provider_executions (
                agent_action_id, provider, status, request_json, response_json, error, latency_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                provider,
                status,
                json.dumps(request, ensure_ascii=True)[:8000],
                response_json,
                error,
                int((time.monotonic() - started) * 1000),
            ),
        )


def _read_provider_stdin() -> dict[str, object]:
    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("action-provider requires JSON on stdin")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("action-provider input must be a JSON object")
    return parsed


def _outbox_write(
    conn,
    *,
    agent_action_id: int | None,
    provider: str,
    target_type: str,
    target_ref: str,
    title: str,
    body: str,
    status: str,
    payload: dict[str, object],
) -> int:
    clean_payload = redact_obj(conn, payload)  # redact leaves, never the JSON envelope (C-3)
    conn.execute(
        """
        INSERT INTO action_outbox (
            agent_action_id, provider, target_type, target_ref, title, body, status, payload_json, sent_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? = 'sent' THEN CURRENT_TIMESTAMP ELSE NULL END)
        """,
        (
            agent_action_id,
            provider,
            target_type,
            target_ref,
            title,
            body,
            status,
            json.dumps(clean_payload, ensure_ascii=True),
            status,
        ),
    )
    outbox_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    out_dir = resolve_db_path().parent / "outbox"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_target = re.sub(r"[^A-Za-z0-9_.-]+", "-", target_ref or target_type).strip("-") or "draft"
    (out_dir / f"action-{outbox_id}-{safe_target}.md").write_text(f"# {title}\n\n{body}\n")
    return outbox_id


def _provider_body(payload: dict[str, object]) -> str:
    return str(payload.get("draft") or payload.get("body") or payload.get("text") or "").strip()


def _provider_target_summary(payload: dict[str, object]) -> str:
    target = str(payload.get("target") or payload.get("target_type") or "outbox").lower()
    if target == "jira":
        return f"jira:{payload.get('issue_key') or 'missing_issue_key'}"
    if target == "github":
        owner = payload.get("owner") or os.getenv("GITHUB_OWNER", "")
        repo = payload.get("repo") or os.getenv("GITHUB_REPO", "")
        number = payload.get("issue_number") or payload.get("pr_number") or "missing_number"
        return f"github:{owner}/{repo}#{number}"
    return target


def _post_jira_comment(issue_key: str, body: str) -> str:
    base_url = os.getenv("JIRA_BASE_URL", "").rstrip("/")
    email = os.getenv("JIRA_USER_EMAIL", "")
    token = os.getenv("JIRA_API_TOKEN", "")
    if not (base_url and email and token and issue_key):
        raise ValueError("missing Jira target or credentials")
    auth = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
    payload = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": body}],
                }
            ],
        }
    }
    req = urllib.request.Request(
        f"{base_url}/rest/api/3/issue/{issue_key}/comment",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")[:1000]


def _post_github_comment(payload: dict[str, object], body: str) -> str:
    token = os.getenv("GITHUB_TOKEN", "")
    owner = str(payload.get("owner") or os.getenv("GITHUB_OWNER", "")).strip()
    repo = str(payload.get("repo") or os.getenv("GITHUB_REPO", "")).strip()
    issue_number = str(payload.get("issue_number") or payload.get("pr_number") or "").strip()
    if not (token and owner and repo and issue_number):
        raise ValueError("missing GitHub target or credentials")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments",
        data=json.dumps({"body": body}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")[:1000]


def approve_and_execute(conn, action_id: int, *, do_approve: bool = True, execute: bool = True) -> dict:
    """Approve and/or execute a single agent_action. Returns a structured outcome
    (no printing / no SystemExit) so both cmd_act and _handle_proposals can drive it.

    code ∈ not_found | approved_only | noop | needs_approval | already_executed
          | already_handled | executed | failed
    """
    row = conn.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
    if not row:
        return {"code": "not_found", "approved": False, "result": "", "status": ""}
    approved = False
    if do_approve and row["status"] in ("proposed", "failed"):
        conn.execute("UPDATE agent_actions SET status='approved' WHERE id = ?", (action_id,))
        conn.commit()
        approved = True
        row = conn.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
    if not execute:
        return {"code": "approved_only" if approved else "noop", "approved": approved, "result": "", "status": row["status"]}
    if row["requires_approval"] and row["status"] not in ("approved", "executed"):
        return {"code": "needs_approval", "approved": approved, "result": "", "status": row["status"]}
    if row["status"] == "executed":
        return {"code": "already_executed", "approved": approved, "result": "", "status": "executed"}
    claim = conn.execute(
        "UPDATE agent_actions SET status='executing' WHERE id=? AND status=?",
        (action_id, row["status"]),
    )
    if claim.rowcount == 0:
        conn.commit()
        return {"code": "already_handled", "approved": approved, "result": "", "status": row["status"]}
    conn.commit()
    row = conn.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
    result = _execute_agent_action(conn, row)
    new_status = _status_from_result(result)
    conn.execute(
        "UPDATE agent_actions SET status=?, "
        "executed_at=CASE WHEN ?='executed' THEN CURRENT_TIMESTAMP ELSE executed_at END, result=? WHERE id = ?",
        (new_status, new_status, result, action_id),
    )
    conn.execute(
        """
        INSERT INTO agent_observations (agent_task_id, observation_type, content, confidence)
        VALUES (?, 'action_result', ?, 0.85)
        """,
        (row["agent_task_id"], f"action #{action_id}: {result}"),
    )
    append_event(
        conn,
        "agent_action_executed",
        "agent_action",
        action_id,
        json.dumps({"task_id": row["agent_task_id"], "result": result}, ensure_ascii=True),
    )
    conn.commit()
    return {"code": "failed" if new_status == "failed" else "executed",
            "approved": approved, "result": result, "status": new_status}


def _print_exec_outcome(res: dict, action_id: int) -> None:
    code = res["code"]
    if code == "executed":
        print(f"    ✓ executed action #{action_id}: {res['result']}")
    elif code == "failed":
        print(f"    ✗ action #{action_id} failed: {res['result']}")
    elif code == "needs_approval":
        print(f"    action #{action_id} still needs approval.")
    else:
        print(f"    action #{action_id}: {code}")


def _handle_proposals(conn, action_ids: list[int]) -> None:
    """Apply graded autonomy to each proposed action: auto-run, one-tap confirm, or block."""
    level = autonomy.level_from_policy(conn)
    for aid in action_ids:
        row = conn.execute(
            "SELECT id, action_type, title, payload_json, status, requires_approval FROM agent_actions WHERE id = ?",
            (aid,),
        ).fetchone()
        if not row:
            continue
        payload = json.loads(row["payload_json"] or "{}")
        verdict = autonomy.classify_action(row["action_type"], payload, level=level)
        tier = verdict["tier"]
        print(f"  • action #{row['id']} [{row['action_type']}] {row['title']}  ({tier})")
        print(f"    target: {_provider_target_summary(payload)}")
        body = _provider_body(payload)
        if body:
            print(f"    draft: {body if len(body) <= 300 else body[:297] + '...'}")

        if tier == autonomy.BLOCKED:
            print(f"    ⛔ blocked ({verdict['reason']}). Will not auto-execute — do this manually.")
            continue
        if tier == autonomy.AUTO:
            print("    ▶ auto-executing (safe)…")
            _print_exec_outcome(approve_and_execute(conn, row["id"], do_approve=True, execute=True), row["id"])
            continue
        # confirm tier — one-tap
        try:
            answer = input(f"    Approve action #{row['id']} now? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = "n"
        if answer == "y":
            _print_exec_outcome(approve_and_execute(conn, row["id"], do_approve=True, execute=True), row["id"])
        else:
            print(f"    left in queue — approve later with `myos approve --action {row['id']} --execute`.")

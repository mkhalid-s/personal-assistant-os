"""Generic agent-CLI backend driven as a subprocess.

Base for external agent CLIs and the catch-all ``command`` backend. Speaks the
shared contract two ways:

* ``input_mode="json"`` (the legacy ``MYOS_AI_COMMAND`` path): send the request as
  JSON on stdin, expect ``{plan, actions}`` JSON on stdout.
* ``input_mode="prompt"`` (raw agent CLIs): send a rendered prompt on
  stdin, capture freeform stdout as ``reply``; if the agent happens to emit a JSON
  ``{plan, actions}`` block we parse it, otherwise there are simply no proposals.

Hardened vs. the original inline path: ``shlex.split`` (never ``shell=True``),
timeout, errors degrade to an empty response, and each call is audited to
``ai_provider_calls``.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time

from . import BaseBackend

_REASON_INSTRUCTION = (
    "You are a planning assistant. Answer the objective. If you want to propose "
    "concrete follow-up actions, end your reply with a single JSON object on its "
    'own line: {"plan":[{"step","detail"}],"actions":[{"action_type","title",'
    '"payload","requires_approval"}]}. Only use action_type "create_inbox_item" '
    "for safe local notes; everything else stays requires_approval=1."
)


class AgentCliBackend(BaseBackend):
    def __init__(
        self,
        name: str = "command",
        command: str | None = None,
        input_mode: str = "json",
        timeout: int = 120,
    ) -> None:
        self.name = name
        self.input_mode = input_mode
        self.timeout = timeout
        self._command = command if command is not None else self._default_command()

    def _default_command(self) -> str:
        env_specific = os.getenv(f"MYOS_AGENT_CMD_{self.name.upper()}", "").strip()
        return env_specific or os.getenv("MYOS_AI_COMMAND", "").strip()

    def _argv(self) -> list[str]:
        return shlex.split(self._command) if self._command else []

    def available(self) -> tuple[bool, str]:
        argv = self._argv()
        if not argv:
            return False, f"no command configured (set MYOS_AGENT_CMD_{self.name.upper()} or MYOS_AI_COMMAND)"
        exe = shutil.which(argv[0])
        if not exe:
            return False, f"executable not found on PATH: {argv[0]}"
        return True, exe

    # -- executor / harness mode -------------------------------------------------
    def executor_argv(self, task_text: str) -> list[str] | None:
        """argv to run the agent on a freeform coding task (overridden per CLI)."""
        argv = self._argv()
        return (argv + [task_text]) if argv else None

    # -- brain mode --------------------------------------------------------------
    def reason(self, conn, request: dict) -> dict:
        argv = self._argv()
        if not argv:
            return {"reply": "", "plan": [], "actions": []}
        payload_in = (
            json.dumps(request, ensure_ascii=True)
            if self.input_mode == "json"
            else _render_prompt(request)
        )
        started = time.monotonic()
        status, error, raw = "error", "", ""
        try:
            proc = subprocess.run(
                argv,
                input=payload_in,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
            raw = proc.stdout or ""
            if proc.returncode != 0:
                error = (proc.stderr or proc.stdout or f"exit={proc.returncode}")[:1000]
                raise RuntimeError(error)
            result = _parse_output(raw)
            status = "ok"
            return result
        except Exception as exc:  # noqa: BLE001 - degrade, never crash the REPL
            error = error or str(exc)[:1000]
            return {"reply": "", "plan": [], "actions": []}
        finally:
            _audit(conn, self.name, request, raw, status, error, int((time.monotonic() - started) * 1000))


def _render_prompt(request: dict) -> str:
    parts = [_REASON_INSTRUCTION, "", f"Objective: {request.get('objective', '')}"]
    if request.get("context"):
        parts.append(f"Context:\n{request['context']}")
    return "\n".join(parts)


def _parse_output(raw: str) -> dict:
    text = (raw or "").strip()
    obj = _extract_json_object(text)
    if isinstance(obj, dict) and ("plan" in obj or "actions" in obj):
        return {
            "reply": str(obj.get("reply", "")).strip() or text,
            "plan": obj.get("plan") or [],
            "actions": obj.get("actions") or [],
        }
    return {"reply": text, "plan": [], "actions": []}


def _extract_json_object(text: str):
    # Try whole-string first, then EVERY balanced top-level {...} span — return the
    # first that parses and looks like our contract. (Finding #5: a trailing
    # `{see you}` sign-off must not discard the real {plan,actions} object.)
    try:
        whole = json.loads(text)
        if isinstance(whole, dict):
            return whole
    except Exception:
        pass
    spans, start, depth = [], -1, 0
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append(text[start : i + 1])
    parsed = []
    for s in spans:
        try:
            obj = json.loads(s)
        except Exception:
            continue
        if isinstance(obj, dict):
            parsed.append(obj)
    for obj in parsed:
        if "plan" in obj or "actions" in obj:
            return obj
    return parsed[0] if parsed else None


def _audit(conn, provider, request, raw, status, error, latency_ms) -> None:
    try:
        # Redact before persisting: the request carries the user's objective/context and the
        # raw stdout is freeform provider output — both can contain emails/phones/secrets, so
        # the audit row must honor the same privacy policy as everything else (finding P1a).
        from ..privacy import apply_privacy_filters, redact_obj

        safe_request = json.dumps(redact_obj(conn, request), ensure_ascii=True)[:8000]
        safe_raw = apply_privacy_filters(conn, raw or "")[:8000]
        conn.execute(
            """
            INSERT INTO ai_provider_calls (provider, purpose, status, request_json, response_json, error, latency_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider,
                str(request.get("purpose", "chat")),
                status,
                safe_request,
                safe_raw,
                apply_privacy_filters(conn, error or "")[:1000],
                latency_ms,
            ),
        )
        conn.commit()
    except Exception:  # pragma: no cover - auditing must never break the call
        pass

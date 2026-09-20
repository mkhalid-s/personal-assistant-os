"""Devin backend integration for MYOS.

This backend allows MYOS to use Devin (Cognition's AI agent) as its reasoning engine.
Devin can be invoked either via the Devin CLI or through direct API calls.

The backend follows the MYOS contract:
- request: {"purpose", "objective", "context", "analogies", ...}
- response: {"reply": str, "plan": [{"step","detail"}], "actions": [action,...]}

All actions are proposed and go through MYOS's approval queue for safety.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time

from . import BaseBackend

_SYSTEM_PROMPT = """You are MYOS, an always-on personal chief-of-staff integrated with Devin.
You help with software engineering tasks including testing, code review, and development.

How you work:
- Use MYOS's read tools to understand the current state before proposing changes
- For code changes, testing, or external mutations, propose them via actions
- All external mutations require approval through MYOS's safety system
- Be concise and focus on the user's objective
- When running tests, provide clear summaries of results and any issues found
"""


class DevinBackend(BaseBackend):
    name = "devin"

    def __init__(self, command: str | None = None, timeout: int = 300):
        self.command = command or os.getenv("MYOS_DEVIN_COMMAND", "devin")
        self.timeout = timeout

    def available(self) -> tuple[bool, str]:
        """Check if Devin CLI is available."""
        exe = shutil.which(self.command.split()[0] if " " in self.command else self.command)
        if exe:
            return True, exe
        return False, f"Devin command not found: {self.command}"

    def reason(self, conn, request: dict) -> dict:
        """Call Devin to reason about the request and return structured output."""
        objective = request.get("objective", "")
        context = request.get("context", "")
        analogies = request.get("analogies", [])

        # Build prompt for Devin
        prompt = self._build_prompt(objective, context, analogies)

        started = time.monotonic()
        status, error, raw = "error", "", ""

        try:
            # Call Devin CLI with --print flag for non-interactive mode
            argv = shlex.split(self.command) if " " in self.command else [self.command]
            proc = subprocess.run(
                argv + ["--print", prompt],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
            raw = proc.stdout or ""

            if proc.returncode != 0:
                error = (proc.stderr or proc.stdout or f"exit={proc.returncode}")[:1000]
                raise RuntimeError(error)

            result = self._parse_devin_output(raw)
            status = "ok"
            return result

        except subprocess.TimeoutExpired:
            error = f"Devin command timed out after {self.timeout}s"
            return {"reply": error, "plan": [], "actions": []}
        except Exception as exc:
            error = error or str(exc)[:1000]
            return {"reply": f"Devin error: {error}", "plan": [], "actions": []}
        finally:
            self._audit(conn, request, raw, status, error, int((time.monotonic() - started) * 1000))

    def _build_prompt(self, objective: str, context: str, analogies: list) -> str:
        """Build the prompt to send to Devin."""
        parts = [_SYSTEM_PROMPT, ""]

        if objective:
            parts.append(f"Objective: {objective}")

        if context:
            parts.append(f"Context:\n{context}")

        if analogies:
            parts.append("Relevant context from previous work:")
            for score, source, content in analogies[:3]:
                parts.append(f"- {source} (relevance {score:.2f}): {content[:200]}...")

        parts.append("""
Respond with a JSON object at the end containing:
{
  "reply": "your text response",
  "plan": [{"step": "step name", "detail": "description"}],
  "actions": [
    {
      "action_type": "create_inbox_item|draft_external_update|apply_patch|etc",
      "title": "action title",
      "payload": {...},
      "requires_approval": true
    }
  ]
}
""")

        return "\n".join(parts)

    def _parse_devin_output(self, raw: str) -> dict:
        """Parse Devin's output and extract the structured response."""
        text = (raw or "").strip()

        # Try to extract JSON object from the output
        obj = self._extract_json_object(text)
        if isinstance(obj, dict) and ("plan" in obj or "actions" in obj):
            return {
                "reply": str(obj.get("reply", "")).strip() or text,
                "plan": obj.get("plan") or [],
                "actions": obj.get("actions") or [],
            }

        # Fallback: no structured output found
        return {"reply": text, "plan": [], "actions": []}

    def _extract_json_object(self, text: str):
        """Extract the first JSON object that looks like our contract."""
        try:
            whole = json.loads(text)
            if isinstance(whole, dict):
                return whole
        except Exception:
            pass

        # Find balanced JSON objects
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

    def _audit(self, conn, request, raw, status, error, latency_ms) -> None:
        """Audit the Devin call for safety and monitoring."""
        try:
            from ..privacy import apply_privacy_filters, redact_obj

            safe_request = json.dumps(redact_obj(conn, request), ensure_ascii=True)[:8000]
            safe_raw = apply_privacy_filters(conn, raw or "")[:8000]

            conn.execute(
                """
                INSERT INTO ai_provider_calls (provider, purpose, status, request_json, response_json, error, latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "devin",
                    str(request.get("purpose", "chat")),
                    status,
                    safe_request,
                    safe_raw,
                    apply_privacy_filters(conn, error or "")[:1000],
                    latency_ms,
                ),
            )
            conn.commit()
        except Exception:
            pass  # Auditing must never break the call

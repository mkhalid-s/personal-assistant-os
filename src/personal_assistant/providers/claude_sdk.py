"""Claude Agent SDK backend — the engine Claude Code runs on, as a library.

Gives the brain *real doing power*: file read/edit/write, bash, test-running, the
user's MCP servers (Jira/GitHub/Confluence/Slack), and subagents. Every tool call
is gated by `autonomy.classify_tool` through the SDK's `can_use_tool` callback:

    auto    -> allow silently
    confirm -> one-tap [y/N] (or deny when running unattended / no TTY)
    blocked -> deny with a reason

Local-only + Bedrock: set `MYOS_LLM_BACKEND=bedrock` and we export
`CLAUDE_CODE_USE_BEDROCK=1` so the SDK routes through an AWS Bedrock transport.

Requires the `claude-agent-sdk` package and a Node `claude` engine. If either is
missing, `available()` reports false and the orchestrator falls back to the raw
`claude` backend (no live edit/bash, but everything else still works). Any runtime
error in a turn also degrades to the raw brain rather than crashing the REPL.
"""

from __future__ import annotations

import asyncio
import os
import sys

from .. import autonomy
from . import BaseBackend, _history_to_context

SYSTEM_PROMPT_SDK = (
    "You are MYOS, an always-on chief-of-staff and coding partner for a Staff/Senior "
    "engineering manager. You can read/edit files, run commands and tests, "
    "and use connected tools (Jira/GitHub/Confluence/Slack) to actually get work done. "
    "A safety policy gates your tools: safe/read actions run automatically, reversible "
    "changes ask for one tap, and destructive/major actions are blocked — never try to "
    "circumvent it. Be concise and lead with the outcome."
)


class ClaudeSdkBackend(BaseBackend):
    name = "claude-sdk"

    def __init__(self) -> None:
        self.model = os.getenv("MYOS_CLAUDE_MODEL", "").strip() or "claude-opus-4-8"

    def available(self) -> tuple[bool, str]:
        try:
            import claude_agent_sdk  # noqa: F401
        except Exception:
            return False, "claude-agent-sdk not installed (pip install claude-agent-sdk; needs Node `claude`)"
        backend = os.getenv("MYOS_LLM_BACKEND", "").strip().lower()
        if backend in ("bedrock", "aws"):
            return True, f"{backend} transport"
        if os.getenv("ANTHROPIC_API_KEY", "").strip():
            return True, "ANTHROPIC_API_KEY set"
        return False, "ANTHROPIC_API_KEY not set (or set MYOS_LLM_BACKEND=bedrock|aws)"

    def _configure_env(self) -> None:
        if os.getenv("MYOS_LLM_BACKEND", "").strip().lower() == "bedrock":
            os.environ.setdefault("CLAUDE_CODE_USE_BEDROCK", "1")

    def _make_can_use_tool(self, level: str, interactive: bool):
        from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

        async def can_use_tool(tool_name, tool_input, context):  # noqa: ANN001
            verdict = autonomy.classify_tool(tool_name, tool_input or {}, level=level)
            tier = verdict["tier"]
            if tier == autonomy.AUTO:
                return PermissionResultAllow(updated_input=tool_input)
            if tier == autonomy.BLOCKED:
                return PermissionResultDeny(message=f"Blocked by autonomy policy: {verdict['reason']}")
            # confirm tier
            if not interactive:
                return PermissionResultDeny(message="Needs confirmation but running unattended; skipped.")
            try:
                ans = input(f"\n  ▸ Allow tool '{tool_name}'? ({verdict['reason']}) [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            return (
                PermissionResultAllow(updated_input=tool_input)
                if ans == "y"
                else PermissionResultDeny(message="User declined")
            )

        return can_use_tool

    def run_turn(self, conn, user_text: str, history: list[dict], on_text=None) -> dict:
        level = autonomy.level_from_policy(conn)
        try:
            return asyncio.run(self._arun_turn(user_text, history, on_text, level))
        except Exception as exc:  # noqa: BLE001 - degrade to the raw brain, never crash
            from .claude import ClaudeBackend

            print(f"[claude-sdk unavailable: {exc}; falling back to raw Claude brain]")
            return ClaudeBackend().run_turn(conn, user_text, history, on_text=on_text)

    def _options_kwargs(self, level: str, interactive: bool) -> dict:
        """Build the ClaudeAgentOptions kwargs. Extracted so the setting_sources
        gating is unit-testable without the SDK installed (review A6)."""
        kwargs = dict(
            system_prompt=SYSTEM_PROMPT_SDK,
            model=self.model,
            can_use_tool=self._make_can_use_tool(level, interactive),
        )
        # SECURITY (review finding #1): do NOT load .claude/settings.json by default.
        # Its `permissions.allow` rules short-circuit can_use_tool in the Agent SDK,
        # which would let an allow-listed tool (e.g. `Bash(git :*)`) bypass the
        # autonomy gate entirely — including BLOCKED/destructive. Keeping settings
        # unloaded makes can_use_tool the SOLE authority. Opt in only if you have
        # vetted the project/user allow-list and accept that it overrides the gate.
        if os.getenv("MYOS_SDK_LOAD_SETTINGS", "").strip().lower() in ("1", "true", "yes"):
            kwargs["setting_sources"] = ["project", "user"]
        return kwargs

    async def _arun_turn(self, user_text: str, history: list[dict], on_text, level: str) -> dict:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        self._configure_env()
        # Minimal continuity: prepend recent history as context (each turn is a fresh
        # client; full session resume is a later enhancement).
        prompt = user_text
        ctx = _history_to_context(history)
        if ctx:
            prompt = f"Recent conversation:\n{ctx}\n\nNow: {user_text}"

        options = ClaudeAgentOptions(**self._options_kwargs(level, sys.stdin.isatty()))
        reply_parts: list[str] = []
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                for block in getattr(message, "content", None) or []:
                    text = getattr(block, "text", None)
                    if text:
                        reply_parts.append(text)
                        if on_text is not None:
                            on_text(text)

        reply = "".join(reply_parts).strip()
        new_history = history + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": reply or "(no reply)"},
        ]
        return {"reply": reply, "proposed_action_ids": [], "history": new_history, "backend": "claude-sdk"}

    def reason(self, conn, request: dict) -> dict:
        # One-shot structured reasoning is simpler via the raw Messages API (also
        # honors Bedrock), so delegate the batch path to the raw Claude backend.
        from .claude import ClaudeBackend

        return ClaudeBackend().reason(conn, request)

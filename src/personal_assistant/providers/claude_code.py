"""Claude Code CLI adapter.

This is the subprocess-backed Claude Code path. The SDK-backed Claude Code path
uses ``providers.claude_sdk`` and MYOS's tool permission callback.
"""

from __future__ import annotations

import os
import shlex
import shutil

from .agent_cli import AgentCliBackend


class ClaudeCodeBackend(AgentCliBackend):
    def __init__(self) -> None:
        super().__init__(name="claude-code", input_mode="prompt_arg")

    def _default_command(self) -> str:
        return os.getenv("MYOS_AGENT_CMD_CLAUDE_CODE", "").strip() or "claude -p"

    def available(self) -> tuple[bool, str]:
        argv = self._argv()
        exe = argv[0] if argv else "claude"
        found = shutil.which(exe)
        if not found:
            return False, f"Claude Code CLI executable not found on PATH: {exe} (install `claude` or set MYOS_AGENT_CMD_CLAUDE_CODE)"
        return True, found

    def executor_argv(self, task_text: str) -> list[str] | None:
        override = os.getenv("MYOS_AGENT_EXEC_CLAUDE_CODE", "").strip()
        if override:
            return shlex.split(override) + [task_text]
        if shutil.which("claude"):
            return ["claude", "-p", task_text]
        return None

"""Cursor CLI (``cursor-agent``) adapter.

Exact flags vary by version, so both invocations are env-overridable:

* brain mode    -> ``MYOS_AGENT_CMD_CURSOR``  (default: ``cursor-agent``, prompt on stdin)
* executor mode -> ``MYOS_AGENT_EXEC_CURSOR`` (default: ``cursor-agent -p`` + task, run
  in an isolated worktree by the orchestrator)

The orchestrator runs the executor invocation inside a throwaway git worktree and
turns the resulting diff into a proposal, so a harnessed Cursor run never edits the
real tree without approval.
"""

from __future__ import annotations

import os
import shlex
import shutil

from .agent_cli import AgentCliBackend


class CursorBackend(AgentCliBackend):
    def __init__(self) -> None:
        super().__init__(name="cursor", input_mode="prompt")

    def _default_command(self) -> str:
        return os.getenv("MYOS_AGENT_CMD_CURSOR", "").strip() or "cursor-agent"

    def available(self) -> tuple[bool, str]:
        argv = self._argv()
        exe = argv[0] if argv else "cursor-agent"
        found = shutil.which(exe) or shutil.which("cursor-agent")
        if not found:
            return False, "cursor-agent CLI not found on PATH (install Cursor CLI or set MYOS_AGENT_CMD_CURSOR)"
        return True, found

    def executor_argv(self, task_text: str) -> list[str] | None:
        override = os.getenv("MYOS_AGENT_EXEC_CURSOR", "").strip()
        if override:
            return shlex.split(override) + [task_text]
        if shutil.which("cursor-agent"):
            return ["cursor-agent", "-p", task_text]
        return None

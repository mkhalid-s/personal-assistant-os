"""GitHub Copilot CLI adapter.

Exact flags vary by Copilot CLI version, so both invocations are env-overridable:

* brain mode   -> ``MYOS_AGENT_CMD_COPILOT``  (default: ``copilot``, prompt on stdin)
* executor mode-> ``MYOS_AGENT_EXEC_COPILOT`` (default: ``copilot -p`` + task)

Point these at whatever your installed CLI expects (e.g. a small wrapper that
speaks the JSON contract for richer proposals).
"""

from __future__ import annotations

import os
import shlex
import shutil

from .agent_cli import AgentCliBackend


class CopilotBackend(AgentCliBackend):
    def __init__(self) -> None:
        super().__init__(name="copilot", input_mode="prompt")

    def _default_command(self) -> str:
        return os.getenv("MYOS_AGENT_CMD_COPILOT", "").strip() or "copilot"

    def available(self) -> tuple[bool, str]:
        argv = self._argv()
        exe = argv[0] if argv else "copilot"
        found = shutil.which(exe) or shutil.which("copilot") or shutil.which("gh")
        if not found:
            return False, "copilot CLI not found on PATH (install GitHub Copilot CLI or set MYOS_AGENT_CMD_COPILOT)"
        return True, found

    def executor_argv(self, task_text: str) -> list[str] | None:
        override = os.getenv("MYOS_AGENT_EXEC_COPILOT", "").strip()
        if override:
            return shlex.split(override) + [task_text]
        if shutil.which("copilot"):
            return ["copilot", "-p", task_text]
        if shutil.which("gh"):
            return ["gh", "copilot", "suggest", "-t", "shell", task_text]
        return None

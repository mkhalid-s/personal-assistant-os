"""Cursor Agent CLI adapter.

Brain mode defaults to read-only/ask mode so MYOS chat can use Cursor as a
reasoning backend without granting direct write power. Executor mode is separate
and runs only through MYOS's existing worktree/proposal harness.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from .agent_cli import AgentCliBackend


class CursorBackend(AgentCliBackend):
    def __init__(self) -> None:
        super().__init__(name="cursor", input_mode="prompt_arg")

    def _default_command(self) -> str:
        override = os.getenv("MYOS_AGENT_CMD_CURSOR", "").strip()
        if override:
            return override
        if shutil.which("cursor") and not shutil.which("agent"):
            return "cursor agent --print --trust --mode ask --output-format text"
        return "agent --print --trust --mode ask --output-format text"

    def available(self) -> tuple[bool, str]:
        argv = self._argv()
        exe = argv[0] if argv else "agent"
        found = shutil.which(exe)
        if not found:
            return False, f"Cursor Agent CLI executable not found on PATH: {exe} (install Cursor CLI or set MYOS_AGENT_CMD_CURSOR)"
        if Path(found).name == "agent":
            try:
                proc = subprocess.run(
                    [found, "status"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                status = (proc.stdout or proc.stderr or "").strip().splitlines()
                if proc.returncode == 0 and status:
                    return True, f"{found} ({status[0]})"
            except Exception:
                pass
        return True, found

    def executor_argv(self, task_text: str) -> list[str] | None:
        override = os.getenv("MYOS_AGENT_EXEC_CURSOR", "").strip()
        if override:
            return shlex.split(override) + [task_text]
        if shutil.which("agent"):
            return ["agent", "--print", "--trust", "--output-format", "text", task_text]
        if shutil.which("cursor"):
            return ["cursor", "agent", "--print", "--trust", "--output-format", "text", task_text]
        return None

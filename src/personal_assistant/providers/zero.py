"""GitLawb Zero CLI adapter.

Zero is used as an optional coding executor. MYOS still owns routing, memory,
review packets, approvals, and audit; Zero is only the repo-scoped worker.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

from .agent_cli import AgentCliBackend


class ZeroBackend(AgentCliBackend):
    def __init__(self) -> None:
        super().__init__(name="zero", input_mode="prompt_arg")

    def _default_command(self) -> str:
        return (
            os.getenv("MYOS_AGENT_CMD_ZERO", "").strip()
            or "zero exec --output-format text --auto low --no-notify"
        )

    def available(self) -> tuple[bool, str]:
        argv = self._argv()
        exe = argv[0] if argv else "zero"
        found = shutil.which(exe)
        if not found:
            return False, f"Zero CLI executable not found on PATH: {exe} (install `zero` or set MYOS_AGENT_CMD_ZERO)"
        try:
            proc = subprocess.run(
                [found, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 - availability should be diagnostic, not fatal
            return False, f"{found} (--version failed: {exc})"
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or f"exit={proc.returncode}").strip()
            return False, f"{found} (--version failed: {detail[:200]})"
        version = (proc.stdout or proc.stderr or "").strip().splitlines()
        return True, f"{found} ({version[0] if version else 'version unknown'})"

    def executor_argv(self, task_text: str) -> list[str] | None:
        override = os.getenv("MYOS_AGENT_EXEC_ZERO", "").strip()
        if override:
            return shlex.split(override) + [task_text]
        if shutil.which("zero"):
            return [
                "zero",
                "exec",
                "--output-format",
                "text",
                "--auto",
                "low",
                "--no-notify",
                task_text,
            ]
        return None

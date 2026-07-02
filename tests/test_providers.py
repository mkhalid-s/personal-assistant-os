from __future__ import annotations

import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path


class ProviderBackendTest(unittest.TestCase):
    def _write_executable(self, path: Path, body: str) -> None:
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _conn(self) -> sqlite3.Connection:
        from personal_assistant.db import initialize_schema

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        initialize_schema(conn)
        return conn

    def test_cursor_backend_is_first_class_and_uses_prompt_argument(self) -> None:
        from personal_assistant.providers import available_backends, get_backend

        old_path = os.environ.get("PATH", "")
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            agent = bin_dir / "agent"
            self._write_executable(
                agent,
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "if len(sys.argv) > 1 and sys.argv[1] == 'status':\n"
                "    print('Logged in as fake@example.com')\n"
                "    raise SystemExit(0)\n"
                "print('cursor reply: ' + sys.argv[-1])\n",
            )
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
            try:
                backend = get_backend("cursor")
                self.assertEqual(backend.name, "cursor")
                ok, detail = backend.available()
                self.assertTrue(ok)
                self.assertIn("Logged in", detail)
                conn = self._conn()
                result = backend.reason(conn, {"purpose": "chat", "objective": "hello", "context": ""})
                self.assertIn("cursor reply:", result["reply"])
                row = conn.execute("SELECT provider, status FROM ai_provider_calls").fetchone()
                self.assertEqual(row["provider"], "cursor")
                self.assertEqual(row["status"], "ok")
                names = {item["name"] for item in available_backends()}
                self.assertIn("cursor", names)
            finally:
                os.environ["PATH"] = old_path

    def test_claude_code_backend_and_sdk_aliases(self) -> None:
        from personal_assistant.providers import available_backends, get_backend

        old_path = os.environ.get("PATH", "")
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            claude = bin_dir / "claude"
            self._write_executable(
                claude,
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "print('claude-code reply: ' + sys.argv[-1])\n",
            )
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
            try:
                backend = get_backend("claude-code")
                self.assertEqual(backend.name, "claude-code")
                ok, detail = backend.available()
                self.assertTrue(ok)
                self.assertIn(str(claude), detail)
                conn = self._conn()
                result = backend.reason(conn, {"purpose": "chat", "objective": "hello", "context": ""})
                self.assertIn("claude-code reply:", result["reply"])
                self.assertEqual(get_backend("claudecode").name, "claude-code")
                self.assertEqual(get_backend("claude-code-sdk").name, "claude-sdk")
                names = {item["name"] for item in available_backends()}
                self.assertIn("claude-code", names)
                self.assertIn("claude-code-sdk", names)
            finally:
                os.environ["PATH"] = old_path

    def test_configured_backend_command_must_exist(self) -> None:
        from personal_assistant.providers import get_backend

        old_path = os.environ.get("PATH", "")
        old_cursor = os.environ.get("MYOS_AGENT_CMD_CURSOR")
        old_claude_code = os.environ.get("MYOS_AGENT_CMD_CLAUDE_CODE")
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            self._write_executable(bin_dir / "agent", "#!/usr/bin/env python3\nprint('agent fallback')\n")
            self._write_executable(bin_dir / "claude", "#!/usr/bin/env python3\nprint('claude fallback')\n")
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
            os.environ["MYOS_AGENT_CMD_CURSOR"] = "missing-cursor-agent --print"
            os.environ["MYOS_AGENT_CMD_CLAUDE_CODE"] = "missing-claude-code -p"
            try:
                cursor_ok, cursor_detail = get_backend("cursor").available()
                claude_ok, claude_detail = get_backend("claude-code").available()
                self.assertFalse(cursor_ok)
                self.assertIn("missing-cursor-agent", cursor_detail)
                self.assertFalse(claude_ok)
                self.assertIn("missing-claude-code", claude_detail)
            finally:
                os.environ["PATH"] = old_path
                if old_cursor is None:
                    os.environ.pop("MYOS_AGENT_CMD_CURSOR", None)
                else:
                    os.environ["MYOS_AGENT_CMD_CURSOR"] = old_cursor
                if old_claude_code is None:
                    os.environ.pop("MYOS_AGENT_CMD_CLAUDE_CODE", None)
                else:
                    os.environ["MYOS_AGENT_CMD_CLAUDE_CODE"] = old_claude_code

    def test_cursor_backend_supports_cursor_cli_without_agent_shim(self) -> None:
        from personal_assistant.providers import get_backend

        old_path = os.environ.get("PATH", "")
        old_cursor = os.environ.get("MYOS_AGENT_CMD_CURSOR")
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            cursor = bin_dir / "cursor"
            self._write_executable(
                cursor,
                f"#!{sys.executable}\n"
                "import sys\n"
                "print('cursor cli reply: ' + sys.argv[-1])\n",
            )
            os.environ["PATH"] = str(bin_dir)
            os.environ.pop("MYOS_AGENT_CMD_CURSOR", None)
            try:
                backend = get_backend("cursor")
                ok, detail = backend.available()
                self.assertTrue(ok)
                self.assertIn(str(cursor), detail)
                conn = self._conn()
                result = backend.reason(conn, {"purpose": "chat", "objective": "hello", "context": ""})
                self.assertIn("cursor cli reply:", result["reply"])
                self.assertEqual(
                    backend.executor_argv("do work")[:5],
                    ["cursor", "agent", "--print", "--trust", "--output-format"],
                )
            finally:
                os.environ["PATH"] = old_path
                if old_cursor is None:
                    os.environ.pop("MYOS_AGENT_CMD_CURSOR", None)
                else:
                    os.environ["MYOS_AGENT_CMD_CURSOR"] = old_cursor


if __name__ == "__main__":
    unittest.main()

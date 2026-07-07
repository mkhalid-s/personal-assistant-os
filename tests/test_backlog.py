"""Backlog / inventory smoke tests.

These tests are the executable counterpart to the command contract release
gate in `myos release-check --strict`. The contract audit validates
metadata (tiers, safety, examples, summaries, parser drift). This file
validates *runtime wiring*: for every command registered in
`command_registry.COMMAND_SPECS` we confirm that argparse can actually
render its help without crashing, and that either the top-level command
handler is bound or the required subparser dispatch is present.

If a future change adds a new command to the registry but forgets to
wire it into `build_parser`, or breaks an existing sub-parser, this test
fails deterministically — so a broken command can never ship past CI.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import unittest


class CommandInventorySmokeTest(unittest.TestCase):
    """Every registered CLI command must be reachable via argparse."""

    @classmethod
    def setUpClass(cls) -> None:
        from personal_assistant import cli, command_registry

        cls.parser: argparse.ArgumentParser = cli.build_parser()
        cls.subparser_action = next(
            action for action in cls.parser._actions if getattr(action, "dest", "") == "command"
        )
        cls.parser_commands: set[str] = set(cls.subparser_action.choices)
        cls.registered_commands: tuple = command_registry.COMMAND_SPECS

    def _run_help(self, *argv: str) -> int:
        """Invoke argparse `--help` for `argv` and return the resulting exit
        code. argparse raises SystemExit; a valid parser exits with code 0.
        stdout/stderr are captured so the test suite stays quiet."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                self.parser.parse_args([*argv, "--help"])
            except SystemExit as exc:
                code = exc.code
                if code is None:
                    return 0
                if isinstance(code, int):
                    return code
                return 1
            return 0

    def test_every_registered_command_renders_help(self) -> None:
        failures: list[str] = []
        for spec in self.registered_commands:
            with self.subTest(command=spec.command):
                if spec.command not in self.parser_commands:
                    failures.append(
                        f"{spec.command}: registered in command_registry but missing from build_parser subparsers"
                    )
                    continue
                exit_code = self._run_help(spec.command)
                if exit_code != 0:
                    failures.append(
                        f"{spec.command}: `myos {spec.command} --help` exited with code {exit_code} instead of 0"
                    )
        self.assertEqual(failures, [], "\n".join(failures))

    def _dispatch_failures(self, parser: argparse.ArgumentParser, path: str) -> list[str]:
        """Recursively verify every leaf parser under `parser` binds `func`.
        A parser is a leaf when it has no child subparser action; otherwise
        every child parser must also resolve to a leaf that binds `func`."""
        if parser.get_default("func") is not None:
            return []
        child_actions = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
        if not child_actions:
            return [f"{path}: no `func` bound and no child subparsers to dispatch to"]
        problems: list[str] = []
        for child_action in child_actions:
            for child_name, child_parser in child_action.choices.items():
                problems.extend(self._dispatch_failures(child_parser, f"{path} {child_name}"))
        return problems

    def test_every_registered_command_has_dispatch(self) -> None:
        """Every top-level command must eventually route to a `func` handler,
        either directly on its sub-parser or through any depth of nested
        required subparsers."""
        failures: list[str] = []
        for spec in self.registered_commands:
            with self.subTest(command=spec.command):
                sub_parser = self.subparser_action.choices.get(spec.command)
                if sub_parser is None:
                    failures.append(f"{spec.command}: no argparse subparser found")
                    continue
                failures.extend(self._dispatch_failures(sub_parser, spec.command))
        self.assertEqual(failures, [], "\n".join(failures))

    def test_root_parser_help_renders(self) -> None:
        exit_code = self._run_help()
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest


class CommandRegistryTest(unittest.TestCase):
    def test_registry_contains_key_commands_and_safety(self) -> None:
        from personal_assistant import command_registry

        inventory = command_registry.command_inventory()
        self.assertIn("do", inventory["daily"])
        self.assertIn("factory", inventory["workflow"])
        self.assertIn("context", inventory["expert"])
        self.assertIn("release-check", inventory["diagnostic"])

        approve = command_registry.find_command("approve")
        self.assertIsNotNone(approve)
        self.assertEqual(approve.safety, "approval_gated")
        self.assertTrue(approve.requires_confirmation)

        sync = command_registry.find_command("sync")
        self.assertIsNotNone(sync)
        self.assertEqual(sync.safety, "external_write")
        self.assertTrue(sync.requires_confirmation)

        loop = command_registry.find_command("loop")
        self.assertIsNotNone(loop)
        self.assertIn("ledger", loop.subcommands)

    def test_compact_catalog_is_model_safe_metadata(self) -> None:
        from personal_assistant import command_registry

        catalog = command_registry.compact_catalog(limit=5)
        self.assertEqual(len(catalog), 5)
        for item in catalog:
            self.assertIn("command", item)
            self.assertIn("usage", item)
            self.assertIn("tier", item)
            self.assertIn("safety", item)
            self.assertIn("intent", item)
            self.assertIn("subcommands", item)
            self.assertIn("required_args", item)
            self.assertNotIn("raw_text", item)

    def test_local_model_command_mapper_covers_all_registered_commands(self) -> None:
        from personal_assistant import command_registry

        mapper = command_registry.local_model_command_mapper()
        self.assertEqual(mapper["schema"], "myos.command_mapper.v1")
        commands = mapper["commands"]
        self.assertEqual(len(commands), len(command_registry.all_commands()))
        by_name = {item["command"]: item for item in commands}
        self.assertIn("factory", by_name)
        self.assertIn("policy", by_name["factory"]["subcommands"])
        self.assertIn("autopilot", by_name)
        self.assertIn("--loop-goal", by_name["autopilot"]["subcommands"])
        self.assertNotIn("raw_text", str(mapper))

    def test_filter_commands(self) -> None:
        from personal_assistant import command_registry

        workflow = command_registry.filter_commands(tier="workflow")
        self.assertTrue(workflow)
        self.assertTrue(all(spec.tier == "workflow" for spec in workflow))
        gated = command_registry.filter_commands(safety="approval_gated")
        self.assertTrue(any(spec.command == "factory" for spec in gated))

    def test_registry_covers_top_level_parser_commands(self) -> None:
        from personal_assistant import cli, command_registry

        parser = cli.build_parser()
        subparser_action = next(action for action in parser._actions if getattr(action, "dest", "") == "command")
        parser_commands = set(subparser_action.choices)
        registry_commands = {spec.command for spec in command_registry.all_commands()}
        self.assertEqual(sorted(parser_commands - registry_commands), [])

    def test_autonomy_loop_autopilot_and_factory_handlers_are_module_backed(self) -> None:
        from personal_assistant import cli, cli_autonomy, cli_autopilot, cli_factory

        self.assertTrue(callable(cli_autonomy.cmd_autonomy))
        self.assertTrue(callable(cli_autonomy.cmd_loop))
        self.assertTrue(callable(cli_autopilot.cmd_autopilot))
        self.assertTrue(callable(cli_autopilot.run_autopilot_cycle))
        self.assertTrue(callable(cli_factory.cmd_factory))
        parser = cli.build_parser()
        autonomy_args = parser.parse_args(["autonomy", "eval", "--no-record"])
        loop_args = parser.parse_args(["loop", "status"])
        autopilot_args = parser.parse_args(["autopilot", "--once"])
        factory_args = parser.parse_args(["factory", "policy", "list"])
        self.assertIs(autonomy_args.func, cli.cmd_autonomy)
        self.assertIs(loop_args.func, cli.cmd_loop)
        self.assertIs(autopilot_args.func, cli.cmd_autopilot)
        self.assertIs(factory_args.func, cli.cmd_factory)


if __name__ == "__main__":
    unittest.main()

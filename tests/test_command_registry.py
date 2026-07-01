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

    def test_compact_catalog_is_model_safe_metadata(self) -> None:
        from personal_assistant import command_registry

        catalog = command_registry.compact_catalog(limit=5)
        self.assertEqual(len(catalog), 5)
        for item in catalog:
            self.assertIn("command", item)
            self.assertIn("tier", item)
            self.assertIn("safety", item)
            self.assertIn("intent", item)
            self.assertNotIn("raw_text", item)

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


if __name__ == "__main__":
    unittest.main()

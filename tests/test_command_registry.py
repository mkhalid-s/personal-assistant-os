from __future__ import annotations

import json
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
            self.assertIn("side_effects", item)
            self.assertIn("dry_run_by_default", item)
            self.assertIn("long_running", item)
            self.assertIn("subcommands", item)
            self.assertIn("required_args", item)
            self.assertIn("examples", item)
            self.assertNotIn("raw_text", item)

    def test_local_model_command_mapper_covers_all_registered_commands(self) -> None:
        from personal_assistant import command_registry

        mapper = command_registry.local_model_command_mapper()
        self.assertEqual(mapper["schema"], "myos.command_mapper.v1")
        self.assertEqual(set(mapper["tiers"]), set(command_registry.TIERS))
        self.assertEqual(set(mapper["safety_levels"]), set(command_registry.SAFETY_LEVELS))
        self.assertEqual(set(mapper["side_effect_types"]), set(command_registry.SIDE_EFFECT_TYPES))
        commands = mapper["commands"]
        self.assertEqual(len(commands), len(command_registry.all_commands()))
        by_name = {item["command"]: item for item in commands}
        self.assertIn("factory", by_name)
        self.assertIn("policy", by_name["factory"]["subcommands"])
        self.assertIn("autopilot", by_name)
        self.assertIn("--loop-goal", by_name["autopilot"]["subcommands"])
        self.assertIn("myos autopilot --once --loop-goal", by_name["autopilot"]["examples"])
        self.assertIn("side_effect_types", mapper)
        self.assertNotIn("raw_text", str(mapper))

    def test_runtime_command_mapper_marks_side_effect_boundaries(self) -> None:
        from personal_assistant import command_registry

        mapper = command_registry.local_model_command_mapper()
        by_name = {item["command"]: item for item in mapper["commands"]}

        setup_live = by_name["setup-live"]
        self.assertTrue(setup_live["dry_run_by_default"])
        self.assertIn("local_file_write", setup_live["side_effects"])
        self.assertIn("local_db_write", setup_live["side_effects"])
        self.assertIn("os_service_write", setup_live["side_effects"])

        launchd_install = by_name["launchd-install"]
        self.assertTrue(launchd_install["dry_run_by_default"])
        self.assertTrue(launchd_install["requires_confirmation"])
        self.assertIn("os_service_write", launchd_install["side_effects"])

        start = by_name["start"]
        self.assertTrue(start["requires_confirmation"])
        self.assertIn("os_service_write", start["side_effects"])

        stop = by_name["stop"]
        self.assertTrue(stop["requires_confirmation"])
        self.assertIn("os_service_write", stop["side_effects"])

        restore = by_name["restore"]
        self.assertTrue(restore["requires_confirmation"])
        self.assertIn("database_restore", restore["side_effects"])

        dashboard = by_name["dashboard"]
        self.assertTrue(dashboard["long_running"])
        self.assertIn("long_running", dashboard["side_effects"])

        declared_effects = set(mapper["side_effect_types"])
        for item in mapper["commands"]:
            self.assertLessEqual(set(item["side_effects"]), declared_effects)

    def test_local_model_command_mapper_public_hygiene(self) -> None:
        from personal_assistant import command_registry

        mapper_json = json.dumps(command_registry.local_model_command_mapper(), sort_keys=True)
        forbidden = [
            "Guide" + "wire",
            "GW Bed" + "rock",
            "/Users/" + "mshaikh",
            "raw_text",
            "raw_request",
            "raw_command_args",
            "request_json",
            "note_hash",
            "note_length",
        ]
        for pattern in forbidden:
            self.assertNotIn(pattern, mapper_json)

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

    def test_command_contract_report_covers_parser_and_metadata(self) -> None:
        from personal_assistant import cli, command_registry

        parser = cli.build_parser()
        subparser_action = next(action for action in parser._actions if getattr(action, "dest", "") == "command")
        parser_commands = set(subparser_action.choices)
        report = command_registry.command_contract_report(parser_commands)
        self.assertEqual(report["schema"], "myos.command_contract.v1")
        self.assertTrue(report["ok"], report["issues"])
        self.assertEqual(report["command_count"], len(parser_commands))
        self.assertEqual(report["parser_command_count"], len(parser_commands))
        self.assertTrue(all(not values for values in report["issues"].values()))

    def test_extracted_command_handlers_are_module_backed(self) -> None:
        from personal_assistant import (
            cli,
            cli_agent,
            cli_autonomy,
            cli_autopilot,
            cli_diagnostics,
            cli_factory,
            cli_health,
            cli_knowledge,
            cli_launchd,
            cli_local_data,
            cli_operations,
            cli_planning,
            cli_review,
            cli_runtime,
            cli_setup_live,
            cli_workflow,
        )

        self.assertTrue(callable(cli_autonomy.cmd_autonomy))
        self.assertTrue(callable(cli_autonomy.cmd_loop))
        self.assertTrue(callable(cli_autopilot.cmd_autopilot))
        self.assertTrue(callable(cli_autopilot.run_autopilot_cycle))
        self.assertTrue(callable(cli_factory.cmd_factory))
        self.assertTrue(callable(cli_agent.cmd_delegate))
        self.assertTrue(callable(cli_agent.cmd_act))
        self.assertTrue(callable(cli_agent.cmd_approve))
        self.assertTrue(callable(cli_agent.cmd_execution_receipt))
        self.assertTrue(callable(cli_planning.cmd_intent))
        self.assertTrue(callable(cli_planning.cmd_plan))
        self.assertTrue(callable(cli_planning.cmd_evidence))
        self.assertTrue(callable(cli_planning.cmd_review_packet))
        self.assertTrue(callable(cli_knowledge.cmd_entity))
        self.assertTrue(callable(cli_knowledge.cmd_relationship))
        self.assertTrue(callable(cli_knowledge.cmd_claim))
        self.assertTrue(callable(cli_diagnostics.cmd_model))
        self.assertTrue(callable(cli_diagnostics.cmd_router))
        self.assertTrue(callable(cli_diagnostics.cmd_trace))
        self.assertTrue(callable(cli_diagnostics.cmd_context))
        self.assertTrue(callable(cli_diagnostics.cmd_why))
        self.assertTrue(callable(cli_diagnostics.cmd_retrieval_run))
        self.assertTrue(callable(cli_workflow.cmd_capture))
        self.assertTrue(callable(cli_workflow.cmd_triage))
        self.assertTrue(callable(cli_workflow.cmd_sync))
        self.assertTrue(callable(cli_workflow.cmd_ingest_external))
        self.assertTrue(callable(cli_workflow.cmd_inbox_process))
        self.assertTrue(callable(cli_review.cmd_close_day))
        self.assertTrue(callable(cli_review.cmd_brief))
        self.assertTrue(callable(cli_review.cmd_metrics))
        self.assertTrue(callable(cli_review.cmd_next_action))
        self.assertTrue(callable(cli_operations.cmd_run_day))
        self.assertTrue(callable(cli_operations.cmd_go_live))
        self.assertTrue(callable(cli_operations.cmd_orchestrate))
        self.assertTrue(callable(cli_operations.cmd_worker))
        self.assertTrue(callable(cli_health.cmd_doctor))
        self.assertTrue(callable(cli_health.cmd_sanity))
        self.assertTrue(callable(cli_health.cmd_snapshot))
        self.assertTrue(callable(cli_health.cmd_uat))
        self.assertTrue(callable(cli_health.cmd_tune))
        self.assertTrue(callable(cli_runtime.cmd_dashboard))
        self.assertTrue(callable(cli_runtime.cmd_runbook))
        self.assertTrue(callable(cli_runtime.cmd_launchd_status))
        self.assertTrue(callable(cli_runtime.cmd_health))
        self.assertTrue(callable(cli_launchd.cmd_launchd_install))
        self.assertTrue(callable(cli_launchd.cmd_launchd_uninstall))
        self.assertTrue(callable(cli_launchd.cmd_activate))
        self.assertTrue(callable(cli_launchd.cmd_start))
        self.assertTrue(callable(cli_launchd.cmd_stop))
        self.assertTrue(callable(cli_launchd.cmd_live))
        self.assertTrue(callable(cli_local_data.cmd_backup))
        self.assertTrue(callable(cli_local_data.cmd_restore))
        self.assertTrue(callable(cli_local_data.cmd_migrations))
        self.assertTrue(callable(cli_local_data.cmd_config_init))
        self.assertTrue(callable(cli_local_data.cmd_cleanup))
        self.assertTrue(callable(cli_setup_live.cmd_setup_live))
        parser = cli.build_parser()
        autonomy_args = parser.parse_args(["autonomy", "eval", "--no-record"])
        loop_args = parser.parse_args(["loop", "status"])
        autopilot_args = parser.parse_args(["autopilot", "--once"])
        factory_args = parser.parse_args(["factory", "policy", "list"])
        delegate_args = parser.parse_args(["delegate", "Handle blocked launch dependency"])
        act_args = parser.parse_args(["act", "--list"])
        approve_args = parser.parse_args(["approve", "--list"])
        receipt_args = parser.parse_args(["execution-receipt", "list"])
        learn_args = parser.parse_args(["learn", "--task", "1", "--outcome", "success"])
        coach_args = parser.parse_args(["coach", "blocked launch dependency"])
        agent_status_args = parser.parse_args(["agent-status"])
        agent_run_args = parser.parse_args(["agent-run", "--intent", "1", "--role", "planner"])
        intent_args = parser.parse_args(["intent", "list"])
        plan_args = parser.parse_args(["plan", "show", "--id", "1"])
        evidence_args = parser.parse_args(["evidence", "attach", "--intent", "1", "--retrieval-run", "1"])
        review_packet_args = parser.parse_args(["review-packet", "--plan", "1"])
        entity_args = parser.parse_args(["entity", "list"])
        relationship_args = parser.parse_args(["relationship", "list"])
        claim_args = parser.parse_args(["claim", "list"])
        model_args = parser.parse_args(["model", "status"])
        router_args = parser.parse_args(["router", "commands"])
        trace_args = parser.parse_args(["trace", "list"])
        context_args = parser.parse_args(["context", "launch risk"])
        why_args = parser.parse_args(["why", "--item", "1"])
        retrieval_run_args = parser.parse_args(["retrieval-run", "list"])
        link_args = parser.parse_args(["link", "--from-item", "1", "--to-item", "2"])
        related_args = parser.parse_args(["related", "--item", "1"])
        capture_args = parser.parse_args(["capture", "Follow up with platform"])
        triage_args = parser.parse_args(["triage"])
        today_args = parser.parse_args(["today"])
        risk_radar_args = parser.parse_args(["risk-radar"])
        sync_args = parser.parse_args(["sync", "--connector", "all"])
        ingest_external_args = parser.parse_args(["ingest-external"])
        inbox_process_args = parser.parse_args(["inbox-process"])
        close_day_args = parser.parse_args(["close-day"])
        morning_brief_args = parser.parse_args(["morning"])
        brief_args = parser.parse_args(["brief"])
        metrics_args = parser.parse_args(["metrics"])
        next_action_args = parser.parse_args(["next-action"])
        weekly_review_args = parser.parse_args(["weekly-review"])
        run_day_args = parser.parse_args(["run-day"])
        go_live_args = parser.parse_args(["go-live"])
        orchestrate_args = parser.parse_args(["orchestrate", "--workflow", "daily"])
        workflow_runs_args = parser.parse_args(["workflow-runs"])
        queue_add_args = parser.parse_args(["queue-add", "--workflow", "daily"])
        worker_args = parser.parse_args(["worker"])
        doctor_args = parser.parse_args(["doctor"])
        sanity_args = parser.parse_args(["sanity"])
        snapshot_args = parser.parse_args(["snapshot"])
        cutover_args = parser.parse_args(["cutover-check"])
        uat_args = parser.parse_args(["uat"])
        tune_args = parser.parse_args(["tune"])
        dashboard_args = parser.parse_args(["dashboard", "--once"])
        runbook_args = parser.parse_args(["runbook"])
        launchd_status_args = parser.parse_args(["launchd-status"])
        health_args = parser.parse_args(["health"])
        ui_args = parser.parse_args(["ui"])
        launchd_install_args = parser.parse_args(["launchd-install"])
        launchd_uninstall_args = parser.parse_args(["launchd-uninstall"])
        activate_args = parser.parse_args(["activate"])
        start_args = parser.parse_args(["start"])
        stop_args = parser.parse_args(["stop"])
        live_args = parser.parse_args(["live"])
        backup_args = parser.parse_args(["backup"])
        restore_args = parser.parse_args(["restore", "--from", "backup.db"])
        migrations_args = parser.parse_args(["migrations"])
        config_init_args = parser.parse_args(["config-init"])
        cleanup_args = parser.parse_args(["cleanup"])
        setup_live_args = parser.parse_args(["setup-live"])
        self.assertIs(autonomy_args.func, cli.cmd_autonomy)
        self.assertIs(loop_args.func, cli.cmd_loop)
        self.assertIs(autopilot_args.func, cli.cmd_autopilot)
        self.assertIs(factory_args.func, cli.cmd_factory)
        self.assertIs(delegate_args.func, cli.cmd_delegate)
        self.assertIs(act_args.func, cli.cmd_act)
        self.assertIs(approve_args.func, cli.cmd_approve)
        self.assertIs(receipt_args.func, cli.cmd_execution_receipt)
        self.assertIs(learn_args.func, cli.cmd_learn)
        self.assertIs(coach_args.func, cli.cmd_coach)
        self.assertIs(agent_status_args.func, cli.cmd_agent_status)
        self.assertIs(agent_run_args.func, cli.cmd_agent_run)
        self.assertIs(intent_args.func, cli.cmd_intent)
        self.assertIs(plan_args.func, cli.cmd_plan)
        self.assertIs(evidence_args.func, cli.cmd_evidence)
        self.assertIs(review_packet_args.func, cli.cmd_review_packet)
        self.assertIs(entity_args.func, cli.cmd_entity)
        self.assertIs(relationship_args.func, cli.cmd_relationship)
        self.assertIs(claim_args.func, cli.cmd_claim)
        self.assertIs(model_args.func, cli.cmd_model)
        self.assertIs(router_args.func, cli.cmd_router)
        self.assertIs(trace_args.func, cli.cmd_trace)
        self.assertIs(context_args.func, cli.cmd_context)
        self.assertIs(why_args.func, cli.cmd_why)
        self.assertIs(retrieval_run_args.func, cli.cmd_retrieval_run)
        self.assertIs(link_args.func, cli.cmd_link)
        self.assertIs(related_args.func, cli.cmd_related)
        self.assertIs(capture_args.func, cli.cmd_capture)
        self.assertIs(triage_args.func, cli.cmd_triage)
        self.assertIs(today_args.func, cli.cmd_today)
        self.assertIs(risk_radar_args.func, cli.cmd_risk_radar)
        self.assertIs(sync_args.func, cli.cmd_sync)
        self.assertIs(ingest_external_args.func, cli.cmd_ingest_external)
        self.assertIs(inbox_process_args.func, cli.cmd_inbox_process)
        self.assertIs(close_day_args.func, cli.cmd_close_day)
        self.assertIs(morning_brief_args.func, cli.cmd_morning)
        self.assertIs(brief_args.func, cli.cmd_brief)
        self.assertIs(metrics_args.func, cli.cmd_metrics)
        self.assertIs(next_action_args.func, cli.cmd_next_action)
        self.assertIs(weekly_review_args.func, cli.cmd_weekly_review)
        self.assertIs(run_day_args.func, cli.cmd_run_day)
        self.assertIs(go_live_args.func, cli.cmd_go_live)
        self.assertIs(orchestrate_args.func, cli.cmd_orchestrate)
        self.assertIs(workflow_runs_args.func, cli.cmd_workflow_runs)
        self.assertIs(queue_add_args.func, cli.cmd_queue_add)
        self.assertIs(worker_args.func, cli.cmd_worker)
        self.assertIs(doctor_args.func, cli.cmd_doctor)
        self.assertIs(sanity_args.func, cli.cmd_sanity)
        self.assertIs(snapshot_args.func, cli.cmd_snapshot)
        self.assertIs(cutover_args.func, cli.cmd_cutover_check)
        self.assertIs(uat_args.func, cli.cmd_uat)
        self.assertIs(tune_args.func, cli.cmd_tune)
        self.assertIs(dashboard_args.func, cli.cmd_dashboard)
        self.assertIs(runbook_args.func, cli.cmd_runbook)
        self.assertIs(launchd_status_args.func, cli.cmd_launchd_status)
        self.assertIs(health_args.func, cli.cmd_health)
        self.assertIs(ui_args.func, cli.cmd_ui)
        self.assertIs(launchd_install_args.func, cli.cmd_launchd_install)
        self.assertIs(launchd_uninstall_args.func, cli.cmd_launchd_uninstall)
        self.assertIs(activate_args.func, cli.cmd_activate)
        self.assertIs(start_args.func, cli.cmd_start)
        self.assertIs(stop_args.func, cli.cmd_stop)
        self.assertIs(live_args.func, cli.cmd_live)
        self.assertIs(backup_args.func, cli.cmd_backup)
        self.assertIs(restore_args.func, cli.cmd_restore)
        self.assertIs(migrations_args.func, cli.cmd_migrations)
        self.assertIs(config_init_args.func, cli.cmd_config_init)
        self.assertIs(cleanup_args.func, cli.cmd_cleanup)
        self.assertIs(setup_live_args.func, cli.cmd_setup_live)


if __name__ == "__main__":
    unittest.main()

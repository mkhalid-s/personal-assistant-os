from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

TIERS = ("daily", "workflow", "expert", "diagnostic")
SAFETY_LEVELS = ("read_only", "local_write", "approval_gated", "external_write", "diagnostic")
SIDE_EFFECT_TYPES = (
    "local_db_write",
    "local_file_write",
    "os_service_write",
    "database_restore",
    "external_read",
    "external_write",
    "long_running",
)


@dataclass(frozen=True)
class CommandSpec:
    command: str
    tier: str
    safety: str
    intent: str
    summary: str
    subcommands: tuple[str, ...] = ()
    required_args: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    requires_confirmation: bool = False
    side_effects: tuple[str, ...] = ()
    dry_run_by_default: bool = False
    long_running: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["subcommands"] = list(self.subcommands)
        data["required_args"] = list(self.required_args)
        data["examples"] = list(self.examples)
        data["side_effects"] = list(self.side_effects)
        return data


COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("chat", "daily", "read_only", "unknown", "Interactive assistant with routed intent awareness.", examples=("myos chat",)),
    CommandSpec("voice", "daily", "read_only", "unknown", "Voice-first assistant using the routed chat loop.", examples=("myos voice",)),
    CommandSpec("do", "daily", "local_write", "unknown", "Route a natural-language request to a MYOS workflow.", required_args=("text",), examples=("myos do 'what should I work on today?'",)),
    CommandSpec("code", "workflow", "approval_gated", "factory_run", "Delegate a repository coding task to an external executor and propose the diff for approval.", required_args=("objective",), examples=("myos code 'Fix the failing tests' --repo . --backend zero",), requires_confirmation=True, side_effects=("local_file_write", "long_running"), long_running=True),
    CommandSpec("autopilot", "daily", "approval_gated", "factory_run", "Run proactive local cycles and leave risky actions for approval.", subcommands=("--once", "--factory", "--loop-goal"), examples=("myos autopilot --once --factory", "myos autopilot --once --loop-goal"), requires_confirmation=True, side_effects=("local_db_write", "long_running"), long_running=True),
    CommandSpec("approve", "daily", "approval_gated", "approval_review", "Review and optionally execute approval-gated actions.", subcommands=("--list", "--action", "--execute", "--stale-only"), examples=("myos approve --list", "myos approve --list --json", "myos approve --list --stale-only"), requires_confirmation=True),
    CommandSpec("capture", "daily", "local_write", "capture", "Capture an inbox item.", required_args=("text",), examples=("myos capture 'Follow up by Friday'",)),
    CommandSpec("morning", "daily", "read_only", "daily_brief", "Generate a morning focus brief.", examples=("myos morning",)),
    CommandSpec("today", "daily", "read_only", "daily_brief", "Show today's focus list.", examples=("myos today --meeting-hours 3",)),
    CommandSpec("next-action", "daily", "read_only", "daily_brief", "Recommend one highest-value next action.", examples=("myos next-action",)),
    CommandSpec("close-day", "daily", "local_write", "daily_brief", "Close the day and write a summary log.", examples=("myos close-day --mode hybrid",)),
    CommandSpec("intent", "workflow", "local_write", "plan_intent", "Create, list, and inspect first-class assistant intents.", subcommands=("create", "list", "show", "evidence"), examples=("myos intent create 'Resolve launch risk'", "myos intent list --json")),
    CommandSpec("plan", "workflow", "local_write", "plan_intent", "Create and inspect intent-tied plans.", subcommands=("create", "show"), examples=("myos plan create --intent 1", "myos plan show --id 1 --json")),
    CommandSpec("review-packet", "workflow", "local_write", "plan_intent", "Build a review packet for a plan.", required_args=("--plan",), examples=("myos review-packet --plan 1",)),
    CommandSpec("factory", "workflow", "approval_gated", "factory_run", "Run review-first AI factory workflows.", subcommands=("start", "status", "review", "approve", "policy", "learn", "insights", "--executor"), examples=("myos factory start --intent 1", "myos factory start --intent 1 --pack software_delivery --executor zero", "myos factory status --id 1 --json"), requires_confirmation=True),
    CommandSpec("delegate", "workflow", "approval_gated", "factory_run", "Delegate an objective to the assistant core.", required_args=("objective",), examples=("myos delegate 'Handle blocked launch dependency'",), requires_confirmation=True),
    CommandSpec("loop", "workflow", "approval_gated", "factory_run", "Run a bounded durable autonomy loop cycle.", subcommands=("start", "resume", "status", "goals", "run-goal", "ledger"), examples=("myos loop start 'Handle blocked launch dependency'", "myos loop status --json", "myos loop ledger --task 1 --json"), requires_confirmation=True),
    CommandSpec("agent-run", "workflow", "local_write", "factory_run", "Run one durable agent role for an intent.", required_args=("--intent", "--role"), examples=("myos agent-run --intent 1 --role planner",)),
    CommandSpec("evidence", "workflow", "local_write", "plan_intent", "Attach evidence artifacts to intents.", subcommands=("attach", "sync-external"), examples=("myos evidence sync-external --intent 1",)),
    CommandSpec("sync", "workflow", "external_write", "retrieve_context", "Sync external connector context when credentials are configured.", examples=("myos sync --connector all",), requires_confirmation=True, side_effects=("external_read", "local_db_write")),
    CommandSpec("weekly-review", "workflow", "read_only", "daily_brief", "Generate weekly review health signals.", examples=("myos weekly-review",)),
    CommandSpec("run-day", "workflow", "local_write", "daily_brief", "Run the daily pipeline end-to-end.", examples=("myos run-day",), side_effects=("external_read", "local_db_write")),
    CommandSpec("orchestrate", "workflow", "local_write", "factory_run", "Run a tracked workflow orchestration.", required_args=("--workflow",), examples=("myos orchestrate --workflow daily",)),
    CommandSpec("context", "expert", "read_only", "retrieve_context", "Find semantic context from indexed chunks.", required_args=("query",), examples=("myos context 'launch risks' --graph",)),
    CommandSpec("why", "expert", "read_only", "retrieve_context", "Explain why a work item exists.", required_args=("--item",), examples=("myos why --item 1 --graph",)),
    CommandSpec("retrieval-run", "expert", "read_only", "retrieve_context", "Inspect persisted retrieval traces.", subcommands=("list", "show"), examples=("myos retrieval-run show --id 1",)),
    CommandSpec("claim", "expert", "local_write", "retrieve_context", "Extract and list deterministic claims.", subcommands=("extract", "list"), examples=("myos claim list",)),
    CommandSpec("entity", "expert", "local_write", "retrieve_context", "Extract and list deterministic graph entities.", subcommands=("extract", "list"), examples=("myos entity extract --text 'Launch depends on platform'",)),
    CommandSpec("relationship", "expert", "local_write", "retrieve_context", "Extract and list typed entity relationships.", subcommands=("extract", "list"), examples=("myos relationship list",)),
    CommandSpec("execution-receipt", "expert", "read_only", "approval_review", "Inspect execution receipts.", subcommands=("list", "show"), examples=("myos execution-receipt list", "myos execution-receipt list --json", "myos execution-receipt show --id 1 --json")),
    CommandSpec("action-provider", "expert", "approval_gated", "connector_update", "Execute or dry-run explicit connector adapters.", examples=("myos action-provider",), requires_confirmation=True),
    CommandSpec("agent-status", "expert", "read_only", "factory_run", "Show agent task status.", examples=("myos agent-status",)),
    CommandSpec("autopilot-status", "expert", "read_only", "daily_brief", "Show autopilot run state.", examples=("myos autopilot-status", "myos autopilot-status --json")),
    CommandSpec("digest", "expert", "read_only", "daily_brief", "Show assistant digests.", examples=("myos digest",)),
    CommandSpec("goal", "expert", "local_write", "plan_intent", "Manage standing assistant goals.", subcommands=("add", "pause", "resume", "list"), examples=("myos goal list",)),
    CommandSpec("watch-dir", "expert", "local_write", "capture", "Configure watched local folders.", subcommands=("add", "list"), examples=("myos watch-dir list",)),
    CommandSpec("watch-scan", "expert", "local_write", "capture", "Scan watched folders for ingestible notes.", examples=("myos watch-scan",)),
    CommandSpec("policy", "expert", "local_write", "system_health", "View or set privacy and retention policy.", examples=("myos policy",)),
    CommandSpec("help", "daily", "read_only", "system_health", "Show simplified command tiers.", examples=("myos help all",)),
    CommandSpec("now", "daily", "read_only", "daily_brief", "Alias for one immediate next action.", examples=("myos now",)),
    CommandSpec("end", "daily", "local_write", "daily_brief", "Quick end-of-day close and report.", examples=("myos end",)),
    CommandSpec("weekly", "daily", "local_write", "daily_brief", "Run the simple weekly review workflow.", examples=("myos weekly",)),
    CommandSpec("note", "daily", "local_write", "capture", "Capture free-form text with inferred filing.", required_args=("text",), examples=("myos note 'Decision: ship staged rollout'",)),
    CommandSpec("meeting", "daily", "local_write", "capture", "Capture meeting notes or audio-derived text.", examples=("myos meeting 'Decision: launch Friday'",)),
    CommandSpec("1on1", "daily", "local_write", "capture", "Log a one-on-one and extract action items.", required_args=("--person", "notes"), examples=("myos 1on1 --person Alex 'Discussed priorities'",)),
    CommandSpec("brief", "daily", "read_only", "daily_brief", "Generate an executive daily brief.", examples=("myos brief --meeting-hours 3",)),
    CommandSpec("risk-radar", "daily", "read_only", "daily_brief", "Show current risk-ranked work.", examples=("myos risk-radar",)),
    CommandSpec("at-risk", "daily", "read_only", "daily_brief", "Show at-risk work items above a threshold.", examples=("myos at-risk --threshold 60",)),
    CommandSpec("waiting-on", "daily", "read_only", "daily_brief", "Show work waiting on owners.", examples=("myos waiting-on",)),
    CommandSpec("delegation-candidates", "daily", "read_only", "daily_brief", "Show work likely worth delegating.", examples=("myos delegation-candidates",)),
    CommandSpec("stop-doing", "daily", "read_only", "daily_brief", "Recommend lower-value work to pause.", examples=("myos stop-doing --capacity 4",)),
    CommandSpec("report", "daily", "local_write", "daily_brief", "Write a local daily brief report file.", examples=("myos report",)),
    CommandSpec("review-draft", "daily", "read_only", "daily_brief", "Assemble a performance-review packet.", required_args=("--person",), examples=("myos review-draft --person self",)),
    CommandSpec("team", "daily", "local_write", "capture", "List or manage team and stakeholder records.", subcommands=("add",), examples=("myos team add Alex",)),
    CommandSpec("triage", "workflow", "local_write", "capture", "Triage inbox items into work items.", examples=("myos triage",)),
    CommandSpec("inbox-process", "workflow", "local_write", "capture", "Extract suggested inbox items from media assets.", examples=("myos inbox-process",)),
    CommandSpec("ingest-external", "workflow", "local_write", "retrieve_context", "Ingest synced external items into the inbox.", examples=("myos ingest-external --limit 100",)),
    CommandSpec("transcribe", "workflow", "local_write", "capture", "Transcribe or attach audio text for indexing.", required_args=("audio_file",), examples=("myos transcribe meeting.m4a --text 'summary'",)),
    CommandSpec("ingest-image", "workflow", "local_write", "capture", "Extract image text into indexed context.", required_args=("image_file",), examples=("myos ingest-image screenshot.png",)),
    CommandSpec("link", "workflow", "local_write", "retrieve_context", "Link two work items in the knowledge graph.", required_args=("--from-item", "--to-item"), examples=("myos link --from-item 1 --to-item 2",)),
    CommandSpec("related", "workflow", "read_only", "retrieve_context", "Show graph-related work items.", required_args=("--item",), examples=("myos related --item 1",)),
    CommandSpec("recall", "expert", "read_only", "retrieve_context", "Search scored conversation memory.", required_args=("query",), examples=("myos recall 'launch risk'",)),
    CommandSpec("reflect", "expert", "local_write", "retrieve_context", "Distill observations into insights and run memory hygiene.", examples=("myos reflect",)),
    CommandSpec("suggestions", "expert", "local_write", "plan_intent", "List, accept, dismiss, or apply improvement suggestions.", subcommands=("list", "accept", "dismiss", "apply"), examples=("myos suggestions list",)),
    CommandSpec("memory", "expert", "read_only", "retrieve_context", "Show conversation and memory overview.", examples=("myos memory",)),
    CommandSpec("reindex", "expert", "local_write", "retrieve_context", "Backfill graph nodes and chunks for existing data.", examples=("myos reindex",)),
    CommandSpec("model", "diagnostic", "local_write", "system_health", "Manage optional tiny local router models.", subcommands=("recommend", "setup", "status"), examples=("myos model status",)),
    CommandSpec("autonomy", "diagnostic", "diagnostic", "system_health", "Evaluate and calibrate autonomy decision policy.", subcommands=("eval", "feedback", "recommendation-feedback", "recommendations"), examples=("myos autonomy eval",)),
    CommandSpec("config-init", "diagnostic", "local_write", "system_health", "Create a local connector env template.", examples=("myos config-init --path .env.myos",), side_effects=("local_file_write",)),
    CommandSpec("onboard", "diagnostic", "read_only", "system_health", "Show connector onboarding diagnostics.", examples=("myos onboard",)),
    CommandSpec("activate", "diagnostic", "approval_gated", "system_health", "Run go-live activation flow.", examples=("myos activate --env-file .env.myos",), requires_confirmation=True, side_effects=("external_read", "local_db_write", "os_service_write")),
    CommandSpec("go-live", "diagnostic", "approval_gated", "system_health", "Alias for live activation and cutover checks.", examples=("myos go-live --env-file .env.myos",), requires_confirmation=True, side_effects=("external_read", "local_db_write")),
    CommandSpec("live", "diagnostic", "approval_gated", "system_health", "Simple live activation flow.", examples=("myos live --env-file .env.myos",), requires_confirmation=True, side_effects=("external_read", "local_db_write", "os_service_write")),
    CommandSpec("pulse", "diagnostic", "approval_gated", "factory_run", "Run continuous orchestration loop.", examples=("myos pulse --once",), requires_confirmation=True, side_effects=("local_db_write", "long_running"), long_running=True),
    CommandSpec("ui", "diagnostic", "read_only", "system_health", "Open the simple dashboard server.", examples=("myos ui --port 8787",)),
    CommandSpec("metrics", "diagnostic", "read_only", "system_health", "Show KPI and connector metrics.", examples=("myos metrics --days 7",)),
    CommandSpec("launchd-install", "diagnostic", "local_write", "system_health", "Install optional launchd service files.", examples=("myos launchd-install",), requires_confirmation=True, side_effects=("local_file_write", "os_service_write"), dry_run_by_default=True),
    CommandSpec("launchd-uninstall", "diagnostic", "local_write", "system_health", "Remove optional launchd service files.", examples=("myos launchd-uninstall",), requires_confirmation=True, side_effects=("local_file_write", "os_service_write"), dry_run_by_default=True),
    CommandSpec("launchd-status", "diagnostic", "read_only", "system_health", "Show launchd service status.", examples=("myos launchd-status",)),
    CommandSpec("start", "diagnostic", "local_write", "system_health", "Start the local MYOS runtime plan.", examples=("myos start --env-file .env.myos",), requires_confirmation=True, side_effects=("external_read", "local_db_write", "os_service_write")),
    CommandSpec("stop", "diagnostic", "local_write", "system_health", "Stop local MYOS runtime components.", examples=("myos stop",), requires_confirmation=True, side_effects=("os_service_write",)),
    CommandSpec("runbook", "diagnostic", "read_only", "system_health", "Print the operational runbook.", examples=("myos runbook --short",)),
    CommandSpec("cleanup", "diagnostic", "local_write", "system_health", "Archive stale work and apply retention cleanup.", examples=("myos cleanup --days 30",), side_effects=("local_db_write", "local_file_write")),
    CommandSpec("renegotiate", "workflow", "read_only", "daily_brief", "Find commitments that need renegotiation.", examples=("myos renegotiate --days-ahead 2",)),
    CommandSpec("snapshot", "diagnostic", "local_write", "system_health", "Write a local system snapshot.", examples=("myos snapshot --output snapshot.json",)),
    CommandSpec("workflow-runs", "workflow", "read_only", "factory_run", "List tracked workflow runs.", examples=("myos workflow-runs",)),
    CommandSpec("queue-add", "workflow", "local_write", "factory_run", "Queue a workflow job.", required_args=("workflow",), examples=("myos queue-add daily",)),
    CommandSpec("worker", "workflow", "local_write", "factory_run", "Process queued workflow jobs.", examples=("myos worker --limit 1",), side_effects=("local_db_write", "long_running"), long_running=True),
    CommandSpec("act", "workflow", "approval_gated", "approval_review", "List, approve, or execute agent actions.", examples=("myos act --action 1 --execute",), requires_confirmation=True),
    CommandSpec("learn", "workflow", "local_write", "factory_run", "Record learning from an agent task.", required_args=("--task", "--outcome"), examples=("myos learn --task 1 --outcome success",)),
    CommandSpec("coach", "expert", "read_only", "retrieve_context", "Show assistant coaching from local context.", required_args=("query",), examples=("myos coach 'blocked launch dependency'",)),
    CommandSpec("self-review", "diagnostic", "read_only", "system_health", "Review MYOS operating health and risks.", examples=("myos self-review",)),
    CommandSpec("tune", "diagnostic", "local_write", "system_health", "Recommend and optionally apply policy tuning.", examples=("myos tune --days 14",)),
    CommandSpec("review-evidence", "daily", "read_only", "daily_brief", "List captured review evidence.", examples=("myos review-evidence --person self",)),
    CommandSpec("log-evidence", "daily", "local_write", "capture", "Log review evidence for a person.", required_args=("--person", "--category", "--impact"), examples=("myos log-evidence --person self --category impact --impact 'Shipped launch'",)),
    CommandSpec("resolve-commitment", "daily", "local_write", "daily_brief", "Resolve a commitment work item.", required_args=("--item",), examples=("myos resolve-commitment --item 1",)),
    CommandSpec("risk-scan", "workflow", "approval_gated", "connector_update", "Scan project risks and optionally draft nudges.", examples=("myos risk-scan --draft-nudges",), requires_confirmation=True),
    CommandSpec("doctor", "diagnostic", "diagnostic", "system_health", "Show local system and connector health.", examples=("myos doctor --strict", "myos doctor --json")),
    CommandSpec("health", "diagnostic", "diagnostic", "system_health", "Run health and sanity checks.", examples=("myos health",)),
    CommandSpec("router", "diagnostic", "diagnostic", "system_health", "Evaluate and inspect smart routing quality.", subcommands=("eval", "feedback", "overrides", "commands"), examples=("myos router eval",)),
    CommandSpec("trace", "diagnostic", "diagnostic", "system_health", "Inspect bounded command and agent execution traces.", subcommands=("list", "cleanup", "rollups"), examples=("myos trace list",)),
    CommandSpec("sanity", "diagnostic", "diagnostic", "system_health", "Run operational sanity checks.", examples=("myos sanity --strict",)),
    CommandSpec("release-check", "diagnostic", "diagnostic", "system_health", "Run release readiness checks.", examples=("myos release-check --strict", "myos release-check --strict --json")),
    CommandSpec("dependency-check", "diagnostic", "diagnostic", "system_health", "Check dependency and license hygiene.", examples=("myos dependency-check --strict",)),
    CommandSpec("performance-baseline", "diagnostic", "diagnostic", "system_health", "Measure retrieval and readiness query timing.", examples=("myos performance-baseline",)),
    CommandSpec("migrations", "diagnostic", "diagnostic", "system_health", "Inspect and verify schema migration health.", subcommands=("verify", "list"), examples=("myos migrations verify --strict",)),
    CommandSpec("backup", "diagnostic", "local_write", "system_health", "Create a verified SQLite database backup.", examples=("myos backup",), side_effects=("local_file_write",)),
    CommandSpec("restore", "diagnostic", "local_write", "system_health", "Restore the SQLite database from backup.", required_args=("--from",), examples=("myos restore --from backup.db",), requires_confirmation=True, side_effects=("database_restore", "local_file_write")),
    CommandSpec("dashboard", "diagnostic", "read_only", "system_health", "Serve or export local dashboard.", examples=("myos dashboard --once",), side_effects=("local_file_write", "long_running"), long_running=True),
    CommandSpec("setup-live", "diagnostic", "local_write", "system_health", "Prepare live config, folders, goals, and safe defaults.", examples=("myos setup-live --check",), side_effects=("local_file_write", "local_db_write", "os_service_write"), dry_run_by_default=True),
    CommandSpec("cutover-check", "diagnostic", "diagnostic", "system_health", "Check live credential and sync readiness.", examples=("myos cutover-check",)),
    CommandSpec("uat", "diagnostic", "diagnostic", "system_health", "Evaluate UAT quality metrics on recent data.", examples=("myos uat",)),
)


def all_commands() -> list[CommandSpec]:
    return list(COMMAND_SPECS)


def command_inventory() -> dict[str, list[str]]:
    inventory: dict[str, list[str]] = {tier: [] for tier in TIERS}
    for spec in COMMAND_SPECS:
        inventory.setdefault(spec.tier, []).append(spec.command)
    return inventory


def find_command(command: str) -> CommandSpec | None:
    for spec in COMMAND_SPECS:
        if spec.command == command:
            return spec
    return None


def filter_commands(*, tier: str = "", safety: str = "", intent: str = "") -> list[CommandSpec]:
    specs = COMMAND_SPECS
    if tier:
        specs = tuple(spec for spec in specs if spec.tier == tier)
    if safety:
        specs = tuple(spec for spec in specs if spec.safety == safety)
    if intent:
        specs = tuple(spec for spec in specs if spec.intent == intent)
    return list(specs)


def command_contract_report(parser_commands: list[str] | set[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
    specs = all_commands()
    spec_names = [spec.command for spec in specs]
    spec_name_set = set(spec_names)
    parser_name_set = set(parser_commands or [])
    duplicates = sorted({name for name in spec_names if spec_names.count(name) > 1})
    invalid_tiers = sorted(spec.command for spec in specs if spec.tier not in TIERS)
    invalid_safety = sorted(spec.command for spec in specs if spec.safety not in SAFETY_LEVELS)
    invalid_side_effects = sorted(
        spec.command
        for spec in specs
        if any(side_effect not in SIDE_EFFECT_TYPES for side_effect in spec.side_effects)
    )
    missing_summary = sorted(spec.command for spec in specs if not spec.summary.strip())
    missing_examples = sorted(spec.command for spec in specs if not spec.examples)
    malformed_examples = sorted(
        spec.command
        for spec in specs
        if any(not str(example).strip().startswith("myos ") for example in spec.examples)
    )
    risky_without_confirmation = sorted(
        spec.command
        for spec in specs
        if (
            spec.safety in {"approval_gated", "external_write"}
            or "database_restore" in spec.side_effects
        )
        and not spec.requires_confirmation
    )
    issues: dict[str, list[str]] = {
        "duplicate_registry_commands": duplicates,
        "invalid_tiers": invalid_tiers,
        "invalid_safety": invalid_safety,
        "invalid_side_effects": invalid_side_effects,
        "missing_summary": missing_summary,
        "missing_examples": missing_examples,
        "malformed_examples": malformed_examples,
        "risky_without_confirmation": risky_without_confirmation,
    }
    if parser_commands is not None:
        issues["missing_registry_metadata"] = sorted(parser_name_set - spec_name_set)
        issues["extra_registry_metadata"] = sorted(spec_name_set - parser_name_set)
    open_issues = {name: values for name, values in issues.items() if values}
    return {
        "schema": "myos.command_contract.v1",
        "ok": not open_issues,
        "command_count": len(spec_name_set),
        "parser_command_count": len(parser_name_set) if parser_commands is not None else None,
        "issues": issues,
    }


def _model_safe_item(spec: CommandSpec) -> dict[str, Any]:
    return {
        "command": spec.command,
        "usage": f"myos {spec.command}",
        "tier": spec.tier,
        "safety": spec.safety,
        "intent": spec.intent,
        "requires_confirmation": spec.requires_confirmation,
        "side_effects": list(spec.side_effects),
        "dry_run_by_default": spec.dry_run_by_default,
        "long_running": spec.long_running,
        "summary": spec.summary[:160],
        "subcommands": list(spec.subcommands),
        "required_args": list(spec.required_args),
        "examples": list(spec.examples[:3]),
    }


def compact_catalog(*, limit: int = 40) -> list[dict[str, Any]]:
    items = []
    selected = COMMAND_SPECS if int(limit) <= 0 else COMMAND_SPECS[: max(0, int(limit))]
    for spec in selected:
        items.append(_model_safe_item(spec))
    return items


def local_model_command_mapper(*, limit: int = 0) -> dict[str, Any]:
    return {
        "schema": "myos.command_mapper.v1",
        "description": "Local-model-safe MYOS CLI command map. It contains command metadata only, never user data.",
        "tiers": list(TIERS),
        "safety_levels": list(SAFETY_LEVELS),
        "side_effect_types": list(SIDE_EFFECT_TYPES),
        "commands": compact_catalog(limit=limit),
    }

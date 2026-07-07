from __future__ import annotations

import argparse
import json

from . import command_registry, graphrag, model_setup, observability, router
from .db import append_event, connection
from .graph import connect_work_items
from .retrieval import hybrid_score


def cmd_link(args: argparse.Namespace) -> None:
    with connection() as conn:
        connect_work_items(conn, args.from_item, args.to_item, args.relation, args.weight)
        append_event(
            conn,
            "link",
            "knowledge_edge",
            None,
            json.dumps(
                {"from_item": args.from_item, "to_item": args.to_item, "relation": args.relation},
                ensure_ascii=True,
            ),
        )
        conn.commit()
        print(f"Linked work item {args.from_item} -> {args.to_item} with relation '{args.relation}'.")


def cmd_related(args: argparse.Namespace) -> None:
    with connection() as conn:
        row = conn.execute(
            "SELECT id FROM knowledge_nodes WHERE node_type = 'work_item' AND ref_id = ?",
            (args.item,),
        ).fetchone()
        node_id = int(row["id"]) if row else None
        if node_id is None:
            print("Work item is not indexed. Run `myos triage` first.")
            return

        rows = conn.execute(
            """
            SELECT
                e.relation AS relation,
                e.weight AS weight,
                w.id AS work_item_id,
                w.title AS title,
                w.status AS status
            FROM knowledge_edges e
            JOIN knowledge_nodes n ON n.id = e.to_node_id
            JOIN work_items w ON w.id = n.ref_id
            WHERE e.from_node_id = ? AND n.node_type = 'work_item'
            ORDER BY e.weight DESC, w.id ASC
            LIMIT ?
            """,
            (node_id, args.limit),
        ).fetchall()

    if not rows:
        print("No related work items found.")
        return

    print(f"Related work for item {args.item}:")
    for r in rows:
        print(f"- #{r['work_item_id']} [{r['relation']}] {r['title']} (status={r['status']}, weight={r['weight']:.2f})")


def cmd_context(args: argparse.Namespace) -> None:
    with connection() as conn:
        if getattr(args, "graph", False):
            hits = graphrag.retrieve(
                conn,
                args.query,
                limit=args.limit,
                graph_hops=args.graph_hops,
                record_run=True,
                mode="context_graph",
            )
            conn.commit()
            if not hits:
                print("No relevant graph context found.")
                return
            print(f"Graph context results for: {args.query}")
            print(f"retrieval run: #{hits[0]['retrieval_run_id']}")
            for hit in hits:
                snippet = str(hit["content"]).strip().replace("\n", " ")
                if len(snippet) > 120:
                    snippet = snippet[:117] + "..."
                print(f"- ({hit['score']:.3f}) {hit['citation']}: {snippet}")
                print(f"  reason: {hit['reason']}")
                if hit["graph_path"]:
                    print(f"  path: {' -> '.join(hit['graph_path'])}")
            return

        rows = conn.execute(
            """
            SELECT source_type, source_id, content
            FROM text_chunks
            ORDER BY created_at DESC
            LIMIT 400
            """
        ).fetchall()
        if not rows:
            print("No context chunks indexed yet.")
            return

        scored = []
        for row in rows:
            score = hybrid_score(args.query, row["content"])
            if score > 0:
                scored.append((score, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[: args.limit]
        if not top:
            print("No relevant context found.")
            return

        print(f"Context results for: {args.query}")
        for score, row in top:
            snippet = row["content"].strip().replace("\n", " ")
            if len(snippet) > 120:
                snippet = snippet[:117] + "..."
            print(f"- ({score:.3f}) {row['source_type']}#{row['source_id']}: {snippet}")


def cmd_retrieval_run(args: argparse.Namespace) -> None:
    with connection() as conn:
        action = getattr(args, "retrieval_run_action", "list") or "list"
        if action == "show":
            if args.id is None:
                print("Usage: myos retrieval-run show --id N")
                raise SystemExit(1)
            run = conn.execute(
                """
                SELECT id, query, mode, limit_requested, graph_hops, candidate_limit, selected_count, created_at
                FROM retrieval_runs
                WHERE id = ?
                """,
                (args.id,),
            ).fetchone()
            if not run:
                print("Retrieval run not found.")
                return
            print(f"Retrieval run #{run['id']} [{run['mode']}]")
            print(f"query: {run['query']}")
            print(
                f"requested: limit={run['limit_requested']} graph_hops={run['graph_hops']} "
                f"candidates={run['candidate_limit']} selected={run['selected_count']}"
            )
            print(f"created: {run['created_at']}")
            sources = conn.execute(
                """
                SELECT rank, citation, score, reason, graph_path_json, content_preview
                FROM retrieval_run_sources
                WHERE retrieval_run_id = ?
                ORDER BY rank ASC
                """,
                (run["id"],),
            ).fetchall()
            if not sources:
                print("sources: none")
                return
            print("sources:")
            for source in sources:
                preview = source["content_preview"] or ""
                print(f"{source['rank']}. ({source['score']:.3f}) {source['citation']}: {preview}")
                print(f"   reason: {source['reason']}")
                path = json.loads(source["graph_path_json"] or "[]")
                if path:
                    print(f"   path: {' -> '.join(path)}")
            return

        rows = conn.execute(
            """
            SELECT id, query, mode, selected_count, created_at
            FROM retrieval_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        if not rows:
            print("No retrieval runs recorded.")
            return
        print("Retrieval runs:")
        for row in rows:
            print(
                f"- #{row['id']} [{row['mode']}] {row['query']} "
                f"(sources={row['selected_count']}, created={row['created_at']})"
            )


def _print_model_plan(plan: dict[str, object]) -> None:
    print("Router model setup plan:")
    print(f"- runtime: {plan['runtime']} ({'available' if plan['runtime_available'] else 'not available'})")
    print(f"- runtime_detail: {plan['runtime_detail']}")
    print(f"- model: {plan['model']} ({plan['model_label']})")
    print(f"- footprint: {plan['footprint']}")
    print(f"- quality: {plan['quality']}")
    print(f"- pull_command: {plan['pull_command_text']}")
    print(f"- wrapper_path: {plan['wrapper_path']}")
    print("- env:")
    for line in plan["env_lines"]:
        print(f"  {line}")
    print(f"- privacy: {plan['privacy_note']}")


def cmd_model(args: argparse.Namespace) -> None:
    action = getattr(args, "model_action", "")
    if action == "recommend":
        try:
            rec = model_setup.recommended_model(args.purpose)
        except ValueError as exc:
            print(str(exc))
            raise SystemExit(1) from exc
        print(f"Recommended {rec['purpose']} model: {rec['model']} ({rec['label']})")
        print(f"- footprint: {rec['footprint']}")
        print(f"- quality: {rec['quality']}")
        return
    if action == "status":
        status = model_setup.router_status()
        print("Router model status:")
        print(f"- backend: {status['backend']}")
        print(f"- model: {status['model']}")
        print(f"- command: {status['command']}")
        print(f"- runtime: {status['runtime']}")
        print(f"- available: {bool(status['available'])}")
        print(f"- detail: {status['detail']}")
        return
    if action == "setup":
        if not args.router:
            print("Only router model setup is supported in this release. Use --router.")
            raise SystemExit(1)
        try:
            plan = model_setup.setup_plan(runtime=args.runtime, model=args.model, command=args.command)
        except ValueError as exc:
            print(str(exc))
            raise SystemExit(1) from exc
        _print_model_plan(plan)
        result = model_setup.apply_setup(plan, dry_run=not args.apply)
        if not args.apply:
            print("Dry run only. Re-run with --apply to pull the model and write the wrapper.")
            return
        print(f"Apply status: {result['status']}")
        if result.get("wrapper"):
            print(f"Wrapper written: {result['wrapper']}")
        if result.get("stdout"):
            print(f"stdout: {result['stdout']}")
        if result.get("stderr"):
            print(f"stderr: {result['stderr']}")
        if result["status"] == "failed":
            raise SystemExit(1)
        return
    raise SystemExit("Unknown model command.")


def cmd_router(args: argparse.Namespace) -> None:
    action = getattr(args, "router_action", "")
    if action == "eval":
        try:
            result = router.evaluate_routes(
                fixture_path=args.fixture or None,
                model_shadow=args.model_shadow,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"Router eval failed: {exc}")
            raise SystemExit(1) from exc
        with connection() as conn:
            run_id = 0
            if not args.no_record:
                run_id = router.record_route_eval(conn, result)
        summary = result["summary"]
        print("Router eval:")
        print(f"- fixtures: {summary['total']} from {result['fixture_path']}")
        print(f"- passed: {summary['passed']} failed={summary['failed']} accuracy={summary['accuracy']:.2%}")
        print(f"- low_confidence: {summary['low_confidence']}")
        if args.model_shadow:
            print(
                f"- model_shadow: overrides={summary['model_overrides']} "
                f"wins={summary['model_wins']} losses={summary['model_losses']}"
            )
        print(f"- calibration: {summary['calibration']}")
        if run_id:
            print(f"- recorded_eval_run: #{run_id}")
        failures = [case for case in result["cases"] if not case["passed"]]
        if failures:
            print("Failures:")
            for case in failures[:10]:
                print(
                    f"- {case['fixture_id']}: expected={case['expected_intent']} "
                    f"actual={case['actual_intent']} confidence={case['confidence']:.2f}"
                )
        return
    if action == "feedback":
        with connection() as conn:
            try:
                feedback_id = router.record_route_feedback(
                    conn,
                    event_id=args.event,
                    expected_intent=args.expected_intent,
                    note=args.note or "",
                )
            except (ValueError, json.JSONDecodeError) as exc:
                print(f"Router feedback failed: {exc}")
                raise SystemExit(1) from exc
        print(f"Router feedback recorded: #{feedback_id}")
        print("Privacy: note text was hashed; raw request text was not stored.")
        return
    if action == "overrides":
        with connection() as conn:
            rows = router.list_route_overrides(conn, limit=args.limit)
        if not rows:
            print("No active router overrides.")
            return
        print("Router overrides:")
        for row in rows:
            print(
                f"- #{row['id']} intent={row['expected_intent']} status={row['status']} "
                f"hash={row['text_hash'][:12]} feedback=#{row['source_feedback_id'] or 'none'} "
                f"updated={row['updated_at']}"
            )
        return
    if action == "commands":
        specs = command_registry.filter_commands(
            tier=args.tier or "",
            safety=args.safety or "",
            intent=args.intent or "",
        )
        print("Router command registry:")
        if not specs:
            print("- no commands match filters")
            return
        for spec in specs[: args.limit]:
            confirm = " confirmation=yes" if spec.requires_confirmation else ""
            subcommands = f" subcommands={','.join(spec.subcommands)}" if spec.subcommands else ""
            required = f" required={','.join(spec.required_args)}" if spec.required_args else ""
            effects = f" side_effects={','.join(spec.side_effects)}" if spec.side_effects else " side_effects=none"
            dry_run = " dry_run=yes" if spec.dry_run_by_default else ""
            long_running = " long_running=yes" if spec.long_running else ""
            example = f" example={spec.examples[0]}" if spec.examples else ""
            print(
                f"- myos {spec.command} tier={spec.tier} safety={spec.safety} "
                f"intent={spec.intent}{confirm}{subcommands}{required}{effects}{dry_run}{long_running}{example}"
            )
        return
    raise SystemExit("Unknown router command.")


def cmd_trace(args: argparse.Namespace) -> None:
    action = getattr(args, "trace_action", "")
    with connection() as conn:
        if action == "list":
            rows = observability.list_traces(
                conn,
                limit=args.limit,
                status=args.status or "",
                command=args.command_filter or "",
            )
            current_trace = observability.current_correlation_id()
            if current_trace:
                rows = [row for row in rows if row.get("correlation_id") != current_trace]
            if not rows:
                print("No execution traces.")
                return
            print("Execution traces:")
            for row in rows:
                links = []
                if row.get("route_event_id"):
                    links.append(f"route_event=#{row['route_event_id']}")
                if row.get("factory_run_id"):
                    links.append(f"factory_run=#{row['factory_run_id']}")
                if row.get("agent_task_id"):
                    links.append(f"agent_task=#{row['agent_task_id']}")
                if row.get("receipt_id"):
                    links.append(f"receipt=#{row['receipt_id']}")
                link_text = f" {' '.join(links)}" if links else ""
                print(
                    f"- #{row['id']} {row['command_path']} status={row['status']} "
                    f"duration_ms={row['duration_ms']} corr={row['correlation_id']}{link_text}"
                )
            return
        if action == "cleanup":
            result = observability.cleanup_traces(
                conn,
                retention_days=args.retention_days,
                max_rows=args.max_rows,
            )
            print(
                "Trace cleanup: "
                f"rolled_up={result['rolled_up']} deleted={result['deleted']} remaining={result['remaining']}"
            )
            return
        if action == "rollups":
            rows = observability.rollups(conn, limit=args.limit)
            if not rows:
                print("No execution trace rollups.")
                return
            print("Execution trace rollups:")
            for row in rows:
                print(
                    f"- {row['bucket_date']} {row['command_path']} status={row['status']} "
                    f"count={row['trace_count']} duration_ms={row['total_duration_ms']}"
                )
            return
    raise SystemExit("Unknown trace command.")


def cmd_why(args: argparse.Namespace) -> None:
    with connection() as conn:
        row = conn.execute(
            """
            SELECT w.id, w.title, w.kind, w.risk_score, p.source_type, p.source_ref, p.extractor, p.confidence, p.snippet
            FROM work_items w
            LEFT JOIN text_chunks tc ON tc.source_type='work_item' AND tc.source_id=w.id
            LEFT JOIN provenance p ON p.id=tc.provenance_id
            WHERE w.id = ?
            LIMIT 1
            """,
            (args.item,),
        ).fetchone()
        if not row:
            print("Work item not found.")
            return
        print(f"Work item #{row['id']}: {row['title']}")
        print(f"kind={row['kind']} risk={row['risk_score']}")
        if row["extractor"]:
            print(
                f"provenance: extractor={row['extractor']} source={row['source_type']}:{row['source_ref']} confidence={row['confidence']}"
            )
        if row["snippet"]:
            print(f"snippet: {row['snippet'][:180]}")
        if getattr(args, "graph", False):
            hits = graphrag.retrieve(
                conn,
                row["title"],
                limit=args.limit,
                graph_hops=args.graph_hops,
                record_run=True,
                mode="why_graph",
            )
            conn.commit()
            evidence = [
                hit
                for hit in hits
                if hit["graph_path"] or hit["source_type"] != "work_item" or int(hit["source_id"]) != int(row["id"])
            ]
            if not evidence:
                print("graph: no related evidence found.")
                return
            print(f"retrieval run: #{hits[0]['retrieval_run_id']}")
            print("graph evidence:")
            for hit in evidence:
                snippet = str(hit["content"]).strip().replace("\n", " ")
                if len(snippet) > 120:
                    snippet = snippet[:117] + "..."
                print(f"- ({hit['score']:.3f}) {hit['citation']}: {snippet}")
                print(f"  reason: {hit['reason']}")
                if hit["graph_path"]:
                    print(f"  path: {' -> '.join(hit['graph_path'])}")

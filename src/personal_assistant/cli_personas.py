from __future__ import annotations

import argparse
import json

from . import personas
from .db import connection


def cmd_persona(args: argparse.Namespace) -> None:
    with connection() as conn:
        action = args.persona_action
        if action == "list":
            rows = personas.list_personas(conn, active_only=not bool(args.all))
            if args.json:
                print(
                    json.dumps(
                        {"schema": "myos.persona.list.v1", "count": len(rows), "personas": rows}, ensure_ascii=True
                    )
                )
                return
            print("Personas:")
            for row in rows:
                kind = "built-in" if row["is_builtin"] else "custom"
                print(f"- {row['name']} ({kind}): {row['description']}")
            return
        if action == "show":
            selected = personas.get_persona(conn, args.name)
            if selected is None:
                if args.json:
                    print(json.dumps({"schema": "myos.persona.show.v1", "error": "not_found", "name": args.name}))
                else:
                    print(f"Persona not found: {args.name}")
                raise SystemExit(1)
            if args.json:
                print(json.dumps({"schema": "myos.persona.show.v1", "persona": selected}, ensure_ascii=True))
                return
            print(f"Persona: {selected['display_name']} ({selected['name']})")
            print(f"Description: {selected['description']}")
            print(f"Instructions: {selected['instructions']}")
            print("Allowed actions: " + (", ".join(selected["allowed_actions"]) or "none"))
            print("Retrieval scopes: " + (", ".join(selected["retrieval_scopes"]) or "none"))
            print(f"Default backend: {selected['default_backend'] or 'inherit'}")
            return
        try:
            row = personas.create_persona(
                conn,
                name=args.name,
                display_name=args.display_name,
                description=args.description,
                instructions=args.instructions,
                allowed_actions=args.allow_action,
                retrieval_scopes=args.retrieval_scope,
                default_backend=args.backend,
            )
        except ValueError as exc:
            print(f"Persona create failed: {exc}")
            raise SystemExit(1) from exc
        print(f"Created persona: {row['name']}")


def register_subparsers(sub: argparse._SubParsersAction) -> None:
    persona = sub.add_parser("persona", help="Manage scoped assistant personas.")
    persona_sub = persona.add_subparsers(dest="persona_action", required=True)
    persona_list = persona_sub.add_parser("list", help="List available personas.")
    persona_list.add_argument("--all", action="store_true", help="Include inactive personas.")
    persona_list.add_argument("--json", action="store_true")
    persona_list.set_defaults(func=cmd_persona)
    persona_show = persona_sub.add_parser("show", help="Show one persona manifest.")
    persona_show.add_argument("name")
    persona_show.add_argument("--json", action="store_true")
    persona_show.set_defaults(func=cmd_persona)
    persona_create = persona_sub.add_parser("create", help="Create a custom persona with a narrow action scope.")
    persona_create.add_argument("name")
    persona_create.add_argument("--display-name", default="")
    persona_create.add_argument("--description", default="")
    persona_create.add_argument("--instructions", required=True)
    persona_create.add_argument("--allow-action", action="append", default=["create_inbox_item"])
    persona_create.add_argument("--retrieval-scope", action="append", default=["local_memory", "work_items"])
    persona_create.add_argument(
        "--backend",
        choices=["claude", "cursor", "zero", "claude-code", "copilot", "command", "local"],
        default="",
    )
    persona_create.set_defaults(func=cmd_persona)


__all__ = ["cmd_persona", "register_subparsers"]

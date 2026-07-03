from __future__ import annotations

import argparse

from . import claims, entities, relationships
from .db import get_connection


def cmd_entity(args: argparse.Namespace) -> None:
    conn = get_connection()
    action = getattr(args, "entity_action", "")
    if action == "extract":
        recorded = entities.record_entities(
            conn,
            args.text,
            source_type=args.source_type,
            source_id=args.source_id,
        )
        conn.commit()
        if not recorded:
            print("No deterministic entities found.")
            return
        print(f"Recorded {len(recorded)} entities:")
        for entity in recorded:
            aliases = ", ".join(entity["aliases"])
            print(
                f"- #{entity['id']} [{entity['entity_type']}] {entity['canonical_name']} "
                f"confidence={entity['confidence']:.2f} aliases={aliases}"
            )
        return

    if action == "list":
        rows = entities.list_entities(conn, entity_type=args.type, limit=args.limit)
        if not rows:
            print("No entities found.")
            return
        print("Entities:")
        for row in rows:
            aliases = ", ".join(row["aliases"]) if row["aliases"] else "none"
            print(f"- #{row['id']} [{row['entity_type']}] {row['canonical_name']} aliases={aliases}")
        return

    raise SystemExit("Unknown entity command.")


def cmd_relationship(args: argparse.Namespace) -> None:
    conn = get_connection()
    action = getattr(args, "relationship_action", "")
    if action == "extract":
        recorded = relationships.record_relationships(
            conn,
            args.text,
            source_type=args.source_type,
            source_id=args.source_id,
        )
        conn.commit()
        if not recorded:
            print("No deterministic relationships found.")
            return
        print(f"Recorded {len(recorded)} relationships:")
        for rel in recorded:
            src = rel["from_entity"]["canonical_name"]
            dst = rel["to_entity"]["canonical_name"]
            print(
                f"- #{rel['id']} {src} -[{rel['relation_type']}]-> {dst} "
                f"confidence={rel['confidence']:.2f}"
            )
        return

    if action == "list":
        rows = relationships.list_relationships(conn, relation_type=args.type, limit=args.limit)
        if not rows:
            print("No relationships found.")
            return
        print("Relationships:")
        for row in rows:
            source = f" source={row['source_type']}:{row['source_id']}" if row["source_type"] else ""
            print(
                f"- #{row['id']} {row['from_name']} -[{row['relation_type']}]-> {row['to_name']}"
                f" confidence={row['confidence']:.2f}{source}"
            )
        return

    raise SystemExit("Unknown relationship command.")


def cmd_claim(args: argparse.Namespace) -> None:
    conn = get_connection()
    action = getattr(args, "claim_action", "")
    if action == "extract":
        recorded = claims.record_claims(
            conn,
            args.text,
            source_type=args.source_type,
            source_id=args.source_id,
        )
        conn.commit()
        if not recorded:
            print("No high-confidence claims found.")
            return
        print(f"Recorded {len(recorded)} claim(s):")
        for claim in recorded:
            print(f"- #{claim['id']} ({claim['confidence']:.2f}) {claim['claim_text']}")
        return

    if action == "list":
        rows = claims.list_claims(conn, source_type=args.source_type, limit=args.limit)
        if not rows:
            print("No claims recorded.")
            return
        print("Claims:")
        for row in rows:
            source_id = f":{row['source_id']}" if row["source_id"] else ""
            print(
                f"- #{row['id']} source={row['source_type']}{source_id} "
                f"confidence={row['confidence']:.2f} {row['claim_text']}"
            )
        return

    raise SystemExit("Unknown claim command.")

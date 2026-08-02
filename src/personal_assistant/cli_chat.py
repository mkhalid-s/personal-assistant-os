"""Interactive assistant CLIs: ``myos chat``, ``myos voice``, and ``myos do``.

Extracted out of ``cli.py`` (P0.7 slice) so the god-file shrinks without
changing behavior. Each command is a thin wrapper around the shared
``assistant`` / ``router`` core so the interactive UI logic (input loops,
backend availability, proposal handling) lives beside the rest of the CLI
surface tier.
"""

from __future__ import annotations

import argparse
import contextlib
import os
from collections.abc import Callable

from . import assistant, autonomy, cli_autonomy, personas, providers, router
from .db import connection
from .execution import _handle_proposals


def cmd_do(args: argparse.Namespace) -> None:
    """Route a natural-language request to the best MYOS workflow.

    Mirrors the prior in-line body from ``cli.py``: consults the router with
    autonomy-decision context, records the decision, and only then executes
    the routed workflow (or raises ``SystemExit(1)`` when the decision is
    ``BLOCKED``).
    """
    with connection() as conn:
        route_decision = router.route_with_feedback(conn, args.text, surface="do")
        autonomy_decision = router.autonomy_decision_for_route(conn, route_decision)
        cli_autonomy.print_autonomy_decision(autonomy_decision)
        cli_autonomy.print_recommendations(
            conn,
            autonomy.recommend_next_steps(
                autonomy_decision,
                command="do",
                intent=route_decision.intent,
                workflow_pack=route_decision.workflow_pack,
            ),
        )
        if autonomy_decision["decision"] == autonomy.BLOCKED:
            raise SystemExit(1)
        result = router.execute_route(conn, args.text, surface="do", decision=route_decision)
        result["autonomy"] = autonomy_decision
        conn.commit()
    print(router.summarize_result(result))
    decision = result["decision"]
    if decision.get("requires_confirmation"):
        print("Safety: route is review-first or clarification-oriented; external mutations remain approval-gated.")


def cmd_chat(args: argparse.Namespace, *, load_env_file: Callable[[str], int]) -> None:
    """Interactive text chat with the assistant core.

    External mutations remain approval-gated: every proposal returned by
    ``assistant.run_turn`` flows through ``_handle_proposals`` so the user
    sees pending approvals inline before deciding.
    """
    if getattr(args, "env_file", ""):
        load_env_file(args.env_file)
    with connection() as conn:
        persona = personas.get_persona(conn, args.persona) if getattr(args, "persona", "") else None
        if getattr(args, "persona", "") and persona is None:
            print(f"Persona not found: {args.persona}")
            raise SystemExit(1)
        backend_name = args.backend or (persona["default_backend"] if persona else "") or None
        backend = providers.get_backend(backend_name)
        if persona is not None and backend.name == "claude-sdk":
            print("Scoped personas cannot safely use the claude-sdk backend yet; choose --backend claude.")
            raise SystemExit(1)
        ok, detail = backend.available()
        if not ok:
            print(f"Backend '{backend.name}' is not available: {detail}")
            raise SystemExit(1)
        persona_label = f" / {persona['display_name']}" if persona else ""
        print(
            f"MYOS chat [{backend.name}{persona_label}] — ask anything; external changes are proposed for your approval."
        )
        print("Type 'exit' to quit.")
        history: list[dict] = []
        conversation_id: int | None = None
        while True:
            try:
                user = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user:
                continue
            if user.lower() in ("exit", "quit", ":q"):
                break
            result = assistant.run_turn(
                conn,
                user,
                history,
                backend_name=backend_name,
                surface="chat",
                conversation_id=conversation_id,
                persona_name=(persona["name"] if persona else None),
            )
            conversation_id = result.get("conversation_id", conversation_id)
            history = result.get("history", history)
            reply = (result.get("reply") or "").strip()
            if reply:
                print(f"\nmyos> {reply}")
            _handle_proposals(conn, result.get("proposed_action_ids", []))


def cmd_voice(args: argparse.Namespace, *, load_env_file: Callable[[str], int]) -> None:
    """Push-to-talk voice loop; identical bounds to ``cmd_chat`` for approvals."""
    from . import voice

    if getattr(args, "env_file", ""):
        load_env_file(args.env_file)
    with connection() as conn:
        persona = personas.get_persona(conn, args.persona) if getattr(args, "persona", "") else None
        if getattr(args, "persona", "") and persona is None:
            print(f"Persona not found: {args.persona}")
            raise SystemExit(1)
        backend_name = args.backend or (persona["default_backend"] if persona else "") or None
        backend = providers.get_backend(backend_name)
        if persona is not None and backend.name == "claude-sdk":
            print("Scoped personas cannot safely use the claude-sdk backend yet; choose --backend claude.")
            raise SystemExit(1)
        ok, detail = backend.available()
        if not ok:
            print(f"Backend '{backend.name}' is not available: {detail}")
            raise SystemExit(1)
        persona_label = f" / {persona['display_name']}" if persona else ""
        print(f"MYOS voice [{backend.name}{persona_label}] — push-to-talk. Ctrl-C to quit.")
        history: list[dict] = []
        conversation_id: int | None = None
        while True:
            try:
                wav = voice.record_push_to_talk()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not wav:
                print("Voice capture unavailable; exiting voice mode.")
                break
            text = voice.transcribe(wav)
            with contextlib.suppress(OSError):
                os.remove(wav)
            if not text:
                print("(heard nothing — try again)")
                continue
            print(f"you> {text}")
            result = assistant.run_turn(
                conn,
                text,
                history,
                backend_name=backend_name,
                surface="voice",
                conversation_id=conversation_id,
                persona_name=(persona["name"] if persona else None),
            )
            conversation_id = result.get("conversation_id", conversation_id)
            history = result.get("history", history)
            reply = (result.get("reply") or "").strip()
            if reply:
                print(f"myos> {reply}")
                if not args.text_reply:
                    voice.speak(reply)
            _handle_proposals(conn, result.get("proposed_action_ids", []))

"""Pluggable reasoning/execution backends.

A *backend* is whatever agent does the thinking: ``claude`` (in-process Anthropic
SDK), SDK-backed coding agents, or an external agent CLI driven as a subprocess
(``cursor``, ``claude-code``, ``copilot`` or a generic ``command``). They share
one contract so the rest of the app never cares which one is active:

    request  = {"purpose", "objective", "context", "analogies", ...}
    response = {"reply": str, "plan": [{"step","detail"}], "actions": [action,...]}

where each ``action`` = {"action_type","title","payload","requires_approval"}.

Every action a backend returns is *proposed*, never executed -- the orchestrator
routes them through ``agentcore.enqueue_proposal`` into the existing approval queue.
"""

from __future__ import annotations

import os
import threading

from .. import agentcore, personas

DEFAULT_BACKEND = "claude"


def _coerce_approval(value) -> int:
    """Coerce a backend's ``requires_approval`` into 0/1 without crashing.

    External agent CLIs often return JSON booleans-as-strings ("true"/"false") or
    actual bools; a bare ``int("false")`` would raise and kill the chat turn (review
    P2a). Anything ambiguous fails SAFE — defaults to 1 (requires approval)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return 1 if value else 0
    s = str(value).strip().lower()
    if s in ("0", "false", "no", "off", ""):
        return 0
    if s in ("1", "true", "yes", "on"):
        return 1
    return 1  # unknown -> require approval (never silently auto-run something ambiguous)


def resolve_backend_name(explicit: str | None = None) -> str:
    name = (explicit or os.getenv("MYOS_AGENT_BACKEND") or DEFAULT_BACKEND).strip().lower()
    return name or DEFAULT_BACKEND


class BaseBackend:
    """Common machinery. Subclasses must implement :meth:`reason`.

    The default :meth:`run_turn` (one ``reason`` call + enqueue) is what the
    subprocess backends use; :class:`~providers.claude.ClaudeBackend` overrides it
    with a streaming tool-use loop.
    """

    name = "base"

    def available(self) -> tuple[bool, str]:  # pragma: no cover - trivial
        return True, ""

    def reason(self, conn, request: dict) -> dict:
        raise NotImplementedError

    def run_turn(self, conn, user_text: str, history: list[dict], on_text=None, *, persona: dict | None = None) -> dict:
        # CLI backends produce no token stream; on_text is accepted for a uniform
        # call signature and simply ignored here.
        context = _history_to_context(history)
        objective = user_text
        if persona is not None:
            objective = (
                f"Persona: {persona['display_name']}\n"
                f"Instructions: {persona['instructions']}\n"
                f"Allowed actions: {', '.join(persona['allowed_actions']) or 'none'}\n\n"
                f"User request: {user_text}"
            )
        result = self.reason(
            conn,
            {"purpose": "chat", "objective": objective, "context": context, "analogies": []},
        )
        reply = (result.get("reply") or _plan_to_text(result.get("plan", []))).strip()
        ids: list[int] = []
        task_id: int | None = None
        actions = result.get("actions", [])
        rejected: list[str] = []
        if persona is not None:
            actions, rejected = personas.filter_actions(persona, actions)
        for action in actions:
            if task_id is None:
                task_id = agentcore.ensure_turn_task(conn, user_text)
            ids.append(
                agentcore.enqueue_proposal(
                    conn,
                    task_id=task_id,
                    action_type=str(action.get("action_type", "draft_external_update")),
                    title=str(action.get("title", "Proposed action")),
                    payload=action.get("payload", {}) or {},
                    requires_approval=_coerce_approval(action.get("requires_approval", 1)),
                )
            )
        conn.commit()
        new_history = history + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": reply or "(no reply)"},
        ]
        return {
            "reply": reply,
            "proposed_action_ids": ids,
            "history": new_history,
            "backend": self.name,
            **({"persona": persona["name"], "rejected_action_types": rejected} if persona is not None else {}),
        }


def _history_to_context(history: list[dict], limit: int = 6) -> str:
    parts = []
    for msg in history[-limit:]:
        content = msg.get("content")
        if isinstance(content, list):  # anthropic block form
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        parts.append(f"{msg.get('role', 'user')}: {content}")
    return "\n".join(parts)


def _plan_to_text(plan: list[dict]) -> str:
    if not plan:
        return ""
    return "\n".join(f"{i}. {s.get('step', '')}: {s.get('detail', '')}" for i, s in enumerate(plan, 1))


def get_backend(name: str | None = None):
    resolved = resolve_backend_name(name)
    if resolved in ("claude-sdk", "claude_sdk", "claude-code-sdk", "claude_code_sdk", "sdk"):
        from .claude_sdk import ClaudeSdkBackend

        return ClaudeSdkBackend()
    if resolved == "claude":
        from .claude import ClaudeBackend

        return ClaudeBackend()
    if resolved == "copilot":
        from .copilot import CopilotBackend

        return CopilotBackend()
    if resolved == "cursor":
        from .cursor import CursorBackend

        return CursorBackend()
    if resolved == "zero":
        from .zero import ZeroBackend

        return ZeroBackend()
    if resolved in ("claude-code", "claude_code", "claudecode"):
        from .claude_code import ClaudeCodeBackend

        return ClaudeCodeBackend()
    if resolved in ("command", "cli"):
        from .agent_cli import AgentCliBackend

        return AgentCliBackend(name="command")
    # Auto-discovery: scan providers/ for {resolved}_backend.py containing a
    # BaseBackend subclass. Inspired by aisuite's plugin convention — drop a file,
    # get a backend, no registry edit needed.
    plugin = _discover_plugin_backend(resolved)
    if plugin is not None:
        return plugin
    # Unknown name: prefer the generic command backend if one is configured,
    # otherwise fall back to Claude.
    if os.getenv("MYOS_AI_COMMAND", "").strip():
        from .agent_cli import AgentCliBackend

        return AgentCliBackend(name=resolved)
    from .claude import ClaudeBackend

    return ClaudeBackend()


# Module-level cache for discovered plugin classes keyed by backend name.
# Avoids re-executing the plugin file on every get_backend() call.
# Maps name → (class | None) where None means "file exists but no valid class".
_plugin_cache: dict[str, type | None] = {}
_plugin_cache_lock = threading.Lock()


def _discover_plugin_backend(name: str):
    """Scan providers/ for {name}_backend.py with a BaseBackend subclass.

    Convention: a file named ``mybackend_backend.py`` containing a class that
    inherits BaseBackend is automatically available as ``get_backend("mybackend")``.
    Returns a fresh backend instance, or None when no matching file/class found.

    Results are cached at the class level so the plugin file is only executed
    once per process, not on every get_backend() call.
    """
    import importlib.util
    from pathlib import Path

    with _plugin_cache_lock:
        if name in _plugin_cache:
            cls = _plugin_cache[name]
            return cls() if cls is not None else None

        path = Path(__file__).parent / f"{name}_backend.py"
        if not path.exists():
            _plugin_cache[name] = None
            return None
        try:
            spec = importlib.util.spec_from_file_location(f"_plugin_{name}", path)
            if spec is None or spec.loader is None:
                _plugin_cache[name] = None
                return None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            for attr_name in dir(mod):
                obj = getattr(mod, attr_name)
                if isinstance(obj, type) and issubclass(obj, BaseBackend) and obj is not BaseBackend:
                    _plugin_cache[name] = obj
                    return obj()
        except Exception:  # noqa: BLE001 — plugin errors must never crash the host
            pass
        _plugin_cache[name] = None
        return None


def available_backends() -> list[dict]:
    """Best-effort availability probe for ``myos doctor``.

    Includes built-in backends and any auto-discovered plugin backends
    found in providers/*_backend.py.
    """
    from pathlib import Path

    out = []
    for name in ("claude", "claude-sdk", "claude-code-sdk", "cursor", "zero", "claude-code", "copilot", "command"):
        try:
            ok, detail = get_backend(name).available()
        except Exception as exc:  # pragma: no cover - defensive
            ok, detail = False, str(exc)[:200]
        out.append({"name": name, "available": ok, "detail": detail})

    # Surface auto-discovered plugin backends.
    providers_dir = Path(__file__).parent
    for path in sorted(providers_dir.glob("*_backend.py")):
        plugin_name = path.stem.replace("_backend", "")
        if any(b["name"] == plugin_name for b in out):
            continue  # already listed above
        try:
            plugin = _discover_plugin_backend(plugin_name)
            if plugin is not None:
                ok, detail = plugin.available()
                out.append({"name": plugin_name, "available": ok, "detail": detail, "plugin": True})
        except Exception:  # noqa: BLE001
            pass
    return out

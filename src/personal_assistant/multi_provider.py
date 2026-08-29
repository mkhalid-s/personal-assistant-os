"""Multi-provider parallel reasoning (X1, inspired by Spotify Xirp).

Runs N provider backends concurrently and picks the best response using a
simple heuristic scorer. Each thread opens its own sqlite3 connection to
avoid SQLite's single-writer threading restriction.

Usage:
    Set MYOS_MULTI_PROVIDER=claude,cursor (or any backends in providers/)
    The autonomy loop and factory _reason() calls will fan out automatically.

When only one or zero backends respond within the timeout, the result is
equivalent to single-backend reasoning — no degradation.
"""
from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .db import resolve_db_path


def _score_response(response: dict[str, Any]) -> float:
    """Score a provider response by action quality.

    Rewards: structured actions, approval-required actions (safer), non-empty plan.
    All weights are intentionally simple — diversity of opinions matters more than
    fine-grained scoring at this stage.
    """
    actions = response.get("actions") or []
    if not isinstance(actions, list):
        return 0.0
    score = 0.0
    for action in actions:
        if not isinstance(action, dict):
            continue
        score += 2.0 if action.get("requires_approval") else 1.0
    if response.get("plan"):
        score += 0.5
    if response.get("reply", "").strip():
        score += 0.5
    return score


def _call_backend(
    backend_name: str,
    request: dict[str, Any],
    timeout_sec: float,
    db_path: str,
) -> tuple[str, dict[str, Any]]:
    """Call one backend in a worker thread with its own DB connection."""
    from . import providers

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        backend = providers.get_backend(backend_name)
        ok, detail = backend.available()
        if not ok:
            raise RuntimeError(f"backend {backend_name!r} unavailable: {detail}")
        result = backend.reason(conn, request)
        return backend_name, result
    finally:
        conn.close()


def fan_out_reason(
    request: dict[str, Any],
    backend_names: list[str],
    *,
    timeout_sec: float = 60.0,
    db_path: str = "",
) -> list[tuple[str, dict[str, Any]]]:
    """Call all named backends concurrently and return their responses.

    Each backend runs in its own thread with its own sqlite3 connection.
    Backends that fail or exceed timeout_sec are silently excluded.
    Returns a list of (backend_name, response) pairs for successful backends.
    """
    if not backend_names:
        return []
    _db_path = db_path or resolve_db_path()
    results: list[tuple[str, dict[str, Any]]] = []

    with ThreadPoolExecutor(max_workers=len(backend_names)) as pool:
        futures = {
            pool.submit(_call_backend, name, request, timeout_sec, _db_path): name
            for name in backend_names
        }
        try:
            for future in as_completed(futures, timeout=timeout_sec + 5):
                try:
                    backend_name, response = future.result(timeout=1)
                    results.append((backend_name, response))
                except Exception:  # noqa: BLE001
                    pass  # backend failed — excluded from pick
        except TimeoutError:
            pass  # outer timeout: return whatever completed so far

    return results


def pick_best(
    responses: list[tuple[str, dict[str, Any]]],
) -> tuple[str, dict[str, Any]] | None:
    """Return the (backend_name, response) with the highest score.

    Returns None when responses is empty.
    """
    if not responses:
        return None
    return max(responses, key=lambda r: _score_response(r[1]))


def multi_reason(
    request: dict[str, Any],
    backend_names: list[str],
    *,
    timeout_sec: float = 60.0,
    db_path: str = "",
) -> tuple[str, dict[str, Any]] | None:
    """Fan out to multiple backends and return the best (name, response).

    Returns None when no backend succeeds, allowing callers to fall back to
    single-backend reasoning.
    """
    responses = fan_out_reason(request, backend_names, timeout_sec=timeout_sec, db_path=db_path)
    return pick_best(responses)


def configured_backends() -> list[str]:
    """Return the list of backends from MYOS_MULTI_PROVIDER env var.

    Returns an empty list when the var is unset or empty.
    """
    raw = os.getenv("MYOS_MULTI_PROVIDER", "").strip()
    if not raw:
        return []
    return [b.strip() for b in raw.split(",") if b.strip()]

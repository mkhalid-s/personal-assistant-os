"""Model price map for the LLM usage ledger.

Rates are expressed in **millicents per million tokens** (100_000
millicents = $1.00) so all ledger arithmetic stays in integers. The four
billing categories mirror how Anthropic actually prices Messages API calls
(docs/COST_OBSERVABILITY.md §2.1): base input, output, cache writes
(1.25x/2x input for 5m/1h TTL), and cache reads (0.1x input). The
multipliers are baked into the stored rates, so cost math is a plain
dot-product of token counts and rates.

The map ships as package data (`model_prices.json`) — same pattern as
`route_eval_fixtures.json` — and is deliberately small: models not in the
map price as ``None`` (tokens still recorded, cost NULL) rather than
guessing.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

_RATE_KEYS = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")


def _empty_map() -> dict[str, Any]:
    return {"version": "", "currency": "usd", "per_mtok": {}}


_MAP: dict[str, Any] | None = None


def load_price_map(refresh: bool = False) -> dict[str, Any]:
    """Load the packaged price map (cached; pass refresh=True in tests)."""
    global _MAP
    if _MAP is None or refresh:
        try:
            raw = resources.files("personal_assistant").joinpath("model_prices.json").read_text(encoding="utf-8")
            parsed = json.loads(raw)
        except (OSError, ValueError):
            parsed = _empty_map()
        _MAP = parsed if isinstance(parsed, dict) else _empty_map()
    return _MAP


def normalize_model(model: str | None) -> str:
    """Canonical price-lookup key for a reported model id.

    Bedrock qualifies model ids with an ``anthropic.`` prefix
    (providers/claude.py) and callers may pass mixed case; the ledger stores
    the model as reported, but price lookup keys on the bare id.
    """
    text = (model or "").strip().lower()
    for prefix in ("anthropic.", "bedrock/", "us.anthropic."):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text


def rate_for(model: str | None) -> tuple[dict[str, int] | None, str]:
    """Return ``(rates, price_version)`` for a model, or ``(None, version)``
    when the model is not in the map (cost stays NULL downstream)."""
    price_map = load_price_map()
    rates = price_map.get("per_mtok", {}).get(normalize_model(model))
    if not isinstance(rates, dict):
        return None, str(price_map.get("version", ""))
    cleaned = {}
    for key in _RATE_KEYS:
        value = rates.get(key)
        cleaned[key] = int(value) if isinstance(value, (int, float)) else 0
    return cleaned, str(price_map.get("version", ""))


def compute_cost_millicents(rates: dict[str, int] | None, tokens: dict[str, int]) -> int | None:
    """Dot-product token counts (per category) with per-MTok rates.

    ``tokens`` keys: input_tokens, output_tokens, cache_write_tokens,
    cache_read_tokens. Returns None when no rates are known so callers can
    distinguish "free" from "unknown".
    """
    if rates is None:
        return None
    total = 0
    for token_key, rate_key in (
        ("input_tokens", "input"),
        ("output_tokens", "output"),
        ("cache_write_tokens", "cache_write_5m"),
        ("cache_read_tokens", "cache_read"),
    ):
        count = tokens.get(token_key) or 0
        if count:
            total += int(count) * int(rates.get(rate_key, 0))
    # Rates are per million tokens; integer floor keeps the ledger exact in
    # millicents without float drift.
    return total // 1_000_000

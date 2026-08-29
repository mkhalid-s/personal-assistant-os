"""Lightweight context budget utilities for MYOS provider calls.

All functions use a 4-chars-per-token heuristic — accurate to ±20% on English
prose, sufficient as a pre-flight gate before sending to the API. The goal is to
warn early and trim, not to compete with a real tokenizer.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Approximate token limits per model family (conservative — actual limits higher).
# These are used for pre-flight budget checks, not hard enforcement.
_MODEL_CONTEXT_TOKENS: dict[str, int] = {
    "claude-opus": 200_000,
    "claude-sonnet": 200_000,
    "claude-haiku": 200_000,
    "default": 200_000,
}

# Warn when estimated tokens exceed this fraction of the model's context limit.
_WARN_FRACTION = 0.80


def estimate_tokens(text: str) -> int:
    """Estimate token count from a string (4 chars ≈ 1 token heuristic)."""
    return max(1, len(text) // 4)


def messages_token_estimate(messages: list[dict]) -> int:
    """Estimate total tokens across a messages list.

    Each message's content may be a string or a list of content blocks.
    Stringifies both forms then applies the 4-char heuristic.
    """
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += estimate_tokens(block.get("text", "") or json.dumps(block))
                else:
                    total += estimate_tokens(str(block))
        else:
            total += estimate_tokens(str(content))
    return total


def context_limit_for(model: str) -> int:
    """Return the estimated context token limit for the given model name."""
    model_lower = model.lower()
    for key, limit in _MODEL_CONTEXT_TOKENS.items():
        if key in model_lower:
            return limit
    return _MODEL_CONTEXT_TOKENS["default"]


def check_budget(messages: list[dict], model: str = "") -> tuple[bool, int, int]:
    """Check whether messages fit within the model's context budget.

    Returns (ok, estimated_tokens, limit) where ok=True when within budget.
    Logs a warning when the estimate exceeds _WARN_FRACTION of the limit.
    """
    estimated = messages_token_estimate(messages)
    limit = context_limit_for(model)
    ok = estimated < limit
    if not ok or estimated > limit * _WARN_FRACTION:
        logger.warning(
            "Context budget: ~%d tokens estimated (limit %d, model=%r)",
            estimated,
            limit,
            model or "unknown",
        )
    return ok, estimated, limit


def trim_history(
    messages: list[dict],
    *,
    keep_last: int = 20,
    budget_tokens: int | None = None,
    model: str = "",
) -> list[dict]:
    """Return a trimmed copy of messages that fits within budget.

    Strategy:
    1. Always keep the first message (system prompt if present).
    2. Apply a sliding window of keep_last pairs from the end.
    3. If budget_tokens is set, additionally trim until the estimate fits.

    The returned list is safe to pass directly to the API.
    """
    if not messages:
        return messages

    # Separate system (first message) from the conversation.
    has_system = messages[0].get("role") == "system"
    system = [messages[0]] if has_system else []
    convo = messages[1:] if has_system else messages[:]

    # Apply sliding window — keep pairs (user + assistant) from the end.
    # Each pair is 2 messages; keep_last refers to individual messages.
    trimmed = convo[-keep_last:] if len(convo) > keep_last else convo[:]

    # Additionally enforce token budget if provided.
    if budget_tokens is not None:
        effective_limit = budget_tokens
    elif model:
        effective_limit = int(context_limit_for(model) * _WARN_FRACTION)
    else:
        return system + trimmed

    while trimmed and messages_token_estimate(system + trimmed) > effective_limit:
        # Drop the two oldest messages (user + assistant pair) at a time.
        trimmed = trimmed[2:] if len(trimmed) >= 2 else trimmed[1:]

    return system + trimmed

"""Policy, privacy/redaction, and retention helpers.

Extracted from cli.py (refactor #12) so the redaction + retention logic lives in a
small, testable module rather than the 5k-line god-file. Pure of `cli`; takes a
`conn` so it stays import-light.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Any


def get_policy_map(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM assistant_policies ORDER BY key ASC").fetchall()
    return {str(r["key"]): str(r["value"]) for r in rows}


def _policy_bool(value: str, default: bool = True) -> bool:
    if value == "":
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _policy_int(value: str, default: int) -> int:
    """Tolerant int parse for free-form policy values (review #2): a stray/non-numeric
    retention value (e.g. 'never') falls back to the default instead of crashing the whole
    `myos cleanup` command with a ValueError. Mirrors _policy_bool's leniency."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# High-confidence secret/credential patterns. Each is specific enough to avoid mangling
# ordinary prose; tokens are the data we most need to keep out of a persisted, FTS-indexed
# conversation log. Names are intentionally NOT redacted — the EM domain is built on them
# and this is local-only data; redacting every capitalized word would destroy the product.
_SECRET_PATTERNS = (
    r"\bAKIA[0-9A-Z]{16}\b",  # AWS access key id
    r"\bgh[pousr]_[A-Za-z0-9]{16,}\b",  # GitHub tokens
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",  # Slack tokens
    r"\b(?:sk|pk|rk)-[A-Za-z0-9]{16,}\b",  # provider secret/publishable keys
    r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b",  # JWT
    r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|bearer)\b\s*[:=]\s*['\"]?[A-Za-z0-9._\-]{6,}",
)
_SSN_PATTERN = r"\b\d{3}-\d{2}-\d{4}\b"
# 13–19 digits with optional single separators BETWEEN digits only — the final atom is a
# digit, so the match never swallows a trailing space/dash and glue the next word (review #3/#11).
_CARD_CANDIDATE = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
# Phone: two branches, tried in order.
#   1. E.164 / international: a leading '+' followed by 7..17 more digits with optional single
#      separators. The '+' anchors it, so it never matches a bare date/duration/ID — this
#      unambiguously recovers the international numbers the NANP-only rewrite dropped entirely
#      (round-3 #9: +44/+91/+33/+1 forms, with or without spaces, incl. 'tel:+1555…').
#   2. NANP 3-3-4 with separators / parenthesized area / optional +country (the prior pattern).
# We deliberately do NOT add a bare-contiguous-digit branch: round-2 #9 established that bare
# 10-digit runs are too ambiguous with ticket/order numbers (see test_phone_redaction_not_over_
# eager) and a 16-digit grouped run collides with disabled-card text — keeping over-redaction off
# is the explicit prior decision, so we only add the '+'-anchored form which has no such collision.
_PHONE_PATTERN = (
    r"(?:"
    # 1) E.164 / international: '+' followed by 7..17 more digits with optional single
    #    separators. Lookbehind guards mid-token '+' (e.g. 'JIRA-+12345678') and the
    #    trailing (?!\d) stops partial matching on 19+ digit runs, leaving a clean
    #    non-phone token (review R4-7/R4-8). Comment update: the '+' anchor alone is NOT
    #    sufficient to avoid over-redaction — the lookbehind is required too (round-4 #7).
    r"(?<![\w.\-])\+\d(?:[\s.\-]?\d){7,17}(?!\d)"
    r"|"
    # 2) NANP 3-3-4 with separators / parenthesized area / optional +country code.
    r"(?<![\w.\-])"
    r"(?:\+\d{1,3}[\s.\-]?)?"  #    optional +country
    r"(?:\(\d{3}\)[\s.\-]?|\d{3}[\s.\-])"  #    area code: (555) or 555-
    r"\d{3}[\s.\-]\d{4}(?!\d)"  #    local 3-4 with a separator
    r")"
)


def _luhn_ok(digits: str) -> bool:
    """Luhn checksum — gates credit-card redaction so we don't clobber arbitrary long
    digit runs (order numbers, IDs) that merely look card-shaped."""
    nums = [int(c) for c in digits if c.isdigit()]
    if not (13 <= len(nums) <= 19):
        return False
    total, parity = 0, len(nums) % 2
    for i, n in enumerate(nums):
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def apply_privacy_filters(conn: sqlite3.Connection, text: str) -> str:
    """Redact PII/secrets from a string before it is persisted or indexed.

    Covers emails, phones, and (default-on) common secrets/tokens, US SSNs, and
    Luhn-valid credit-card numbers. Does NOT redact personal names by design — they are
    core to the EM domain and the data stays local. Each class is policy-gated."""
    policy = get_policy_map(conn)
    cleaned = text
    if _policy_bool(policy.get("redact_emails", "1"), True):
        cleaned = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[REDACTED_EMAIL]", cleaned)
    # Specific secret/SSN/card patterns run BEFORE the broad phone regex so a card or SSN
    # gets its correct label rather than being swallowed as a phone number.
    if _policy_bool(policy.get("redact_secrets", "1"), True):
        for pat in _SECRET_PATTERNS:
            cleaned = re.sub(pat, "[REDACTED_SECRET]", cleaned)
        cleaned = re.sub(_SSN_PATTERN, "[REDACTED_SSN]", cleaned)
    # Card redaction is separately gated (review #10) so a numeric-ID-heavy user can disable
    # just cards (Luhn still chance-matches some legitimate 13–19 digit IDs) without losing
    # secret/SSN redaction.
    if _policy_bool(policy.get("redact_cards", "1"), True):
        cleaned = _CARD_CANDIDATE.sub(lambda m: "[REDACTED_CARD]" if _luhn_ok(m.group(0)) else m.group(0), cleaned)
    if _policy_bool(policy.get("redact_phones", "1"), True):
        cleaned = re.sub(_PHONE_PATTERN, "[REDACTED_PHONE]", cleaned)
    return cleaned


def redact_obj(conn: sqlite3.Connection, obj: Any) -> Any:
    """Recursively redact string leaves of a dict/list, leaving non-strings (ints,
    bools, None) intact. Use this for payloads instead of regexing a serialized JSON
    string — the phone regex would otherwise mangle integer literals (e.g. an
    issue_number) into invalid JSON and crash json.loads (review C-3)."""
    if isinstance(obj, str):
        return apply_privacy_filters(conn, obj)
    if isinstance(obj, dict):
        return {k: redact_obj(conn, v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(conn, v) for v in obj]
    return obj


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cleanup_policy_retention(conn: sqlite3.Connection) -> dict[str, int]:
    policy = get_policy_map(conn)
    media_days = _policy_int(policy.get("retention_media_days", "30"), 30)
    evidence_days = _policy_int(policy.get("retention_evidence_days", "365"), 365)
    # Conversation history defaults to KEEP-forever (0): the user asked to log everything
    # for better analysis, so we don't silently delete it. A privacy-conscious user sets a
    # positive day count to enable purging (review M2).
    conversation_days = _policy_int(policy.get("retention_conversation_days", "0"), 0)
    media_cutoff = f"-{media_days} days"
    old_media = "SELECT id FROM media_assets WHERE created_at < datetime('now', ?)"
    # Delete the dependent text_chunks FIRST so the FTS delete-trigger purges them
    # from the search index too — otherwise purged media stays searchable forever,
    # defeating retention/privacy (finding #22). No FK cascade exists on text_chunks.
    conn.execute(
        f"DELETE FROM text_chunks WHERE source_type='media_asset' AND source_id IN ({old_media})",
        (media_cutoff,),
    )
    # media_assets has non-cascading FK children (media_imports, file_ingests); delete those
    # before the parent or PRAGMA foreign_keys=ON aborts the whole cleanup (review #1 regression).
    conn.execute(f"DELETE FROM media_imports WHERE media_asset_id IN ({old_media})", (media_cutoff,))
    conn.execute(f"DELETE FROM file_ingests WHERE media_asset_id IN ({old_media})", (media_cutoff,))
    # The watch-dir ingest creates a provenance row (source_type='file', source_ref=file_path)
    # per media file; without this it outlives the purged media as an orphan (review #10). Tie
    # the cleanup to the file_path of the media being purged so only matching rows are removed.
    # Guard: exclude any file_path still referenced by a NON-aged media asset — media_assets
    # has no UNIQUE(file_path), so a content-changed re-ingest creates a second same-path row,
    # and deleting its provenance would destroy a live media row's attribution (review R4-9).
    conn.execute(
        "DELETE FROM provenance WHERE source_type='file' "
        "AND source_ref IN "
        "(SELECT file_path FROM media_assets WHERE created_at < datetime('now', ?)) "
        "AND source_ref NOT IN "
        "(SELECT file_path FROM media_assets WHERE created_at >= datetime('now', ?))",
        (media_cutoff, media_cutoff),
    )
    media_deleted = conn.execute(
        "DELETE FROM media_assets WHERE created_at < datetime('now', ?)", (media_cutoff,)
    ).rowcount
    evidence_deleted = conn.execute(
        "DELETE FROM review_evidence WHERE created_at < datetime('now', ?)",
        (f"-{evidence_days} days",),
    ).rowcount

    conversation_turns_deleted = 0
    if conversation_days > 0:
        cutoff = f"-{conversation_days} days"
        old_turns = "SELECT id FROM conversation_turns WHERE created_at < datetime('now', ?)"
        # 1) Purge the mirrored chunks first so the FTS delete-trigger drops them from search.
        conn.execute(
            f"DELETE FROM text_chunks WHERE source_type='conversation' AND source_id IN ({old_turns})",
            (cutoff,),
        )
        # 2) Observations FK-reference turns (no ON DELETE CASCADE), so delete them before the turns.
        conn.execute(
            f"DELETE FROM context_observations WHERE turn_id IN ({old_turns})",
            (cutoff,),
        )
        # 3) The turns themselves, then 4) any conversation left with no turns.
        conversation_turns_deleted = conn.execute(
            "DELETE FROM conversation_turns WHERE created_at < datetime('now', ?)",
            (cutoff,),
        ).rowcount
        # Only purge conversations that are BOTH empty AND aged past the cutoff — otherwise a
        # conversation just created by start_conversation that has not yet logged its first turn
        # would be deleted out from under an active session (race on a shared DB) (review #10).
        conn.execute(
            "DELETE FROM conversations WHERE started_at < datetime('now', ?) "
            "AND id NOT IN (SELECT DISTINCT conversation_id FROM conversation_turns)",
            (cutoff,),
        )
        # 5) Derived artifacts must not outlive their source conversations in cleartext:
        #    purge old reflection insights (review #7) and the context-derived relationship
        #    graph, then drop person nodes left with no edges (review #8).
        conn.execute(
            "DELETE FROM context_insights WHERE kind='reflection' AND created_at < datetime('now', ?)",
            (cutoff,),
        )
        conn.execute(
            "DELETE FROM knowledge_edges WHERE source='context' AND created_at < datetime('now', ?)",
            (cutoff,),
        )
        conn.execute(
            "DELETE FROM knowledge_nodes WHERE node_type='person' AND id NOT IN "
            "(SELECT from_node_id FROM knowledge_edges UNION SELECT to_node_id FROM knowledge_edges)"
        )
    return {
        "media": media_deleted,
        "evidence": evidence_deleted,
        "conversation_turns": conversation_turns_deleted,
    }

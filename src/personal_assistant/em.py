"""Engineering-manager domain layer: people, performance evidence, 1:1s,
competencies, and meetings — plus inference-first ingestion of free-form input.

Design principle (per user feedback): the user should be able to type or paste
*anything* — a 1:1 note, a praise blurb, a status update, a meeting transcript —
and the system figures out what it is and where it goes. The Claude brain does the
high-quality inference by calling the structured functions here as tools; this
module also ships a deterministic `infer_note` / `infer_meeting` so the
`myos note` / `myos meeting` commands still work with no model available.

Import-light (db + agentcore only) to avoid a cycle with cli.
"""

from __future__ import annotations

import re

from . import agentcore
from .db import append_event
from .privacy import apply_privacy_filters

# ---------------------------------------------------------------- people


def resolve_person(conn, name: str, *, create: bool = True, **fields) -> int | None:
    name = (name or "").strip()
    if not name:
        return None
    row = conn.execute("SELECT id FROM people WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row:
        pid = int(row["id"])
        sets = {k: v for k, v in fields.items() if v}
        if sets:
            cols = ", ".join(f"{k} = ?" for k in sets)
            conn.execute(
                f"UPDATE people SET {cols}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (*sets.values(), pid)
            )
        return pid
    if not create:
        return None
    conn.execute(
        "INSERT INTO people (name, role, team, relation, notes) VALUES (?, ?, ?, ?, ?)",
        (name, fields.get("role"), fields.get("team"), fields.get("relation", "report"), fields.get("notes")),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def upsert_person(conn, name: str, **fields) -> int:
    return resolve_person(conn, name, create=True, **fields)


def list_team(conn) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute("SELECT id, name, role, team, relation FROM people ORDER BY relation, name").fetchall()
    ]


# ---------------------------------------------------------------- evidence / 1:1 / competency

EVIDENCE_CATEGORIES = ("leadership", "delivery", "technical", "communication", "collaboration", "growth", "ownership")


def record_evidence(
    conn, person: str, category: str, impact: str, artifact_link: str | None = None, privacy: str = "internal"
) -> int:
    pid = resolve_person(conn, person)
    # Redact free-text here — em.py is the single chokepoint for EM writes, so every
    # caller (CLI `myos note/log-evidence` AND the Claude `log_evidence` tool) is covered
    # at one place (review findings #2/#3/#7/#8). Names/categories are entity labels, kept.
    impact = apply_privacy_filters(conn, impact or "")
    conn.execute(
        "INSERT INTO review_evidence (person, person_id, category, impact, artifact_link, privacy_level) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (person, pid, category or "general", impact, artifact_link, privacy),
    )
    eid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    append_event(conn, "evidence_logged", "review_evidence", eid, f"{person}:{category}")
    return eid


def log_one_on_one(
    conn,
    person: str,
    raw_text: str,
    *,
    occurred_on: str | None = None,
    summary: str | None = None,
    sentiment: str | None = None,
    action_items: list[str] | None = None,
) -> dict:
    pid = resolve_person(conn, person)
    # Redact before persisting/indexing. Note _first_sentence/_extract_action_items derive
    # from the redacted raw_text, and model-supplied summary/action_items are filtered too,
    # so nothing reaches one_on_ones OR the inbox/FTS in cleartext (findings #2/#7).
    raw_text = apply_privacy_filters(conn, raw_text or "")
    if summary:
        summary = apply_privacy_filters(conn, summary)
    if action_items:
        action_items = [apply_privacy_filters(conn, str(a)) for a in action_items]
    conn.execute(
        "INSERT INTO one_on_ones (person_id, occurred_on, raw_text, summary, sentiment) VALUES (?, ?, ?, ?, ?)",
        (pid, occurred_on, raw_text, summary or _first_sentence(raw_text), sentiment or _infer_sentiment(raw_text)),
    )
    oid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    item_ids = []
    for ai in action_items or _extract_action_items(raw_text):
        new_id, created = agentcore.capture_item(
            conn, text=f"[1:1 {person}] {ai}", kind="commitment", owner=person, source="one_on_one"
        )
        if created:
            item_ids.append(new_id)
    append_event(conn, "one_on_one_logged", "one_on_one", oid, person)
    return {"one_on_one_id": oid, "action_item_ids": item_ids}


def record_competency(conn, person: str, competency: str, level: str | None = None, notes: str | None = None) -> int:
    pid = resolve_person(conn, person)
    if notes:
        notes = apply_privacy_filters(conn, notes)
    conn.execute(
        "INSERT INTO competency_snapshots (person_id, competency, level, notes) VALUES (?, ?, ?, ?)",
        (pid, competency, level, notes),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


# ---------------------------------------------------------------- meetings (P3)


def capture_meeting(
    conn,
    title: str,
    raw_text: str,
    *,
    source: str = "manual",
    occurred_on: str | None = None,
    summary: str | None = None,
    items: list[dict] | None = None,
) -> dict:
    # Redact the transcript/notes before storing or indexing — audio transcripts are
    # exactly where spoken phone numbers/PII land (findings #2/#7). Derived items and the
    # inbox action items below come from this already-filtered text. The title can be the
    # transcript's first sentence (cmd_meeting), so redact it too — it propagates to the
    # `[mtg: {title}]` inbox action items.
    title = apply_privacy_filters(conn, title) if title else title
    raw_text = apply_privacy_filters(conn, raw_text or "")
    if summary:
        summary = apply_privacy_filters(conn, summary)
    conn.execute(
        "INSERT INTO meetings (title, occurred_on, source, raw_text, summary) VALUES (?, ?, ?, ?, ?)",
        (title or "Untitled meeting", occurred_on, source, raw_text, summary or _first_sentence(raw_text)),
    )
    mid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    if items is None:
        items = _extract_meeting_items(raw_text)
    item_ids, action_count = [], 0
    for item in items:
        kind = item.get("kind", "action")
        # Model-supplied item text may not derive from the redacted raw_text, so filter it.
        text = apply_privacy_filters(conn, (item.get("text") or "").strip())
        if not text:
            continue
        conn.execute(
            "INSERT INTO meeting_items (meeting_id, kind, text, owner, due_date) VALUES (?, ?, ?, ?, ?)",
            (mid, kind, text, item.get("owner"), item.get("due_date")),
        )
        item_ids.append(int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]))
        if kind == "action":
            action_count += 1
            agentcore.capture_item(
                conn,
                text=f"[mtg: {title}] {text}",
                kind="commitment",
                owner=item.get("owner"),
                due_date=item.get("due_date"),
                source="meeting",
            )
    append_event(conn, "meeting_captured", "meeting", mid, f"{title}:{action_count} actions")
    return {"meeting_id": mid, "item_ids": item_ids, "action_items": action_count}


# ---------------------------------------------------------------- dossier / review


def person_dossier(conn, person: str) -> dict:
    pid = resolve_person(conn, person, create=False)
    if pid is None:
        return {"error": f"no person named '{person}'"}
    p = conn.execute("SELECT * FROM people WHERE id = ?", (pid,)).fetchone()
    evidence = [
        dict(r)
        for r in conn.execute(
            "SELECT category, impact, artifact_link, created_at FROM review_evidence "
            "WHERE person_id = ? OR person = ? COLLATE NOCASE ORDER BY created_at DESC",
            (pid, person),
        ).fetchall()
    ]
    one_on_ones = [
        dict(r)
        for r in conn.execute(
            "SELECT occurred_on, summary, sentiment, created_at FROM one_on_ones "
            "WHERE person_id = ? ORDER BY created_at DESC",
            (pid,),
        ).fetchall()
    ]
    competencies = [
        dict(r)
        for r in conn.execute(
            "SELECT competency, level, notes, assessed_on FROM competency_snapshots "
            "WHERE person_id = ? ORDER BY assessed_on DESC",
            (pid,),
        ).fetchall()
    ]
    open_items = [
        dict(r)
        for r in conn.execute(
            "SELECT text, kind, due_date FROM inbox_items "
            "WHERE owner = ? COLLATE NOCASE AND status != 'archived' ORDER BY created_at DESC LIMIT 20",
            (person,),
        ).fetchall()
    ]
    return {
        "person": dict(p),
        "evidence": evidence,
        "one_on_ones": one_on_ones,
        "competencies": competencies,
        "open_items": open_items,
    }


def build_review_packet(conn, person: str) -> str:
    """Deterministic markdown review packet assembled from the dossier. The brain's
    draft_review tool produces richer prose; this is the no-model baseline."""
    d = person_dossier(conn, person)
    if "error" in d:
        return d["error"]
    p = d["person"]
    lines = [f"# Review packet — {p['name']}" + (f" ({p['role']})" if p.get("role") else ""), ""]

    by_cat: dict[str, list[str]] = {}
    for e in d["evidence"]:
        by_cat.setdefault(e["category"], []).append(e["impact"])
    lines.append("## Evidence by category")
    if by_cat:
        for cat, impacts in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"### {cat} ({len(impacts)})")
            lines += [f"- {i}" for i in impacts]
    else:
        lines.append("_No evidence logged yet._")

    lines += ["", "## Competencies"]
    lines += [
        f"- {c['competency']}: {c['level'] or 'n/a'}" + (f" — {c['notes']}" if c.get("notes") else "")
        for c in d["competencies"]
    ] or ["_None assessed._"]

    lines += ["", "## 1:1 themes"]
    lines += [
        f"- {o.get('occurred_on') or o['created_at'][:10]}: {o.get('summary') or ''} "
        f"({o.get('sentiment') or 'neutral'})"
        for o in d["one_on_ones"]
    ] or ["_No 1:1s logged._"]

    lines += ["", "## Open commitments"]
    lines += [f"- {i['text']}" + (f" (due {i['due_date']})" if i.get("due_date") else "") for i in d["open_items"]] or [
        "_None tracked._"
    ]

    top = ", ".join(c for c, _ in sorted(by_cat.items(), key=lambda kv: -len(kv[1]))[:3]) or "no logged areas yet"
    lines += [
        "",
        "## Suggested narrative (draft)",
        f"{p['name']} has {len(d['evidence'])} logged evidence item(s), strongest in {top}. "
        "Review the items above and expand into the review template; ask MYOS to draft prose "
        "from this packet for a polished version.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- inference (deterministic fallback)

_CATEGORY_HINTS = {
    "leadership": ("led", "drove", "mentor", "coached", "stepped up", "owned the", "rallied"),
    "delivery": ("shipped", "delivered", "launched", "on time", "completed", "closed out"),
    "technical": ("designed", "architected", "debugged", "refactored", "fixed the", "root caused", "performance"),
    "communication": ("communicated", "updated stakeholders", "wrote", "presented", "clarified", "aligned"),
    "collaboration": ("helped", "paired", "unblocked", "supported", "cross-team", "collaborated"),
    "growth": ("improved", "learning", "grew", "took feedback", "stretch", "developed"),
    "ownership": ("incident", "on-call", "took ownership", "followed up", "saw it through"),
}
_POSITIVE = ("great", "excellent", "strong", "well", "impressive", "nailed", "calm", "smoothly", "kudos", "praise")
_CONCERN = ("missed", "slipped", "concern", "struggled", "dropped", "late", "frustrated", "blocked", "risk")


def classify_note(text: str) -> str:
    t = text.lower()
    # 1:1 is the most specific cue.
    if any(
        k in t for k in ("1:1", "1-1", "one on one", "one-on-one", "synced with", "caught up with", "checked in with")
    ):
        return "one_on_one"
    # Meeting BEFORE the bare decision branch (finding #6): a multi-line note that
    # records a decision and assigns actions is a meeting — otherwise it would be
    # filed as a single 'decision' and all its action items/owners would be lost.
    multiline = "\n" in text.strip()
    # Word-boundary cues (review B5): \bsync\b must NOT match inside "async", and bare
    # "retro"/"kickoff" on a one-liner are too weak — require multiline for those.
    strong_cue = bool(re.search(r"\b(meetings?|stand-?ups?|kick-?offs?|syncs?)\b", t)) or "review with" in t
    retro_cue = multiline and bool(re.search(r"\bretro(spective)?\b", t))
    if (
        strong_cue
        or retro_cue
        or (multiline and ("we decided" in t or "decided to" in t or "action item" in t or "\n-" in text))
    ):
        return "meeting"
    if t.startswith("decision:") or "we decided" in t or "decided to" in t:
        return "decision"
    if any(k in t for k in _CONCERN) and not any(k in t for k in _POSITIVE):
        return "risk"
    if any(h in t for hints in _CATEGORY_HINTS.values() for h in hints):
        return "evidence"
    if any(k in t for k in ("status", "update:", "fyi")):
        return "status"
    return "note"


def infer_category(text: str) -> str:
    t = text.lower()
    best, score = "general", 0
    for cat, hints in _CATEGORY_HINTS.items():
        n = sum(1 for h in hints if h in t)
        if n > score:
            best, score = cat, n
    return best


def _infer_sentiment(text: str) -> str:
    t = text.lower()
    if any(k in t for k in _CONCERN) and not any(k in t for k in _POSITIVE):
        return "concern"
    if any(k in t for k in _POSITIVE):
        return "positive"
    return "neutral"


def extract_person(conn, text: str) -> str | None:
    # 1) match against known people first (most reliable)
    try:
        names = [r["name"] for r in conn.execute("SELECT name FROM people").fetchall()]
    except Exception:
        names = []
    for name in names:
        if re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE):
            return name
    # 2) capitalized token following a relational cue
    m = re.search(r"\b(?:with|that|for|gave|told|from|:)\s+([A-Z][a-z]+)\b", text)
    if m and m.group(1).lower() not in _NON_PERSON_WORDS:
        return m.group(1)
    # 3) leading capitalized name ("Priya led the ...")
    m = re.search(r"^([A-Z][a-z]+)\b", text.strip())
    if m and m.group(1).lower() not in _NON_PERSON_WORDS:
        return m.group(1)
    return None


# Capitalized words that must NOT be treated as a person (so `myos note` does not
# auto-create a "Friday"/"Monday"/"The" report — low-severity ingestion finding).
_NON_PERSON_WORDS = {
    "the",
    "decision",
    "we",
    "i",
    "they",
    "status",
    "fyi",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "today",
    "tomorrow",
    "next",
    "action",
    "risk",
    "also",
    "let",
    "team",
    "this",
    "that",
    "it",
    "update",
}


def _extract_action_items(text: str) -> list[str]:
    items = []
    for line in re.split(r"[\n;]|(?<=[.!?])\s+", text):
        s = line.strip(" -•\t")
        if not s:
            continue
        low = s.lower()
        if any(k in low for k in _ACTION_CUES):
            items.append(re.sub(r"^(action:?|todo:?)\s*", "", s, flags=re.IGNORECASE))
    return items[:10]


def _extract_meeting_items(text: str) -> list[dict]:
    items = []
    for line in re.split(r"[\n;]|(?<=[.!?])\s+", text):
        s = line.strip(" -•\t")
        if not s:
            continue
        low = s.lower()
        if low.startswith("decision") or "we decided" in low or "decided to" in low:
            items.append({"kind": "decision", "text": re.sub(r"^decision:?\s*", "", s, flags=re.IGNORECASE)})
        elif any(k in low for k in ("risk", "blocked", "blocker", "concern")):
            items.append({"kind": "risk", "text": s})
        elif any(k in low for k in _ACTION_CUES):
            items.append(
                {
                    "kind": "action",
                    "text": re.sub(r"^(action:?)\s*", "", s, flags=re.IGNORECASE),
                    "owner": _lead_owner(s),
                }
            )
    return items


# Action cues. A date-anchored "by <when>" only — bare "by " mis-tags things like
# "30% faster by load time" as actions (finding #24).
_ACTION_CUES = (
    "will ",
    "i'll",
    "we'll",
    "action:",
    "action item",
    "todo",
    "to do",
    "follow up",
    "follow-up",
    "next step",
    "owns ",
    "take ",
    "by monday",
    "by tuesday",
    "by wednesday",
    "by thursday",
    "by friday",
    "by saturday",
    "by sunday",
    "by eod",
    "by tomorrow",
    "by next",
)
# Leading capitalized words that are NOT owners.
_NOT_OWNERS = {
    "The",
    "We",
    "I",
    "Let",
    "Also",
    "Next",
    "Action",
    "Risk",
    "Decision",
    "Team",
    "This",
    "That",
    "They",
    "It",
}


def _lead_owner(s: str) -> str | None:
    m = re.match(r"\s*([A-Z][a-z]+)\b", s)
    if m and m.group(1) not in _NOT_OWNERS:
        return m.group(1)
    return None


def _first_sentence(text: str, limit: int = 200) -> str:
    text = (text or "").strip()
    m = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)
    s = m[0] if m else text
    return s if len(s) <= limit else s[: limit - 3] + "..."


def infer_note(conn, text: str) -> dict:
    """Deterministic classify + extract for free-form input (no-model fallback)."""
    text = (text or "").strip()
    kind = classify_note(text)
    person = extract_person(conn, text)
    out = {"kind": kind, "person": person}
    if kind == "evidence":
        out.update(category=infer_category(text), impact=text, sentiment=_infer_sentiment(text))
    elif kind == "one_on_one":
        out.update(
            summary=_first_sentence(text), sentiment=_infer_sentiment(text), action_items=_extract_action_items(text)
        )
    elif kind == "meeting":
        out.update(items=_extract_meeting_items(text))
    return out


def route_note(conn, text: str) -> dict:
    """Infer what a free-form note is and persist it to the right place."""
    info = infer_note(conn, text)
    kind = info["kind"]
    if kind == "evidence" and info.get("person"):
        eid = record_evidence(conn, info["person"], info["category"], info["impact"])
        return {"routed": "evidence", "person": info["person"], "category": info["category"], "id": eid}
    if kind == "one_on_one" and info.get("person"):
        res = log_one_on_one(
            conn,
            info["person"],
            text,
            summary=info.get("summary"),
            sentiment=info.get("sentiment"),
            action_items=info.get("action_items"),
        )
        return {"routed": "one_on_one", "person": info["person"], **res}
    if kind == "meeting":
        res = capture_meeting(conn, _first_sentence(text, 60), text)
        return {"routed": "meeting", **res}
    # decision / risk / status / note -> inbox (existing triage pipeline handles these)
    new_id, created = agentcore.capture_item(conn, text=text, kind=_inbox_kind(kind), source="note")
    return {"routed": "inbox", "kind": _inbox_kind(kind), "id": new_id, "created": created}


def _inbox_kind(kind: str) -> str:
    return {"decision": "decision", "risk": "risk", "status": "note", "note": "note"}.get(kind, "note")

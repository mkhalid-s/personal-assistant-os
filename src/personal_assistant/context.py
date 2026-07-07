"""Context Intelligence Loop — the memory substrate that makes MYOS improve from use.

Three jobs, mapped to the three questions this subsystem exists to answer:

1. *"Where is everything I converse + the responses logged?"* — every turn is persisted
   (``conversations`` / ``conversation_turns``) and mirrored into the FTS-indexed
   ``text_chunks`` so it is immediately recall-able across sessions.
2. *"How do relationships & intelligence improve from context?"* — after each turn we
   extract lightweight episodic *observations* (people, commitments, preferences, risks)
   into ``context_observations`` (the memory stream). :func:`reflect` distills recent
   observations into ``context_insights`` and derives relationship edges in the knowledge
   graph (``knowledge_edges``). Retrieval is *scored* by recency + relevance + importance.
3. *"How is related context + improvement tracked?"* — :func:`propose_suggestion` records
   improvement ideas into ``context_suggestions`` with a gated lifecycle
   (proposed → accepted/dismissed/applied). Suggestions are never auto-applied; the
   accept/dismiss decision is itself logged back as feedback so the loop learns.

Dependency-light (db / retrieval / graph / em / privacy + json/re) and free of ``cli``,
so any backend or the orchestrator can call it without import cycles.
"""

from __future__ import annotations

import json
import re

from . import agentcore, em, graph
from .db import append_event
from .privacy import _policy_bool, apply_privacy_filters, get_policy_map
from .retrieval import hybrid_score

# How important an observation of each kind is by default. Preferences and risks are the
# most decision-relevant (and least re-derivable), so they survive hygiene the longest.
_KIND_IMPORTANCE = {
    "preference": 0.85,
    "risk": 0.80,
    "feedback": 0.75,
    "commitment": 0.70,
    "decision": 0.70,
    "person": 0.55,
    "topic": 0.40,
}

# Cues that a user utterance expresses a durable preference about how MYOS should behave.
_PREFERENCE_CUES = (
    "i prefer",
    "i'd prefer",
    "i like",
    "i don't like",
    "i dislike",
    "i hate",
    "always ",
    "never ",
    "from now on",
    "please always",
    "please never",
    "don't ask",
    "stop asking",
    "remember that",
    "going forward",
)


# --------------------------------------------------------------------------------------
# Slice 1 — conversation logging (the chokepoint every surface routes through)
# --------------------------------------------------------------------------------------
def logging_enabled(conn) -> bool:
    """Conversation logging honors a kill-switch policy (default ON). A privacy-conscious
    user can `myos policy set log_conversations false` to disable all turn persistence."""
    return _policy_bool(get_policy_map(conn).get("log_conversations", "1"), True)


def start_conversation(conn, *, surface: str = "chat", backend: str | None = None) -> int:
    conn.execute(
        "INSERT INTO conversations (surface, backend) VALUES (?, ?)",
        (surface, backend),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def log_turn(
    conn,
    *,
    user_text: str,
    assistant_text: str,
    conversation_id: int | None = None,
    surface: str = "chat",
    backend: str | None = None,
    proposed_action_ids: list | None = None,
    retrieval_run_ids: list | None = None,
    latency_ms: int | None = None,
    extract: bool = True,
) -> dict:
    """Persist one conversational turn and (optionally) extract observations from it.

    Redacts both sides with the same privacy filters used everywhere else before the
    text touches disk. No-op (returns ``{}``) when logging is disabled by policy. Commits
    so the turn is durable even if the process dies immediately afterward.
    """
    if not logging_enabled(conn):
        return {}
    if conversation_id is None:
        conversation_id = start_conversation(conn, surface=surface, backend=backend)

    user_clean = apply_privacy_filters(conn, user_text or "")
    asst_clean = apply_privacy_filters(conn, assistant_text or "")
    # Self-heal a threaded conversation_id that no longer exists (DB reset / shared
    # MYOS_DB_PATH / maintenance) rather than crashing the rest of the session (review L7).
    row = conn.execute("SELECT turn_count FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if row is None:
        conversation_id = start_conversation(conn, surface=surface, backend=backend)
        turn_index = 0
    else:
        turn_index = int(row["turn_count"])
    conn.execute(
        """
        INSERT INTO conversation_turns
            (
                conversation_id, turn_index, user_text, assistant_text, backend,
                proposed_action_ids, retrieval_run_ids, latency_ms
            )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            conversation_id,
            turn_index,
            user_clean,
            asst_clean,
            backend,
            json.dumps(list(proposed_action_ids or []), ensure_ascii=True),
            json.dumps(list(retrieval_run_ids or []), ensure_ascii=True),
            latency_ms,
        ),
    )
    turn_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "UPDATE conversations SET turn_count = turn_count + 1, last_turn_at = CURRENT_TIMESTAMP WHERE id = ?",
        (conversation_id,),
    )
    # Mirror into the searchable memory so the conversation itself is recall-able via FTS.
    agentcore.remember(
        conn,
        f"you: {user_clean}\nmyos: {asst_clean}".strip(),
        source_type="conversation",
        source_id=turn_id,
    )
    n_obs = extract_observations(conn, turn_id, user_clean, asst_clean) if extract else 0
    conn.commit()
    return {"conversation_id": conversation_id, "turn_id": turn_id, "observations": n_obs}


# --------------------------------------------------------------------------------------
# Slice 2 — observation stream, reflection, relationship-graph derivation
# --------------------------------------------------------------------------------------
def _insert_observation(conn, turn_id: int | None, kind: str, subject: str | None, detail: str) -> bool:
    """Insert one observation, skipping an exact active duplicate (cheap dedup at write
    time; :func:`hygiene` handles the rest). Returns True if a row was written."""
    detail = (detail or "").strip()
    if not detail:
        return False
    dup = conn.execute(
        "SELECT 1 FROM context_observations WHERE kind = ? AND IFNULL(subject,'') = IFNULL(?,'') "
        "AND detail = ? AND status = 'active' LIMIT 1",
        (kind, subject, detail[:1000]),
    ).fetchone()
    if dup:
        return False
    conn.execute(
        "INSERT INTO context_observations (turn_id, kind, subject, detail, importance) VALUES (?, ?, ?, ?, ?)",
        (turn_id, kind, subject, detail[:1000], _KIND_IMPORTANCE.get(kind, 0.5)),
    )
    return True


def extract_observations(conn, turn_id: int | None, user_text: str, assistant_text: str) -> int:
    """Inference-first episodic extraction from a turn — both sides. Cheap, deterministic,
    no model: reuses the EM heuristics (known-people match, action-item cues, concern cues)
    so the observation stream stays consistent with how notes are already classified.

    The *user* side carries intent (preferences, what they're tracking); the *assistant*
    side carries commitments MYOS made ("I'll follow up …") — both are mined so the loop
    captures the whole turn, not just half of it (review M4).
    """
    user = (user_text or "").strip()
    asst = (assistant_text or "").strip()
    if not (user or asst):
        return 0
    count = 0
    combined = f"{user}\n{asst}".strip()

    # People — known team members named anywhere in the turn (most reliable signal).
    try:
        known = [r["name"] for r in conn.execute("SELECT name FROM people").fetchall()]
    except Exception:
        known = []
    mentioned: list[str] = []
    for name in known:
        if re.search(rf"\b{re.escape(name)}\b", combined, re.IGNORECASE):
            mentioned.append(name)
            if _insert_observation(conn, turn_id, "person", name, f"Mentioned {name}: {em._first_sentence(combined)}"):
                count += 1
    if not mentioned and user:  # fall back to a single best-guess name only when no known match
        guess = em.extract_person(conn, user)
        if guess:
            mentioned.append(guess)
            if _insert_observation(conn, turn_id, "person", guess, f"Mentioned {guess}: {em._first_sentence(user)}"):
                count += 1

    # Commitments / action items — the user's, and the ones MYOS committed to in its reply.
    for item in em._extract_action_items(user):
        if _insert_observation(conn, turn_id, "commitment", em._lead_owner(item), item):
            count += 1
    for item in em._extract_action_items(asst):
        # An action item in the assistant's reply is a commitment MYOS made on the user's behalf.
        if _insert_observation(conn, turn_id, "commitment", em._lead_owner(item) or "MYOS", item):
            count += 1

    # Durable preferences about how MYOS should behave — stated by the user (high importance).
    if (
        user
        and any(cue in user.lower() for cue in _PREFERENCE_CUES)
        and _insert_observation(conn, turn_id, "preference", None, em._first_sentence(user))
    ):
        count += 1

    # Risk / concern signal anywhere in the turn.
    low = combined.lower()
    if any(cue in low for cue in em._CONCERN) and not any(cue in low for cue in em._POSITIVE):
        subj = mentioned[0] if mentioned else None
        if _insert_observation(conn, turn_id, "risk", subj, em._first_sentence(combined)):
            count += 1

    # Co-mention edges: any two entities named in the same turn are related.
    _derive_comention_edges(conn, mentioned)
    return count


def _derive_comention_edges(conn, names: list[str]) -> int:
    """Add/strengthen an undirected co-mention edge between every pair of people named
    together. Repeated co-mentions accumulate weight, so the relationship graph reflects
    how tightly entities cluster in the user's actual conversations."""
    uniq = sorted({n for n in names if n})
    edges = 0
    for i in range(len(uniq)):
        for j in range(i + 1, len(uniq)):
            a = graph.upsert_node(conn, "person", _person_ref(conn, uniq[i]), uniq[i])
            b = graph.upsert_node(conn, "person", _person_ref(conn, uniq[j]), uniq[j])
            existing = conn.execute(
                "SELECT id, weight FROM knowledge_edges WHERE from_node_id = ? AND to_node_id = ? AND relation = 'co_mentioned'",
                (a, b),
            ).fetchone()
            if existing:
                conn.execute("UPDATE knowledge_edges SET weight = weight + 1.0 WHERE id = ?", (existing["id"],))
            else:
                conn.execute(
                    "INSERT INTO knowledge_edges (from_node_id, to_node_id, relation, weight, source) "
                    "VALUES (?, ?, 'co_mentioned', 1.0, 'context')",
                    (a, b),
                )
            edges += 1
    return edges


def _person_ref(conn, name: str) -> int:
    """ref_id for a person node: the people.id if known, else a stable hash so unknown
    names still get a consistent node (negative to avoid colliding with real ids)."""
    row = conn.execute("SELECT id FROM people WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return int(row["id"])
    return -(abs(hash(name)) % 1_000_000_000)


def reflect(conn, *, lookback: int = 200, min_cluster: int = 2) -> dict:
    """Distill recent observations into higher-level insights, one per recurring subject.

    Clusters the most recent active observations by subject; any subject seen
    ``min_cluster``+ times gets a refreshed insight (the prior one is superseded, so the
    insight table is versioned, not duplicated). Mirrors the Generative-Agents reflection
    step: episodic observations → semantic insight.
    """
    rows = conn.execute(
        "SELECT id, kind, subject, detail FROM context_observations "
        "WHERE status = 'active' AND subject IS NOT NULL AND subject != '' "
        "ORDER BY created_at DESC, id DESC LIMIT ?",  # id tiebreak: deterministic window (L3)
        (lookback,),
    ).fetchall()
    clusters: dict[str, list] = {}
    for r in rows:
        clusters.setdefault(r["subject"], []).append(r)

    insights = 0
    for subject, obs in clusters.items():
        if len(obs) < min_cluster:
            continue
        kinds = sorted({o["kind"] for o in obs})
        summary = f"Recurring context around {subject} ({len(obs)} observations: {', '.join(kinds)})."
        evidence = json.dumps({"subject": subject, "observation_ids": [o["id"] for o in obs[:20]]}, ensure_ascii=True)
        conn.execute(
            "INSERT INTO context_insights (kind, subject, summary, evidence_json, confidence) "
            "VALUES ('reflection', ?, ?, ?, ?)",
            (subject, summary, evidence, min(0.5 + 0.1 * len(obs), 0.95)),
        )
        new_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        # Supersede EVERY prior open reflection for this exact subject (equality, not LIKE —
        # so a name containing %/_ can't collide with another subject's insight; review M3).
        conn.execute(
            "UPDATE context_insights SET superseded_by = ? "
            "WHERE kind = 'reflection' AND subject = ? AND superseded_by IS NULL AND id != ?",
            (new_id, subject, new_id),
        )
        insights += 1

    # Expire open reflections whose subject no longer has enough active support — otherwise
    # an insight lingers forever after its observations decay/merge below min_cluster
    # (review #4). superseded_by = own id marks "closed, no successor" without a new column.
    expired = 0
    for r in conn.execute(
        "SELECT id, subject FROM context_insights WHERE kind = 'reflection' AND superseded_by IS NULL"
    ).fetchall():
        still = conn.execute(
            "SELECT COUNT(*) FROM context_observations WHERE status = 'active' AND subject = ?",
            (r["subject"],),
        ).fetchone()[0]
        if still < min_cluster:
            conn.execute("UPDATE context_insights SET superseded_by = id WHERE id = ?", (r["id"],))
            expired += 1

    suggestions = generate_suggestions(conn)
    conn.commit()
    return {"insights": insights, "subjects": len(clusters), "suggestions": suggestions, "expired": expired}


def generate_suggestions(conn, *, limit_each: int = 5) -> int:
    """Turn strong observed patterns into *proposed* improvement suggestions (gated).

    Deliberately conservative — only high-signal patterns, deduped by title — so the
    ledger stays a short list the user actually triages rather than noise. Nothing here
    executes; each suggestion waits for :func:`decide_suggestion`.
    """
    made = 0
    # 1) A person with a recurring conversational risk that isn't tracked as work.
    risky = conn.execute(
        "SELECT subject, COUNT(*) AS n FROM context_observations "
        "WHERE status='active' AND kind='risk' AND subject IS NOT NULL AND subject != '' "
        "GROUP BY subject ORDER BY n DESC LIMIT ?",
        (limit_each,),
    ).fetchall()
    for r in risky:
        if propose_suggestion(
            conn,
            title=f"Track an open risk for {r['subject']} as a work item",
            rationale=f"{r['n']} risk observation(s) surfaced in conversation but not tracked as work.",
            suggested_action="create_inbox_item",
        ):
            made += 1
    # 2) Durable preferences stated in conversation — capture so MYOS applies them consistently.
    prefs = conn.execute(
        "SELECT detail FROM context_observations WHERE status='active' AND kind='preference' "
        "ORDER BY created_at DESC LIMIT ?",
        (limit_each,),
    ).fetchall()
    for p in prefs:
        # Use the full detail in the title (propose_suggestion caps at 500) so two distinct
        # preferences sharing a short prefix don't collapse into one suggestion (review L2).
        if propose_suggestion(
            conn,
            title=f"Adopt standing preference: {p['detail']}",
            rationale="You stated this as a durable preference; capturing it lets MYOS apply it consistently.",
        ):
            made += 1
    return made


# --------------------------------------------------------------------------------------
# Slice 3 — improvement-suggestion ledger (gated) + scored retrieval
# --------------------------------------------------------------------------------------
def propose_suggestion(
    conn, *, title: str, rationale: str = "", suggested_action: str = "", insight_id: int | None = None
) -> int | None:
    """Record an improvement suggestion (status='proposed'). Deduped by title among
    still-open suggestions. Returns the id, or None if an identical open one exists.

    Suggestions are advisory only — nothing here executes. The user accepts/dismisses via
    :func:`decide_suggestion`; that decision is the gate (Rex/BerriAI propose-then-approve).
    """
    title = (title or "").strip()
    if not title:
        return None
    # Dedup across ALL statuses, not just open ones (review L2): once the user has
    # decided a suggestion (e.g. dismissed it), re-proposing the same title every reflect
    # cycle would re-nag them. A decided title stays suppressed.
    dup = conn.execute("SELECT id FROM context_suggestions WHERE title = ? LIMIT 1", (title[:500],)).fetchone()
    if dup:
        return None
    conn.execute(
        "INSERT INTO context_suggestions (insight_id, title, rationale, suggested_action, status) "
        "VALUES (?, ?, ?, ?, 'proposed')",
        (insight_id, title[:500], rationale[:2000], suggested_action[:1000]),
    )
    sid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    append_event(conn, "suggestion_proposed", "context_suggestion", sid, json.dumps({"title": title[:200]}))
    return sid


def list_suggestions(conn, *, status: str = "proposed", limit: int = 20) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, insight_id, title, rationale, suggested_action, status, created_at, decided_at, feedback "
            "FROM context_suggestions WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    ]


def decide_suggestion(conn, suggestion_id: int, decision: str, *, feedback: str = "") -> dict:
    """Accept / dismiss / mark-applied a suggestion. The decision is logged back as a
    'feedback' observation so the loop learns from what the user values (Reflexion)."""
    decision = decision.strip().lower()
    if decision not in ("accepted", "dismissed", "applied"):
        return {"error": f"decision must be accepted|dismissed|applied, got {decision!r}"}
    row = conn.execute("SELECT title, status FROM context_suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    if not row:
        return {"error": f"suggestion #{suggestion_id} not found"}
    # Gate the lifecycle one-way (review L1): a decision is only valid from 'proposed',
    # plus the natural accepted->applied follow-through. This stops a re-decision from
    # overwriting decided_at and double-counting the Reflexion feedback signal.
    current = row["status"]
    allowed = current == "proposed" or (current == "accepted" and decision == "applied")
    if not allowed:
        return {"error": f"suggestion #{suggestion_id} already {current}; cannot mark {decision}"}
    # Redact before persisting — feedback is free-text CLI input that can contain secrets/
    # phones/emails; it fans out to context_suggestions, an observation, and the event log
    # (review R4-4).
    feedback = apply_privacy_filters(conn, (feedback or "").strip())
    conn.execute(
        "UPDATE context_suggestions SET status = ?, decided_at = CURRENT_TIMESTAMP, feedback = ? WHERE id = ?",
        (decision, feedback[:2000], suggestion_id),
    )
    _insert_observation(
        conn,
        None,
        "feedback",
        None,
        f"User {decision} suggestion: {row['title']}" + (f" — {feedback}" if feedback else ""),
    )
    append_event(
        conn, f"suggestion_{decision}", "context_suggestion", suggestion_id, json.dumps({"feedback": feedback[:200]})
    )
    conn.commit()
    return {"id": suggestion_id, "status": decision}


def scored_retrieve(
    conn, query: str, *, limit: int = 5, half_life_days: float = 14.0, candidates: int = 400
) -> list[dict]:
    """Rank observations by the Generative-Agents memory score:
    ``relevance + recency + importance`` (each in [0,1]).

    * relevance — lexical+semantic hybrid against the observation detail
    * recency   — exponential decay by age (``0.5 ** (age_days / half_life_days)``)
    * importance — the stored per-observation importance

    Touching an observation bumps its ``last_accessed_at``/``access_count`` so frequently
    useful memories resist hygiene decay (reinforcement).
    """
    rows = conn.execute(
        "SELECT id, kind, subject, detail, importance, "
        "       (julianday('now') - julianday(created_at)) AS age_days "
        "FROM context_observations WHERE status = 'active' "
        "ORDER BY created_at DESC, id DESC LIMIT ?",  # id tiebreak: stable candidate window (L3)
        (candidates,),
    ).fetchall()
    scored = []
    for r in rows:
        relevance = hybrid_score(query, r["detail"])
        if relevance <= 0:
            continue
        age = max(float(r["age_days"] or 0.0), 0.0)
        recency = 0.5 ** (age / max(half_life_days, 0.1))
        importance = float(r["importance"] or 0.5)
        score = relevance + recency + importance
        scored.append(
            {
                "id": int(r["id"]),
                "score": round(score, 4),
                "relevance": round(relevance, 4),
                "recency": round(recency, 4),
                "importance": round(importance, 4),
                "kind": r["kind"],
                "subject": r["subject"],
                "detail": r["detail"],
            }
        )
    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:limit]
    if top:
        ids = [s["id"] for s in top]
        conn.execute(
            f"UPDATE context_observations SET last_accessed_at = CURRENT_TIMESTAMP, "
            f"access_count = access_count + 1 WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        )
        conn.commit()
    return top


# --------------------------------------------------------------------------------------
# Slice 4 — memory hygiene (non-destructive: status flips only, never DELETE)
# --------------------------------------------------------------------------------------
def hygiene(conn, *, decay_days: float = 60.0, importance_floor: float = 0.4, purge_days: float | None = None) -> dict:
    """Keep the observation stream healthy. Default mode never deletes:

    * dedup — collapse exact (kind, subject, detail) duplicates, keeping the earliest
      and flipping the rest to status='merged'.
    * decay — flip stale, low-importance, never-recalled observations to 'decayed' so
      they drop out of reflection/retrieval but remain auditable.

    Opt-in ``purge_days`` (review L4) hard-DELETEs already merged/decayed observations
    older than that many days, so a privacy-conscious user can expunge derived content
    that would otherwise stay queryable in cleartext forever. Off by default (None).
    """
    merged = conn.execute(
        """
        UPDATE context_observations SET status = 'merged'
        WHERE status = 'active' AND id NOT IN (
            SELECT MIN(id) FROM context_observations WHERE status = 'active'
            GROUP BY kind, IFNULL(subject,''), detail
        )
        """
    ).rowcount
    decayed = conn.execute(
        "UPDATE context_observations SET status = 'decayed' "
        "WHERE status = 'active' AND access_count = 0 AND importance < ? "
        "AND julianday('now') - julianday(created_at) > ?",
        (importance_floor, decay_days),
    ).rowcount
    purged = 0
    if purge_days is not None:
        purged = conn.execute(
            "DELETE FROM context_observations WHERE status IN ('merged', 'decayed') "
            "AND julianday('now') - julianday(created_at) > ?",
            (purge_days,),
        ).rowcount
    conn.commit()
    return {"merged": merged, "decayed": decayed, "purged": purged}


# --------------------------------------------------------------------------------------
# Read helpers for the CLI
# --------------------------------------------------------------------------------------
def relationships(conn, *, limit: int = 20) -> list[dict]:
    """Strongest derived relationships (co-mention edges), heaviest first."""
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT fn.label AS a, tn.label AS b, e.relation, e.weight
            FROM knowledge_edges e
            JOIN knowledge_nodes fn ON fn.id = e.from_node_id
            JOIN knowledge_nodes tn ON tn.id = e.to_node_id
            WHERE e.source = 'context'
            ORDER BY e.weight DESC, e.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    ]


def summary(conn) -> dict:
    """One-glance health of the loop for `myos context`."""

    def _count(sql, params=()):
        return int(conn.execute(sql, params).fetchone()[0])

    return {
        "conversations": _count("SELECT COUNT(*) FROM conversations"),
        "turns": _count("SELECT COUNT(*) FROM conversation_turns"),
        "observations_active": _count("SELECT COUNT(*) FROM context_observations WHERE status='active'"),
        "insights": _count("SELECT COUNT(*) FROM context_insights WHERE superseded_by IS NULL"),
        "suggestions_open": _count("SELECT COUNT(*) FROM context_suggestions WHERE status='proposed'"),
        "relationships": _count("SELECT COUNT(*) FROM knowledge_edges WHERE source='context'"),
    }

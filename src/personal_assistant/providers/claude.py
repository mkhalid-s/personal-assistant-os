"""Claude backend: in-process Anthropic SDK + a manual tool-use loop.

This is the reference brain. It exposes:

* :meth:`reason` -- one structured-output call returning ``{reply, plan, actions}``,
  used by ``cli._ai_reason_artifacts`` to upgrade ``delegate``/``autopilot``.
* :meth:`run_turn` -- a streaming agentic loop for the chat/voice REPL. Read tools
  auto-run; ``capture_item`` is a safe local write; every ``propose_*`` tool only
  *enqueues* into the approval queue. The model has no tool that posts externally,
  so propose-and-approve is structural, not advisory.

Model defaults to ``claude-opus-4-8`` with adaptive thinking + high effort.
Prompt caching is applied to the conversation prefix (top-level ``cache_control``);
it only kicks in once the prefix exceeds the model's minimum cacheable size, so it
helps longer/continuing turns, not the first short one.
"""

from __future__ import annotations

import json
import os

from .. import agentcore, em, queries, watch
from . import BaseBackend

SYSTEM_PROMPT = """You are MYOS, an always-on personal chief-of-staff for a Staff/Senior \
software engineer. You run locally in their terminal and over voice.

How you work:
- Use the read tools (get_brief, list_at_risk, list_waiting_on, query_context, get_today, \
risk_radar, why_item, metrics) freely to ground every answer in their real data. Do not \
guess at state you can look up.
- capture_item silently records a note/task to their inbox -- safe, no approval needed.
- For ANY change to an external system (Jira/GitHub/Slack), you may only PROPOSE it via a \
propose_* tool. Proposing enqueues a draft for one-tap human approval; it does NOT send \
anything. Never claim you have posted, commented, or notified -- say you've drafted/proposed \
it and it is awaiting approval.
- Be concise and direct; lead with the answer. You are talking to a busy senior engineer. \
Default to a few sentences; expand only when asked. Avoid filler.
"""

_READ_TOOLS = {
    "get_brief",
    "list_at_risk",
    "list_waiting_on",
    "query_context",
    "get_today",
    "risk_radar",
    "why_item",
    "metrics",
    "recall",
    "list_team",
    "person_dossier",
    "draft_review",
    "scan_risks",
}

# Local-write tools that auto-run (safe local bookkeeping — no external mutation).
_EM_WRITE_TOOLS = {"upsert_person", "log_evidence", "log_one_on_one", "record_competency", "capture_meeting"}

TOOLS = [
    {
        "name": "get_brief",
        "description": "Executive daily brief: inbox/open/at-risk counts and top outcomes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "meeting_hours": {"type": "number"},
                "risk_threshold": {"type": "integer"},
                "top": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "list_at_risk",
        "description": "Open work items at or above a risk threshold.",
        "input_schema": {
            "type": "object",
            "properties": {"threshold": {"type": "integer"}, "limit": {"type": "integer"}},
            "required": [],
        },
    },
    {
        "name": "list_waiting_on",
        "description": "Open items blocked on / owned by someone else.",
        "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": []},
    },
    {
        "name": "query_context",
        "description": "Search indexed notes/transcripts/external items for relevant context.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_today",
        "description": "Today's focus list of open work items.",
        "input_schema": {"type": "object", "properties": {"meeting_hours": {"type": "number"}}, "required": []},
    },
    {
        "name": "risk_radar",
        "description": "Open work items ranked by risk score.",
        "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": []},
    },
    {
        "name": "why_item",
        "description": "Explain why a work item exists (provenance/source).",
        "input_schema": {"type": "object", "properties": {"item_id": {"type": "integer"}}, "required": ["item_id"]},
    },
    {
        "name": "metrics",
        "description": "KPI snapshot over the last N days.",
        "input_schema": {"type": "object", "properties": {"days": {"type": "integer"}}, "required": []},
    },
    {
        "name": "capture_item",
        "description": "Safe local capture of a note/task/commitment to the inbox. No approval needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "kind": {"type": "string"},
                "owner": {"type": "string"},
                "due_date": {"type": "string"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "remember",
        "description": "Persist a durable fact to long-term memory (people, projects, decisions, preferences) so you can recall it in future sessions. Safe, no approval needed.",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
    {
        "name": "recall",
        "description": "Search long-term memory and all indexed notes/transcripts/external items for relevant facts.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    },
    # --- Engineering-manager tools (local, safe — auto-run) ---
    {
        "name": "upsert_person",
        "description": "Create or update a team member / stakeholder record.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "role": {"type": "string"},
                "team": {"type": "string"},
                "relation": {"type": "string", "enum": ["report", "peer", "stakeholder", "manager"]},
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_team",
        "description": "List known people (reports, peers, stakeholders).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "log_evidence",
        "description": "Record a piece of performance evidence about a person. Infer the category (leadership/delivery/technical/communication/collaboration/growth/ownership) from what happened.",
        "input_schema": {
            "type": "object",
            "properties": {
                "person": {"type": "string"},
                "category": {"type": "string"},
                "impact": {"type": "string"},
                "artifact_link": {"type": "string"},
            },
            "required": ["person", "category", "impact"],
        },
    },
    {
        "name": "log_one_on_one",
        "description": "Record a 1:1 with a person. Pass the raw notes; optionally a summary, sentiment (positive/neutral/concern), and action items.",
        "input_schema": {
            "type": "object",
            "properties": {
                "person": {"type": "string"},
                "raw_text": {"type": "string"},
                "summary": {"type": "string"},
                "sentiment": {"type": "string"},
                "action_items": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["person", "raw_text"],
        },
    },
    {
        "name": "record_competency",
        "description": "Record a competency assessment for a person (e.g. technical: meets).",
        "input_schema": {
            "type": "object",
            "properties": {
                "person": {"type": "string"},
                "competency": {"type": "string"},
                "level": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["person", "competency"],
        },
    },
    {
        "name": "person_dossier",
        "description": "Get everything tracked about a person: evidence, 1:1s, competencies, open commitments.",
        "input_schema": {"type": "object", "properties": {"person": {"type": "string"}}, "required": ["person"]},
    },
    {
        "name": "draft_review",
        "description": "Assemble a performance-review packet for a person from accumulated evidence/1:1s/competencies. Use its output to write a polished narrative.",
        "input_schema": {"type": "object", "properties": {"person": {"type": "string"}}, "required": ["person"]},
    },
    {
        "name": "capture_meeting",
        "description": "Record a meeting from notes/transcript and extract decisions + action items (with owners) into tracked commitments.",
        "input_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "raw_text": {"type": "string"}},
            "required": ["title", "raw_text"],
        },
    },
    {
        "name": "scan_risks",
        "description": "Scan synced Jira/GitHub items and work items for things needing attention (overdue, at-risk, PRs awaiting review, high-priority/blocked). Returns findings you can turn into nudges via propose_* tools.",
        "input_schema": {
            "type": "object",
            "properties": {"risk_threshold": {"type": "integer"}, "limit": {"type": "integer"}},
            "required": [],
        },
    },
    {
        "name": "propose_jira_comment",
        "description": "Draft a Jira comment for approval (does NOT post).",
        "input_schema": {
            "type": "object",
            "properties": {"issue_key": {"type": "string"}, "body": {"type": "string"}},
            "required": ["issue_key", "body"],
        },
    },
    {
        "name": "propose_github_comment",
        "description": "Draft a GitHub issue/PR comment for approval (does NOT post).",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer"},
                "body": {"type": "string"},
                "owner": {"type": "string"},
                "repo": {"type": "string"},
            },
            "required": ["issue_number", "body"],
        },
    },
    {
        "name": "propose_slack_message",
        "description": "Draft a Slack message for approval (does NOT send).",
        "input_schema": {
            "type": "object",
            "properties": {"channel": {"type": "string"}, "text": {"type": "string"}},
            "required": ["channel", "text"],
        },
    },
    {
        "name": "propose_external_update",
        "description": "Draft a generic external update/notification for approval.",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}, "draft": {"type": "string"}, "target": {"type": "string"}},
            "required": ["draft"],
        },
    },
]


class ClaudeBackend(BaseBackend):
    name = "claude"

    def _client_and_model(self):
        import anthropic

        backend = os.getenv("MYOS_LLM_BACKEND", "").strip().lower()
        model = os.getenv("MYOS_CLAUDE_MODEL", "").strip() or "claude-opus-4-8"
        if backend == "bedrock":
            client = anthropic.AnthropicBedrockMantle(aws_region=os.getenv("AWS_REGION") or "us-east-1")
            # Only add the provider prefix when the id has none — a region-qualified
            # id like "us.anthropic.claude-..." must not become "anthropic.us.anthropic..." (#24).
            if "anthropic." not in model:
                model = f"anthropic.{model}"
        elif backend == "aws":
            # Claude Platform on AWS requires both, with no default (#9) — fail clearly here
            # rather than constructing a client that throws on first use.
            if not os.getenv("AWS_REGION", "").strip() or not os.getenv("ANTHROPIC_AWS_WORKSPACE_ID", "").strip():
                raise RuntimeError("Claude Platform on AWS needs AWS_REGION and ANTHROPIC_AWS_WORKSPACE_ID set")
            client = anthropic.AnthropicAWS()
        else:
            client = anthropic.Anthropic()
        return client, model

    def available(self) -> tuple[bool, str]:
        try:
            import anthropic  # noqa: F401
        except Exception as exc:  # pragma: no cover - import guard
            return False, f"anthropic SDK not installed ({exc})"
        backend = os.getenv("MYOS_LLM_BACKEND", "").strip().lower()
        if backend == "bedrock":
            if not os.getenv("AWS_REGION", "").strip():
                return False, "bedrock backend needs AWS_REGION"
            return True, "bedrock transport (AWS credentials)"
        if backend == "aws":
            missing = [v for v in ("AWS_REGION", "ANTHROPIC_AWS_WORKSPACE_ID") if not os.getenv(v, "").strip()]
            if missing:
                return False, f"Claude Platform on AWS needs {', '.join(missing)}"
            return True, "Claude Platform on AWS"
        if os.getenv("ANTHROPIC_API_KEY", "").strip():
            return True, "ANTHROPIC_API_KEY set"
        return False, "ANTHROPIC_API_KEY not set (or set MYOS_LLM_BACKEND=bedrock|aws)"

    @staticmethod
    def _system_blocks() -> list[dict]:
        # Plain system block: it's far below the cacheable minimum so a cache_control
        # marker here never fires (finding #10). Real caching is the top-level
        # cache_control on the stream call, which caches the growing conversation.
        return [{"type": "text", "text": SYSTEM_PROMPT}]

    # -- conversational REPL turn -------------------------------------------------
    def run_turn(self, conn, user_text: str, history: list[dict], on_text=None) -> dict:
        client, model = self._client_and_model()
        messages = list(history) + [{"role": "user", "content": user_text}]
        ctx = {"task_id": None, "ids": []}
        reply_parts: list[str] = []
        stream_kwargs = dict(
            model=model,
            max_tokens=16000,
            system=self._system_blocks(),
            tools=TOOLS,
            thinking={"type": "adaptive", "display": "summarized"},  # #24: avoid empty-thinking pause
            output_config={"effort": "high"},
        )
        # Automatic (top-level) cache_control isn't supported on Bedrock (review B3) —
        # it would 400 or silently no-op there; only enable it off-Bedrock.
        if os.getenv("MYOS_LLM_BACKEND", "").strip().lower() != "bedrock":
            stream_kwargs["cache_control"] = {"type": "ephemeral"}  # #10: cache the growing prefix

        for _ in range(12):  # hard cap on tool-loop iterations
            with client.messages.stream(messages=messages, **stream_kwargs) as stream:
                if on_text is not None:
                    for event in stream:
                        if event.type == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
                            on_text(event.delta.text)
                response = stream.get_final_message()

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "tool_use":
                # Capture any assistant text emitted alongside the tool call (#23):
                # in the non-streaming path it would otherwise be lost.
                if on_text is None:
                    for block in response.content:
                        if block.type == "text" and block.text:
                            reply_parts.append(block.text)
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        out, is_error = self._dispatch(conn, block.name, dict(block.input or {}), ctx)
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": out,
                                **({"is_error": True} if is_error else {}),
                            }
                        )
                messages.append({"role": "user", "content": tool_results})
                continue

            if response.stop_reason == "refusal":
                reply_parts.append("I can't help with that request.")
            else:
                for block in response.content:
                    if block.type == "text":
                        reply_parts.append(block.text)
                if response.stop_reason == "max_tokens":  # #24: don't present truncation as complete
                    reply_parts.append("\n[reply truncated at the token limit — ask me to continue]")
            break

        conn.commit()
        reply = "\n".join(p for p in reply_parts if p).strip()
        return {"reply": reply, "proposed_action_ids": ctx["ids"], "history": messages, "backend": "claude"}

    def _dispatch(self, conn, name: str, args: dict, ctx: dict) -> tuple[str, bool]:
        try:
            if name in _READ_TOOLS:
                return json.dumps(self._read(conn, name, args), default=str), False
            if name == "capture_item":
                inbox_id, created = agentcore.capture_item(
                    conn,
                    text=str(args.get("text", "")),
                    kind=str(args.get("kind", "task")),
                    owner=args.get("owner"),
                    due_date=args.get("due_date"),
                )
                conn.commit()
                return (f"captured inbox item #{inbox_id}" if created else "duplicate; already captured"), False
            if name == "remember":
                chunk_id = agentcore.remember(conn, str(args.get("text", "")))
                conn.commit()
                return (f"remembered (memory #{chunk_id})" if chunk_id else "nothing to remember"), False
            if name in _EM_WRITE_TOOLS:
                out = self._em_write(conn, name, args)
                conn.commit()
                return out, False
            if name.startswith("propose_"):
                return self._propose(conn, name, args, ctx), False
            return f"unknown tool: {name}", True
        except Exception as exc:  # noqa: BLE001 - surface to the model, don't crash
            return f"tool error: {exc}", True

    @staticmethod
    def _read(conn, name: str, args: dict):
        if name == "get_brief":
            return queries.brief(
                conn,
                float(args.get("meeting_hours", 0.0)),
                int(args.get("top", 5)),
                int(args.get("risk_threshold", 60)),
            )
        if name == "list_at_risk":
            return queries.at_risk(conn, int(args.get("threshold", 50)), int(args.get("limit", 10)))
        if name == "list_waiting_on":
            return queries.waiting_on(conn, int(args.get("limit", 10)))
        if name in ("query_context", "recall"):
            return queries.context_search(conn, str(args.get("query", "")), int(args.get("limit", 5)))
        if name == "get_today":
            return queries.today(conn, float(args.get("meeting_hours", 0.0)))
        if name == "risk_radar":
            return queries.risk_radar(conn, int(args.get("limit", 10)))
        if name == "why_item":
            return queries.why(conn, int(args.get("item_id", 0)))
        if name == "metrics":
            return queries.metrics(conn, int(args.get("days", 7)))
        if name == "list_team":
            return em.list_team(conn)
        if name == "person_dossier":
            return em.person_dossier(conn, str(args.get("person", "")))
        if name == "draft_review":
            return {"packet_markdown": em.build_review_packet(conn, str(args.get("person", "")))}
        if name == "scan_risks":
            return watch.scan_project_risks(
                conn, risk_threshold=int(args.get("risk_threshold", 60)), limit=int(args.get("limit", 25))
            )
        return {"error": f"unknown read tool {name}"}

    @staticmethod
    def _em_write(conn, name: str, args: dict) -> str:
        if name == "upsert_person":
            pid = em.upsert_person(
                conn,
                str(args.get("name", "")),
                role=args.get("role"),
                team=args.get("team"),
                relation=args.get("relation", "report"),
            )
            return f"saved person #{pid} ({args.get('name')})"
        # Free-text redaction for EM writes now lives in em.py (the single chokepoint that
        # also covers the equivalent `myos note/1on1/meeting` CLI commands and the model-
        # supplied action_items list) — findings #2/#7. No wrapping needed here.
        if name == "log_evidence":
            eid = em.record_evidence(
                conn,
                str(args.get("person", "")),
                str(args.get("category", "general")),
                str(args.get("impact", "")),
                artifact_link=args.get("artifact_link"),
            )
            return f"logged evidence #{eid} for {args.get('person')} [{args.get('category')}]"
        if name == "log_one_on_one":
            res = em.log_one_on_one(
                conn,
                str(args.get("person", "")),
                str(args.get("raw_text", "")),
                summary=args.get("summary"),
                sentiment=args.get("sentiment"),
                action_items=args.get("action_items"),
            )
            return f"logged 1:1 #{res['one_on_one_id']} with {len(res['action_item_ids'])} action item(s)"
        if name == "record_competency":
            cid = em.record_competency(
                conn,
                str(args.get("person", "")),
                str(args.get("competency", "")),
                level=args.get("level"),
                notes=args.get("notes"),
            )
            return f"recorded competency #{cid}"
        if name == "capture_meeting":
            res = em.capture_meeting(conn, str(args.get("title", "Meeting")), str(args.get("raw_text", "")))
            return f"captured meeting #{res['meeting_id']} with {res['action_items']} action item(s)"
        return f"unknown EM tool {name}"

    def _propose(self, conn, name: str, args: dict, ctx: dict) -> str:
        if ctx["task_id"] is None:
            ctx["task_id"] = agentcore.ensure_turn_task(conn, "assistant chat proposals")
        if name == "propose_jira_comment":
            title = f"Jira comment on {args.get('issue_key', '?')}"
            payload = {"target": "jira", "issue_key": args.get("issue_key", ""), "draft": args.get("body", "")}
        elif name == "propose_github_comment":
            title = f"GitHub comment on #{args.get('issue_number', '?')}"
            payload = {
                "target": "github",
                "issue_number": args.get("issue_number"),
                "owner": args.get("owner", ""),
                "repo": args.get("repo", ""),
                "draft": args.get("body", ""),
            }
        elif name == "propose_slack_message":
            title = f"Slack message to {args.get('channel', '?')}"
            payload = {"target": "slack", "channel": args.get("channel", ""), "draft": args.get("text", "")}
        else:  # propose_external_update
            title = str(args.get("summary") or "External update")
            payload = {
                "target": args.get("target", "outbox"),
                "summary": args.get("summary", ""),
                "draft": args.get("draft", ""),
            }
        action_id = agentcore.enqueue_proposal(
            conn,
            task_id=ctx["task_id"],
            action_type="draft_external_update",
            title=title,
            payload=payload,
            requires_approval=1,
        )
        conn.commit()
        ctx["ids"].append(action_id)
        return f"Proposed as action #{action_id} (pending approval). Nothing was sent."

    # -- one-shot structured reasoning (delegate/autopilot) ----------------------
    def reason(self, conn, request: dict) -> dict:
        client, model = self._client_and_model()
        objective = str(request.get("objective", ""))
        context = str(request.get("context", ""))
        analogies = request.get("analogies") or []
        prompt = (
            f"Objective: {objective}\n\nContext: {context}\n\n"
            + (
                "Relevant prior outcomes:\n" + "\n".join(f"- {a.get('content', a)}" for a in analogies[:5])
                if analogies
                else ""
            )
            + "\n\nProduce a short plan and concrete proposed actions. Use action_type "
            '"create_inbox_item" only for safe local notes (requires_approval 0); use '
            '"draft_external_update" for anything that touches Jira/GitHub/Slack '
            "(requires_approval 1)."
        )
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "reply": {"type": "string"},
                "plan": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"step": {"type": "string"}, "detail": {"type": "string"}},
                        "required": ["step", "detail"],
                    },
                },
                "actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "action_type": {"type": "string"},
                            "title": {"type": "string"},
                            # Structured outputs require additionalProperties:false everywhere and
                            # forbid free-form objects (review B1) — carry payload as a JSON string.
                            "payload": {"type": "string", "description": "JSON-encoded payload object"},
                            "requires_approval": {"type": "integer"},
                        },
                        "required": ["action_type", "title", "payload", "requires_approval"],
                    },
                },
            },
            "required": ["reply", "plan", "actions"],
        }
        resp = client.messages.create(
            model=model,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "{}")
        data = json.loads(text)
        actions = data.get("actions", [])
        for a in actions:  # decode the JSON-string payload back into a dict for downstream
            if isinstance(a.get("payload"), str):
                try:
                    a["payload"] = json.loads(a["payload"]) if a["payload"].strip() else {}
                except (ValueError, TypeError):
                    a["payload"] = {"draft": a["payload"]}
        return {"reply": data.get("reply", ""), "plan": data.get("plan", []), "actions": actions}

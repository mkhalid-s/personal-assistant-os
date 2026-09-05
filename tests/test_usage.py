"""Tests for the LLM usage ledger: prices, usage.record, budget gates, capture sites.

No network: Anthropic calls are faked through the same ``_client_and_model`` seam
the ClaudeLoopTest in test_assistant.py uses; Zero runs are constructed as
``ZeroRunResult`` values directly; router usage is exercised through
``record_route_event``.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from personal_assistant import autonomy_loop, prices, router, usage
from personal_assistant.db import initialize_schema


def _fresh_db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


def _set_budget(conn: sqlite3.Connection, key: str, value: int) -> None:
    conn.execute(
        "INSERT INTO assistant_policies (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
        (key, str(value)),
    )
    conn.commit()


class PricesTest(unittest.TestCase):
    def test_normalize_model_strips_provider_prefixes(self):
        self.assertEqual(prices.normalize_model("claude-opus-4-8"), "claude-opus-4-8")
        self.assertEqual(prices.normalize_model("anthropic.claude-opus-4-8"), "claude-opus-4-8")
        self.assertEqual(prices.normalize_model("us.anthropic.Claude-Opus-4-8"), "claude-opus-4-8")

    def test_rate_for_known_and_unknown_models(self):
        rates, version = prices.rate_for("claude-opus-4-8")
        self.assertTrue(rates)
        self.assertGreater(rates["output"], rates["input"])
        self.assertGreater(rates["input"], rates["cache_read"])
        self.assertGreater(rates["cache_write_5m"], rates["input"])
        self.assertTrue(version)
        unknown_rates, unknown_version = prices.rate_for("totally-unknown-model")
        self.assertIsNone(unknown_rates)
        self.assertEqual(unknown_version, version)  # version stamped even when price unknown

    def test_cost_math_separates_cache_categories(self):
        rates, _ = prices.rate_for("claude-sonnet-4-5")
        tokens = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_tokens": 500_000}
        cost = prices.compute_cost_millicents(rates, tokens)
        self.assertEqual(cost, 465_000)
        naive = prices.compute_cost_millicents(
            rates, {"input_tokens": 1_500_000, "output_tokens": 100_000}
        )
        self.assertEqual(naive, 600_000)
        self.assertLess(cost, naive)  # cache-aware math must price below naive input billing

    def test_cost_is_none_without_rates(self):
        self.assertIsNone(prices.compute_cost_millicents(None, {"input_tokens": 10}))

    def test_empty_tokens_cost_zero_when_priced(self):
        rates, _ = prices.rate_for("claude-haiku-4-5")
        self.assertEqual(prices.compute_cost_millicents(rates, {}), 0)


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.conn = _fresh_db_conn()
        self.addCleanup(self.conn.close)

    def _latest(self) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM llm_usage_events ORDER BY id DESC LIMIT 1").fetchone()

    def test_record_anthropic_shape_with_correlation(self):
        os.environ["MYOS_TRACE_CORRELATION_ID"] = "trace_test123"
        try:
            row_id = usage.record(
                self.conn,
                backend="claude",
                model="claude-opus-4-8",
                purpose="chat",
                usage={
                    "input_tokens": 120,
                    "output_tokens": 40,
                    "cache_creation_input_tokens": 10,
                    "cache_read_input_tokens": 900,
                    "requests": 3,
                },
                persona="chief-of-staff",
            )
        finally:
            os.environ.pop("MYOS_TRACE_CORRELATION_ID", None)
        self.assertIsNotNone(row_id)
        row = self.conn.execute("SELECT * FROM llm_usage_events WHERE id = ?", (row_id,)).fetchone()
        self.assertEqual(row["correlation_id"], "trace_test123")
        self.assertEqual(row["backend"], "claude")
        self.assertEqual(row["model"], "claude-opus-4-8")
        self.assertEqual(row["purpose"], "chat")
        self.assertEqual(row["input_tokens"], 120)
        self.assertEqual(row["output_tokens"], 40)
        self.assertEqual(row["cache_write_tokens"], 10)
        self.assertEqual(row["cache_read_tokens"], 900)
        self.assertEqual(row["requests"], 3)
        self.assertEqual(row["persona"], "chief-of-staff")
        self.assertEqual(row["estimated"], 0)
        self.assertIsNotNone(row["cost_millicents"])  # known model → priced
        self.assertTrue(row["price_version"])

    def test_record_zero_camelcase_and_self_reported_cost(self):
        usage.record(
            self.conn,
            backend="zero",
            model="claude-sonnet-4-5",
            purpose="execute",
            usage={"promptTokens": 500, "completionTokens": 100, "totalTokens": 600, "costUsd": 0.05},
        )
        row = self._latest()
        self.assertEqual(row["input_tokens"], 500)
        self.assertEqual(row["output_tokens"], 100)
        self.assertEqual(row["self_reported_cost_millicents"], 5000)
        self.assertIsNotNone(row["cost_millicents"])

    def test_record_unknown_model_keeps_tokens_with_null_cost(self):
        usage.record(
            self.conn,
            backend="command",
            model="mystery-model",
            purpose="chat",
            usage={"input_tokens": 10, "output_tokens": 5},
        )
        row = self._latest()
        self.assertIsNone(row["cost_millicents"])
        self.assertEqual(row["input_tokens"], 10)
        self.assertEqual(row["output_tokens"], 5)

    def test_record_estimated_flag(self):
        usage.record(
            self.conn,
            backend="local-tiny",
            model="router",
            purpose="route",
            usage={"input_tokens": 42},
            estimated=True,
        )
        self.assertEqual(self._latest()["estimated"], 1)

    def test_record_defaults_request_count_to_one(self):
        usage.record(self.conn, backend="claude", model="claude-opus-4-8", purpose="chat")
        self.assertEqual(self._latest()["requests"], 1)

    def test_record_never_raises_on_dead_connection(self):
        dead = sqlite3.connect(":memory:")
        dead.close()
        self.assertIsNone(
            usage.record(dead, backend="claude", model="claude-opus-4-8", purpose="chat", usage={"input_tokens": 1})
        )


class ClaudeUsageLedgerTest(unittest.TestCase):
    """Mirror of ClaudeLoopTest with usage-bearing responses (no network)."""

    def test_run_turn_sums_usage_across_tool_loop(self):
        from personal_assistant.providers.claude import ClaudeBackend

        conn = _fresh_db_conn()
        self.addCleanup(conn.close)

        class _Block:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class _Usage:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class _Resp:
            def __init__(self, content, stop_reason, usage=None):
                self.content = content
                self.stop_reason = stop_reason
                self.usage = usage

        class _Stream:
            def __init__(self, resp):
                self._resp = resp

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                return iter(())

            def get_final_message(self):
                return self._resp

        class _Messages:
            def __init__(self):
                self.calls = 0

            def stream(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return _Stream(
                        _Resp(
                            [
                                _Block(
                                    type="tool_use",
                                    id="t1",
                                    name="propose_jira_comment",
                                    input={"issue_key": "ABC-9", "body": "nudge"},
                                )
                            ],
                            "tool_use",
                            usage=_Usage(
                                input_tokens=100,
                                output_tokens=50,
                                cache_creation_input_tokens=0,
                                cache_read_input_tokens=200,
                            ),
                        )
                    )
                return _Stream(
                    _Resp(
                        [_Block(type="text", text="Drafted.")],
                        "end_turn",
                        usage=_Usage(
                            input_tokens=30,
                            output_tokens=10,
                            cache_creation_input_tokens=5,
                            cache_read_input_tokens=0,
                        ),
                    )
                )

        class _Client:
            def __init__(self):
                self.messages = _Messages()

        backend = ClaudeBackend()
        backend._client_and_model = lambda: (_Client(), "claude-opus-4-8")
        result = backend.run_turn(conn, "nudge the platform team", [])

        self.assertEqual(result["usage"]["requests"], 2)
        rows = conn.execute("SELECT * FROM llm_usage_events").fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["backend"], "claude")
        self.assertEqual(row["model"], "claude-opus-4-8")
        self.assertEqual(row["purpose"], "chat")
        self.assertEqual(row["requests"], 2)
        self.assertEqual(row["input_tokens"], 130)
        self.assertEqual(row["output_tokens"], 60)
        self.assertEqual(row["cache_write_tokens"], 5)
        self.assertEqual(row["cache_read_tokens"], 200)
        self.assertEqual(row["estimated"], 0)
        self.assertIsNotNone(row["cost_millicents"])


class AgentCliUsageTest(unittest.TestCase):
    def test_cli_backend_usage_passthrough_ledgers_row(self):
        from personal_assistant.providers.agent_cli import AgentCliBackend

        conn = _fresh_db_conn()
        self.addCleanup(conn.close)
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake_agent.sh"
            script.write_text(
                "#!/bin/sh\ncat >/dev/null\n"
                "echo '{\"reply\":\"ok\",\"plan\":[],\"actions\":[],\"model\":\"test-model\","
                "\"usage\":{\"input_tokens\":321,\"output_tokens\":11}}'\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            backend = AgentCliBackend(name="command", command=str(script), input_mode="prompt")
            result = backend.run_turn(conn, "do a thing", [])
        self.assertEqual(result["reply"], "ok")
        row = conn.execute("SELECT * FROM llm_usage_events ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["backend"], "command")
        self.assertEqual(row["model"], "test-model")
        self.assertEqual(row["input_tokens"], 321)
        self.assertEqual(row["output_tokens"], 11)


class ZeroRunLedgerTest(unittest.TestCase):
    def test_record_zero_agent_run_writes_usage_event(self):
        from personal_assistant.zero_executor import ZeroRunResult, record_zero_agent_run

        conn = _fresh_db_conn()
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO agent_tasks (objective, context, constraints_json, priority, status) "
            "VALUES ('zero task', '', '{}', 2, 'open')"
        )
        task_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        result = ZeroRunResult(
            status="success",
            exit_code=0,
            model="claude-sonnet-4-5",
            usage={"promptTokens": 800, "completionTokens": 200, "costUsd": 0.03},
        )
        run_id = record_zero_agent_run(conn, task_id=task_id, result=result)
        row = conn.execute("SELECT * FROM llm_usage_events WHERE agent_run_id = ?", (run_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["backend"], "zero")
        self.assertEqual(row["input_tokens"], 800)
        self.assertEqual(row["output_tokens"], 200)
        self.assertIsNotNone(row["cost_millicents"])
        self.assertEqual(row["self_reported_cost_millicents"], 3000)


class RouterUsageTest(unittest.TestCase):
    def test_record_route_event_ledgers_model_usage(self):
        conn = _fresh_db_conn()
        self.addCleanup(conn.close)
        decision = router.RouteDecision(
            intent="capture",
            confidence=0.9,
            reason="model route",
            recommended_workflow="",
            backend="model",
            model_usage={"input_tokens": 33, "requests": 1, "latency_ms": 5},
        )
        router.record_route_event(conn, "some text", surface="chat", decision=decision)
        row = conn.execute("SELECT * FROM llm_usage_events ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["backend"], "local-tiny")
        self.assertEqual(row["purpose"], "route")
        self.assertEqual(row["estimated"], 1)
        self.assertEqual(row["input_tokens"], 33)
        self.assertEqual(row["latency_ms"], 5)

    def test_model_usage_excluded_from_routing_contract(self):
        decision = router.RouteDecision(
            intent="capture",
            confidence=0.9,
            reason="r",
            recommended_workflow="",
            model_usage={"input_tokens": 1},
        )
        self.assertNotIn("model_usage", decision.to_dict())


class BudgetGateTest(unittest.TestCase):
    def setUp(self):
        self.conn = _fresh_db_conn()
        self.addCleanup(self.conn.close)

    def test_disabled_budget_allows_proposing(self):
        allowed, reason = usage.proposing_allowed(self.conn)
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_exceeded_daily_budget_blocks_proposing(self):
        _set_budget(self.conn, usage.DAILY_BUDGET_KEY, 100_000)
        usage.record(
            self.conn,
            backend="claude",
            model="claude-opus-4-8",
            purpose="chat",
            usage={"input_tokens": 1_000_000},  # $15 at placeholder opus input rates
        )
        status = usage.budget_status(self.conn)
        self.assertEqual(status["state"], "exceeded")
        allowed, reason = usage.proposing_allowed(self.conn)
        self.assertFalse(allowed)
        self.assertIn("daily", reason)

    def test_warn_state_does_not_gate(self):
        _set_budget(self.conn, usage.DAILY_BUDGET_KEY, 100_000)
        usage.record(
            self.conn,
            backend="claude",
            model="claude-haiku-4-5",
            purpose="chat",
            usage={"input_tokens": 850_000},  # $8.50 at placeholder haiku rates → 85% of budget
        )
        status = usage.budget_status(self.conn)
        self.assertEqual(status["state"], "warn")
        allowed, _ = usage.proposing_allowed(self.conn)
        self.assertTrue(allowed)

    def test_monthly_budget_trips_even_when_daily_clear(self):
        _set_budget(self.conn, usage.MONTHLY_BUDGET_KEY, 50_000)
        usage.record(
            self.conn,
            backend="claude",
            model="claude-opus-4-8",
            purpose="chat",
            usage={"input_tokens": 400_000},  # $6 at placeholder opus rates
        )
        status = usage.budget_status(self.conn)
        self.assertEqual(status["state"], "exceeded")

    def test_null_cost_rows_do_not_trip_budget(self):
        _set_budget(self.conn, usage.DAILY_BUDGET_KEY, 1)
        usage.record(
            self.conn,
            backend="command",
            model="mystery-model",
            purpose="chat",
            usage={"input_tokens": 999_999},
        )
        allowed, _ = usage.proposing_allowed(self.conn)
        self.assertTrue(allowed)  # unknown price cannot spend budget; tokens still visible in reports


class AutonomyLoopBudgetGateTest(unittest.TestCase):
    def test_reason_skips_backend_when_budget_exceeded(self):
        conn = _fresh_db_conn()
        self.addCleanup(conn.close)
        _set_budget(conn, usage.DAILY_BUDGET_KEY, 1)
        usage.record(conn, backend="claude", model="claude-opus-4-8", purpose="chat", usage={"input_tokens": 1000})
        calls: list[dict] = []

        class _FakeBackend:
            name = "fake"

            def available(self):
                return True, ""

            def reason(self, conn, request):
                calls.append(request)
                return {"reply": "hi", "plan": [{"step": "s", "detail": "d"}], "actions": []}

        original = autonomy_loop.providers.get_backend
        autonomy_loop.providers.get_backend = lambda name: _FakeBackend()
        try:
            plan, actions, provider, reply = autonomy_loop._reason(
                conn, objective="obj", context="ctx", backend_name="fake", purpose="autonomy_loop"
            )
        finally:
            autonomy_loop.providers.get_backend = original
        self.assertEqual(calls, [])  # backend never invoked under an exceeded budget
        self.assertEqual(provider, "local_loop")
        self.assertIn("budget", reply.lower())
        self.assertTrue(plan)  # local fallback still plans

    def test_reason_uses_backend_when_budget_clear(self):
        conn = _fresh_db_conn()
        self.addCleanup(conn.close)

        class _FakeBackend:
            name = "fake"

            def available(self):
                return True, ""

            def reason(self, conn, request):
                return {"reply": "hi", "plan": [{"step": "s", "detail": "d"}], "actions": []}

        original = autonomy_loop.providers.get_backend
        autonomy_loop.providers.get_backend = lambda name: _FakeBackend()
        try:
            _, _, provider, reply = autonomy_loop._reason(
                conn, objective="obj", context="ctx", backend_name="fake", purpose="autonomy_loop"
            )
        finally:
            autonomy_loop.providers.get_backend = original
        self.assertEqual(provider, "fake")
        self.assertNotIn("budget", reply.lower())


class CleanupTest(unittest.TestCase):
    def test_cleanup_respects_max_rows(self):
        conn = _fresh_db_conn()
        self.addCleanup(conn.close)
        for _ in range(5):
            usage.record(conn, backend="claude", model="claude-opus-4-8", purpose="chat")
        outcome = usage.cleanup_usage_events(conn, retention_days=365, max_rows=3)
        self.assertEqual(outcome["deleted"], 2)
        self.assertEqual(outcome["remaining"], 3)


if __name__ == "__main__":
    unittest.main()

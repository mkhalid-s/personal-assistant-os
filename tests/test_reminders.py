"""Tests for the reminders + scheduler + notify subsystem (slices S1-S4).

Covers:

- ``reminders.parse_when`` deterministic edge cases (past HH:MM rolls
  to tomorrow, ISO with/without tz, invalid input, +Nm / +Nh offsets).
- CRUD round-trip on ``reminders`` (create/list/mark_fired/mark_done/
  snooze/cancel) with privacy redaction on ``text``.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _fresh_db_conn():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)  # noqa: SIM115
    tmp.close()
    os.environ["MYOS_DB_PATH"] = tmp.name
    from personal_assistant.db import get_connection

    return get_connection(), tmp.name


def _to_utc_iso_test(dt: datetime) -> str:
    """Test helper that mirrors ``reminders._to_utc_iso`` without importing
    the private symbol, so ordering / equality assertions can compute the
    expected timestamp string with the same second-precision rounding."""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(UTC).isoformat(timespec="seconds")


class ParseWhenTest(unittest.TestCase):
    """Parser edge cases.

    ``HH:MM`` tests inject a *local-naive* ``now`` so the wall-clock
    day arithmetic is deterministic on any test-runner timezone —
    ``parse_when`` treats a naive ``now`` as local (matching the CLI
    user expectation that ``--at 15:00`` means "3pm on my clock").
    """

    def test_hhmm_future_stays_today(self) -> None:
        from personal_assistant.reminders import parse_when

        now_local = datetime(2026, 7, 8, 10, 0)  # naive → local 10am
        result = parse_when("15:00", now=now_local)
        parsed_local = datetime.fromisoformat(result).astimezone()
        # 15:00 local is later today; we should not have rolled to tomorrow.
        self.assertEqual(parsed_local.date(), now_local.date())
        self.assertEqual((parsed_local.hour, parsed_local.minute), (15, 0))

    def test_hhmm_past_rolls_to_tomorrow(self) -> None:
        from personal_assistant.reminders import parse_when

        now_local = datetime(2026, 7, 8, 18, 0)  # naive → local 6pm
        # ``05:00`` local has already passed today, so the parser must
        # roll to 2026-07-09.
        result = parse_when("05:00", now=now_local)
        parsed_local = datetime.fromisoformat(result).astimezone()
        self.assertEqual(parsed_local.date(), (now_local + timedelta(days=1)).date())
        self.assertEqual((parsed_local.hour, parsed_local.minute), (5, 0))

    def test_relative_offset_minutes(self) -> None:
        from personal_assistant.reminders import parse_when

        now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
        result = parse_when("+30m", now=now)
        parsed = datetime.fromisoformat(result)
        self.assertEqual(parsed, now + timedelta(minutes=30))

    def test_relative_offset_hours(self) -> None:
        from personal_assistant.reminders import parse_when

        now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
        result = parse_when("+2h", now=now)
        parsed = datetime.fromisoformat(result)
        self.assertEqual(parsed, now + timedelta(hours=2))

    def test_iso_with_tz_passthrough(self) -> None:
        from personal_assistant.reminders import parse_when

        raw = "2026-12-25T09:00:00+00:00"
        result = parse_when(raw)
        self.assertTrue(result.startswith("2026-12-25T09:00:00"))
        self.assertTrue(result.endswith("+00:00"))

    def test_iso_z_suffix_is_utc(self) -> None:
        from personal_assistant.reminders import parse_when

        result = parse_when("2026-12-25T09:00:00Z")
        self.assertEqual(result, "2026-12-25T09:00:00+00:00")

    def test_iso_naive_treated_as_local(self) -> None:
        from personal_assistant.reminders import parse_when

        # Naive ISO should be interpreted as local time and converted to
        # UTC on write. We don't assert an exact string because the local
        # tz is machine-dependent, but we assert the shape.
        result = parse_when("2026-12-25T09:00:00")
        self.assertTrue(result.endswith("+00:00"))
        self.assertIn("T", result)

    def test_invalid_raises(self) -> None:
        from personal_assistant.reminders import ReminderError, parse_when

        for bad in ("", "not-a-time", "25:00", "5pm tomorrow", "-30m", "+m", "+30x"):
            with self.assertRaises(ReminderError, msg=f"expected ReminderError for {bad!r}"):
                parse_when(bad)


class ParseDurationTest(unittest.TestCase):
    def test_valid_shapes(self) -> None:
        from personal_assistant.reminders import parse_duration

        self.assertEqual(parse_duration("30m"), timedelta(minutes=30))
        self.assertEqual(parse_duration("2h"), timedelta(hours=2))
        self.assertEqual(parse_duration("1h"), timedelta(hours=1))

    def test_invalid_raises(self) -> None:
        from personal_assistant.reminders import ReminderError, parse_duration

        for bad in ("", "30", "+30m", "2d", "abc"):
            with self.assertRaises(ReminderError):
                parse_duration(bad)


class ReminderCrudTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn, self.path = _fresh_db_conn()

    def tearDown(self) -> None:
        self.conn.close()
        os.unlink(self.path)

    def test_create_list_get_roundtrip(self) -> None:
        from personal_assistant import reminders

        rid = reminders.create(self.conn, "prep for standup", "+30m", kind="standup", source_ref="loop:1")
        self.assertGreater(rid, 0)
        row = reminders.get(self.conn, rid)
        self.assertIsNotNone(row)
        assert row is not None  # narrow for mypy
        self.assertEqual(row["text"], "prep for standup")
        self.assertEqual(row["kind"], "standup")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["source_ref"], "loop:1")
        pending = reminders.list_pending(self.conn)
        self.assertEqual([r["id"] for r in pending], [rid])
        self.assertEqual(reminders.count_pending(self.conn), 1)
        self.assertEqual(reminders.next_scheduled_at(self.conn), row["scheduled_at"])

    def test_privacy_redaction_on_text(self) -> None:
        from personal_assistant import reminders

        rid = reminders.create(self.conn, "ping alice@example.com about launch", "+15m")
        row = reminders.get(self.conn, rid)
        assert row is not None
        self.assertNotIn("alice@example.com", row["text"])
        self.assertIn("[REDACTED_EMAIL]", row["text"])

    def test_unknown_kind_rejected(self) -> None:
        from personal_assistant import reminders

        with self.assertRaises(reminders.ReminderError):
            reminders.create(self.conn, "test", "+5m", kind="bogus")

    def test_empty_text_rejected(self) -> None:
        from personal_assistant import reminders

        with self.assertRaises(reminders.ReminderError):
            reminders.create(self.conn, "   ", "+5m")

    def test_mark_fired_moves_to_fired_and_is_idempotent(self) -> None:
        from personal_assistant import reminders

        rid = reminders.create(self.conn, "task", "+1m")
        first = reminders.mark_fired(self.conn, rid)
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first["status"], "fired")
        self.assertIsNotNone(first["fired_at"])
        # Second call is a no-op (row not pending anymore).
        second = reminders.mark_fired(self.conn, rid)
        self.assertIsNone(second)

    def test_mark_done_from_pending_and_fired(self) -> None:
        from personal_assistant import reminders

        rid = reminders.create(self.conn, "task", "+1m")
        done = reminders.mark_done(self.conn, rid)
        assert done is not None
        self.assertEqual(done["status"], "done")

        rid2 = reminders.create(self.conn, "task2", "+1m")
        reminders.mark_fired(self.conn, rid2)
        done2 = reminders.mark_done(self.conn, rid2)
        assert done2 is not None
        self.assertEqual(done2["status"], "done")

    def test_snooze_moves_time_forward_and_keeps_pending(self) -> None:
        from personal_assistant import reminders

        # for_delta on a *pending future* reminder postpones from the
        # existing schedule (not from now), so the delta compounds on top
        # of the +5m already in the future — final result is +20m from now.
        now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
        rid = reminders.create(self.conn, "task", "+5m", now=now)
        original = reminders.get(self.conn, rid)
        assert original is not None
        snoozed = reminders.snooze(self.conn, rid, for_delta=timedelta(minutes=15), now=now)
        assert snoozed is not None
        self.assertEqual(snoozed["status"], "pending")
        self.assertGreater(snoozed["scheduled_at"], original["scheduled_at"])
        self.assertEqual(snoozed["snoozed_until"], original["scheduled_at"])
        # Confirm the postpone semantics: schedule is now +5m +15m = +20m from ``now``.
        expected = _to_utc_iso_test(now + timedelta(minutes=20))
        self.assertEqual(snoozed["scheduled_at"], expected)

    def test_snooze_of_fired_reminder_uses_now_as_base(self) -> None:
        from personal_assistant import reminders

        # A *fired* reminder should snooze relative to *now* (the "remind
        # me again in 15 min" mental model). Since fired_at was earlier,
        # max(now, scheduled) == now, so the new time is now + delta.
        now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
        later = now + timedelta(minutes=30)
        rid = reminders.create(self.conn, "task", "+5m", now=now)
        reminders.mark_fired(self.conn, rid, now=now + timedelta(minutes=6))
        snoozed = reminders.snooze(self.conn, rid, for_delta=timedelta(minutes=15), now=later)
        assert snoozed is not None
        expected = _to_utc_iso_test(later + timedelta(minutes=15))
        self.assertEqual(snoozed["scheduled_at"], expected)

    def test_snooze_requires_exactly_one_arg(self) -> None:
        from personal_assistant import reminders

        rid = reminders.create(self.conn, "task", "+5m")
        with self.assertRaises(reminders.ReminderError):
            reminders.snooze(self.conn, rid)
        with self.assertRaises(reminders.ReminderError):
            reminders.snooze(
                self.conn,
                rid,
                for_delta=timedelta(minutes=1),
                until=datetime(2026, 12, 25, tzinfo=UTC),
            )

    def test_cancel_moves_to_cancelled(self) -> None:
        from personal_assistant import reminders

        rid = reminders.create(self.conn, "task", "+5m")
        cancelled = reminders.cancel(self.conn, rid)
        assert cancelled is not None
        self.assertEqual(cancelled["status"], "cancelled")
        # Cancelled rows don't show up in list_pending.
        self.assertEqual(reminders.list_pending(self.conn), [])
        # Cancel is a terminal state — second call is a no-op.
        self.assertIsNone(reminders.cancel(self.conn, rid))

    def test_list_due_filters_by_time(self) -> None:
        from personal_assistant import reminders

        now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
        past_id = reminders.create(self.conn, "past", "+1m", now=now - timedelta(hours=1))
        future_id = reminders.create(self.conn, "future", "+1h", now=now)
        due = reminders.list_due(self.conn, now=now)
        self.assertEqual([r["id"] for r in due], [past_id])
        self.assertNotIn(future_id, [r["id"] for r in due])

    def test_event_log_rows_written(self) -> None:
        from personal_assistant import reminders

        rid = reminders.create(self.conn, "task", "+1m")
        reminders.mark_fired(self.conn, rid)
        events = [
            row["event_type"]
            for row in self.conn.execute(
                "SELECT event_type FROM event_log WHERE entity_type = 'reminder' AND entity_id = ? ORDER BY id",
                (rid,),
            ).fetchall()
        ]
        self.assertEqual(events, ["reminder_created", "reminder_fired"])

    def test_get_missing_returns_none(self) -> None:
        from personal_assistant import reminders

        self.assertIsNone(reminders.get(self.conn, 999_999))


class NotifyTest(unittest.TestCase):
    """Cover the three dispatch channels of the notification pipeline
    (custom hook, macOS osascript, terminal fallback) plus the audit
    guarantee (event_log + inbox_items on every dispatch, even when
    the channel fails). Mac and non-mac paths are exercised by
    toggling ``MYOS_NOTIFY_DISABLE_*`` env vars so the test suite is
    deterministic on any test-runner OS."""

    def setUp(self) -> None:
        self.conn, self.path = _fresh_db_conn()
        # Isolate every test from the developer's real notify config.
        self._env_backup: dict[str, str] = {}
        for key in (
            "MYOS_NOTIFY_COMMAND",
            "MYOS_NOTIFY_DISABLE_HOOK",
            "MYOS_NOTIFY_DISABLE_OSASCRIPT",
            "MYOS_NOTIFY_DISABLE_NOTIFY_SEND",
        ):
            if key in os.environ:
                self._env_backup[key] = os.environ.pop(key)
        # Force stdout fallback by default; individual tests opt in to the hook.
        os.environ["MYOS_NOTIFY_DISABLE_OSASCRIPT"] = "1"
        os.environ["MYOS_NOTIFY_DISABLE_NOTIFY_SEND"] = "1"

    def tearDown(self) -> None:
        self.conn.close()
        os.unlink(self.path)
        for key in (
            "MYOS_NOTIFY_COMMAND",
            "MYOS_NOTIFY_DISABLE_HOOK",
            "MYOS_NOTIFY_DISABLE_OSASCRIPT",
            "MYOS_NOTIFY_DISABLE_NOTIFY_SEND",
        ):
            os.environ.pop(key, None)
        for key, value in self._env_backup.items():
            os.environ[key] = value

    def _write_hook(self, tmpdir: Path, body: str) -> Path:
        hook = tmpdir / "hook.sh"
        hook.write_text(body)
        hook.chmod(hook.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
        return hook

    def test_custom_hook_receives_envelope(self) -> None:
        from personal_assistant import notify

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sink = tmpdir / "sink.json"
            hook = self._write_hook(
                tmpdir,
                "#!/usr/bin/env bash\ncat > " + str(sink) + "\n",
            )
            os.environ["MYOS_NOTIFY_COMMAND"] = str(hook)
            result = notify.notify(
                self.conn,
                title="Standup in 5",
                body="prep the demo",
                kind="reminder",
                correlation_id="corr-1",
                source_ref="reminder:1",
            )
            self.assertTrue(result["dispatched"])
            self.assertEqual(result["channel"], "custom_command")
            self.assertIsNone(result["error"])
            envelope = json.loads(sink.read_text())
        self.assertEqual(envelope["schema"], "myos.notify.v1")
        self.assertEqual(envelope["title"], "Standup in 5")
        self.assertEqual(envelope["body"], "prep the demo")
        self.assertEqual(envelope["kind"], "reminder")
        self.assertEqual(envelope["correlation_id"], "corr-1")
        self.assertEqual(envelope["source_ref"], "reminder:1")
        # Audit guarantee: event_log + inbox_items row on success.
        events = self.conn.execute(
            "SELECT event_type, payload FROM event_log WHERE event_type = 'notify_dispatch'"
        ).fetchall()
        self.assertEqual(len(events), 1)
        audit = json.loads(events[0]["payload"])
        self.assertEqual(audit["channel"], "custom_command")
        self.assertTrue(audit["dispatched"])
        inbox = self.conn.execute("SELECT kind, text, source FROM inbox_items WHERE source LIKE 'notify:%'").fetchall()
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["kind"], "reminder")
        self.assertEqual(inbox[0]["source"], "notify:reminder")
        self.assertIn("Standup in 5", inbox[0]["text"])

    def test_hook_failure_records_missed_reminder(self) -> None:
        from personal_assistant import notify

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            hook = self._write_hook(
                tmpdir,
                "#!/usr/bin/env bash\ncat > /dev/null\nexit 3\n",
            )
            os.environ["MYOS_NOTIFY_COMMAND"] = str(hook)
            result = notify.notify(
                self.conn,
                title="Ship review",
                body="check outbox",
                kind="reminder",
            )
        # Hook failed, but the stdout fallback still succeeded, so
        # ``dispatched`` is True on the terminal channel.
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["channel"], "stdout")
        # The FIRST channel's error string is what we surface.
        self.assertIsNotNone(result["error"])
        assert result["error"] is not None
        self.assertIn("hook exit=3", result["error"])
        # Because stdout dispatched, the inbox row is 'reminder', not
        # 'reminder_missed' — a missed row would over-report failure.
        inbox = self.conn.execute("SELECT kind FROM inbox_items WHERE source LIKE 'notify:%'").fetchall()
        self.assertEqual([r["kind"] for r in inbox], ["reminder"])

    def test_stdout_fallback_when_no_hook(self) -> None:
        from personal_assistant import notify

        # No hook configured and darwin osascript disabled; must fall
        # through to stdout so headless CI still sees the notification.
        result = notify.notify(
            self.conn,
            title="Fallback",
            body="terminal only",
            kind="digest",
        )
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["channel"], "stdout")
        events = self.conn.execute("SELECT payload FROM event_log WHERE event_type = 'notify_dispatch'").fetchall()
        audit = json.loads(events[0]["payload"])
        self.assertEqual(audit["channel"], "stdout")
        self.assertTrue(audit["dispatched"])

    def test_body_is_privacy_filtered(self) -> None:
        from personal_assistant import notify

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sink = tmpdir / "sink.json"
            hook = self._write_hook(
                tmpdir,
                "#!/usr/bin/env bash\ncat > " + str(sink) + "\n",
            )
            os.environ["MYOS_NOTIFY_COMMAND"] = str(hook)
            notify.notify(
                self.conn,
                title="Ping",
                body="ping alice@example.com about the retro",
            )
            envelope = json.loads(sink.read_text())
        self.assertNotIn("alice@example.com", envelope["body"])
        self.assertIn("[REDACTED_EMAIL]", envelope["body"])

    def test_envelope_kind_and_urgency_sanitized(self) -> None:
        from personal_assistant.notify import build_envelope

        envelope = build_envelope(title="t", body="b", kind="bogus", urgency="extreme")
        self.assertEqual(envelope["kind"], "generic")
        self.assertEqual(envelope["urgency"], "normal")
        envelope = build_envelope(title="t", body="b", kind="digest", urgency="high")
        self.assertEqual(envelope["kind"], "digest")
        self.assertEqual(envelope["urgency"], "high")


class SchemaVersionTest(unittest.TestCase):
    def test_reminders_table_and_version(self) -> None:
        conn, path = _fresh_db_conn()
        try:
            from personal_assistant.db import EXPECTED_SCHEMA_VERSION

            row = conn.execute(
                "SELECT MAX(version) AS v FROM schema_migrations WHERE name = 'add_reminders'"
            ).fetchone()
            self.assertEqual(row["v"], 39)
            self.assertGreaterEqual(EXPECTED_SCHEMA_VERSION, 39)
            cols = {c["name"] for c in conn.execute("PRAGMA table_info(reminders)").fetchall()}
            self.assertEqual(
                cols,
                {
                    "id",
                    "text",
                    "scheduled_at",
                    "status",
                    "kind",
                    "source_ref",
                    "correlation_id",
                    "created_at",
                    "fired_at",
                    "completed_at",
                    "snoozed_until",
                },
            )
        finally:
            conn.close()
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()

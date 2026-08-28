from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from personal_assistant.db import initialize_schema
from personal_assistant.locks import acquire_lock, release_lock


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


def _file_conn(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=2)
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


class AcquireLockBasicTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _mem_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_first_acquire_succeeds(self) -> None:
        self.assertTrue(acquire_lock(self.conn, "autopilot", "worker-1"))

    def test_same_owner_reacquire_is_idempotent(self) -> None:
        acquire_lock(self.conn, "autopilot", "worker-1")
        # INSERT OR IGNORE leaves the existing row; SELECT confirms owner match.
        self.assertTrue(acquire_lock(self.conn, "autopilot", "worker-1"))

    def test_different_owner_cannot_acquire_held_lock(self) -> None:
        acquire_lock(self.conn, "autopilot", "worker-1")
        self.assertFalse(acquire_lock(self.conn, "autopilot", "worker-2"))

    def test_acquire_two_distinct_locks_independently(self) -> None:
        self.assertTrue(acquire_lock(self.conn, "autopilot", "worker-1"))
        self.assertTrue(acquire_lock(self.conn, "pulse", "worker-1"))

    def test_lock_name_is_stored(self) -> None:
        acquire_lock(self.conn, "my-lock", "owner-a")
        row = self.conn.execute(
            "SELECT owner FROM pipeline_locks WHERE name = ?", ("my-lock",)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["owner"], "owner-a")


class ReleaseLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _mem_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_release_removes_own_lock(self) -> None:
        acquire_lock(self.conn, "autopilot", "worker-1")
        release_lock(self.conn, "autopilot", "worker-1")
        row = self.conn.execute(
            "SELECT 1 FROM pipeline_locks WHERE name = ?", ("autopilot",)
        ).fetchone()
        self.assertIsNone(row)

    def test_release_by_wrong_owner_is_noop(self) -> None:
        acquire_lock(self.conn, "autopilot", "worker-1")
        release_lock(self.conn, "autopilot", "worker-2")  # different owner
        row = self.conn.execute(
            "SELECT owner FROM pipeline_locks WHERE name = ?", ("autopilot",)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["owner"], "worker-1")

    def test_release_nonexistent_lock_is_noop(self) -> None:
        # Should not raise; DELETE on a missing row is a no-op.
        release_lock(self.conn, "does-not-exist", "worker-1")

    def test_release_then_reacquire_by_new_owner(self) -> None:
        acquire_lock(self.conn, "autopilot", "worker-1")
        release_lock(self.conn, "autopilot", "worker-1")
        self.assertTrue(acquire_lock(self.conn, "autopilot", "worker-2"))


class StaleReclaimTest(unittest.TestCase):
    """Locks older than 1 hour are reclaimed by acquire_lock."""

    def setUp(self) -> None:
        self.conn = _mem_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_fresh_lock_is_not_reclaimed(self) -> None:
        acquire_lock(self.conn, "autopilot", "worker-1")
        # A different owner cannot take a fresh lock.
        self.assertFalse(acquire_lock(self.conn, "autopilot", "worker-2"))

    def test_stale_lock_is_reclaimed(self) -> None:
        # Manually insert a lock with an old acquired_at timestamp.
        self.conn.execute(
            "INSERT INTO pipeline_locks (name, owner, acquired_at) VALUES (?, ?, ?)",
            ("autopilot", "old-worker", "2000-01-01 00:00:00"),
        )
        self.conn.commit()
        # A new owner should reclaim it.
        self.assertTrue(acquire_lock(self.conn, "autopilot", "new-worker"))
        row = self.conn.execute(
            "SELECT owner FROM pipeline_locks WHERE name = ?", ("autopilot",)
        ).fetchone()
        self.assertEqual(row["owner"], "new-worker")


class OpenTransactionErrorTest(unittest.TestCase):
    """BEGIN IMMEDIATE inside an open transaction must raise, not return False."""

    def setUp(self) -> None:
        self.conn = _mem_conn()

    def tearDown(self) -> None:
        try:
            self.conn.rollback()
        except Exception:
            pass
        self.conn.close()

    def test_raises_on_nested_transaction(self) -> None:
        # Open a write transaction so BEGIN IMMEDIATE hits the "cannot start
        # a transaction within a transaction" OperationalError.
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaises(sqlite3.OperationalError):
            acquire_lock(self.conn, "autopilot", "worker-1")


class ConcurrentAcquireTest(unittest.TestCase):
    """Only one thread acquires a contested lock; the other gets False."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = str(Path(self._tmpdir.name) / "test.db")
        # Initialise schema once.
        init_conn = _file_conn(self._db_path)
        init_conn.close()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_only_one_of_two_threads_acquires(self) -> None:
        results: list[bool] = []
        barrier = threading.Barrier(2)

        def try_acquire() -> None:
            conn = _file_conn(self._db_path)
            try:
                barrier.wait()  # both threads attempt simultaneously
                result = acquire_lock(conn, "shared-lock", f"thread-{threading.get_ident()}")
                results.append(result)
            finally:
                conn.close()

        t1 = threading.Thread(target=try_acquire)
        t2 = threading.Thread(target=try_acquire)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(len(results), 2)
        # Exactly one thread should have won the lock.
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 1)

    def test_lock_held_after_concurrent_contest(self) -> None:
        results: list[tuple[bool, str]] = []
        barrier = threading.Barrier(2)

        def try_acquire(owner: str) -> None:
            conn = _file_conn(self._db_path)
            try:
                barrier.wait()
                result = acquire_lock(conn, "shared-lock", owner)
                results.append((result, owner))
            finally:
                conn.close()

        t1 = threading.Thread(target=try_acquire, args=("alpha",))
        t2 = threading.Thread(target=try_acquire, args=("beta",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        winner = next(owner for ok, owner in results if ok)
        verify_conn = _file_conn(self._db_path)
        try:
            row = verify_conn.execute(
                "SELECT owner FROM pipeline_locks WHERE name = ?", ("shared-lock",)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["owner"], winner)
        finally:
            verify_conn.close()

    def test_release_then_second_owner_can_acquire(self) -> None:
        conn1 = _file_conn(self._db_path)
        conn2 = _file_conn(self._db_path)
        try:
            self.assertTrue(acquire_lock(conn1, "sequential-lock", "first"))
            self.assertFalse(acquire_lock(conn2, "sequential-lock", "second"))
            release_lock(conn1, "sequential-lock", "first")
            self.assertTrue(acquire_lock(conn2, "sequential-lock", "second"))
        finally:
            conn1.close()
            conn2.close()


if __name__ == "__main__":
    unittest.main()

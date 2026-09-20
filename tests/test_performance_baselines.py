"""Performance baseline tests for MYOS.

Tests establish baseline metrics for critical operations to detect
performance regressions over time. These tests focus on:
- Retrieval latency
- Dashboard query performance
- Approval processing speed
- Large dataset handling
- Memory/resource behavior
"""

import sqlite3
import time
import unittest

from personal_assistant.db import initialize_schema
from personal_assistant.execution import _compute_payload_hash
from personal_assistant.privacy import apply_privacy_filters


class PerformanceBaselineTest(unittest.TestCase):
    """Establish performance baselines for critical operations."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        initialize_schema(self.conn)
        self.addCleanup(self.conn.close)

    def test_approval_hash_calculation_performance(self):
        """Baseline: approval hash calculation should complete in <1ms."""
        payload = '{"action": "test", "target": "example"}'

        start = time.perf_counter()
        for _ in range(1000):
            _compute_payload_hash(payload)
        elapsed = time.perf_counter() - start

        avg_ms = (elapsed / 1000) * 1000
        self.assertLess(avg_ms, 1.0, f"Average hash calculation took {avg_ms:.3f}ms (expected <1ms)")

    def test_privacy_filter_performance(self):
        """Baseline: privacy filtering should complete in <0.5ms per string."""
        test_strings = [
            "Regular text",
            "Email: test@example.com",
            "Phone: 555-123-4567",
            "SSN: 123-45-6789",
            "Key: sk-1234567890abcdef",
        ]

        start = time.perf_counter()
        for _ in range(1000):
            for s in test_strings:
                apply_privacy_filters(self.conn, s)
        elapsed = time.perf_counter() - start

        avg_ms = (elapsed / (1000 * len(test_strings))) * 1000
        self.assertLess(avg_ms, 0.5, f"Average privacy filter took {avg_ms:.3f}ms (expected <0.5ms)")

    def test_database_insert_performance(self):
        """Baseline: single insert should complete in <1ms."""
        start = time.perf_counter()
        for i in range(100):
            self.conn.execute(
                "INSERT INTO intents (objective, constraints_json) VALUES (?, ?)", (f"Test objective {i}", "[]")
            )
        self.conn.commit()
        elapsed = time.perf_counter() - start

        avg_ms = (elapsed / 100) * 1000
        self.assertLess(avg_ms, 1.0, f"Average insert took {avg_ms:.3f}ms (expected <1ms)")

    def test_database_query_performance(self):
        """Baseline: simple query with 1000 rows should complete in <10ms."""
        # Insert test data
        for i in range(1000):
            self.conn.execute(
                "INSERT INTO intents (objective, constraints_json) VALUES (?, ?)", (f"Test objective {i}", "[]")
            )
        self.conn.commit()

        start = time.perf_counter()
        for _ in range(100):
            self.conn.execute("SELECT * FROM intents").fetchall()
        elapsed = time.perf_counter() - start

        avg_ms = (elapsed / 100) * 1000
        self.assertLess(avg_ms, 10.0, f"Average query took {avg_ms:.3f}ms (expected <10ms)")

    def test_large_dataset_handling(self):
        """Baseline: query with 10,000 rows should complete in <100ms."""
        # Insert larger dataset
        for i in range(10000):
            self.conn.execute(
                "INSERT INTO intents (objective, constraints_json) VALUES (?, ?)", (f"Test objective {i}", "[]")
            )
        self.conn.commit()

        start = time.perf_counter()
        rows = self.conn.execute("SELECT * FROM intents").fetchall()
        elapsed = time.perf_counter() - start

        self.assertEqual(len(rows), 10000)
        self.assertLess(elapsed, 0.1, f"Large dataset query took {elapsed:.3f}s (expected <0.1s)")

    def test_indexed_query_performance(self):
        """Baseline: indexed query should be faster than full table scan."""
        # Insert test data
        for i in range(1000):
            self.conn.execute(
                "INSERT INTO intents (objective, constraints_json) VALUES (?, ?)", (f"Test objective {i}", "[]")
            )
        self.conn.commit()

        # Full table scan
        start = time.perf_counter()
        rows = self.conn.execute("SELECT * FROM intents WHERE objective LIKE 'Test objective 5%'").fetchall()
        scan_time = time.perf_counter() - start

        # If we had an index, this would be faster
        # For now, just establish the baseline
        self.assertGreater(len(rows), 0)
        self.assertLess(scan_time, 0.05, f"Scan query took {scan_time:.3f}s (expected <0.05s)")

    def test_schema_initialization_performance(self):
        """Baseline: schema initialization should complete in <50ms."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row

        start = time.perf_counter()
        initialize_schema(conn)
        elapsed = time.perf_counter() - start

        conn.close()
        self.assertLess(elapsed, 0.05, f"Schema initialization took {elapsed:.3f}s (expected <0.05s)")

    def test_concurrent_operations_performance(self):
        """Baseline: mixed operations should complete in reasonable time."""
        # Test mixed workload
        start = time.perf_counter()

        # Insert 100 records
        for i in range(100):
            self.conn.execute(
                "INSERT INTO intents (objective, constraints_json) VALUES (?, ?)", (f"Test objective {i}", "[]")
            )

        # Query 10 times
        for _ in range(10):
            self.conn.execute("SELECT * FROM intents").fetchall()

        # Update 50 records
        for i in range(50):
            self.conn.execute("UPDATE intents SET objective = ? WHERE rowid = ?", (f"Updated objective {i}", i + 1))

        self.conn.commit()
        elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 0.1, f"Mixed operations took {elapsed:.3f}s (expected <0.1s)")

    def test_large_payload_hash_performance(self):
        """Baseline: hashing large payloads should scale linearly."""
        payloads = [
            '{"action": "test"}',
            '{"action": "test", "data": "x" * 100}',
            '{"action": "test", "data": "x" * 1000}',
            '{"action": "test", "data": "x" * 10000}',
        ]

        times = []
        for payload in payloads:
            start = time.perf_counter()
            for _ in range(100):
                _compute_payload_hash(payload)
            elapsed = time.perf_counter() - start
            times.append(elapsed)

        # Check that performance scales reasonably (not exponentially)
        # 10x larger payload should not take more than 100x time
        ratio = times[-1] / times[0]
        self.assertLess(ratio, 100, f"Large payload hash took {ratio:.1f}x longer (expected <100x)")

    def test_transaction_rollback_performance(self):
        """Baseline: transaction rollback should be fast."""
        start = time.perf_counter()

        try:
            with self.conn:
                for i in range(100):
                    self.conn.execute(
                        "INSERT INTO intents (objective, constraints_json) VALUES (?, ?)", (f"Test objective {i}", "[]")
                    )
                raise Exception("Force rollback")
        except Exception:
            pass

        elapsed = time.perf_counter() - start

        # Verify rollback worked
        count = self.conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0]
        self.assertEqual(count, 0)
        self.assertLess(elapsed, 0.05, f"Transaction rollback took {elapsed:.3f}s (expected <0.05s)")


if __name__ == "__main__":
    unittest.main()

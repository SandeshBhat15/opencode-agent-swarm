"""Tests for :mod:`swarmkit.usage` (real usage -> budget accounting)."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmkit import budget, telemetry, usage
from swarmkit.budget import Budget, Limits
from swarmkit.telemetry import Usage


def session_record(sid: str, *, inp: int = 10, out: int = 5, reasoning: int = 2, cached: int = 0) -> dict:
    return {
        "id": sid,
        "cost": 0,
        "tokens": {"input": inp, "output": out, "reasoning": reasoning, "cache": {"read": cached, "write": 0}},
        "outcome": "succeeded",
        "time": {"created": 1_000_000, "updated": 1_001_000},
    }


class TestUsageLedger(unittest.TestCase):
    def test_normal_run_records_usage(self) -> None:
        b = Budget(Limits(max_tokens=1000))
        ledger = usage.UsageLedger(budget=b)
        res = ledger.record_session(session_record("s1"))
        self.assertEqual(res.status, "recorded")
        self.assertEqual(res.tokens_added, 10 + 5 + 2)
        self.assertEqual(b.tokens_in, 10)
        self.assertEqual(b.tokens_out, 5 + 2)
        self.assertIn("s1", ledger.seen)

    def test_duplicate_is_idempotent(self) -> None:
        b = Budget(Limits(max_tokens=1000))
        ledger = usage.UsageLedger(budget=b)
        ledger.record_session(session_record("s1"))
        before_in, before_out = b.tokens_in, b.tokens_out
        res2 = ledger.record_session(session_record("s1"))
        self.assertEqual(res2.status, "duplicate")
        self.assertEqual(res2.tokens_added, 0)
        self.assertEqual(b.tokens_in, before_in)
        self.assertEqual(b.tokens_out, before_out)

    def test_missing_telemetry_is_not_zero(self) -> None:
        b = Budget(Limits(max_tokens=1000))
        ledger = usage.UsageLedger(budget=b)
        res = ledger.record_session({"id": "empty", "outcome": "succeeded"})
        self.assertEqual(res.status, "missing")
        self.assertEqual(res.tokens_added, 0)
        self.assertIsNone(res.usage.total_tokens) if res.usage else True
        self.assertEqual(b.tokens_in, 0)
        self.assertEqual(b.tokens_out, 0)

    def test_budget_exhaustion_does_not_charge(self) -> None:
        b = Budget(Limits(max_tokens=10))
        ledger = usage.UsageLedger(budget=b)
        res = ledger.record_session(session_record("big", inp=100, out=0, reasoning=0))
        self.assertEqual(res.status, "exhausted")
        self.assertEqual(res.tokens_added, 0)
        self.assertEqual(b.tokens_in, 0)
        self.assertEqual(b.tokens_out, 0)

    def test_failure_still_records_usage(self) -> None:
        b = Budget(Limits(max_tokens=1000))
        ledger = usage.UsageLedger(budget=b)
        rec = session_record("fail", inp=7, out=3, reasoning=1)
        rec["outcome"] = "failed"
        res = ledger.record_session(rec)
        self.assertEqual(res.status, "recorded")
        self.assertEqual(b.tokens_in, 7)
        self.assertEqual(b.tokens_out, 3 + 1)

    def test_retry_does_not_double_count(self) -> None:
        b = Budget(Limits(max_tokens=1000))
        ledger = usage.UsageLedger(budget=b)
        ledger.record_session(session_record("s1"))
        # A "retry" that re-reports the same session must not charge again.
        ledger.record_session(session_record("s1"))
        self.assertEqual(b.tokens_in, 10)
        self.assertEqual(b.tokens_out, 5 + 2)

    def test_can_afford(self) -> None:
        b = Budget(Limits(max_tokens=100))
        ledger = usage.UsageLedger(budget=b)
        ledger.record_session(session_record("s1", inp=30, out=10, reasoning=0))
        ok, msg = ledger.can_afford(50)
        self.assertTrue(ok)
        ok2, msg2 = ledger.can_afford(1000)
        self.assertFalse(ok2)

    def test_can_afford_no_ceiling(self) -> None:
        b = Budget(Limits(max_tokens=None))
        ledger = usage.UsageLedger(budget=b)
        ok, msg = ledger.can_afford(10**9)
        self.assertTrue(ok)

    def test_record_many(self) -> None:
        b = Budget(Limits(max_tokens=1000))
        ledger = usage.UsageLedger(budget=b)
        results = ledger.record_many([session_record("a"), session_record("b")])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.status == "recorded" for r in results))
        self.assertEqual(b.tokens_in, 20)

    def test_convenience_record_session(self) -> None:
        b = Budget(Limits(max_tokens=1000))
        seen: set = set()
        r1 = usage.record_session(b, session_record("x"), seen=seen)
        r2 = usage.record_session(b, session_record("x"), seen=seen)
        self.assertEqual(r1.status, "recorded")
        self.assertEqual(r2.status, "duplicate")

    def test_status_never_treats_missing_as_zero_in_message(self) -> None:
        b = Budget(Limits(max_tokens=1000))
        ledger = usage.UsageLedger(budget=b)
        res = ledger.record_session({"id": "no-tokens"})
        self.assertEqual(res.status, "missing")
        self.assertIn("unmeasured", res.reason)


if __name__ == "__main__":
    unittest.main()

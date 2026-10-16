"""Budget: concurrency, duplicate execution, spawn/retry/fix caps, wall clock, cost."""

from __future__ import annotations

import time
import unittest

import json

from swarmkit import budget as budget_mod
from swarmkit.budget import Budget, Limits
from swarmkit.errors import BudgetExceeded, ConfigError
from tests.support import TempWorkspace


class TestLimitsValidation(unittest.TestCase):
    def test_defaults_are_sane(self) -> None:
        lim = Limits()
        self.assertEqual(lim.max_concurrent_agents, 4)
        self.assertEqual(lim.max_fix_cycles, 2)
        self.assertIsNone(lim.max_tokens)
        self.assertIsNone(lim.price_in_per_mtok)

    def test_unknown_key_is_refused(self) -> None:
        with self.assertRaises(ConfigError):
            Limits.from_dict({"max_agents": 3})

    def test_non_positive_is_refused(self) -> None:
        for bad in (0, -1):
            with self.subTest(value=bad):
                with self.assertRaises(ConfigError):
                    Limits.from_dict({"max_concurrent_agents": bad})

    def test_non_numeric_is_refused(self) -> None:
        with self.assertRaises(ConfigError):
            Limits.from_dict({"max_fix_cycles": "two"})

    def test_valid_override_roundtrips(self) -> None:
        lim = Limits.from_dict({"max_concurrent_agents": 2, "max_tokens": 1000})
        self.assertEqual(lim.max_concurrent_agents, 2)
        self.assertEqual(lim.max_tokens, 1000)
        self.assertEqual(Limits.from_dict(None).max_concurrent_agents, 4)


class TestConcurrency(unittest.TestCase):
    def test_lease_blocks_over_concurrency(self) -> None:
        b = Budget(limits=Limits(max_concurrent_agents=2))
        b.lease("a")
        b.lease("b")
        with self.assertRaises(BudgetExceeded):
            b.lease("c")

    def test_release_frees_a_slot(self) -> None:
        b = Budget(limits=Limits(max_concurrent_agents=1))
        b.lease("a")
        b.release("a")
        b.lease("b")  # must not raise
        self.assertEqual(b.inflight, 1)

    def test_duplicate_worker_is_refused(self) -> None:
        """This is what stops the same workstream running twice."""
        b = Budget()
        b.lease("build")
        with self.assertRaises(BudgetExceeded):
            b.lease("build")

    def test_release_is_idempotent(self) -> None:
        b = Budget()
        b.lease("a")
        b.release("a")
        b.release("a")  # must not raise

    def test_released_worker_may_be_leased_again(self) -> None:
        b = Budget()
        b.lease("a")
        b.release("a")
        b.lease("a")  # must not raise


class TestSpawnAndRetry(unittest.TestCase):
    def test_spawn_cap(self) -> None:
        b = Budget(limits=Limits(max_spawns_total=2))
        b.record_spawn(name="a")
        b.record_spawn(name="b")
        with self.assertRaises(BudgetExceeded):
            b.record_spawn(name="c")

    def test_retry_cap_per_worker(self) -> None:
        b = Budget(limits=Limits(max_retries_per_worker=2))
        self.assertEqual(b.record_retry("a"), 1)
        self.assertEqual(b.record_retry("a"), 2)
        with self.assertRaises(BudgetExceeded):
            b.record_retry("a")

    def test_retry_budget_is_per_worker(self) -> None:
        b = Budget(limits=Limits(max_retries_per_worker=1))
        b.record_retry("a")
        b.record_retry("b")  # different worker, own budget
        self.assertEqual(b.snapshot()["retries"], {"a": 1, "b": 1})

    def test_fix_cycle_cap(self) -> None:
        b = Budget(limits=Limits(max_fix_cycles=2))
        b.check_fix_cycles(1)
        b.check_fix_cycles(2)
        with self.assertRaises(BudgetExceeded):
            b.check_fix_cycles(3)


class TestClock(unittest.TestCase):
    def test_clock_cap(self) -> None:
        b = Budget(limits=Limits(max_wall_clock_seconds=1))
        time.sleep(1.05)
        with self.assertRaises(BudgetExceeded):
            b.check_clock()

    def test_fresh_clock_passes(self) -> None:
        Budget(limits=Limits(max_wall_clock_seconds=3600)).check_clock()

    def test_elapsed_is_measurable(self) -> None:
        self.assertGreaterEqual(Budget().elapsed(), 0.0)


class TestCost(unittest.TestCase):
    def test_cost_is_none_without_pricing(self) -> None:
        """Never fabricate spend."""
        b = Budget()
        b.record_tokens(1_000_000, 1_000_000)
        self.assertIsNone(b.estimated_cost_usd())

    def test_cost_computed_when_priced(self) -> None:
        b = Budget(
            limits=Limits(price_in_per_mtok=1.0, price_out_per_mtok=10.0),
        )
        b.record_tokens(1_000_000, 1_000_000)
        self.assertAlmostEqual(b.estimated_cost_usd(), 11.0)

    def test_token_cap(self) -> None:
        b = Budget(limits=Limits(max_tokens=100))
        b.record_tokens(60, 30)
        with self.assertRaises(BudgetExceeded):
            b.record_tokens(20, 0)


class TestPersistence(TempWorkspace):
    def test_snapshot_shape(self) -> None:
        b = Budget()
        b.lease("a")
        b.record_spawn(name="a")
        b.record_retry("a")
        b.record_tokens(10, 20)
        snap = b.snapshot()
        for key in ("inflight", "spawns", "retries", "tokens_in", "tokens_out", "limits"):
            self.assertIn(key, snap)
        self.assertEqual(snap["inflight"], ["a"])

    def test_counters_survive_reload(self) -> None:
        run = self.make_run()
        b = Budget(limits=Limits(max_spawns_total=5))
        b.record_spawn(name="a")
        b.record_tokens(11, 22)
        b.persist(run)
        again = Budget.load(run)
        self.assertEqual(again.spawns, 1)
        self.assertEqual(again.tokens_in, 11)
        self.assertEqual(again.tokens_out, 22)
        self.assertEqual(again.limits.max_spawns_total, 5)

    def test_reload_restores_live_leases(self) -> None:
        """Caps must hold across separate CLI invocations, or they are decoration."""
        run = self.make_run()
        b = Budget(limits=Limits(max_concurrent_agents=1))
        b.lease("build")
        b.persist(run)
        again = Budget.load(run)
        self.assertEqual(again.inflight, 1)
        with self.assertRaises(BudgetExceeded):
            again.lease("ui")

    def test_reload_drops_a_stale_lease(self) -> None:
        """A lease whose process died must not deadlock the resumed run."""
        run = self.make_run()
        b = Budget(limits=Limits(max_concurrent_agents=1))
        b.lease("stuck")
        snap = b.snapshot()
        snap["leases"]["stuck"]["since"] = "2000-01-01T00:00:00+00:00"
        (run / "budget.json").write_text(json.dumps(snap), encoding="utf-8")
        again = Budget.load(run)
        self.assertEqual(again.inflight, 0)
        again.lease("fresh")  # must not raise

    def test_reload_drops_an_unreadable_lease(self) -> None:
        run = self.make_run()
        b = Budget(limits=Limits(max_concurrent_agents=1))
        b.lease("junk")
        snap = b.snapshot()
        snap["leases"]["junk"]["since"] = "not-a-timestamp"
        (run / "budget.json").write_text(json.dumps(snap), encoding="utf-8")
        self.assertEqual(Budget.load(run).inflight, 0)


if __name__ == "__main__":
    unittest.main()
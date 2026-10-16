"""Worker liveness, retry accounting, and lease safety.

The gap this file closes: `swarmkit` had no way to tell a dead worker from a
slow one, and retry limits only held if somebody remembered to call
`budget retry`. These tests pin the behaviour that replaces both.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from swarmkit import workers as workers_mod
from swarmkit.budget import COUNTERS_LOCK, Budget, Limits
from swarmkit.errors import BudgetExceeded, SwarmError
from swarmkit.store import file_lock, init_run
from tests.support import REPO_ROOT, TempWorkspace, run_cli


class WorkerFixture(TempWorkspace):
    """A run directory plus helpers that keep time injectable."""

    #: A spawn instant far enough in the past to look dead but comfortably
    #: inside any sane timeout, so a test exercises the `stale` path rather
    #: than the `timeout` one by accident.
    LONG_AGO = 600.0

    def make(self, slug: str = "w"):
        self.run_dir = init_run(self.ws, slug)
        return self.run_dir

    def spawn(self, name="build", *, role="builder", timeout_s=100.0, limits=None, now=None):
        return workers_mod.spawn(
            self.run_dir,
            name,
            role=role,
            timeout_s=timeout_s,
            limits=limits or Limits(),
            now=now,
        )

    def spawn_dead(self, name="build", *, timeout_s=10_000.0, limits=None):
        """A worker that stopped beating long ago, but is not yet timed out."""
        return self.spawn(name, timeout_s=timeout_s, limits=limits,
                          now=time.time() - self.LONG_AGO)

    def registry(self):
        return workers_mod.Registry(self.run_dir)


# -- classification: dead vs slow ----------------------------------------


class TestLivenessVerdicts(unittest.TestCase):
    def _worker(self, **kw):
        base = {
            "name": "w",
            "epoch": "e",
            "spawned_at": "2026-01-01T00:00:00+00:00",
            "last_beat": "2026-01-01T00:00:00+00:00",
            "timeout_s": 100.0,
            "state": "running",
        }
        base.update(kw)
        return workers_mod.Worker(**base)

    def _t(self, iso: str) -> float:
        return workers_mod._parse(iso)

    def test_a_fresh_worker_is_alive(self) -> None:
        w = self._worker()
        t0 = self._t(w.spawned_at)
        self.assertEqual(workers_mod.classify(w, stale_seconds=50, now=t0 + 1), "alive")

    def test_a_silent_worker_goes_stale_not_slow(self) -> None:
        """A crashed worker stops beating. That is the dead signal."""
        w = self._worker()
        t0 = self._t(w.spawned_at)
        self.assertEqual(workers_mod.classify(w, stale_seconds=50, now=t0 + 60), "stale")

    def test_a_beating_worker_is_slow_not_stale(self) -> None:
        """Long-running but still beating must NOT be killed. This is the
        dead/slow distinction the whole module exists for."""
        w = self._worker(last_beat="2026-01-01T00:00:50+00:00")
        t0 = self._t(w.spawned_at)
        verdict = workers_mod.classify(w, stale_seconds=50, now=t0 + 90)
        self.assertEqual(verdict, "slow")
        self.assertNotEqual(verdict, "stale")

    def test_a_worker_past_its_timeout_is_timed_out_even_if_beating(self) -> None:
        """Heartbeats alone cannot save a worker in an infinite loop."""
        w = self._worker(last_beat="2026-01-01T00:09:00+00:00")
        t0 = self._t(w.spawned_at)
        self.assertEqual(workers_mod.classify(w, stale_seconds=50, now=t0 + 150), "timeout")

    def test_a_finished_worker_reports_its_state_not_a_verdict(self) -> None:
        for state in workers_mod.DONE:
            w = self._worker(state=state, last_beat="2020-01-01T00:00:00+00:00")
            self.assertEqual(workers_mod.classify(w, stale_seconds=1), state)

    def test_a_missing_timestamp_reads_as_ancient(self) -> None:
        """Fail closed: no timestamp means old, never 'freshly spawned'."""
        w = self._worker(spawned_at="", last_beat="")
        verdict = workers_mod.classify(w, stale_seconds=50, now=time.time())
        self.assertIn(verdict, {workers_mod.STALE, workers_mod.TIMEOUT})
        self.assertNotEqual(verdict, workers_mod.ALIVE,
                            "an untimestamped record must never look healthy")


class TestBackoff(unittest.TestCase):
    def test_delay_grows_geometrically(self) -> None:
        got = [workers_mod.retry_delay(n) for n in range(1, 6)]
        self.assertEqual(got, [2.0, 4.0, 8.0, 16.0, 32.0])

    def test_delay_is_capped(self) -> None:
        self.assertEqual(workers_mod.retry_delay(50), workers_mod.MAX_RETRY_BACKOFF_SECONDS)

    def test_attempt_below_one_is_clamped(self) -> None:
        for bad in (0, -5):
            self.assertEqual(workers_mod.retry_delay(bad), workers_mod.DEFAULT_RETRY_BACKOFF_SECONDS)


# -- spawning, retries, duplicate execution ------------------------------


class TestSpawnAndRetry(WorkerFixture):
    def test_first_spawn_is_attempt_one_with_no_retry_charged(self) -> None:
        self.make()
        result = self.spawn()
        self.assertEqual(result.worker.attempt, 1)
        budget = Budget.load(self.run_dir)
        self.assertEqual(budget.retries, {}, "the first spawn is not a retry")

    def test_retry_charged_before_the_attempt_starts(self) -> None:
        """An exhausted budget must stop the retry, not start it and kill it."""
        self.make()
        limits = Limits(max_retries_per_worker=1)
        self.spawn(limits=limits)
        self._fail_current_attempt()
        again = self.spawn(limits=limits)
        self.assertEqual(again.worker.attempt, 2)
        self.assertEqual(Budget.load(self.run_dir).retries["build"], 1,
                         "the retry must be charged to the budget")

    def test_retry_budget_is_exhausted_and_refuses(self) -> None:
        self.make()
        limits = Limits(max_retries_per_worker=1)
        self.spawn(limits=limits)
        self._fail_current_attempt()
        self.spawn(limits=limits)  # attempt 2, one retry consumed
        self._fail_current_attempt()
        with self.assertRaises(BudgetExceeded) as ctx:
            self.spawn(limits=limits)  # attempt 3 needs a second retry
        self.assertEqual(ctx.exception.code, "RETRY_LIMIT")
        self.assertEqual(self.registry().find("build").attempt, 2,
                         "the refused retry must not have started attempt 3")

    def _fail_current_attempt(self) -> None:
        reg = self.registry()
        reg.finish("build", reg.find("build").epoch, "failed", "boom")

    def test_duplicate_execution_is_refused_while_alive(self) -> None:
        self.make()
        self.spawn()
        with self.assertRaises(SwarmError) as ctx:
            self.spawn()
        self.assertEqual(ctx.exception.code, "DUPLICATE_WORKER")

    def test_a_stale_predecessor_may_be_respawned_with_a_new_epoch(self) -> None:
        self.make()
        first = self.spawn_dead()  # long past stale/timeout
        second = self.spawn(now=time.time())
        self.assertEqual(second.worker.attempt, 2)
        self.assertNotEqual(
            second.worker.epoch, first.worker.epoch,
            "a new attempt needs a new epoch so the zombie cannot signal",
        )

    def test_a_zombie_cannot_beat_or_finish_after_reclaim(self) -> None:
        self.make()
        zombie = self.spawn_dead()
        self.spawn(now=time.time())  # attempt 2 takes over
        reg = self.registry()
        with self.assertRaises(SwarmError) as ctx:
            reg.heartbeat("build", zombie.worker.epoch)
        self.assertEqual(ctx.exception.code, "EPOCH_MISMATCH")
        with self.assertRaises(SwarmError) as ctx:
            reg.finish("build", zombie.worker.epoch, "ok", "")
        self.assertEqual(ctx.exception.code, "EPOCH_MISMATCH")

    def test_heartbeat_increments_and_only_the_owner_may_beat(self) -> None:
        self.make()
        r = self.spawn()
        reg = self.registry()
        for expected in (1, 2, 3):
            w = reg.heartbeat("build", r.worker.epoch)
            self.assertEqual(w.beats, expected)
        self.assertTrue(reg.owns("build", r.worker.epoch))
        self.assertFalse(reg.owns("build", "someone-else"))

    def test_beating_an_unknown_worker_is_refused(self) -> None:
        self.make()
        with self.assertRaises(SwarmError) as ctx:
            self.registry().heartbeat("ghost", "x")
        self.assertEqual(ctx.exception.code, "NO_SUCH_WORKER")

    def test_finish_only_accepts_a_real_outcome(self) -> None:
        self.make()
        r = self.spawn()
        with self.assertRaises(SwarmError) as ctx:
            self.registry().finish("build", r.worker.epoch, "reclaimed", "")
        self.assertEqual(ctx.exception.code, "BAD_STATE")


# -- reclamation ----------------------------------------------------------


class TestReclaim(WorkerFixture):
    def test_reclaim_frees_a_dead_workers_slot(self) -> None:
        self.make()
        self.spawn_dead()
        budget = Budget.load(self.run_dir)
        self.assertEqual(budget.inflight, 1, "precondition: the slot is held")

        report = workers_mod.reclaim(self.run_dir, now=time.time())
        self.assertEqual([r["name"] for r in report["reclaimed"]], ["build"])
        self.assertEqual(Budget.load(self.run_dir).inflight, 0, "slot must be freed")
        self.assertEqual(self.registry().find("build").state, "reclaimed")

    def test_reclaim_never_touches_a_slow_but_beatting_worker(self) -> None:
        """Killing healthy work is the worst failure mode here."""
        self.make()
        # Spawned 90s ago with a 100s timeout: past 80% of its budget, so
        # `slow`. It keeps beating, so it must never be judged stale.
        r = self.spawn(timeout_s=100.0, now=time.time() - 90)
        reg = self.registry()
        reg.heartbeat("build", r.worker.epoch)
        self.assertEqual(
            workers_mod.classify(reg.find("build"), stale_seconds=300, now=time.time()),
            workers_mod.SLOW,
        )

        report = workers_mod.reclaim(self.run_dir, stale_seconds=300, now=time.time())
        self.assertEqual(report["reclaimed"], [])
        self.assertEqual([s["name"] for s in report["skipped"]], ["build"])
        self.assertEqual(Budget.load(self.run_dir).inflight, 1, "slot must survive")
        self.assertEqual(self.registry().find("build").state, "running")

    def test_reclaim_is_idempotent(self) -> None:
        self.make()
        self.spawn_dead()
        first = workers_mod.reclaim(self.run_dir, now=time.time())
        second = workers_mod.reclaim(self.run_dir, now=time.time())
        self.assertEqual(len(first["reclaimed"]), 1)
        self.assertEqual(second["reclaimed"], [], "a second sweep must not redo work")

    def test_reclaim_persists_nothing_when_there_is_nothing_to_do(self) -> None:
        self.make()
        self.spawn(now=time.time())
        before = (self.run_dir / "budget.json").read_text()
        workers_mod.reclaim(self.run_dir, now=time.time())
        self.assertEqual(
            (self.run_dir / "budget.json").read_text(), before,
            "an empty sweep must not rewrite the budget",
        )

    def test_a_timed_out_worker_is_reported_as_timeout_not_stale(self) -> None:
        self.make()
        self.spawn(timeout_s=1.0, now=time.time() - 5000)
        report = workers_mod.reclaim(self.run_dir, now=time.time())
        self.assertEqual(report["reclaimed"][0]["liveness"], "timeout")
        self.assertEqual(self.registry().find("build").state, "timeout")

    def test_status_counts_each_bucket(self) -> None:
        self.make()
        self.spawn("alive1", now=time.time())
        self.spawn_dead("dead1")
        status = self.registry().status(stale_seconds=50, now=time.time())
        self.assertIn("alive1", status["alive"])
        self.assertIn("dead1", status["stale"])
        self.assertEqual(len(status["workers"]), 2)
        self.assertTrue(all("liveness" in w for w in status["workers"]))


# -- atomicity -----------------------------------------------------------


class TestAtomicCounters(WorkerFixture):
    def test_concurrent_spawns_cannot_overbook_the_concurrency_cap(self) -> None:
        """The race this closes: N processes each read the same inflight
        count, each decide there is room, and the cap is exceeded. Locks must
        make the decision serial."""
        self.make()
        limits = Limits(max_concurrent_agents=4)
        errors: list[str] = []
        granted: list[str] = []

        def worker_n(i: int) -> None:
            try:
                workers_mod.spawn(self.run_dir, f"w{i}", timeout_s=100.0, limits=limits)
                granted.append(f"w{i}")
            except BudgetExceeded as exc:
                errors.append(exc.code)

        threads = [threading.Thread(target=worker_n, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(granted), 4, "exactly the cap may be granted")
        self.assertEqual(len(errors), 8)
        self.assertEqual(set(errors), {"CONCURRENCY_LIMIT"})
        self.assertEqual(Budget.load(self.run_dir).inflight, 4)

    def test_concurrent_retries_do_not_lose_increments(self) -> None:
        """Two racers each doing +1 must produce +2, not +1."""
        self.make()
        run_dir = self.run_dir
        budget = Budget.load(run_dir, Limits(max_retries_per_worker=100))

        def bump() -> None:
            budget.update_locked(
                run_dir,
                lambda b: b.record_retry("build"),
            )

        threads = [threading.Thread(target=bump) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(Budget.load(run_dir).retries["build"], 10,
                         "a lost update means the retry cap under-counts")

    def test_a_refusal_inside_update_locked_persists_nothing(self) -> None:
        self.make()
        budget = Budget.load(self.run_dir, Limits(max_retries_per_worker=0 or 1))

        def explode(b):
            raise BudgetExceeded("nope", code="RETRY_LIMIT")

        with self.assertRaises(BudgetExceeded):
            budget.update_locked(self.run_dir, explode)
        self.assertEqual(Budget.load(self.run_dir).retries, {})

    def test_file_lock_waits_then_times_out_with_a_code(self) -> None:
        self.make()
        guard = self.run_dir / COUNTERS_LOCK
        holding: list[bool] = [True]
        done = threading.Event()

        def hold() -> None:
            with file_lock(guard, timeout=0.0):
                done.set()
                time.sleep(0.6)

        t = threading.Thread(target=hold)
        t.start()
        done.wait(2)
        started = time.monotonic()
        with self.assertRaises(SwarmError) as ctx:
            with file_lock(guard, timeout=0.15):
                pass
        self.assertEqual(ctx.exception.code, "LOCK_HELD")
        self.assertGreaterEqual(time.monotonic() - started, 0.1, "must actually wait")
        self.assertTrue(holding[0])
        t.join()

    def test_a_stale_lock_is_taken_over(self) -> None:
        """A crashed holder must not wedge the run forever."""
        self.make()
        guard = self.run_dir / COUNTERS_LOCK
        guard.write_text("pid=1 at=long-ago\n", encoding="utf-8")
        import os
        old = time.time() - 10_000
        os.utime(guard, (old, old))
        with file_lock(guard, timeout=0.0, stale_after=900):
            pass  # acquiring at all is the assertion

    def test_release_by_the_wrong_epoch_is_refused(self) -> None:
        self.make()
        r = self.spawn()
        budget = Budget.load(self.run_dir)
        with self.assertRaises(BudgetExceeded) as ctx:
            budget.release("build", epoch="not-my-epoch")
        self.assertEqual(ctx.exception.code, "LEASE_NOT_OWNED")
        self.assertEqual(Budget.load(self.run_dir).inflight, 1, "slot must survive")
        budget.release("build", epoch=r.worker.epoch, run_dir=self.run_dir)
        self.assertEqual(Budget.load(self.run_dir).inflight, 0)


# -- corruption ----------------------------------------------------------


class TestCorruption(TempWorkspace):
    def test_a_non_object_registry_is_refused(self) -> None:
        run_dir = init_run(self.ws, "c")
        (run_dir / "workers.json").write_text("[1,2,3]", encoding="utf-8")
        with self.assertRaises(SwarmError) as ctx:
            workers_mod.Registry(run_dir).load()
        self.assertEqual(ctx.exception.code, "CORRUPT_REGISTRY")

    def test_a_malformed_worker_record_is_refused(self) -> None:
        run_dir = init_run(self.ws, "c")
        (run_dir / "workers.json").write_text(
            json.dumps({"version": 1, "workers": {"build": "not-a-record"}}),
            encoding="utf-8",
        )
        with self.assertRaises(SwarmError) as ctx:
            workers_mod.Registry(run_dir).load()
        self.assertEqual(ctx.exception.code, "CORRUPT_REGISTRY")

    def test_an_absent_registry_is_empty_not_an_error(self) -> None:
        run_dir = init_run(self.ws, "c")
        self.assertEqual(workers_mod.Registry(run_dir).load(), {})


# -- CLI surface ---------------------------------------------------------


class TestWorkersViaCli(WorkerFixture):
    def _spawn(self, name="build"):
        proc = run_cli("--json", "workers", "spawn", "--name", name,
                       "--role", "builder", "--slug", "w", workspace=self.ws)
        return json.loads(proc.stdout)

    def test_spawn_beat_finish_round_trip(self) -> None:
        self.make()
        payload = self._spawn()
        epoch = payload["worker"]["epoch"]
        self.assertEqual(payload["worker"]["attempt"], 1)
        self.assertEqual(payload["retry_delay"], workers_mod.DEFAULT_RETRY_BACKOFF_SECONDS)
        self.assertEqual(payload["budget"]["inflight"], ["build"])

        beat = json.loads(run_cli("--json", "workers", "beat", "--name", "build",
                                  "--epoch", epoch, "--slug", "w", workspace=self.ws).stdout)
        self.assertEqual(beat["worker"]["beats"], 1)

        run_cli("workers", "finish", "--name", "build", "--epoch", epoch,
                "--state", "ok", "--slug", "w", workspace=self.ws)
        status = json.loads(run_cli("--json", "workers", "status", "--slug", "w",
                                    workspace=self.ws).stdout)
        self.assertEqual(status["alive"], [])
        self.assertEqual(status["workers"][0]["state"], "ok")

    def test_a_zombie_beat_is_refused_from_the_cli(self) -> None:
        self.make()
        self._spawn()
        proc = run_cli("--json", "workers", "beat", "--name", "build",
                       "--epoch", "0" * 32, "--slug", "w", workspace=self.ws, expect=1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "EPOCH_MISMATCH")

    def test_a_duplicate_spawn_is_refused_from_the_cli(self) -> None:
        self.make()
        self._spawn()
        proc = run_cli("--json", "workers", "spawn", "--name", "build",
                       "--slug", "w", workspace=self.ws, expect=1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "DUPLICATE_WORKER")

    def test_reclaim_reports_what_it_did_from_the_cli(self) -> None:
        self.make()
        self._spawn()
        self.assertEqual(
            json.loads(run_cli("--json", "workers", "reclaim", "--slug", "w",
                               workspace=self.ws).stdout)["reclaimed"],
            [],
            "a fresh worker must not be reclaimed",
        )
        # Age it past the stale threshold without waiting, via the real spawn.
        self.spawn_dead("dead", timeout_s=10_000)
        report = json.loads(run_cli("--json", "workers", "reclaim", "--slug", "w",
                                    workspace=self.ws).stdout)
        self.assertEqual([r["name"] for r in report["reclaimed"]], ["dead"])
        self.assertEqual(report["skipped"][0]["name"], "build")

    def test_an_interrupted_worker_mutation_blocks_the_next_one(self) -> None:
        """The journal must survive an unexpected exception."""
        self.make()
        with self.assertRaises(ZeroDivisionError):
            with __import__("swarmkit.store", fromlist=["mutation"]).mutation(
                self.run_dir, "workers.beat", "build"
            ):
                1 / 0
        proc = run_cli("--json", "workers", "spawn", "--name", "x", "--slug", "w",
                       workspace=self.ws, expect=1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "RECOVERY_REQUIRED")


if __name__ == "__main__":
    unittest.main()
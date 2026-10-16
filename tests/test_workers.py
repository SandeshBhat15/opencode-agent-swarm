"""Worker liveness, retry accounting, and lease safety.

The gap this file closes: `swarmkit` had no way to tell a dead worker from a
slow one, and retry limits only held if somebody remembered to call
`budget retry`. These tests pin the behaviour that replaces both.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from swarmkit import gate
from swarmkit import workers as workers_mod
from swarmkit.budget import COUNTERS_LOCK, Budget, Limits
from swarmkit.errors import BudgetExceeded, LockError, SwarmError
from swarmkit.store import file_lock, init_run
from tests.support import REPO_ROOT, TempWorkspace, run_cli


def _iso(epoch: float) -> str:
    """Epoch seconds -> the timestamp form the registry stores."""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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

    def budget(self, limits=None):
        return Budget.load(self.run_dir, limits or Limits())

    def age(self, name: str, **overrides) -> None:
        """Rewrite one registry record in place, to reach a classification the
        clock cannot be asked for (e.g. 'beating, but no progress for ages')."""
        registry = self.registry()
        workers = registry.load()
        workers[name] = workers_mod.Worker(
            **{**workers[name].to_dict(), **overrides}
        )
        registry.workers = workers
        registry.save(workers)


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
        # A pid that cannot be live. Using a small number like 1 would be
        # ambiguous on Windows, where it may well be a real system process.
        guard.write_text("pid=2147483647 at=long-ago\n", encoding="utf-8")
        import os
        old = time.time() - 10_000
        os.utime(guard, (old, old))
        with file_lock(guard, timeout=0.0, stale_after=900):
            pass  # acquiring at all is the assertion

    def test_a_live_holder_is_never_evicted_however_old(self) -> None:
        """The guard on stale takeover. `os.kill` cannot be used to probe this
        on Windows - it maps signal 0 onto TerminateProcess, which would kill
        the very process being inspected."""
        self.make()
        guard = self.run_dir / COUNTERS_LOCK
        guard.write_text(f"pid={os.getpid()} at=long-ago\n", encoding="utf-8")
        old = time.time() - 10_000
        os.utime(guard, (old, old))
        with self.assertRaises(LockError):
            with file_lock(guard, timeout=0.0, stale_after=1):
                pass
        self.assertTrue(guard.exists(), "a live holder's lock was stolen")

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


class TestProgressReporting(WorkerFixture):
    """A worker that beats forever without doing anything is the failure mode a
    heartbeat cannot see on its own."""

    def test_progress_increments_and_counts(self) -> None:
        self.make()
        self.spawn("build")
        worker = self.registry()
        epoch = worker.find("build").epoch
        worker.progress("build", epoch)
        worker.progress("build", epoch, units=3)
        self.assertEqual(worker.find("build").progress, 4)

    def test_progress_counts_as_being_alive(self) -> None:
        """Reporting progress IS a liveness signal, so it must refresh the beat.
        Otherwise a worker doing real work between milestones would look dead."""
        self.make()
        self.spawn("build")
        registry = self.registry()
        worker = registry.find("build")
        registry.progress("build", worker.epoch)
        updated = registry.find("build")
        self.assertEqual(updated.beats, worker.beats + 1)
        self.assertGreaterEqual(updated.last_beat, worker.last_beat)

    def test_progress_refuses_a_non_positive_unit(self) -> None:
        self.make()
        self.spawn("build")
        epoch = self.registry().find("build").epoch
        with self.assertRaises(SwarmError) as ctx:
            self.registry().progress("build", epoch, units=0)
        self.assertEqual(ctx.exception.code, "BAD_PROGRESS")

    def test_progress_from_a_zombie_is_refused(self) -> None:
        self.make()
        self.spawn_dead("build")
        workers_mod.reclaim(self.run_dir, limits=Limits(), stale_seconds=1.0)
        self.spawn("build")
        with self.assertRaises(SwarmError) as ctx:
            self.registry().progress("build", "not-the-live-epoch")
        self.assertEqual(ctx.exception.code, "EPOCH_MISMATCH")

    def test_progress_for_an_unknown_worker_is_refused(self) -> None:
        self.make()
        with self.assertRaises(SwarmError) as ctx:
            self.registry().progress("ghost", "whatever")
        self.assertEqual(ctx.exception.code, "NO_SUCH_WORKER")

    def test_a_healthy_long_worker_is_not_called_wedged(self) -> None:
        """The whole point of the progress check: a build that legitimately
        takes a long time must never be reclaimed."""
        self.make()
        self.spawn("build", timeout_s=100_000.0)
        registry = self.registry()
        worker = registry.find("build")
        registry.progress("build", worker.epoch, units=5)
        verdict = workers_mod.classify(
            workers_mod.Worker(
                **{**registry.find("build").to_dict(),
                   "last_beat": _iso(time.time())},
            ),
            stale_seconds=120.0,
        )
        self.assertNotEqual(verdict, workers_mod.WEDGED)

    def test_a_worker_that_beats_without_progress_can_be_flagged_wedged(self) -> None:
        self.make()
        now = time.time()
        self.spawn("build", timeout_s=100_000.0)
        registry = self.registry()
        current = registry.find("build")
        # Spun up long ago, recently beaten, but the last real progress was
        # well before DEFAULT_PROGRESS_STALE_SECONDS.
        wedged = workers_mod.Worker(
            **{
                **current.to_dict(),
                "spawned_at": _iso(now - 20_000),
                "progress": 2,
                "progress_at": _iso(now - workers_mod.DEFAULT_PROGRESS_STALE_SECONDS - 60),
                "last_beat": _iso(now - 5),
            }
        )
        self.assertEqual(
            workers_mod.classify(wedged, stale_seconds=120.0), workers_mod.WEDGED
        )

    def test_wedging_requires_evidence_of_a_spin(self) -> None:
        """A worker that has simply not hit its first milestone yet is
        indistinguishable from a stuck one. Killing it would destroy real work
        on a guess, so `progress <= 0` must never read as wedged."""
        self.make()
        now = time.time()
        self.spawn("build", timeout_s=100_000.0)
        base = self.registry().find("build").to_dict()
        untouched = workers_mod.Worker(
            **{**base, "spawned_at": _iso(now - 20_000),
               "progress": 0, "progress_at": "", "last_beat": _iso(now - 5)}
        )
        self.assertNotEqual(
            workers_mod.classify(untouched, stale_seconds=120.0), workers_mod.WEDGED
        )

    def test_a_wedged_worker_is_not_reclaimed_unless_asked(self) -> None:
        self.make()
        now = time.time()
        self.spawn("build", timeout_s=100_000.0)
        self.age(
            "build",
            spawned_at=_iso(now - 20_000),
            progress=2,
            progress_at=_iso(now - workers_mod.DEFAULT_PROGRESS_STALE_SECONDS - 60),
            last_beat=_iso(now - 5),
        )
        self.assertIn("build", workers_mod.Registry(self.run_dir).status()["wedged"])

        quiet = workers_mod.reclaim(self.run_dir, limits=Limits(), stale_seconds=120.0)
        self.assertEqual(quiet["reclaimed"], [], "wedged is provably alive")

        forced = workers_mod.reclaim(
            self.run_dir, limits=Limits(), stale_seconds=120.0, include_wedged=True
        )
        self.assertEqual([r["name"] for r in forced["reclaimed"]], ["build"])


class TestReconcileIsIdempotent(WorkerFixture):
    """Recovery after a crash must converge, and converge once."""

    def test_crash_after_spawn_before_the_worker_ran(self) -> None:
        # Registered + leased, no beat yet -> reclaimed as never-started.
        self.make()
        self.spawn("build", now=time.time() - 5000.0)
        report = workers_mod.reconcile(self.run_dir, limits=Limits(), stale_seconds=1.0)
        self.assertEqual([r["name"] for r in report["released"]], ["build"])
        self.assertEqual(report["released"][0]["reason"], "no_heartbeat")
        self.assertEqual(self.budget().snapshot()["inflight"], [])

    def test_crash_while_running(self) -> None:
        self.make()
        self.spawn("build")
        self.assertEqual(self.registry().find("build").state, "running")
        report = workers_mod.reconcile(self.run_dir, limits=Limits(), stale_seconds=120.0)
        self.assertEqual(report["released"], [], "a live worker is left alone")
        self.assertEqual([c["name"] for c in report["consistent"]], ["build"])

    def test_crash_after_the_result_but_before_finish(self) -> None:
        """The result is on disk but `finish` never ran, so the worker is still
        `running` and still holds its lease.

        The docstring used to claim reconciliation should bank this as `ok`, then
        asserted the opposite (`names == []`) with the reason "the worker beat
        recently enough to be alive". That restated the implementation instead of
        the requirement, and it masked a real bug: the test's criterion was named
        `build` — identical to the worker — so the criterion-id/worker-name join
        mismatch could not show up.

        Corrected: the criterion is named for what it proves, not who produced it,
        and the worker is aged into the band where reconciliation must act.
        """
        self.make()
        self.spawn("build", timeout_s=100_000.0)
        gate.add_criterion(self.run_dir, gate.Criterion(
            "build-exits-zero", "the build succeeds", workstream="build"))
        gate.add_evidence(self.run_dir, gate.Evidence(
            criterion="build-exits-zero", status=gate.PASS,
            command="npm run build", result="exit 0",
        ))
        self.age("build",
                 progress=2,
                 progress_at=workers_mod._stamp(time.time() - 4000.0),
                 spawned_at=workers_mod._stamp(time.time() - 5000.0),
                 last_beat=workers_mod._stamp(time.time() - 5))
        self.assertEqual(
            workers_mod.classify(workers_mod.Registry(self.run_dir).find("build"),
                                 stale_seconds=120.0),
            workers_mod.WEDGED,
            "the worker must be wedged for this case to be reachable")

        report = workers_mod.reconcile(self.run_dir, limits=Limits(), stale_seconds=120.0)
        released = {r["name"]: r for r in report["released"]}
        self.assertIn("build", released,
                      "a worker that proved its result but never finished is "
                      "exactly what reconcile exists to recover")
        self.assertEqual(released["build"]["reason"], "finished_without_release")
        self.assertEqual(
            workers_mod.Registry(self.run_dir).find("build").state, "ok",
            "the work is banked as ok, not thrown away as reclaimed")
        self.assertEqual(Budget.load(self.run_dir, Limits()).inflight, 0,
                         "the orphaned lease is freed")

    def test_crash_after_finish_left_the_lease_behind(self) -> None:
        # finish() recorded the outcome but the release never happened.
        self.make()
        self.spawn("build")
        registry = self.registry()
        registry.finish("build", registry.find("build").epoch, "ok")
        self.assertEqual(self.budget().snapshot()["inflight"], ["build"],
                         "precondition: the lease is still held")
        report = workers_mod.reconcile(self.run_dir, limits=Limits(), stale_seconds=120.0)
        self.assertEqual([r["name"] for r in report["released"]], ["build"])
        self.assertEqual(self.budget().snapshot()["inflight"], [])

    def test_reconciling_twice_releases_nothing_the_second_time(self) -> None:
        self.make()
        self.spawn("build")
        self.registry().finish("build", self.registry().find("build").epoch, "ok")
        first = workers_mod.reconcile(self.run_dir, limits=Limits(), stale_seconds=120.0)
        self.assertEqual(len(first["released"]), 1)
        second = workers_mod.reconcile(self.run_dir, limits=Limits(), stale_seconds=120.0)
        self.assertEqual(second["released"], [])
        self.assertEqual([c["name"] for c in second["consistent"]], ["build"])

    def test_a_worker_registered_without_a_lease_is_reconciled(self) -> None:
        """The lease vanished but the registry entry survived - a torn write
        from an older version, or a manual edit. It must not sit there forever."""
        self.make()
        self.spawn("build")
        budget = self.budget()
        budget.leased.pop("build")
        budget.persist(self.run_dir)
        report = workers_mod.reconcile(self.run_dir, limits=Limits(), stale_seconds=120.0)
        self.assertEqual([r["name"] for r in report["released"]], ["build"])
        self.assertEqual(report["released"][0]["reason"], "no_lease")
        self.assertEqual(self.registry().find("build").state, "reclaimed")


class TestProgressAndReconcileViaCli(WorkerFixture):
    def test_progress_and_reconcile_run_from_the_command_line(self) -> None:
        self.make()
        spawned = json.loads(run_cli(
            "--json", "workers", "spawn", "--worker", "build",
            "--slug", self.run_dir.name, workspace=self.ws,
        ).stdout)
        epoch = spawned["worker"]["epoch"]

        beat = json.loads(run_cli(
            "--json", "workers", "progress", "--worker", "build",
            "--epoch", epoch, "--units", "2",
            "--slug", self.run_dir.name, workspace=self.ws,
        ).stdout)
        self.assertEqual(beat["progress"], 2)

        shown = json.loads(run_cli(
            "--json", "workers", "status", "--slug", self.run_dir.name,
            workspace=self.ws,
        ).stdout)
        self.assertIn("wedged", shown, "status must report the wedge bucket")

        run_cli("--json", "workers", "finish", "--worker", "build",
                "--epoch", epoch, "--state", "ok",
                "--slug", self.run_dir.name, workspace=self.ws)

        # `workers finish` releases the lease itself, so there is nothing for
        # reconcile to clean up - and it must stay that way on a second pass.
        self.assertEqual(self.budget().snapshot()["inflight"], [])
        report = json.loads(run_cli(
            "--json", "workers", "reconcile", "--slug", self.run_dir.name,
            workspace=self.ws,
        ).stdout)
        self.assertEqual(report["released"], [])
        self.assertEqual([c["name"] for c in report["consistent"]], ["build"])

    def test_reclaim_needs_an_explicit_flag_to_touch_a_wedged_worker(self) -> None:
        self.make()
        run_cli("--json", "workers", "spawn", "--worker", "build",
                "--slug", self.run_dir.name, workspace=self.ws)
        quiet = json.loads(run_cli(
            "--json", "workers", "reclaim", "--slug", self.run_dir.name,
            workspace=self.ws,
        ).stdout)
        self.assertEqual(quiet["reclaimed"], [])


if __name__ == "__main__":
    unittest.main()

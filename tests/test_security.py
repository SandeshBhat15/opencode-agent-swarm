"""Regression tests for the defects a read-only security review reproduced.

Every test here corresponds to a finding that was verified by running the
offending code first. They are grouped by the boundary they defend: locking,
path confinement, argv handling, and the registry's unlocked write path.
"""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path

from swarmkit import gate, isolator, store, workers as workers_mod
from swarmkit.budget import COUNTERS_LOCK, Budget, Limits
from swarmkit.errors import ScopeConflict, SwarmError
from swarmkit.store import file_lock, init_run
from tests.support import GitRepo, TempWorkspace, run_cli


class TestLockingIsActuallyExclusive(unittest.TestCase):
    """A lock that can be evicted while its holder is alive is not a lock.

    Measured before the fix: with stale_after=0.5s, A entered at t=0, B stole
    at t=1.0 (both inside simultaneously), A's `finally` unlinked B's lock at
    t=2.0, and C entered at t=2.5 while B was still running. Two critical
    sections overlapped, which is enough to lose a budget increment.
    """

    def setUp(self) -> None:
        import tempfile

        self.dir = Path(tempfile.mkdtemp(prefix="swarm-lock-"))
        self.lock = self.dir / "test.lock"

    def test_a_live_holder_is_never_evicted_however_old_its_lock(self) -> None:
        """mtime alone said "stale"; the pid check is what makes takeover safe."""
        with file_lock(self.lock, timeout=0.0, stale_after=0.05):
            # Make the lock look ancient without waiting for it.
            old = time.time() - 3600
            os.utime(self.lock, (old, old))
            # A second acquisition must fail rather than steal it: the holder
            # is this very process, and it is demonstrably alive.
            with self.assertRaises(SwarmError) as ctx:
                with file_lock(self.lock, timeout=0.0, stale_after=0.05):
                    pass
            self.assertEqual(ctx.exception.code, "LOCK_HELD")
            self.assertTrue(self.lock.exists(), "the live lock must survive")

    def test_an_abandoned_lock_is_still_taken_over(self) -> None:
        """The pid check must not make locks unrecoverable."""
        # No pid= line at all: unreadable, so the age criterion decides.
        self.lock.write_text("garbage\n", encoding="utf-8")
        old = time.time() - 3600
        os.utime(self.lock, (old, old))
        with file_lock(self.lock, timeout=0.0, stale_after=0.05):
            self.assertTrue(self.lock.exists())

    def test_releasing_unlinks_only_the_lock_it_created(self) -> None:
        """The ABA guard: if the lock was replaced while we held it, the
        replacement must survive our exit."""
        with file_lock(self.lock, timeout=0.0, stale_after=10_000):
            replacement = self.dir / "someone-else.lock"
            replacement.write_text("pid=1 at=x\n", encoding="utf-8")
            os.replace(replacement, self.lock)  # simulate a takeover
        self.assertTrue(
            self.lock.exists(),
            "the original holder deleted the NEW holder's lock (ABA)",
        )

    def test_recover_cannot_steal_a_live_lock(self) -> None:
        """`recover` used to unlink the lock and journal with no lock held, so
        running it during a mutation destroyed the evidence require_clean needs."""
        import tempfile

        rundir = init_run(Path(tempfile.mkdtemp(prefix="swarm-rec-")), "r")
        live = rundir / store.LOCK_FILE
        live.write_text(f"pid={os.getpid()} at=x\n", encoding="utf-8")
        store.atomic_write_text(store.journal(rundir), '{"op": "x"}')
        with self.assertRaises(SwarmError):
            store.recover(rundir, "should not have got the lock")
        self.assertTrue(
            store.journal(rundir).exists(),
            "recover deleted a live holder's journal",
        )


class TestRunDirConfinement(TempWorkspace):

    def test_a_slug_cannot_resolve_outside_the_run_root(self) -> None:
        """`--slug ../../elsewhere` resolved out of .opencode/swarm when the
        target happened to hold a state.json, and the next mutation wrote one
        there. Nothing checked containment."""
        outside = self.ws / "elsewhere"
        outside.mkdir()
        (outside / store.STATE_FILE).write_text('{"slug": "elsewhere", "phase": "INTAKE"}')
        with self.assertRaises(SwarmError) as ctx:
            store.find_run(self.ws, "..%s..%selsewhere" % (os.sep, os.sep))
        self.assertEqual(ctx.exception.code, "RUN_OUTSIDE_ROOT")

    def test_a_normal_run_still_resolves(self) -> None:
        run_dir = store.init_run(self.ws, "fine")
        self.assertEqual(store.find_run(self.ws, "fine"), run_dir)

    def test_the_run_dir_is_gitignored(self) -> None:
        """evidence.jsonl holds raw command output verbatim and can contain
        secrets; the run dir must not be stageable."""
        store.init_run(self.ws, "leaky")
        ignore = store.runs_root(self.ws) / ".gitignore"
        self.assertTrue(ignore.exists(), "run root has no .gitignore")
        self.assertEqual(ignore.read_text(encoding="utf-8").strip(), "*")


class TestAtomicWriteIsDurableAndReadable(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.dir = Path(tempfile.mkdtemp(prefix="swarm-atomic-"))

    def test_state_files_are_not_owner_only(self) -> None:
        """mkstemp creates 0600. Run state is not a secret, and owner-only
        files break a shared checkout or a CI cache running as another user."""
        target = self.dir / "state.json"
        store.atomic_write_text(target, "{}\n")
        mode = target.stat().st_mode & 0o777
        self.assertEqual(
            mode & 0o044, 0o044, f"group/other cannot read: {mode:o}"
        )

    def test_no_temp_file_is_left_behind(self) -> None:
        store.atomic_write_text(self.dir / "x.json", "{}\n")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["x.json"])


class TestScopeEntryRefusesAbsolutePaths(unittest.TestCase):
    def test_absolute_posix_is_refused(self) -> None:
        with self.assertRaises(ScopeConflict) as ctx:
            isolator.validate_entry("/etc/passwd")
        self.assertEqual(ctx.exception.code, "SCOPE_ESCAPE")

    def test_absolute_windows_is_refused(self) -> None:
        with self.assertRaises(ScopeConflict) as ctx:
            isolator.validate_entry("C:/Windows")
        self.assertEqual(ctx.exception.code, "SCOPE_ESCAPE")

    def test_relative_paths_still_pass(self) -> None:
        isolator.validate_entry("src/build/**")
        isolator.validate_entry("vite.config.ts")


class TestBadGlobIsRefusedNotCrashed(TempWorkspace):
    def test_an_unparseable_character_class_is_a_refusal(self) -> None:
        """`x[].py` used to reach re.compile and raise re.PatternError out of
        cmd_scope_plan as an uncaught traceback, because main() only catches
        SwarmError.

        Asserted against `glob_to_regex` directly. `overlap_kind` short-circuits
        to `definite` for these patterns and never compiles them, so testing
        through it would pass whether or not the guard existed.
        """
        for pattern in ("x[].py", "x[z-a].py", "x[!].py"):
            with self.subTest(pattern=pattern):
                with self.assertRaises(ScopeConflict) as ctx:
                    isolator.glob_to_regex(pattern)
                self.assertEqual(ctx.exception.code, "BAD_PATTERN")

    def test_a_well_formed_character_class_still_compiles(self) -> None:
        """The guard must not swallow valid patterns."""
        isolator.glob_to_regex("x[a-z].py")
        isolator.glob_to_regex("src/**")

    def test_the_cli_never_leaks_a_regex_traceback(self) -> None:
        """Whatever the verdict, a bad glob must come back as an error code."""
        proc = run_cli("--json", "scope", "plan",
                       "--ws", "a=x[].py", "--ws", "b=y.ts",
                       workspace=self.ws, expect=1)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn(json.loads(proc.stdout)["error"]["code"],
                      {"BAD_PATTERN", "SCOPE_CONFLICT"})


class TestIncludeWedgedFlagReachesReclaim(TempWorkspace):
    """The flag existed in the parser and was never forwarded: `iso`/`workers
    reclaim --include-wedged` exited 0 and reclaimed nothing while telling the
    operator the worker was deliberately spared."""

    def setUp(self) -> None:
        super().setUp()
        self.run_dir = init_run(self.ws, "w")

    def _wedged_worker(self, name: str = "builder") -> None:
        workers_mod.spawn(self.run_dir, name, role="builder", timeout_s=100_000.0)
        registry = workers_mod.Registry(self.run_dir)
        current = registry.find(name)
        beat = time.time() - 5
        spawned = time.time() - 5000.0
        workers = registry.load()
        workers[name] = workers_mod.Worker(**{
            **current.to_dict(),
            # Beating, but no forward progress for ages -> wedged.
            "progress": 3,
            "progress_at": workers_mod._stamp(time.time() - 4000.0),
            "spawned_at": workers_mod._stamp(spawned),
            "last_beat": workers_mod._stamp(beat),
        })
        registry.workers = workers
        registry.save(workers)
        self.assertEqual(
            workers_mod.classify(registry.find(name), stale_seconds=120.0),
            workers_mod.WEDGED,
        )

    def test_the_flag_reaches_the_reclaim_call(self) -> None:
        self._wedged_worker()
        proc = run_cli("--json", "workers", "reclaim", "--include-wedged",
                       "--slug", "w", workspace=self.ws)
        import json

        payload = json.loads(proc.stdout)
        self.assertEqual(
            [r["name"] for r in payload["reclaimed"]], ["builder"],
            "the flag was parsed but never forwarded to reclaim()",
        )

    def test_without_the_flag_a_wedged_worker_is_left_alone(self) -> None:
        self._wedged_worker()
        proc = run_cli("--json", "workers", "reclaim", "--slug", "w",
                       workspace=self.ws)
        import json

        self.assertEqual(json.loads(proc.stdout)["reclaimed"], [])

    def test_a_reclaimed_wedged_worker_is_not_recorded_as_a_timeout(self) -> None:
        """It never exceeded timeout_s. Recording 'timeout' made status lie
        permanently, because classify() short-circuits on a terminal state."""
        self._wedged_worker()
        run_cli("--json", "workers", "reclaim", "--include-wedged",
                "--slug", "w", workspace=self.ws)
        record = workers_mod.Registry(self.run_dir).find("builder")
        self.assertEqual(record.state, "reclaimed")
        self.assertIn("progress", record.note)
        self.assertNotIn("timeout_s", record.note)


class TestReconcileJoinsEvidenceOnWorkstream(TempWorkspace):
    """`_evidence_for` keyed by criterion id while reconcile looked the value
    up by WORKER name, so the headline case — "did its job, died before
    tidying up" — could never fire. `finished_without_release` was dead code."""

    def setUp(self) -> None:
        super().setUp()
        self.run_dir = init_run(self.ws, "r")

    def test_evidence_is_keyed_by_workstream(self) -> None:
        # Criterion id and workstream deliberately differ.
        gate.add_criterion(self.run_dir, gate.Criterion(
            id="ui-snapshot-matches", text="screenshot differs", workstream="ui"))
        gate.add_evidence(self.run_dir, gate.Evidence(
            criterion="ui-snapshot-matches", status=gate.PASS,
            command="pytest -k snapshot", result="1 passed"))
        seen = workers_mod._evidence_for(self.run_dir)
        self.assertEqual(seen.get("ui"), 1)
        self.assertNotIn(
            "ui-snapshot-matches", seen,
            "keying by criterion id is what made the join miss",
        )

    def test_a_wedged_worker_with_evidence_is_reconciled_as_finished(self) -> None:
        gate.add_criterion(self.run_dir, gate.Criterion(
            id="shot-differs", text="screenshot differs", workstream="ui"))
        gate.add_evidence(self.run_dir, gate.Evidence(
            criterion="shot-differs", status=gate.PASS,
            command="pytest -k shot", result="1 passed"))
        workers_mod.spawn(self.run_dir, "ui", timeout_s=100_000.0)
        registry = workers_mod.Registry(self.run_dir)
        current = registry.find("ui")
        workers = registry.load()
        workers["ui"] = workers_mod.Worker(**{
            **current.to_dict(),
            "progress": 2,
            "progress_at": workers_mod._stamp(time.time() - 4000.0),
            "spawned_at": workers_mod._stamp(time.time() - 5000.0),
            "last_beat": workers_mod._stamp(time.time() - 5),
        })
        registry.workers = workers
        registry.save(workers)
        self.assertEqual(
            workers_mod.classify(registry.find("ui"), stale_seconds=120.0),
            workers_mod.WEDGED,
        )
        report = workers_mod.reconcile(self.run_dir, limits=Limits())
        self.assertEqual([r["name"] for r in report["released"]], ["ui"])
        self.assertEqual(report["released"][0]["reason"], "finished_without_release")
        self.assertEqual(Budget.load(self.run_dir, Limits()).inflight, 0)

    def test_reconcile_is_idempotent(self) -> None:
        # A worker has to be genuinely reclaimable before the first pass has
        # anything to release. A fresh, healthy worker is *consistent* and must
        # be left alone — reconcile releasing it would be the bug, not the fix.
        gate.add_criterion(self.run_dir, gate.Criterion(
            id="ghost-artifact", text="the ghost produced its artifact",
            workstream="ghost"))
        gate.add_evidence(self.run_dir, gate.Evidence(
            criterion="ghost-artifact", status=gate.PASS,
            command="make", result="ok"))
        workers_mod.spawn(self.run_dir, "ghost", timeout_s=100_000.0)
        registry = workers_mod.Registry(self.run_dir)
        current = registry.find("ghost")
        workers = registry.load()
        workers["ghost"] = workers_mod.Worker(**{
            **current.to_dict(),
            "progress": 2,
            "progress_at": workers_mod._stamp(time.time() - 4000.0),
            "spawned_at": workers_mod._stamp(time.time() - 5000.0),
            "last_beat": workers_mod._stamp(time.time() - 5),
        })
        registry.workers = workers
        registry.save(workers)

        first = workers_mod.reconcile(self.run_dir, limits=Limits())
        self.assertEqual([r["name"] for r in first["released"]], ["ghost"])
        second = workers_mod.reconcile(self.run_dir, limits=Limits())
        self.assertEqual(second["released"], [], "reconcile is not idempotent")
        self.assertEqual(Budget.load(self.run_dir, Limits()).inflight, 0)


class TestProgressIsLocked(TempWorkspace):
    """`workers progress` did load->mutate->save with no lock at all. Because
    Registry.save rewrites the whole file from its snapshot, that lost its own
    increment AND reverted concurrent changes — which can resurrect a finished
    worker as `running` and defeat the duplicate-execution guard."""

    def setUp(self) -> None:
        super().setUp()
        self.run_dir = init_run(self.ws, "p")

    def test_progress_across_many_processes_is_not_lost(self) -> None:
        """Thread-level, through the library, so the count is deterministic."""
        import threading

        workers_mod.spawn(self.run_dir, "build")
        epoch = workers_mod.Registry(self.run_dir).find("build").epoch
        errors: list[BaseException] = []

        def bump() -> None:
            try:
                # Registry.progress takes the bounded-wait COUNTERS_LOCK
                # itself. Going through store.mutation here would be wrong:
                # that lock is fail-fast by design and would refuse the
                # heartbeat of every worker but the first.
                workers_mod.Registry(self.run_dir).progress("build", epoch)
            except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
                errors.append(exc)

        threads = [threading.Thread(target=bump) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(
            workers_mod.Registry(self.run_dir).find("build").progress, 8,
            "progress increments were lost to an unlocked read-modify-write",
        )


class TestBaseRefCannotBecomeAGitOption(GitRepo):
    """argv injection, not shell injection — and just as real. `--base
    '--output=/tmp/PWNED'` became the argv element "--output=/tmp/PWNED...HEAD",
    git wrote a file at the attacker's path and returned an empty diff."""

    def test_a_dash_leading_base_is_not_an_option(self) -> None:
        trees = isolator.create_worktrees(self.ws, self.ws / ".trees", ["build"])
        wt = trees[0].path
        (wt / "f.txt").write_text("x\n", encoding="utf-8")
        from tests.support import git

        git("add", "-A", cwd=wt)
        git("commit", "-m", "c", cwd=wt)
        target = self.ws.parent / "PWNED"
        # The fix is `--end-of-options`, so git refuses the bogus revspec
        # outright. Asserting the refusal is stronger than asserting an empty
        # patch: an empty patch could also mean "nothing to diff".
        with self.assertRaises(SwarmError):
            isolator.worktree_patch(self.ws, wt, base=f"--output={target}")
        self.assertFalse(
            target.exists(),
            "git parsed --base as an option and wrote to the attacker's path",
        )


class TestStatusSummaryIncludesWedged(TempWorkspace):
    def test_the_human_summary_is_not_optimistically_empty(self) -> None:
        """The per-row line showed the worker, but the summary an operator
        scans said everything was fine."""
        run_dir = init_run(self.ws, "s")
        workers_mod.spawn(run_dir, "ui", timeout_s=100_000.0)
        registry = workers_mod.Registry(run_dir)
        current = registry.find("ui")
        workers = registry.load()
        workers["ui"] = workers_mod.Worker(**{
            **current.to_dict(),
            "progress": 1,
            "progress_at": workers_mod._stamp(time.time() - 4000.0),
            "spawned_at": workers_mod._stamp(time.time() - 5000.0),
            "last_beat": workers_mod._stamp(time.time() - 5),
        })
        registry.workers = workers
        registry.save(workers)
        proc = run_cli("workers", "status", "--slug", "s", workspace=self.ws)
        self.assertIn("wedged=['ui']", proc.stdout)


if __name__ == "__main__":
    unittest.main()
"""State store: atomic writes, torn-line recovery, journals, locks, resume."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from swarmkit import phases, store
from swarmkit.errors import LockError, RecoveryRequired, SwarmError
from tests.support import TempWorkspace


class TestInitAndLayout(TempWorkspace):
    def test_init_creates_full_layout(self) -> None:
        run = store.init_run(self.ws, "my-run")
        for name in (
            store.STATE_FILE,
            store.STATUS_FILE,
            store.HISTORY_FILE,
            store.REPORTS_FILE,
            store.WORKSTREAMS_FILE,
            store.PLAN_FILE,
            store.RETRO_FILE,
            store.CRITERIA_FILE,
            store.EVIDENCE_FILE,
        ):
            with self.subTest(name=name):
                self.assertTrue((run / name).exists(), f"{name} missing from run dir")

    def test_init_refuses_to_clobber_an_existing_run(self) -> None:
        """Re-init must never silently reset a run's state."""
        first = store.init_run(self.ws, "dup")
        st = store.RunState.load(first)
        st.phase = phases.MESH
        st.save(first)
        with self.assertRaises(SwarmError) as ctx:
            store.init_run(self.ws, "dup")
        self.assertEqual(ctx.exception.code, "RUN_EXISTS")
        self.assertEqual(store.RunState.load(first).phase, phases.MESH)

    def test_state_starts_at_intake(self) -> None:
        run = store.init_run(self.ws, "s")
        self.assertEqual(store.RunState.load(run).phase, phases.INTAKE)

    def test_find_run_and_list(self) -> None:
        store.init_run(self.ws, "a")
        store.init_run(self.ws, "b")
        self.assertEqual(store.find_run(self.ws, "a").name, "a")
        self.assertEqual(len(store.list_runs(self.ws)), 2)
        self.assertIsNotNone(store.find_latest_run(self.ws))

    def test_missing_run_raises(self) -> None:
        store.init_run(self.ws, "a")
        with self.assertRaises(SwarmError):
            store.find_run(self.ws, "nope")


class TestResumePrefersUnfinished(TempWorkspace):
    def test_latest_prefers_unfinished(self) -> None:
        """A resumed session must not land on a finished run."""
        done = store.init_run(self.ws, "done-run")
        store.init_run(self.ws, "live-run")
        st = store.RunState.load(done)
        st.phase = phases.DELIVERED
        st.save(done)
        self.assertEqual(store.find_latest_run(self.ws).name, "live-run")


class TestAtomicityAndTornReads(TempWorkspace):
    def test_atomic_write_replaces_wholesale(self) -> None:
        target = self.ws / "f.txt"
        store.atomic_write_text(target, "first")
        store.atomic_write_text(target, "second-longer")
        self.assertEqual(target.read_text(encoding="utf-8"), "second-longer")

    def test_atomic_write_leaves_no_temp_files(self) -> None:
        target = self.ws / "sub"
        target.mkdir()
        store.atomic_write_text(target / "f.txt", "x")
        self.assertEqual([p.name for p in target.iterdir()], ["f.txt"])

    def test_read_jsonl_skips_torn_trailing_line(self) -> None:
        """A kill mid-append must not make the run unreadable."""
        path = self.ws / "evidence.jsonl"
        store.append_jsonl(path, {"criterion": "a", "status": "pass"})
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"criterion": "b", "status": "fa')  # torn write
        records = store.read_jsonl(path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["criterion"], "a")

    def test_read_jsonl_missing_file(self) -> None:
        self.assertEqual(store.read_jsonl(self.ws / "nope.jsonl"), [])

    def test_read_json_missing_returns_default(self) -> None:
        self.assertIsNone(store.read_json(self.ws / "nope.json"))
        self.assertEqual(store.read_json(self.ws / "nope.json", default=[]), [])

    def test_runstate_roundtrip(self) -> None:
        run = self.make_run()
        st = store.RunState.load(run)
        st.phase = phases.MESH
        st.fix_cycles = 1
        st.save(run)
        again = store.RunState.load(run)
        self.assertEqual(again.phase, phases.MESH)
        self.assertEqual(again.fix_cycles, 1)
        self.assertFalse(again.is_finished())

    def test_is_finished(self) -> None:
        run = self.make_run()
        st = store.RunState.load(run)
        st.phase = phases.DELIVERED
        st.save(run)
        self.assertTrue(store.RunState.load(run).is_finished())


class TestJournalAndRecovery(TempWorkspace):
    def test_mutation_writes_and_clears_journal(self) -> None:
        run = self.make_run()
        with store.mutation(run, "phase", "PLAN"):
            self.assertIsNotNone(store.pending_journal(run))
        self.assertIsNone(store.pending_journal(run))

    def test_interrupted_mutation_leaves_recoverable_journal(self) -> None:
        """Simulates the process dying inside a mutation."""
        run = self.make_run()
        try:
            with store.mutation(run, "phase", "PLAN"):
                raise KeyboardInterrupt("power cut")
        except KeyboardInterrupt:
            pass
        pending = store.pending_journal(run)
        self.assertIsNotNone(pending)
        self.assertIn("phase", json.dumps(pending))

    def test_refusal_does_not_leave_a_journal(self) -> None:
        """A SwarmError refusal is handled, not an interruption.

        The delivery gate refusing to open is an expected outcome that commits
        nothing. Leaving a journal there would make the next command demand a
        manual `swarm recover`, breaking the normal QC-reject -> FIX -> QC loop.
        """
        run = self.make_run()
        with self.assertRaises(SwarmError):
            with store.mutation(run, "phase", "DELIVERED"):
                raise SwarmError("delivery gate closed")
        self.assertIsNone(store.pending_journal(run))
        store.require_clean(run)  # must not raise

    def test_run_is_retryable_after_a_refusal(self) -> None:
        """A refusal leaves nothing behind that blocks the corrected retry."""
        run = self.make_run()
        for _ in range(3):
            with self.assertRaises(SwarmError):
                with store.mutation(run, "phase", "DELIVERED"):
                    raise SwarmError("delivery gate closed")
        with store.mutation(run, "phase", "PLAN"):
            state = store.RunState.load(run)
            state.phase = "PLAN"
            state.save(run)
        self.assertEqual(store.RunState.load(run).phase, "PLAN")

    def test_unexpected_error_still_leaves_the_journal(self) -> None:
        """Only known refusals auto-clear; a real fault must stay recoverable."""
        run = self.make_run()
        with self.assertRaises(ZeroDivisionError):
            with store.mutation(run, "integrate", "reports.md"):
                1 / 0
        self.assertIsNotNone(store.pending_journal(run))
        with self.assertRaises(RecoveryRequired):
            store.require_clean(run)

    def test_require_clean_refuses_after_interruption(self) -> None:
        run = self.make_run()
        try:
            with store.mutation(run, "integrate", "reports.md"):
                raise KeyboardInterrupt("power cut")
        except KeyboardInterrupt:
            pass
        with self.assertRaises(RecoveryRequired) as ctx:
            store.require_clean(run)
        self.assertEqual(ctx.exception.code, "RECOVERY_REQUIRED")
        self.assertIn("swarm recover", str(ctx.exception))

    def test_recover_clears_journal_and_logs_it(self) -> None:
        run = self.make_run()
        try:
            with store.mutation(run, "phase", "PLAN"):
                raise KeyboardInterrupt("power cut")
        except KeyboardInterrupt:
            pass
        result = store.recover(run, "power cut during PLAN")
        self.assertIsNone(store.pending_journal(run))
        self.assertTrue(result["ok"])
        self.assertEqual(result["note"], "power cut during PLAN")
        self.assertEqual(result["cleared"].get("op"), "phase")
        history = (run / store.HISTORY_FILE).read_text(encoding="utf-8")
        self.assertIn("power cut", history)

    def test_clean_run_needs_no_recovery(self) -> None:
        run = self.make_run()
        store.require_clean(run)  # must not raise


class TestLocking(TempWorkspace):
    def test_second_mutation_is_refused(self) -> None:
        run = self.make_run()
        with store.mutation(run, "phase", "PLAN"):
            with self.assertRaises(LockError):
                with store.mutation(run, "phase", "MESH"):
                    pass

    def test_lock_released_after_mutation(self) -> None:
        run = self.make_run()
        with store.mutation(run, "phase", "PLAN"):
            pass
        with store.mutation(run, "phase", "MESH"):
            pass  # must not raise

    def test_lock_is_released_even_after_a_crash(self) -> None:
        """A dead process must not hold the lock, but must still be noticed."""
        run = self.make_run()
        try:
            with store.mutation(run, "phase", "PLAN"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertFalse((run / store.LOCK_FILE).exists())
        # Not a LockError — the journal is the signal, and it survived the crash.
        with self.assertRaises(RecoveryRequired):
            with store.mutation(run, "phase", "MESH"):
                pass
        # Deterministic resume: recover, then the same transition succeeds.
        store.recover(run, "reconciled after test crash")
        with store.mutation(run, "phase", "MESH"):
            pass


class TestSlugs(unittest.TestCase):
    def test_slugify_is_filesystem_safe(self) -> None:
        slug = store.slugify("Fix the StÜff: BUILD!! /web-ar")
        self.assertNotIn("/", slug)
        self.assertNotIn(":", slug)
        self.assertNotIn(" ", slug)
        self.assertTrue(slug)

    def test_slugify_falls_back_when_empty(self) -> None:
        self.assertTrue(store.slugify("///"))

    def test_utcnow_is_iso(self) -> None:
        stamp = store.utcnow()
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T")
        self.assertTrue(store.now_hhmm())


if __name__ == "__main__":
    unittest.main()
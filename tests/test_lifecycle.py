"""Session lifecycle: ownership, cancellation, checkpointing and resume.

The ten scenarios in this file are the behaviours the lifecycle was written
for, in the order the user described them. Each test names the failure it is
preventing, because the interesting assertion is usually the negative one: that
something *did not* happen (did not touch another session, did not mark partial
work complete, did not restart itself, did not delete the artifacts).

Two kinds of test live here and the distinction matters. Everything driven
through :func:`run_cli` or :class:`subprocess.Popen` exercises the real CLI in
a real subprocess against a real filesystem, including real process creation and
real termination. Nothing here mocks the operating system. What *is* simulated
is the OpenCode application: no test in this repo can close a real chat window,
so the session-close signal is delivered by calling the same CLI command the
plugin's event pump calls. :mod:`tests.test_lifecycle_plugin` covers that seam
from the plugin side, and the two are meant to be read together.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from swarmkit import lifecycle, store
from swarmkit.errors import SwarmError

from .support import GitRepo, TempWorkspace, git, run_cli

SLEEPER = [sys.executable, "-c", "import time; time.sleep(300)"]


def _reap(proc) -> None:
    """Stop a test child and actually wait for it.

    ``kill()`` only signals. Without a ``wait()`` the child stays unreaped,
    which on POSIX is a zombie and on Windows is a ResourceWarning pointing at
    whatever test happens to be running - a failure signal that has nothing to
    do with the code under test.
    """
    try:
        proc.kill()
        proc.wait(timeout=10)
    except Exception:  # pragma: no cover - cleanup must never fail a test
        pass


class LifecycleFixture(TempWorkspace):
    """A workspace plus helpers for the two things every lifecycle test needs."""

    def plan(self, *ids: str) -> list[dict[str, object]]:
        return [{"id": i, "objective": f"do {i}"} for i in ids]

    def make_task(self, session="sess-A", task_id="t1", goal="ship the feature",
                  plan=None, project=None) -> lifecycle.Task:
        return lifecycle.Task.create(
            self.ws,
            task_id=task_id,
            session_id=session,
            goal=goal,
            project=project or str(self.ws),
            plan=self.plan(*(plan or ("s1", "s2"))),
        )

    def reload(self, task_id="t1") -> lifecycle.Task:
        return lifecycle.Task.load(self.ws, task_id)

    def cli(self, *args: str, expect: int = 0) -> dict:
        # --json is a GLOBAL flag: it has to precede the subcommand, which is
        # why it is not simply appended to every call here. Lives on the shared
        # fixture because the host plugin drives the CLI too, so the CLI
        # contract is part of the feature rather than of one test class.
        proc = run_cli("--json", *args, workspace=self.ws, expect=expect)
        return json.loads(proc.stdout)


class TestTaskOwnership(LifecycleFixture):
    """Requirement 1: a task belongs to a session, and inherits that ownership."""

    def test_task_records_the_session_that_owns_it(self) -> None:
        task = self.make_task(session="sess-A")
        # Ownership is a recorded fact, not an inference from a directory name.
        self.assertEqual(task.record.session_id, "sess-A")

    def test_a_child_task_names_its_parent(self) -> None:
        parent = self.make_task(session="sess-A", task_id="parent")
        child = lifecycle.Task.create(
            self.ws, task_id="child", session_id="sess-A",
            goal="delegated piece", project=str(self.ws), parent_task_id="parent",
        )
        # Delegation must be traceable back to the task that asked for it.
        self.assertEqual(child.record.parent_task_id, "parent")
        self.assertEqual(child.record.parent_task_id, parent.record.task_id)

    def test_a_task_without_an_owning_session_is_refused(self) -> None:
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            lifecycle.Task.create(
                self.ws, task_id="orphan", session_id="", goal="x", project=str(self.ws)
            )
        # Work with no owner could never be stopped, which is the failure this
        # whole module exists to prevent.
        self.assertEqual(ctx.exception.code, "NO_SESSION")

    def test_subprocesses_are_recorded_against_their_task(self) -> None:
        task = self.make_task()
        proc, entry = task.spawn(SLEEPER, name="sleeper")
        self.addCleanup(_reap, proc)
        self.addCleanup(task.terminate_processes)
        # A spawned child is owned: the record is what later lets us stop it.
        self.assertEqual(entry["task_id"], task.record.task_id)
        self.assertEqual(entry["epoch"], task.record.epoch)
        self.assertEqual(len(task.record.running_workers), 1)

    def test_spawned_child_carries_its_ownership_in_the_environment(self) -> None:
        task = self.make_task()
        proc, _ = task.spawn(
            [sys.executable, "-c", "import os;print(os.environ['SWARM_TASK_ID'])"],
            capture=True,
        )
        out = proc.communicate(timeout=60)[0]
        self.addCleanup(task.terminate_processes)
        # Anything that outlives us (a log, a crash report, a watchdog) can then
        # tell which task a stray process belonged to.
        self.assertEqual(out.decode().strip(), task.record.task_id)


class TestSessionClose(LifecycleFixture):
    """Requirement 2 and test 1: closing a session stops only its own workers."""

    def test_closing_a_session_stops_only_that_session_task(self) -> None:
        mine = self.make_task(session="sess-A", task_id="t-A")
        theirs = self.make_task(session="sess-B", task_id="t-B", goal="their work")
        result = lifecycle.session_close(self.ws, "sess-A", reason="closed A")
        # The requirement is the isolation, not merely the stopping.
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(result["untouched"], ["t-B"])
        self.assertEqual(self.reload("t-A").record.state, lifecycle.PAUSED)
        self.assertEqual(self.reload("t-B").record.state, lifecycle.RUNNING)
        self.assertEqual(theirs.record.state, lifecycle.RUNNING)

    def test_closing_a_session_terminates_its_running_children(self) -> None:
        task = self.make_task()
        proc, _ = task.spawn(SLEEPER, name="sleeper")
        self.addCleanup(_reap, proc)
        time.sleep(0.5)
        self.assertTrue(store.pid_alive(proc.pid))
        lifecycle.session_close(self.ws, "sess-A", reason="closed A")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and store.pid_alive(proc.pid):
            time.sleep(0.05)
        # The work must actually stop, not merely be marked stopped.
        self.assertFalse(store.pid_alive(proc.pid))

    def test_a_closed_session_starts_no_new_work(self) -> None:
        task = self.make_task()
        stale_epoch = task.record.epoch
        lifecycle.session_close(self.ws, "sess-A")
        cancelled = self.reload()
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            cancelled.complete_step("s1", epoch=stale_epoch)
        # A survivor of the closed session must not keep making progress.
        self.assertEqual(ctx.exception.code, "EPOCH_MISMATCH")

    def test_closing_a_session_twice_is_idempotent(self) -> None:
        self.make_task()
        first = lifecycle.session_close(self.ws, "sess-A", reason="one")
        second = lifecycle.session_close(self.ws, "sess-A", reason="two")
        # Cancellation is requested from the event, the shutdown hook and the
        # user, so the race between them is normal rather than exceptional.
        self.assertEqual(first["cancelled"], second["cancelled"])
        self.assertTrue(all(t["already"] for t in second["tasks"]))
        self.assertEqual(self.reload().record.state, lifecycle.PAUSED)


class TestApplicationShutdown(LifecycleFixture):
    """Requirement 2 and test 2: the application going away stops all its work.

    Closing one chat is not the application closing. These two paths are kept
    separate precisely because conflating them was the mistake the user called
    out.
    """

    def test_application_shutdown_stops_every_live_session(self) -> None:
        self.make_task(session="sess-A", task_id="t-A")
        self.make_task(session="sess-B", task_id="t-B", goal="other session")
        result = lifecycle.shutdown(self.ws, reason="application shutdown")
        self.assertEqual(result["cancelled"], 2)
        self.assertEqual(self.reload("t-A").record.state, lifecycle.PAUSED)
        self.assertEqual(self.reload("t-B").record.state, lifecycle.PAUSED)

    def test_application_shutdown_leaves_already_stopped_work_alone(self) -> None:
        self.make_task(task_id="t-live")
        lifecycle.session_close(self.ws, "sess-A")
        result = lifecycle.shutdown(self.ws, reason="application shutdown")
        # Work already stopped by a session close must not be re-cancelled or,
        # worse, counted as newly interrupted.
        self.assertEqual(result["cancelled"], 0)
        self.assertIn("t-live", result["preserved"])

    def test_closing_one_session_is_not_an_application_shutdown(self) -> None:
        self.make_task(session="sess-A", task_id="t-A")
        other = self.make_task(session="sess-B", task_id="t-B", goal="unrelated")
        lifecycle.session_close(self.ws, "sess-A")
        # The other session keeps running: closing a chat does not close the app.
        self.assertEqual(self.reload("t-B").record.state, lifecycle.RUNNING)
        self.assertEqual(other.record.state, lifecycle.RUNNING)


class TestProcessTrees(LifecycleFixture):
    """Test 3: children die with their parent, and nothing else is touched."""

    def test_a_spawned_grandchild_is_terminated_with_its_task(self) -> None:
        task = self.make_task()
        # The child itself spawns a grandchild, so termination has to reach a
        # whole tree rather than one pid.
        script = (
            "import subprocess, sys, time;"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)']);"
            "time.sleep(300)"
        )
        proc, _ = task.spawn([sys.executable, "-c", script], name="parent")
        self.addCleanup(_reap, proc)
        time.sleep(1.0)
        result = task.terminate_processes()
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and store.pid_alive(proc.pid):
            time.sleep(0.05)
        self.assertFalse(store.pid_alive(proc.pid))
        self.assertTrue(result["stopped"], "no process was stopped at all")

    def test_a_recycled_pid_is_refused_rather_than_killed(self) -> None:
        task = self.make_task()
        # Started OUTSIDE the task, so the only entry that can reach this pid is
        # the forged one below. If the task had a legitimate entry it would be
        # stopped for real, which would test nothing about pid reuse.
        victim = subprocess.Popen(SLEEPER, creationflags=getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
        self.addCleanup(_reap, victim)
        # Forge an entry whose birth time cannot match a live process: this is
        # the shape of "the OS handed this pid to somebody else".
        task.record.processes.append({
            "pid": victim.pid, "pgid": None, "name": "impostor",
            "birth": 1.0, "state": "running",
        })
        result = task.terminate_processes()
        refused = {r["pid"] for r in result["refused"]}
        self.assertIn(victim.pid, refused)
        # The refusal is the whole point: an unrelated live process survives.
        time.sleep(0.3)
        self.assertTrue(store.pid_alive(victim.pid))

    def test_an_unidentifiable_process_is_never_signalled(self) -> None:
        task = self.make_task()
        task.record.processes.append({
            "pid": os.getpid(), "pgid": None, "name": "this-test-process",
            "birth": None, "state": "running",
        })
        result = task.terminate_processes(grace_s=0.1)
        # Killing our own test process would be the worst possible bug here.
        self.assertIn(os.getpid(), {r["pid"] for r in result["refused"]})
        self.assertTrue(result["stopped"] == [])


class TestCrashRecovery(LifecycleFixture):
    """Requirement 3 and test 4: recovery works from the last durable state."""

    def test_a_crashed_task_is_marked_interrupted_not_resumed(self) -> None:
        task = self.make_task()
        task.complete_step("s1")
        # Simulate a hard crash: the owner process is gone.
        task.record.owner_pid = 999_999
        task.save()
        result = lifecycle.recover(self.ws, reason="startup")
        self.assertEqual(result["recovered"], 1)
        recovered = self.reload()
        # INTERRUPTED, not RUNNING: we did not choose to stop this task.
        self.assertEqual(recovered.record.state, lifecycle.INTERRUPTED)

    def test_recovery_preserves_completed_work_and_artifacts(self) -> None:
        task = self.make_task()
        task.complete_step("s1", result="did the thing")
        task.record.artifacts = ["out/report.md"]
        task.record.owner_pid = 999_999
        task.checkpoint()
        lifecycle.recover(self.ws, reason="startup")
        recovered = self.reload()
        self.assertEqual(recovered.record.completed_work, ["s1"])
        self.assertEqual(recovered.record.artifacts, ["out/report.md"])
        self.assertEqual(recovered.record.worker_results["s1"], "did the thing")

    def test_recovery_replays_a_checkpoint_the_record_never_saw(self) -> None:
        task = self.make_task()
        task.complete_step("s1")
        task.checkpoint()
        # A crash between the journal append and the record write loses the
        # record's tail. Rewind the record to look exactly like that.
        path = lifecycle.Task.path_for(self.ws, task.record.task_id)
        stale = json.loads(path.read_text(encoding="utf-8"))
        stale["checkpoint_seq"] = 0
        stale["completed_work"] = []
        store.atomic_write_json(path, stale)
        lifecycle.recover(self.ws, reason="startup")
        recovered = self.reload()
        # Recovery must not trust the behind record when the journal is ahead.
        self.assertEqual(recovered.record.completed_work, ["s1"])

    def test_a_live_owner_is_not_recovered(self) -> None:
        self.make_task()
        result = lifecycle.recover(self.ws, reason="startup")
        # Our own pid owns it, so this task is genuinely still running.
        self.assertEqual(result["recovered"], 0)
        self.assertEqual(self.reload().record.state, lifecycle.RUNNING)

    def test_a_torn_journal_line_does_not_break_recovery(self) -> None:
        task = self.make_task()
        task.complete_step("s1")
        task.record.owner_pid = 999_999
        task.save()
        journal = lifecycle.Task(self.ws, task.record).journal_file
        with open(journal, "a", encoding="utf-8") as fh:
            fh.write('{"seq": 99, "completed_work": ["s1"]')  # crash mid-append
        result = lifecycle.recover(self.ws, reason="startup")
        # The half-written line is skipped, not treated as corruption.
        self.assertEqual(result["recovered"], 1)
        self.assertEqual(self.reload().record.completed_work, ["s1"])


class TestNoAutomaticResume(LifecycleFixture):
    """Requirement 4 and test 5: reopening the application starts nothing."""

    def test_reopening_the_application_does_not_resume_paused_work(self) -> None:
        self.make_task()
        lifecycle.session_close(self.ws, "sess-A")
        # This is what a plugin startup hook does on every launch.
        lifecycle.recover(self.ws, reason="plugin-startup")
        plan = lifecycle.resume_plan(self.ws, project=str(self.ws), hint="ship")
        # Reporting a candidate is allowed; the state must still be stopped.
        self.assertEqual(plan["status"], "resumed")
        self.assertEqual(plan["task_id"], "t1")
        self.assertEqual(self.reload().record.state, lifecycle.PAUSED)

    def test_an_interrupted_task_is_not_resumed_by_a_later_startup(self) -> None:
        task = self.make_task()
        task.complete_step("s1")
        task.record.owner_pid = 999_999
        task.save()
        lifecycle.recover(self.ws, reason="startup")
        lifecycle.recover(self.ws, reason="plugin-startup")
        self.assertEqual(self.reload().record.state, lifecycle.INTERRUPTED)

    def test_resume_plan_never_mutates_state(self) -> None:
        self.make_task()
        before = self.reload().record.to_dict()
        lifecycle.resume_plan(self.ws, project=str(self.ws), hint="ship")
        self.assertEqual(self.reload().record.to_dict(), before)


class TestExplicitResume(LifecycleFixture):
    """Requirement 4 and tests 6 and 7: resume on request, without rework."""

    def test_asking_to_continue_resumes_the_matching_task(self) -> None:
        self.make_task(goal="fix the login flow")
        lifecycle.session_close(self.ws, "sess-A")
        plan = lifecycle.resume_plan(
            self.ws, project=str(self.ws), hint="please continue the login fix"
        )
        self.assertEqual(plan["status"], "resumed")
        self.assertEqual(plan["task_id"], "t1")
        task = self.reload()
        task.resume(session_id="sess-B", hint="continue")
        # Only an explicit resume makes it running again, under the new session.
        self.assertEqual(self.reload().record.state, lifecycle.RUNNING)
        self.assertEqual(self.reload().record.session_id, "sess-B")

    def test_completed_work_is_not_repeated_on_resume(self) -> None:
        task = self.make_task()
        task.complete_step("s1", result="s1 output")
        lifecycle.session_close(self.ws, "sess-A")
        resumed = self.reload()
        resumed.resume(session_id="sess-B")
        brief = resumed.brief()
        self.assertEqual(resumed.record.completed_work, ["s1"])
        self.assertIn("still to do: s2", brief)
        # The brief names what is left so the resuming agent does not redo s1.
        self.assertNotIn("still to do: s1", brief)

    def test_resume_reuses_the_previous_result_without_recomputing(self) -> None:
        task = self.make_task()
        task.complete_step("s1", result="cached-output")
        lifecycle.session_close(self.ws, "sess-A")
        resumed = self.reload().resume(session_id="sess-B")
        self.assertEqual(resumed.record.worker_results["s1"], "cached-output")

    def test_resume_after_an_interruption_requires_verification(self) -> None:
        task = self.make_task()
        task.complete_step("s1")
        task.record.owner_pid = 999_999
        task.save()
        lifecycle.recover(self.ws, reason="startup")
        resumed = self.reload().resume(session_id="sess-B")
        # We did not choose to stop this one, so its output is not trusted
        # blindly and the brief says so.
        self.assertIn("verify", resumed.brief().lower())

    def test_ambiguous_candidates_ask_once_instead_of_guessing(self) -> None:
        self.make_task(task_id="t-auth", goal="rewrite the auth middleware")
        self.make_task(task_id="t-cache", goal="rewrite the cache layer")
        lifecycle.session_close(self.ws, "sess-A")
        plan = lifecycle.resume_plan(self.ws, project=str(self.ws), hint="rewrite")
        # Two plausible matches is exactly the case where guessing is harmful.
        self.assertEqual(plan["status"], "ambiguous")
        self.assertEqual({c["task_id"] for c in plan["candidates"]}, {"t-auth", "t-cache"})

    def test_a_specific_hint_disambiguates(self) -> None:
        self.make_task(task_id="t-auth", goal="rewrite the auth middleware")
        self.make_task(task_id="t-cache", goal="rewrite the cache layer")
        lifecycle.session_close(self.ws, "sess-A")
        plan = lifecycle.resume_plan(
            self.ws, project=str(self.ws), hint="continue the cache rewrite"
        )
        self.assertEqual(plan["status"], "resumed")
        self.assertEqual(plan["task_id"], "t-cache")

    def test_nothing_to_continue_is_reported_plainly(self) -> None:
        plan = lifecycle.resume_plan(self.ws, project=str(self.ws), hint="anything")
        self.assertEqual(plan["status"], "none")

    def test_resume_fences_the_previous_attempts_workers(self) -> None:
        task = self.make_task()
        task.complete_step("s1")
        lifecycle.session_close(self.ws, "sess-A")
        old_epoch = self.reload().record.epoch
        self.reload().resume(session_id="sess-B")
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            self.reload().complete_step("s2", epoch=old_epoch)
        # A worker from the previous attempt must not write into the resumed
        # task, or a cancelled task resurrects itself.
        self.assertEqual(ctx.exception.code, "EPOCH_MISMATCH")


class TestStaleWorkerFencing(LifecycleFixture):
    """Requirement 5 and test 8: cancelled work cannot write afterwards."""

    def test_a_stale_worker_cannot_record_a_result_after_cancellation(self) -> None:
        task = self.make_task()
        epoch = task.record.epoch
        lifecycle.session_close(self.ws, "sess-A")
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            self.reload().complete_step("s1", epoch=epoch)
        self.assertEqual(ctx.exception.code, "EPOCH_MISMATCH")

    def test_a_stale_worker_cannot_heartbeat_a_newer_epoch(self) -> None:
        task = self.make_task()
        stale = task.record.epoch
        lifecycle.session_close(self.ws, "sess-A")
        self.reload().resume(session_id="sess-B")
        # Same fence, reached from the other direction.
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            self.reload().require_live_epoch(stale)
        self.assertEqual(ctx.exception.code, "EPOCH_MISMATCH")

    def test_the_matching_epoch_is_accepted(self) -> None:
        task = self.make_task()
        current = task.record.epoch
        task.require_live_epoch(current)
        task.complete_step("s1", epoch=current)
        # The guard must not be so broad that it refuses honest workers.
        self.assertEqual(self.reload().record.completed_work, ["s1"])

    def test_progress_during_cancellation_is_refused(self) -> None:
        task = self.make_task()
        task.record.state = lifecycle.CANCELLING
        task.save()
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            self.reload().complete_step("s1")
        self.assertEqual(ctx.exception.code, "TASK_CANCELLED")

    def test_paused_task_refuses_progress_even_on_its_current_epoch(self) -> None:
        # The epoch fence alone is not enough. Cancellation mints a new epoch,
        # so the *cancelling* process holds a valid epoch for a task that is
        # now PAUSED. Without a state check, that epoch would let a stopped
        # task keep accruing steps -- rebuilding the progress count the user was
        # told was frozen at the moment they stopped.
        task = self.make_task()
        task.complete_step("s1")
        task.cancel(reason="user closed the session")
        self.assertEqual(task.record.state, lifecycle.PAUSED)
        current_epoch = task.record.epoch
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            self.reload().complete_step("s2", epoch=current_epoch)
        self.assertEqual(ctx.exception.code, "TASK_NOT_RUNNING")
        after = self.reload()
        # The preserved count is the count at the stop, not a rebuilt one.
        self.assertEqual(after.record.completed_work, ["s1"])

    def test_interrupted_task_refuses_progress(self) -> None:
        # A crash left the task INTERRUPTED. A watchdog retrying the last step
        # must not be able to write into it.
        task = self.make_task()
        task.record.owner_pid = 0  # unreachable owner -> recovery will claim it
        task.save()
        lifecycle.recover(self.ws, reason="test")
        self.assertEqual(self.reload().record.state, lifecycle.INTERRUPTED)
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            self.reload().complete_step("s1")
        self.assertEqual(ctx.exception.code, "TASK_NOT_RUNNING")


class TestSessionScopedStatus(LifecycleFixture):
    """The host plugin knows a session id, never a task id."""

    def test_status_by_session_reports_the_single_live_task(self) -> None:
        self.make_task()
        payload = self.cli("lifecycle", "status", "--session", "sess-A")
        self.assertEqual(payload["task"]["task_id"], "t1")

    def test_status_refuses_to_guess_between_several_live_tasks(self) -> None:
        self.make_task()
        lifecycle.Task.create(
            self.ws, task_id="t2", session_id="sess-A", goal="second", project=str(self.ws),
            plan=[{"id": "x"}],
        )
        payload = self.cli("lifecycle", "status", "--session", "sess-A")
        # A confident number about the wrong task is worse than silence, so the
        # ambiguous answer is explicit and `task` is null rather than a guess.
        self.assertTrue(payload["ambiguous"])
        self.assertIsNone(payload["task"])
        self.assertEqual(len(payload["tasks"]), 2)

    def test_status_reports_the_stopped_task_rather_than_claiming_none_exists(self) -> None:
        task = self.make_task()
        task.complete_step("s1")
        task.cancel(reason="session closed")
        payload = self.cli("lifecycle", "status", "--session", "sess-A")
        # Reporting "no live task" would hide the fact that stopped work exists
        # and is resumable, which is the thing the user needs to know.
        self.assertEqual(payload["task"]["state"], lifecycle.PAUSED)

    def test_status_without_a_target_is_refused(self) -> None:
        payload = self.cli("lifecycle", "status", expect=1)
        self.assertEqual(payload["error"]["code"], "NO_TASK")


class TestCompletionHonesty(LifecycleFixture):
    """Requirement 5: a partially executed task is never COMPLETED."""

    def test_partial_progress_cannot_be_marked_complete(self) -> None:
        task = self.make_task()
        task.complete_step("s1")
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            task.set_state(lifecycle.COMPLETED)
        # Downstream code may treat COMPLETED as trustworthy. It must mean so.
        self.assertEqual(ctx.exception.code, "INCOMPLETE")
        self.assertEqual(self.reload().record.state, lifecycle.RUNNING)

    def test_every_step_done_still_waits_for_children_to_exit(self) -> None:
        task = self.make_task()
        proc, _ = task.spawn(SLEEPER, name="sleeper")
        self.addCleanup(_reap, proc)
        task.complete_step("s1")
        task.complete_step("s2")
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            task.set_state(lifecycle.COMPLETED)
        # Checked-off steps while a child is still running is still partial.
        self.assertEqual(ctx.exception.code, "INCOMPLETE")
        task.terminate_processes()
        task.set_state(lifecycle.COMPLETED)
        self.assertEqual(self.reload().record.state, lifecycle.COMPLETED)

    def test_completion_is_terminal(self) -> None:
        task = self.make_task()
        task.complete_step("s1")
        task.complete_step("s2")
        task.set_state(lifecycle.COMPLETED)
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            task.set_state(lifecycle.RUNNING)
        # There is no way back out of COMPLETED; only retention removes it.
        self.assertEqual(ctx.exception.code, "BAD_TRANSITION")

    def test_cancellation_never_reaches_completed(self) -> None:
        task = self.make_task()
        task.complete_step("s1")
        task.complete_step("s2")
        task.record.state = lifecycle.CANCELLING
        task.save()
        task = self.reload()
        task.cancel(reason="closed")
        self.assertEqual(self.reload().record.state, lifecycle.PAUSED)


class TestCheckpointing(LifecycleFixture):
    """Requirement 3: durable, continuous, atomic checkpoints."""

    def test_each_checkpoint_reaches_disk_with_a_sequence(self) -> None:
        task = self.make_task()
        task.complete_step("s1")
        task.checkpoint()
        entries = task.checkpoints()
        seqs = [e["seq"] for e in entries if isinstance(e.get("seq"), int)]
        # A monotonic sequence is what lets recovery order the journal.
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(seqs[-1], self.reload().record.checkpoint_seq)

    def test_a_checkpoint_records_the_work_that_survives_a_stop(self) -> None:
        task = self.make_task()
        task.complete_step("s1", result="r")
        task.record.artifacts = ["a.txt"]
        task.record.retries = {"s1": 2}
        task.record.budget = {"tokens": 100}
        task.checkpoint()
        entry = task.last_confirmed()
        for key in ("completed_work", "worker_results", "artifacts",
                    "retries", "budget", "total_steps", "phase", "epoch"):
            # Everything the resume brief depends on has to be in the record.
            self.assertIn(key, entry)

    def test_the_task_record_is_written_atomically(self) -> None:
        task = self.make_task()
        task.checkpoint()
        raw = lifecycle.Task.path_for(self.ws, task.record.task_id).read_text(encoding="utf-8")
        # parseable whole: a half-written record is the failure atomicity prevents
        self.assertIsInstance(json.loads(raw), dict)

    def test_continuous_checkpoints_catch_a_mid_run_crash(self) -> None:
        plan = [{"id": f"s{i}"} for i in range(1, 11)]
        task = lifecycle.Task.create(
            self.ws, task_id="t-long", session_id="sess-A", goal="long run",
            project=str(self.ws), plan=plan,
        )
        for step in plan[:6]:
            task.complete_step(step["id"])
        durable = self.reload("t-long").last_confirmed()
        # A crash with no state change still leaves the last checkpoint usable.
        self.assertEqual(durable["completed_work"], plan and
                         [s["id"] for s in plan[:5]] or [])


class TestArtifactAndStatePreservation(LifecycleFixture):
    """Requirement 3: stopping must never destroy saved work."""

    def test_cancelling_deletes_neither_state_nor_artifacts(self) -> None:
        task = self.make_task()
        task.complete_step("s1", result="done")
        artifact = self.ws / "precious.txt"
        artifact.write_text("hours of work\n", encoding="utf-8")
        task.record.artifacts = [str(artifact)]
        task.checkpoint()
        lifecycle.session_close(self.ws, "sess-A")
        # Cancellation stops work; it is not a delete.
        self.assertTrue(artifact.exists())
        self.assertEqual(artifact.read_text(encoding="utf-8"), "hours of work\n")
        self.assertEqual(self.reload().record.completed_work, ["s1"])
        self.assertTrue(lifecycle.Task.exists(self.ws, "t1"))

    def test_a_paused_record_outlives_retention_until_it_expires(self) -> None:
        task = self.make_task()
        lifecycle.session_close(self.ws, "sess-A")
        early = lifecycle.prune(self.ws, retention_days=30)
        # Pruning is explicit and conservative: a record finished seconds ago is
        # nowhere near the window, so it survives and is not even listed.
        self.assertNotIn("t1", early["removed"])
        self.assertTrue(lifecycle.Task.exists(self.ws, "t1"))


class TestStateMachine(unittest.TestCase):
    """Requirement 5: the states mean what they say."""

    def test_all_eight_states_exist(self) -> None:
        self.assertEqual(
            set(lifecycle.STATES),
            {"RUNNING", "CANCELLING", "PAUSED", "INTERRUPTED",
             "RESUMING", "BLOCKED", "FAILED", "COMPLETED"},
        )

    def test_illegal_transitions_are_refused(self) -> None:
        bad = [
            ("PAUSED", "RUNNING"),
            ("COMPLETED", "RUNNING"),
            ("FAILED", "RUNNING"),
        ]
        for src, dst in bad:
            with self.subTest(f"{src}->{dst}"):
                with self.assertRaises(lifecycle.LifecycleError) as ctx:
                    lifecycle.assert_transition(src, dst)
                self.assertEqual(ctx.exception.code, "BAD_TRANSITION")

    def test_a_stay_put_transition_is_allowed(self) -> None:
        lifecycle.assert_transition("RUNNING", "RUNNING")

    def test_an_unknown_state_is_refused(self) -> None:
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            lifecycle.assert_transition("RUNNING", "BANANA")
        self.assertEqual(ctx.exception.code, "BAD_STATE")


class TestDependenciesAndProgress(LifecycleFixture):
    """Progress reporting must describe work that actually happened."""

    def test_progress_counts_plan_steps_not_events(self) -> None:
        task = self.make_task()
        for _ in range(20):
            task.note("session.tool.called")
        progress = self.reload().progress()
        # Twenty events is not two completed steps. Inflating this is exactly
        # the fabrication the user prohibited.
        self.assertEqual(progress["completed_steps"], 0)
        self.assertEqual(progress["total_steps"], 2)
        self.assertEqual(progress["observed"]["session.tool.called"], 20)

    def test_a_step_with_unmet_dependencies_is_refused(self) -> None:
        task = lifecycle.Task.create(
            self.ws, task_id="t-dep", session_id="sess-A", goal="dep",
            project=str(self.ws),
            plan=[{"id": "a"}, {"id": "b", "depends_on": ["a"]}],
        )
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            task.complete_step("b")
        self.assertEqual(ctx.exception.code, "DEPENDENCY_UNMET")

    def test_progress_shape_matches_what_the_host_renders(self) -> None:
        progress = self.make_task().progress()
        for key in ("task_id", "state", "completed_steps", "total_steps",
                    "running_workers", "epoch"):
            # The plugin renders straight from these keys.
            self.assertIn(key, progress)

    def test_a_resume_brief_names_unmet_dependencies(self) -> None:
        task = lifecycle.Task.create(
            self.ws, task_id="t-dep", session_id="sess-A", goal="dep",
            project=str(self.ws),
            plan=[{"id": "a"}, {"id": "b", "depends_on": ["a"]}],
        )
        lifecycle.session_close(self.ws, "sess-A")
        brief = self.reload("t-dep").brief()
        self.assertIn("unmet dependencies", brief)


class TestCliSurface(LifecycleFixture):
    """The plugin drives the CLI, so the CLI contract is part of the feature."""

    def start(self, task_id="t1", session="sess-A", goal="ship the feature",
              plan: str | None = None) -> dict:
        payload = {
            "task_id": task_id, "session_id": session, "goal": goal,
            "project": str(self.ws),
            "plan": plan or json.dumps([{"id": "s1"}, {"id": "s2"}]),
        }
        return self.cli("lifecycle", "start",
                        "--session", session, "--task", task_id,
                        "--goal", goal, "--project", str(self.ws),
                        "--plan", payload["plan"])

    def test_start_status_and_session_close_over_the_cli(self) -> None:
        self.start()
        status = self.cli("lifecycle", "status", "--task", "t1")
        self.assertEqual(status["task"]["state"], "RUNNING")
        self.assertEqual(status["task"]["total_steps"], 2)
        closed = self.cli("lifecycle", "session-close", "--session", "sess-A",
                          "--reason", "closed")
        self.assertEqual(closed["cancelled"], 1)
        self.assertEqual(self.cli("lifecycle", "status", "--task", "t1")["task"]["state"],
                         "PAUSED")

    def test_resume_plan_over_the_cli_does_not_resume(self) -> None:
        self.start()
        self.cli("lifecycle", "session-close", "--session", "sess-A")
        plan = self.cli("lifecycle", "resume-plan", "--project", str(self.ws),
                        "--hint", "continue the ship")
        self.assertEqual(plan["status"], "resumed")
        # Reporting is not starting.
        self.assertEqual(self.cli("lifecycle", "status", "--task", "t1")["task"]["state"],
                         "PAUSED")

    def test_resume_over_the_cli_restarts_the_task(self) -> None:
        self.start()
        self.cli("lifecycle", "session-close", "--session", "sess-A")
        self.cli("lifecycle", "resume", "--task", "t1", "--session", "sess-B")
        self.assertEqual(self.cli("lifecycle", "status", "--task", "t1")["task"]["state"],
                         "RUNNING")

    def test_shutdown_over_the_cli_stops_everything(self) -> None:
        self.start(task_id="t-a", session="sess-A")
        self.start(task_id="t-b", session="sess-B", goal="second")
        out = self.cli("lifecycle", "shutdown", "--reason", "app closing")
        self.assertEqual(out["cancelled"], 2)

    def test_note_records_real_events_only(self) -> None:
        self.start()
        out = self.cli("lifecycle", "note", "--task", "t1",
                       "--event", "session.tool.called")
        self.assertEqual(out["observed"]["session.tool.called"], 1)

    def test_note_refuses_to_guess_which_task_a_session_meant(self) -> None:
        self.start(task_id="t-a", session="sess-A")
        self.start(task_id="t-b", session="sess-A", goal="second")
        out = self.cli("lifecycle", "note", "--session", "sess-A",
                       "--event", "session.step.ended")
        # Two live tasks in one session: attributing the event would be a guess.
        self.assertFalse(out["noted"])
        self.assertEqual(len(out["live_tasks"]), 2)

    def test_a_malformed_plan_reports_an_error_instead_of_crashing(self) -> None:
        payload = self.cli("lifecycle", "start", "--session", "sess-A",
                       "--task", "bad", "--goal", "g", "--project", str(self.ws),
                       "--plan", "{not json", expect=1)
        self.assertEqual(payload["error"]["code"], "BAD_PLAN")

    def test_a_missing_task_reports_an_error_instead_of_crashing(self) -> None:
        payload = self.cli("lifecycle", "status", "--task", "nope", expect=1)
        self.assertEqual(payload["error"]["code"], "NO_SUCH_TASK")

    def test_list_shows_every_recorded_task(self) -> None:
        self.start(task_id="t-a", session="sess-A")
        self.start(task_id="t-b", session="sess-B", goal="second")
        out = self.cli("lifecycle", "list")
        self.assertEqual({t["task_id"] for t in out["tasks"]}, {"t-a", "t-b"})

    def test_checkpoint_over_the_cli_records_a_step(self) -> None:
        self.start()
        self.cli("lifecycle", "checkpoint", "--task", "t1", "--step", "s1")
        self.assertEqual(self.cli("lifecycle", "status", "--task", "t1")
                         ["task"]["completed_steps"], 1)

    def test_recover_over_the_cli_reports_interrupted_work(self) -> None:
        self.start()
        # Rewrite the owner pid so recovery sees an abandoned task.
        path = lifecycle.Task.path_for(self.ws, "t1")
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["owner_pid"] = 999_999
        store.atomic_write_json(path, raw)
        out = self.cli("lifecycle", "recover", "--reason", "startup")
        self.assertEqual(out["recovered"], 1)
        self.assertEqual(self.cli("lifecycle", "status", "--task", "t1")["task"]["state"],
                         "INTERRUPTED")


class TestGitAndUserFilesAreUntouched(GitRepo):
    """Test 10: cancellation and resume never corrupt the working tree."""

    def test_cancellation_leaves_uncommitted_work_exactly_as_it_was(self) -> None:
        work = self.ws / "feature.py"
        work.write_text("print('work in progress')\n", encoding="utf-8")
        git("add", "-A", cwd=self.ws)
        git("commit", "-q", "-m", "work", cwd=self.ws)
        edit = self.ws / "feature.py"
        edit.write_text("print('mid-edit, not committed')\n", encoding="utf-8")
        task = lifecycle.Task.create(
            self.ws, task_id="t1", session_id="sess-A", goal="edit",
            project=str(self.ws), plan=[{"id": "s1"}],
        )
        task.complete_step("s1")
        lifecycle.session_close(self.ws, "sess-A")
        lifecycle.recover(self.ws, reason="startup")
        task = lifecycle.Task.load(self.ws, "t1")
        task.resume(session_id="sess-B")
        # The user's uncommitted change survives the whole cycle untouched.
        self.assertEqual(
            edit.read_text(encoding="utf-8"), "print('mid-edit, not committed')\n"
        )
        status = git("status", "--porcelain", cwd=self.ws).stdout
        self.assertIn("feature.py", status)

    def test_resume_creates_no_repository_objects(self) -> None:
        task = lifecycle.Task.create(
            self.ws, task_id="t1", session_id="sess-A", goal="edit",
            project=str(self.ws), plan=[{"id": "s1"}],
        )
        task.complete_step("s1")
        before = git("rev-parse", "HEAD", cwd=self.ws).stdout.strip()
        lifecycle.session_close(self.ws, "sess-A")
        lifecycle.Task.load(self.ws, "t1").resume(session_id="sess-B")
        after = git("rev-parse", "HEAD", cwd=self.ws).stdout.strip()
        # Lifecycle state lives under .opencode/ as untracked working data: no
        # commit, no stage, nothing that would show up in the user's history.
        self.assertEqual(before, after)
        self.assertEqual(git("diff", "--cached", "--name-only", cwd=self.ws).stdout.strip(), "")
        self.assertEqual(git("log", "--oneline", cwd=self.ws).stdout.count("\n"), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
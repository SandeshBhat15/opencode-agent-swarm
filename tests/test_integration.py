"""End-to-end orchestration lifecycle against a mock worker adapter.

This exercises the real pipeline - planning, delegation, execution, QC,
failure recovery and the delivery gate - through the same ``swarmkit``
functions and the same CLI the agent pages call. Nothing here opens a
socket or calls a model: :class:`MockWorkerAdapter` stands in for a spawned
agent process, and its outcomes are a declared list rather than a live call.
That is what makes the suite repeatable at zero API cost.

``test_swarmkit_cannot_talk_to_the_network`` is the guard that keeps that
claim honest: it replaces ``socket.socket`` with a raising stub and drives a
full run through it.
"""

from __future__ import annotations

import json
import socket
import tempfile
import time
import unittest
from pathlib import Path

from swarmkit import gate, phases, store, workers as workers_mod
from swarmkit.budget import Budget, Limits
from swarmkit.errors import BudgetExceeded, SwarmError
from tests.support import TempWorkspace, run_cli

OK = "ok"
FAILED = "failed"


class MockWorkerAdapter:
    """Deterministic stand-in for a spawned agent process.

    Every outcome is a name in ``outcomes`` the test declares up front, so the
    lifecycle under test never depends on timing, luck, or a model. The
    adapter owns the epoch of each worker exactly as a real worker would be
    handed one, which is what lets us prove a reclaimed worker cannot come
    back.
    """

    def __init__(self, run_dir: Path, *, limits: Limits | None = None) -> None:
        self.run_dir = Path(run_dir)
        self.limits = limits or Limits()
        self.epochs: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    # -- lifecycle primitives, mirroring the CLI verbs ------------------
    def spawn(self, name: str, *, role: str = "", outcome: str = OK,
              timeout_s: float = 100.0, age: float = 0.0):
        result = workers_mod.spawn(
            self.run_dir, name, role=role, timeout_s=timeout_s,
            limits=self.limits, now=time.time() - age,
        )
        self.epochs[name] = result.worker.epoch
        self.calls.append((name, f"spawn:{outcome}"))
        return result

    def beat(self, name: str) -> None:
        workers_mod.Registry(self.run_dir).heartbeat(name, self.epochs[name])
        self.calls.append((name, "beat"))

    def finish(self, name: str, state: str = OK, note: str = "") -> None:
        workers_mod.Registry(self.run_dir).finish(name, self.epochs[name], state, note)
        budget = Budget.load(self.run_dir, self.limits)
        budget.release(name, run_dir=self.run_dir, epoch=self.epochs[name])
        self.calls.append((name, f"finish:{state}"))

    # -- composite behaviours a real worker would produce ---------------
    def do_work(self, name: str, *, command: str, result: str,
                criterion: str | None = None, role: str = "builder") -> None:
        """A worker that does its job and reports it as evidence."""
        self.spawn(name, role=role)
        self.beat(name)
        if criterion:
            gate.add_evidence(self.run_dir, gate.Evidence(
                criterion=criterion, status=gate.PASS,
                command=command, result=result,
            ))
        self.finish(name, OK)
        self.calls.append((name, f"work:{command}"))

    def fail(self, name: str, *, command: str, result: str,
             criterion: str | None = None, role: str = "builder") -> None:
        """A worker that runs, fails its check, and reports the failure."""
        self.spawn(name, role=role)
        self.beat(name)
        if criterion:
            gate.add_evidence(self.run_dir, gate.Evidence(
                criterion=criterion, status=gate.FAIL,
                command=command, result=result,
            ))
        self.finish(name, FAILED, note=result)

    def crash(self, name: str, *, age: float = 600.0, role: str = "builder") -> None:
        """A worker that dies without ever beating - the silent failure."""
        self.spawn(name, role=role, timeout_s=10_000.0, age=age)
        # No beat, no finish. It simply is not there any more.

    def hang(self, name: str, *, age: float = 300.0, role: str = "builder") -> None:
        """A worker that keeps beating but never finishes - slow, not dead."""
        self.spawn(name, role=role, timeout_s=100.0, age=age)
        self.beat(name)

    def status(self, *, stale_seconds: float = workers_mod.DEFAULT_STALE_SECONDS):
        return workers_mod.Registry(self.run_dir).status(stale_seconds=stale_seconds)

    def reclaim(self, *, stale_seconds: float = workers_mod.DEFAULT_STALE_SECONDS):
        return workers_mod.reclaim(self.run_dir, limits=self.limits,
                                   stale_seconds=stale_seconds)

    def budget(self) -> Budget:
        return Budget.load(self.run_dir, self.limits)


class LifecycleFixture(TempWorkspace):
    """A run already planned, with two disjoint workstreams registered."""

    def setUp(self) -> None:
        super().setUp()
        self.slug = "lifecycle"
        self.run_dir = store.init_run(self.ws, self.slug)
        self.limits = Limits()
        self.adapter = MockWorkerAdapter(self.run_dir, limits=self.limits)

    def plan(self) -> None:
        """PLAN: two disjoint scopes plus their acceptance criteria."""
        gate.add_criterion(self.run_dir, gate.Criterion("build", "build exits 0"))
        gate.add_criterion(self.run_dir, gate.Criterion("tests", "tests pass"))
        run_cli("scope", "plan", "--ws", "build=src/build/**",
                "--ws", "ui=src/ui/**", "--strict", workspace=self.ws)

    def phase(self, target: str, *, expect: int = 0):
        # Always --json: on a refusal the error code lands on stdout, which is
        # the only way a test can assert *which* gate refused rather than just
        # that something refused.
        return run_cli("--json", "run", "phase", target, "--slug", self.slug,
                       workspace=self.ws, expect=expect)

    def show(self) -> dict:
        return json.loads(run_cli("--json", "run", "show", "--slug", self.slug,
                                  workspace=self.ws).stdout)

    def to_mesh(self) -> None:
        """Drive the pipeline to MESH without touching MESH itself."""
        self.plan()
        for target in ("PLAN", "MESH"):
            self.phase(target)


class TestHappyPath(LifecycleFixture):
    """The pipeline every run is supposed to take."""

    def test_two_workstreams_deliver_with_evidence(self) -> None:
        self.to_mesh()
        self.adapter.do_work("build", command="npm run build", result="exit 0",
                             criterion="build")
        self.adapter.do_work("ui", command="npm test", result="12 passed",
                             criterion="tests", role="visual")

        self.assertEqual(self.show()["gate_open"], True,
                         "both criteria now have evidence, so the gate opens")
        self.assertEqual(self.show()["criteria_needing_evidence"], [])

        for target in ("INTEGRATE", "QC", "RETRO", "DELIVERED"):
            self.phase(target)

        final = self.show()
        self.assertEqual(final["phase"], phases.DELIVERED)
        self.assertTrue(final["gate_open"])
        self.assertEqual(final["criteria_needing_evidence"], [])

    def test_both_workers_ran_concurrently(self) -> None:
        self.to_mesh()
        self.adapter.spawn("build")
        self.adapter.spawn("ui")
        budget = self.adapter.budget()
        self.assertEqual(budget.snapshot()["inflight"], ["build", "ui"])
        self.assertLessEqual(budget.inflight, self.limits.max_concurrent_agents)

    def test_the_hang_is_never_reclaimed_because_it_is_still_beating(self) -> None:
        """A slow worker is not a dead worker. This is the distinction the
        audit said did not exist; it must not be bought with lost work."""
        self.to_mesh()
        self.adapter.hang("ui", age=90.0)
        self.assertEqual(self.adapter.status()["slow"], ["ui"])

        report = self.adapter.reclaim()
        self.assertEqual(report["reclaimed"], [])
        self.assertEqual([s["name"] for s in report["skipped"]], ["ui"])
        self.assertIn("ui", self.adapter.budget().snapshot()["inflight"])
        self.assertEqual(workers_mod.Registry(self.run_dir).find("ui").state, "running")


class TestDeliveryGate(LifecycleFixture):
    """Nothing ships unproven."""

    def test_a_failed_check_blocks_delivery(self) -> None:
        self.to_mesh()
        self.adapter.do_work("build", command="npm run build", result="exit 0",
                             criterion="build")
        self.adapter.fail("ui", command="npm test", result="3 failed",
                          criterion="tests")

        # QC -> RETRO -> DELIVERED: RETRO is mandatory before delivery.
        for target in ("INTEGRATE", "QC", "RETRO"):
            self.phase(target)
        self.assertEqual(self.show()["gate_open"], False)

        checked = json.loads(run_cli(
            "--json", "gate", "check", "--slug", self.slug, workspace=self.ws
        ).stdout)
        self.assertEqual(checked["open"], False)
        self.assertEqual(checked["criteria_passed"], 1)
        failed = [r["id"] for r in checked["results"] if not r["passed"]]
        self.assertEqual(failed, ["tests"])

        proc = self.phase("DELIVERED", expect=1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "GATE_CLOSED")
        self.assertEqual(self.show()["phase"], phases.RETRO,
                         "the refused transition must not move the phase")

    def test_delivery_is_refused_while_a_criterion_has_no_evidence(self) -> None:
        self.to_mesh()
        self.adapter.do_work("build", command="npm run build", result="exit 0",
                             criterion="build")
        self.phase("INTEGRATE")
        self.phase("QC")
        self.phase("RETRO")
        self.phase("DELIVERED", expect=1)
        self.assertEqual(self.show()["phase"], phases.RETRO)
        self.assertEqual(self.show()["criteria_needing_evidence"], ["tests"])

    def test_a_qc_failure_drives_a_bounded_fix_cycle(self) -> None:
        self.to_mesh()
        self.adapter.do_work("build", command="npm run build", result="exit 0",
                             criterion="build")
        self.phase("INTEGRATE")
        self.phase("QC")

        # QC found a problem: go back and fix it, then re-check.
        self.phase("FIX")
        self.phase("QC")
        self.assertEqual(self.show()["fix_cycles"], 1)

        # Cycle two is the last the budget allows.
        self.phase("FIX")
        self.phase("QC")
        self.assertEqual(self.show()["fix_cycles"], 2)

        proc = self.phase("FIX", expect=1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "FIX_CYCLE_LIMIT")
        self.assertEqual(self.show()["phase"], phases.QC,
                         "an exhausted budget must not advance the phase")


class TestFailureRecovery(LifecycleFixture):
    """A crashed worker is detected, reclaimed, and retried - exactly once
    within budget, and never duplicated."""

    def test_a_crashed_worker_is_detected_reclaimed_and_retried(self) -> None:
        self.to_mesh()
        self.adapter.do_work("build", command="npm run build", result="exit 0",
                             criterion="build")
        self.adapter.crash("ui")

        report = self.adapter.reclaim()
        self.assertEqual([r["name"] for r in report["reclaimed"]], ["ui"])
        self.assertEqual(report["reclaimed"][0]["liveness"], workers_mod.STALE)
        self.assertNotIn("ui", self.adapter.budget().snapshot()["inflight"],
                         "the dead worker must free its slot")

        # The retry succeeds and finally reports the evidence QC needs.
        self.adapter.spawn("ui")
        self.adapter.beat("ui")
        gate.add_evidence(self.run_dir, gate.Evidence(
            criterion="tests", status=gate.PASS,
            command="npm test", result="12 passed",
        ))
        self.adapter.finish("ui")

        self.assertEqual(self.adapter.status()["alive"], [])
        budget = self.adapter.budget()
        self.assertEqual(budget.retries["ui"], 1,
                         "the retry was charged before the attempt started")

        for target in ("INTEGRATE", "QC", "RETRO", "DELIVERED"):
            self.phase(target)
        self.assertEqual(self.show()["phase"], phases.DELIVERED)

    def test_a_reclaimed_worker_cannot_come_back_and_duplicate_the_work(self) -> None:
        self.to_mesh()
        self.adapter.crash("ui")
        self.adapter.reclaim()
        zombie_epoch = self.adapter.epochs["ui"]

        # A fresh attempt is granted...
        self.adapter.spawn("ui")
        new_epoch = self.adapter.epochs["ui"]
        self.assertNotEqual(zombie_epoch, new_epoch)

        # ...and the zombie, still holding its old epoch, is refused.
        registry = workers_mod.Registry(self.run_dir)
        with self.assertRaises(SwarmError) as ctx:
            registry.heartbeat("ui", zombie_epoch)
        self.assertEqual(ctx.exception.code, "EPOCH_MISMATCH")

        with self.assertRaises(SwarmError) as ctx:
            registry.finish("ui", zombie_epoch, "ok")
        self.assertEqual(ctx.exception.code, "EPOCH_MISMATCH")

        # The live attempt is untouched: still running, still holding the slot.
        self.assertEqual(registry.find("ui").state, "running")
        self.assertIn("ui", self.adapter.budget().snapshot()["inflight"])

    def test_duplicate_execution_is_refused_while_a_worker_is_alive(self) -> None:
        self.to_mesh()
        self.adapter.spawn("build")
        with self.assertRaises(SwarmError) as ctx:
            self.adapter.spawn("build")
        self.assertEqual(ctx.exception.code, "DUPLICATE_WORKER")
        self.assertEqual(self.adapter.budget().snapshot()["inflight"], ["build"])
        self.assertEqual(
            workers_mod.Registry(self.run_dir).find("build").attempt, 1,
            "a refused spawn must not burn an attempt number",
        )

    def test_the_retry_budget_stops_a_repeatedly_failing_worker(self) -> None:
        self.to_mesh()
        worker = "flaky"
        attempts = 0
        for _ in range(10):
            try:
                self.adapter.crash(worker, age=600.0)
            except BudgetExceeded:
                break
            attempts += 1
            self.adapter.reclaim()

        self.assertEqual(attempts, self.limits.max_retries_per_worker + 1,
                         "first attempt plus the retries the budget allows")
        with self.assertRaises(BudgetExceeded) as ctx:
            self.adapter.crash(worker, age=600.0)
        self.assertEqual(ctx.exception.code, "RETRY_LIMIT")


class TestInterruptedRun(LifecycleFixture):
    """A killed process must not strand the run, and resuming must not
    deliver a half-proven artifact."""

    def test_an_interrupted_run_recovers_and_still_delivers(self) -> None:
        self.to_mesh()
        self.adapter.do_work("build", command="npm run build", result="exit 0",
                             criterion="build")

        # Simulate a hard kill: a journal survives and the next command refuses.
        store.journal(self.run_dir).write_text(json.dumps({
            "op": "phase", "target": "INTEGRATE", "at": store.utcnow(),
        }), encoding="utf-8")

        proc = run_cli("--json", "run", "phase", "INTEGRATE", "--slug", self.slug,
                       workspace=self.ws, expect=1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "RECOVERY_REQUIRED")

        run_cli("run", "recover", "--slug", self.slug,
                "--note", "resumed after a kill", workspace=self.ws)

        # State survived, evidence survived, and delivery still requires proof.
        self.phase("INTEGRATE")
        self.phase("QC")
        self.phase("RETRO")
        self.phase("DELIVERED", expect=1)
        self.assertEqual(self.show()["criteria_needing_evidence"], ["tests"])

        gate.add_evidence(self.run_dir, gate.Evidence(
            criterion="tests", status=gate.PASS, command="npm test",
            result="12 passed",
        ))
        self.phase("DELIVERED")
        self.assertEqual(self.show()["phase"], phases.DELIVERED)


class TestBudgetInLifecycle(LifecycleFixture):
    def test_a_worker_beyond_the_concurrency_cap_never_starts(self) -> None:
        self.to_mesh()
        tight = Limits(max_concurrent_agents=2)
        self.adapter.limits = tight
        self.adapter.spawn("a")
        self.adapter.spawn("b")
        with self.assertRaises(BudgetExceeded) as ctx:
            self.adapter.spawn("c")
        self.assertEqual(ctx.exception.code, "CONCURRENCY_LIMIT")
        self.assertEqual(self.adapter.budget().snapshot()["inflight"], ["a", "b"])


class TestNoNetworkOrModelSpend(unittest.TestCase):
    """The reason this suite is free: swarmkit has no way to call anything."""

    def test_swarmkit_cannot_talk_to_the_network(self) -> None:
        import importlib

        for name in ("errors", "phases", "store", "budget", "gate", "isolator",
                     "lessons", "contracts", "workers", "cli"):
            module = importlib.import_module(f"swarmkit.{name}")
            source = Path(module.__file__).read_text(encoding="utf-8")
            for banned in ("import socket", "import urllib", "import http",
                           "import requests", "httpx", "openai"):
                self.assertNotIn(banned, source,
                                 f"swarmkit/{name}.py references {banned}")

    def test_a_full_lifecycle_runs_with_sockets_disabled(self) -> None:
        """Stronger than a grep: break socket creation, then drive a run."""
        original = socket.socket

        def forbidden(*args, **kwargs):
            raise AssertionError("swarmkit attempted to open a socket")

        socket.socket = forbidden  # type: ignore[assignment]
        ws = Path(tempfile.mkdtemp(prefix="swarm-nosock-"))
        try:
            run_dir = store.init_run(ws, "nosock")
            adapter = MockWorkerAdapter(run_dir)
            gate.add_criterion(run_dir, gate.Criterion("build", "build exits 0"))
            adapter.do_work("build", command="npm run build", result="exit 0",
                            criterion="build")
            run_cli("run", "phase", "PLAN", "--slug", "nosock", workspace=ws)
            run_cli("run", "phase", "MESH", "--slug", "nosock", workspace=ws)
            for target in ("INTEGRATE", "QC", "RETRO", "DELIVERED"):
                run_cli("run", "phase", target, "--slug", "nosock", workspace=ws)
            shown = json.loads(run_cli("--json", "run", "show", "--slug", "nosock",
                                      workspace=ws).stdout)
            self.assertEqual(shown["phase"], phases.DELIVERED)
        finally:
            socket.socket = original  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()

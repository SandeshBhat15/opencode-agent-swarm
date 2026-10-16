"""One plain-English request, all the way to delivery.

The acceptance bar for the manager is that one sentence is enough: no manual
agent selection, no workflow commands, no "continue" prompts. This drives
exactly that, against the same ``swarmkit`` functions and the same CLI the
agent pages call.

``MockManagerAgent`` stands in for the spawned agents. It is a declared list of
outcomes, not a live call, so the run costs nothing and is identical every
time. That is also the limit of what this file proves: it proves the
*mechanism* carries a request to delivery, not that a real model does.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from swarmkit import gate, manager, phases, store, workers as workers_mod
from swarmkit.budget import Budget, Limits
from swarmkit.errors import SwarmError
from tests.support import TempWorkspace, run_cli

def _iso(epoch):
    """Epoch seconds -> the timestamp form the registry stores."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


REQUEST = "build me a Vite app that renders a subway runner game with sprite scrolling"


class MockManagerAgent:
    """The manager's counterpart of a spawned OpenCode agent.

    Mirrors what a real worker is handed: a name, an epoch, and a scope. Its
    outcomes are declared up front so the lifecycle never depends on timing.
    """

    def __init__(self, run_dir, limits=None):
        self.run_dir = Path(run_dir)
        self.limits = limits or Limits()

    def spawn(self, name, *, role="", timeout_s=900.0):
        return workers_mod.spawn(self.run_dir, name, role=role,
                                 timeout_s=timeout_s, limits=self.limits)

    def beat(self, name, epoch):
        return workers_mod.Registry(self.run_dir).heartbeat(name, epoch)

    def progress(self, name, epoch, units=1):
        return workers_mod.Registry(self.run_dir).progress(name, epoch, units=units)

    def finish(self, name, epoch, state="ok"):
        registry = workers_mod.Registry(self.run_dir)
        registry.finish(name, epoch, state)
        budget = Budget.load(self.run_dir, self.limits)
        budget.release(name, run_dir=self.run_dir, epoch=epoch)
        budget.persist(self.run_dir)

    def record(self, criterion, status, result, command="pytest -q", worker="", attempt=None):
        gate.add_evidence(self.run_dir, gate.Evidence(
            criterion=criterion, status=status, command=command,
            result=result, worker=worker, attempt=attempt))

    def state(self):
        return manager.lifecycle_state(self.run_dir)


class ManagerFixture(TempWorkspace):
    """A run started from one sentence, the way the manager is meant to."""

    def start(self, request=REQUEST, *, objectives=(), auto_plan=True):
        args = ["run", "init", "--request", request]
        for objective in objectives:
            args += ["--objective", objective]
        if auto_plan:
            args.append("--auto-plan")
        proc = run_cli("--workspace", str(self.ws), *args, workspace=self.ws, expect=0)
        return proc

    def phase(self, target, *, expect=0):
        return run_cli("--json", "--workspace", str(self.ws), "run", "phase", target, expect=expect)


class TestOneRequestStartsTheLifecycle(TempWorkspace):
    def test_a_single_sentence_produces_a_runnable_run(self):
        proc = run_cli("--json", "--workspace", str(self.ws), "run", "init", "--request", REQUEST, "--auto-plan", expect=0)
        payload = json.loads(proc.stdout)
        # Understood and planned, not left as prose.
        self.assertEqual(payload["lane"], "R4")
        self.assertTrue(payload["planned"])
        self.assertTrue(payload["tasks"])
        self.assertIn(manager.LIFECYCLE[0], payload["lifecycle"])
        self.assertIn(manager.LIFECYCLE[-1], payload["lifecycle"])
        # The tasks that can start now are named, so delegation is decided.
        self.assertTrue(payload["parallel_frontier"])

    def test_the_plan_is_written_to_disk_so_a_resume_can_read_it(self):
        run_cli("--workspace", str(self.ws), "run", "init", "--request", REQUEST,
                "--auto-plan", expect=0)
        run_dir = store.find_latest_run(self.ws)
        plan = store.read_json(run_dir / store.PLAN_TASKS_FILE)
        self.assertTrue(plan["tasks"])
        # Every task carries something to prove: a criterion-free task makes
        # the gate unprovable and DELIVERED unreachable.
        for task in plan["tasks"]:
            self.assertTrue(task["criteria"], task)

    def test_a_simple_request_stays_lightweight(self):
        proc = run_cli("run", "triage", "what is the capital of France", expect=0)
        self.assertIn("R1", proc.stdout)

    def test_dependent_tasks_are_not_spawned_before_they_can_run(self):
        nodes = manager.plan_tasks(["write the game loop", "write the score panel",
                                    "wire them together"])
        nodes[2].depends_on = [nodes[0].id, nodes[1].id]
        runnable = {n.id for n in manager.parallel_frontier(nodes)}
        self.assertIn(nodes[0].id, runnable)
        self.assertNotIn(nodes[2].id, runnable)

    def test_a_cyclic_plan_is_refused_rather_than_stalling_forever(self):
        nodes = manager.plan_tasks(["a", "b"])
        nodes[0].depends_on = [nodes[1].id]
        nodes[1].depends_on = [nodes[0].id]
        with self.assertRaises(SwarmError) as ctx:
            manager.write_plan(store.find_latest_run(self.ws) if False else self.ws / ".opencode" / "swarm" / "x", nodes)
        self.assertEqual(ctx.exception.code, "PLAN_CYCLE")


class TestRequestThroughToDelivery(ManagerFixture):
    """The definition of done, driven end to end."""

    def test_a_request_runs_to_a_verified_delivery(self):
        self.start()
        run_dir = store.find_latest_run(self.ws)
        agent = MockManagerAgent(run_dir)
        self.assertTrue(manager.lifecycle_state(run_dir)["unfinished"] is False)

        # UNDERSTAND -> PLAN -> MESH
        for phase in (phases.UNDERSTAND, phases.PLAN, phases.MESH):
            self.phase(phase)

        planned = store.read_json(run_dir / store.PLAN_TASKS_FILE)["tasks"]
        # Read the declared criteria rather than deriving ids: the gate keys
        # on the criterion id, which is generated from the criterion text, not
        # from the task id.
        declared = gate.load_criteria(run_dir)
        self.assertEqual(len(declared), len(planned))
        names = []
        for task, criterion in zip(planned, declared):
            spawned = agent.spawn(task["id"], role=task["role"])
            names.append((task["id"], spawned.worker.epoch, criterion.id))
        self.assertEqual(len(names), len(planned))

        # MONITOR: a heartbeat and real progress, exactly as a live worker does.
        for name, epoch, _ in names:
            agent.beat(name, epoch)
            agent.progress(name, epoch)

        # EXECUTE + VERIFY: each worker proves its own criterion.
        for name, epoch, criterion in names:
            agent.record(criterion, gate.PASS, "exit 0", worker=name, attempt=1)
            agent.finish(name, epoch, "ok")

        self.assertEqual(agent.state()["live_workers"], [])

        # INTEGRATE -> QC -> RETRO
        for phase in (phases.INTEGRATE, phases.QC, phases.RETRO):
            self.phase(phase)

        # DELIVER is gated: the gate must actually be open first.
        self.phase(phases.DELIVERED)
        self.assertEqual(store.RunState.load(run_dir).phase, phases.DELIVERED)
        # And the run really is finished, not just labelled so.
        self.assertFalse(manager.lifecycle_state(run_dir)["unfinished"])

    def test_delivery_is_refused_while_a_criterion_is_unproven(self):
        self.start()
        run_dir = store.find_latest_run(self.ws)
        for phase in (phases.UNDERSTAND, phases.PLAN, phases.MESH, phases.INTEGRATE,
                      phases.QC, phases.RETRO):
            self.phase(phase)
        proc = self.phase(phases.DELIVERED, expect=1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "GATE_CLOSED")
        self.assertNotEqual(store.RunState.load(run_dir).phase, phases.DELIVERED)

    def test_a_failed_check_drives_a_bounded_repair_instead_of_delivering(self):
        self.start()
        run_dir = store.find_latest_run(self.ws)
        agent = MockManagerAgent(run_dir)
        for phase in (phases.UNDERSTAND, phases.PLAN, phases.MESH, phases.INTEGRATE, phases.QC):
            self.phase(phase)

        criterion = gate.load_criteria(run_dir)[0].id
        agent.record(criterion, gate.FAIL, "2 tests failed", worker="ws1", attempt=1)

        # QC fails -> FIX -> the worker redoes it -> QC again.
        self.phase(phases.FIX)
        spawned = agent.spawn("ws1", role="swarm/builder")
        agent.record(criterion, gate.PASS, "exit 0", worker="ws1", attempt=spawned.worker.attempt)
        agent.finish("ws1", spawned.worker.epoch, "ok")
        self.phase(phases.QC)

        self.phase(phases.RETRO)
        self.phase(phases.DELIVERED)
        self.assertEqual(store.RunState.load(run_dir).phase, phases.DELIVERED)

    def test_a_stalled_worker_is_reported_instead_of_waited_on_forever(self):
        self.start()
        run_dir = store.find_latest_run(self.ws)
        agent = MockManagerAgent(run_dir)
        for phase in (phases.UNDERSTAND, phases.PLAN, phases.MESH):
            self.phase(phase)
        agent.spawn("sleeper", role="swarm/builder", timeout_s=10_000.0)
        # A worker spawned with no `now` is fresh and genuinely alive; to model
        # a stall the record has to be aged, not the spawn.
        workers_mod.Registry(run_dir).load()["sleeper"]
        registry = workers_mod.Registry(run_dir)
        workers = registry.load()
        workers["sleeper"] = workers_mod.Worker(
            **{**workers["sleeper"].to_dict(),
               "last_beat": _iso(__import__("time").time() - 9000.0)})
        registry.workers = workers
        registry.save(workers)
        state = agent.state()
        self.assertEqual(state["live_workers"], [])
        self.assertTrue(any("sleeper" in o for o in state["obligations"]), state["obligations"])

    def test_recovery_is_idempotent_so_a_resume_can_retry(self):
        self.start()
        run_dir = store.find_latest_run(self.ws)
        for phase in (phases.UNDERSTAND, phases.PLAN):
            self.phase(phase)
        run_cli("--workspace", str(self.ws), "run", "recover", "--slug", run_dir.name, "--note", "operator restart", expect=0)
        run_cli("--workspace", str(self.ws), "run", "recover", "--slug", run_dir.name, "--note", "operator restart", expect=0)
        self.assertEqual(store.RunState.load(run_dir).phase, phases.PLAN)


class TestApprovalBoundaryInALiveRun(TempWorkspace):
    def test_a_destructive_step_is_refused_but_ordinary_work_is_not(self):
        for action, approved in (("deploy the api to production", False),
                                 ("push the release to the remote", False),
                                 ("run the test suite", True),
                                 ("edit the pagination helper", True)):
            proc = run_cli("--json", "approve", action, expect=0 if approved else 1)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["approved"], approved, action)
            if not approved:
                self.assertEqual(payload["code"], "APPROVAL_REQUIRED", action)

    def test_nothing_is_executed_without_the_operator_saying_yes(self):
        proc = run_cli("--json", "approve", "delete the production database", expect=1)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["approved"])
        self.assertFalse(payload["persisted"])


class TestBudgetStopsARunThatCannotFinish(TempWorkspace):
    def test_spawning_past_the_cap_is_refused(self):
        (self.ws / "swarm.config.json").write_text(
            '{"limits": {"max_concurrent_agents": 1}}', encoding="utf-8")
        run_cli("--workspace", str(self.ws), "run", "init", "--request", REQUEST,
                "--auto-plan", expect=0)
        run_dir = store.find_latest_run(self.ws)
        workers_mod.spawn(run_dir, "one", limits=Limits(max_concurrent_agents=1))
        proc = run_cli("--json", "--workspace", str(self.ws), "workers", "spawn",
                       "--name", "two", expect=1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "CONCURRENCY_LIMIT")


if __name__ == "__main__":
    unittest.main()
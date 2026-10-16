"""Tests for the manager: triage, planning, approvals, lifecycle state.

The failure shape these guard against is the one two audit rounds both found
independently: a capability that exists in code but is never invoked, so it
lints clean and ships dead. Each guard here is mutation-checked - remove the
guard and the corresponding test fails.
"""

import unittest

from swarmkit import budget as budget_mod
from swarmkit import gate, manager, phases, store, workers as workers_mod
from swarmkit.errors import SwarmError
from tests.support import TempWorkspace, run_cli


class TestTriage(unittest.TestCase):
    """Cheap work must stay cheap; real deliverables must not be."""

    def test_a_factual_question_is_answered_directly(self) -> None:
        self.assertEqual(manager.triage("what is the capital of France"), manager.TRIAGE_ANSWER)

    def test_a_single_small_edit_stays_cheap(self) -> None:
        self.assertEqual(
            manager.triage("add a single button to index.html"), manager.TRIAGE_ANSWER
        )

    def test_a_single_file_bugfix_skips_the_ceremony(self) -> None:
        self.assertEqual(
            manager.triage("fix the off-by-one in the pagination helper"), manager.TRIAGE_SINGLE
        )

    def test_an_app_and_a_game_is_more_than_one_deliverable(self) -> None:
        # Regression: this routes to R3 when `_names_several_kinds` is removed.
        # It has no "then"/";" marker, so the conjunction check alone misses it.
        self.assertEqual(
            manager.triage("build me a Vite app that renders a subway runner game with sprite scrolling"),
            manager.TRIAGE_FULL,
        )

    def test_comparative_research_gets_the_full_pipeline(self) -> None:
        self.assertEqual(
            manager.triage("research how three.js compares to babylon.js for a small scene"),
            manager.TRIAGE_FULL,
        )

    def test_an_empty_request_is_refused(self) -> None:
        with self.assertRaises(SwarmError) as ctx:
            manager.triage("   ")
        self.assertEqual(ctx.exception.code, "EMPTY_REQUEST")


class TestPlanning(unittest.TestCase):
    def test_every_objective_becomes_a_task(self) -> None:
        nodes = manager.plan_tasks(["wire the API", "build the UI", "write the docs"])
        self.assertEqual([n.objective for n in nodes],
                         ["wire the API", "build the UI", "write the docs"])

    def test_task_ids_are_unique_and_stable(self) -> None:
        first = manager.plan_tasks(["a", "b"])
        second = manager.plan_tasks(["a", "b"])
        self.assertEqual([n.id for n in first], [n.id for n in second])
        self.assertEqual(len({n.id for n in first}), 2)

    def test_research_reads_get_the_researcher(self) -> None:
        nodes = manager.plan_tasks(["research the three.js docs", "wire the API"])
        self.assertEqual(nodes[0].role, "swarm/researcher")
        self.assertEqual(nodes[1].role, "swarm/builder")

    def test_every_task_carries_a_retry_limit(self) -> None:
        nodes = manager.plan_tasks(["a", "b"], retry_limit=3)
        self.assertTrue(all(n.retries == 3 for n in nodes))

    def test_the_frontier_is_everything_runnable_right_now(self) -> None:
        nodes = manager.plan_tasks(["a", "b", "c"])
        ids = [n.id for n in nodes]
        nodes[2].depends_on = [ids[0]]
        frontier = manager.parallel_frontier(nodes)
        self.assertEqual({n.id for n in frontier}, {ids[0], ids[1]})

    def test_a_dependency_cycle_is_detected_not_hung_on(self) -> None:
        nodes = manager.plan_tasks(["a", "b"])
        nodes[0].depends_on = [nodes[1].id]
        nodes[1].depends_on = [nodes[0].id]
        self.assertTrue(manager.blocked_by_cycles(nodes))


class TestApprovalBoundary(unittest.TestCase):
    """Refuse the consequential, allow the ordinary.

    Refusing everything teaches the user to ignore refusals, which is how a
    real destructive action eventually gets waved through.
    """

    def test_deployment_needs_the_user(self) -> None:
        self.assertFalse(manager.needs_approval("deploy to production").approved)
        self.assertEqual(manager.needs_approval("deploy to production").code, "APPROVAL_REQUIRED")

    def test_ordinary_work_is_not_gated(self) -> None:
        for action in ("run the unit tests", "add a deploy button to the dashboard",
                       "edit the config file", "install the dependencies"):
            with self.subTest(action=action):
                self.assertTrue(manager.needs_approval(action).approved, action)

    def test_mentioning_a_risky_word_is_not_the_same_as_doing_it(self) -> None:
        decision = manager.needs_approval("add a deploy button to the dashboard")
        self.assertTrue(decision.approved)
        self.assertEqual(decision.reason, "ordinary local work")


class TestLifecycleState(TempWorkspace):
    def setUp(self) -> None:
        super().setUp()
        self.run_dir = store.init_run(self.ws, "life")

    def test_a_fresh_run_reports_its_own_phase_and_nothing_outstanding(self) -> None:
        state = manager.lifecycle_state(self.run_dir)
        self.assertEqual(state["phase"], phases.INTAKE)
        # INTAKE is not in UNFINISHED any more: a run is only "unfinished"
        # once it has entered the lifecycle proper, at UNDERSTAND.
        self.assertFalse(state["unfinished"])
        self.assertEqual(state["live_workers"], [])
        # A run with nothing to prove says so, rather than reporting a
        # clean bill of health it has not earned.
        self.assertEqual(state["obligations"],
                         ["no acceptance criteria declared yet"])

    def test_a_stalled_worker_becomes_an_obligation_not_a_silent_wait(self) -> None:
        workers_mod.spawn(self.run_dir, "build", role="builder",
                          timeout_s=10_000.0, now=1000.0 - 600.0)
        state = manager.lifecycle_state(self.run_dir)
        self.assertEqual(state["live_workers"], [])
        self.assertTrue(any("build" in o for o in state["obligations"]), state["obligations"])
        # A stall is the manager's work list, not a block. Only a pending
        # journal stops continuation, because resuming past an unknown
        # half-applied mutation is what corrupts a run.
        self.assertTrue(state["clean_to_continue"])

    def test_an_unproven_criterion_is_an_obligation(self) -> None:
        gate.add_criterion(self.run_dir, gate.Criterion("build", "build exits 0"))
        state = manager.lifecycle_state(self.run_dir)
        self.assertTrue(any("build" in o for o in state["obligations"]), state["obligations"])

    def test_a_healthy_worker_is_reported_live_and_not_an_obligation(self) -> None:
        workers_mod.spawn(self.run_dir, "build", role="builder", timeout_s=900.0)
        state = manager.lifecycle_state(self.run_dir)
        self.assertEqual(state["live_workers"], ["build"])
        self.assertTrue(state["clean_to_continue"])


class TestPlanIsPersisted(TempWorkspace):
    def test_write_plan_lands_a_machine_readable_graph(self) -> None:
        run_dir = store.init_run(self.ws, "planned")
        nodes = manager.plan_tasks(["wire the API", "build the UI"])
        manager.write_plan(run_dir, nodes, goal="ship the thing")
        saved = store.read_json(run_dir / store.PLAN_TASKS_FILE)
        self.assertEqual(saved["goal"], "ship the thing")
        self.assertEqual(len(saved["tasks"]), 2)

    def test_a_cyclic_plan_is_refused_rather_than_written(self) -> None:
        run_dir = store.init_run(self.ws, "cyclic")
        nodes = manager.plan_tasks(["a", "b"])
        nodes[0].depends_on = [nodes[1].id]
        nodes[1].depends_on = [nodes[0].id]
        with self.assertRaises(SwarmError) as ctx:
            manager.write_plan(run_dir, nodes, goal="x")
        self.assertEqual(ctx.exception.code, "PLAN_CYCLE")


class TestManagerViaCli(TempWorkspace):
    """The entry point has to be a command, or nothing invokes it."""

    def test_a_plain_request_starts_a_run_without_any_other_input(self) -> None:
        proc = run_cli("--json", "--workspace", str(self.ws),
                       "run", "init", "--request", "build me a small cli", expect=0)
        import json
        payload = json.loads(proc.stdout)
        self.assertIn(payload["lane"], (manager.TRIAGE_FULL, manager.TRIAGE_SINGLE,
                                        manager.TRIAGE_RESEARCH))
        self.assertEqual(payload["next"], "run phase UNDERSTAND")
        self.assertEqual(payload["lifecycle"], list(manager.LIFECYCLE))

    def test_the_run_is_resumable_by_slug_afterwards(self) -> None:
        import json
        payload = json.loads(run_cli("--json", "--workspace", str(self.ws),
                                     "run", "init", "--request", "build me a small cli",
                                     expect=0).stdout)
        listed = json.loads(run_cli("--json", "--workspace", str(self.ws),
                                    "run", "list", expect=0).stdout)
        self.assertIn(payload["slug"], [r["slug"] for r in listed["runs"]])

    def test_auto_plan_seeds_acceptance_criteria_into_the_gate(self) -> None:
        import json
        payload = json.loads(run_cli("--json", "--workspace", str(self.ws),
                                     "run", "init", "--request", "build a cli",
                                     "--auto-plan", expect=0).stdout)
        run_dir = store.find_run(self.ws, payload["slug"])
        self.assertEqual(len(gate.load_criteria(run_dir)), len(payload["tasks"]))

    def test_state_reports_the_lifecycle_position(self) -> None:
        import json
        payload = json.loads(run_cli("--json", "--workspace", str(self.ws),
                                     "run", "init", "--request", "build a cli", expect=0).stdout)
        state = json.loads(run_cli("--json", "--workspace", str(self.ws),
                                   "run", "state", "--slug", payload["slug"], expect=0).stdout)
        self.assertEqual(state["phase"], phases.INTAKE)
        self.assertIn("obligations", state)

    def test_a_privileged_action_is_refused_with_a_non_zero_exit(self) -> None:
        import json
        proc = run_cli("--json", "--workspace", str(self.ws),
                       "approve", "deploy to production", expect=1)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["approved"])
        self.assertEqual(payload["code"], "APPROVAL_REQUIRED")
        self.assertFalse(payload["persisted"])

    def test_an_ordinary_action_is_allowed(self) -> None:
        import json
        payload = json.loads(run_cli("--json", "--workspace", str(self.ws),
                                     "approve", "run the unit tests", expect=0).stdout)
        self.assertTrue(payload["approved"])


if __name__ == "__main__":
    unittest.main()
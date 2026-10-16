"""CLI behaviour: exit codes, refusal messages, and the end-to-end pipeline."""

from __future__ import annotations

import json
import unittest

from swarmkit import store
from tests.support import run_cli
from tests.support import TempWorkspace


class TestUsage(TempWorkspace):
    def test_phases_table_exits_zero(self) -> None:
        out = run_cli("run", "phases", workspace=self.ws).stdout
        self.assertIn("INTAKE", out)
        self.assertIn("DELIVERED", out)

    def test_lint_of_this_repo_exits_zero(self) -> None:
        proc = run_cli("lint", "--root", str(self.ws.parent), expect=None)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_selftest_exits_zero(self) -> None:
        self.assertIn("selftest ok", run_cli("selftest", workspace=self.ws).stdout)


class TestRunLifecycle(TempWorkspace):
    def test_init_then_show(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        out = run_cli("run", "show", workspace=self.ws, expect=None)
        self.assertIn("INTAKE", out.stdout)

    def test_list_prefers_unfinished(self) -> None:
        run_cli("run", "init", "--slug", "a", workspace=self.ws)
        run_cli("run", "init", "--slug", "b", workspace=self.ws)
        out = run_cli("--json", "run", "list", workspace=self.ws).stdout
        self.assertIn("a", out)
        self.assertIn("b", out)

    def test_phase_moves_forward(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        run_cli("run", "phase", "--to", "PLAN", workspace=self.ws)
        state = store.RunState.load(store.find_run(self.ws, "demo"))
        self.assertEqual(state.phase, "PLAN")

    def test_illegal_transition_exits_nonzero_and_explains(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        proc = run_cli("run", "phase", "--to", "DELIVERED", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ILLEGAL", (proc.stdout + proc.stderr).upper())

    def test_status_is_written_on_phase_change(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        run_cli("run", "phase", "--to", "PLAN", workspace=self.ws)
        status = (store.find_run(self.ws, "demo") / store.STATUS_FILE).read_text(encoding="utf-8")
        self.assertIn("PLAN", status)

    def test_abort_is_allowed_from_anywhere(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        run_cli("run", "phase", "--to", "PLAN", workspace=self.ws)
        proc = run_cli("run", "phase", "--to", "ABORTED", "--reason", "blocked",
                       workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(store.RunState.load(store.find_run(self.ws, "demo")).phase, "ABORTED")


class TestInterruptedRunViaCli(TempWorkspace):
    def test_recover_unblocks_a_stalled_run(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        run = store.find_run(self.ws, "demo")
        # Simulate a process killed mid-mutation.
        store.atomic_write_json(
            store.journal(run), {"op": "phase", "target": "PLAN", "pid": 1, "at": "x"}
        )
        proc = run_cli("run", "phase", "--to", "PLAN", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("RECOVERY_REQUIRED", (proc.stdout + proc.stderr).upper())
        run_cli("run", "recover", "--note", "reconciled by hand", workspace=self.ws)
        run_cli("run", "phase", "--to", "PLAN", workspace=self.ws)  # now works

    def test_recover_requires_a_note(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        proc = run_cli("run", "recover", workspace=self.ws, expect=2)
        self.assertIn("--note", proc.stderr)


class TestGateViaCli(TempWorkspace):
    def setUp(self) -> None:
        super().setUp()
        run_cli("run", "init", "--slug", "gate-demo", workspace=self.ws)

    def test_delivery_blocked_with_no_criteria(self) -> None:
        for phase in ("PLAN", "MESH", "INTEGRATE", "QC", "RETRO"):
            run_cli("run", "phase", "--to", phase, workspace=self.ws)
        proc = run_cli("run", "phase", "--to", "DELIVERED", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("NO_CRITERIA", (proc.stdout + proc.stderr).upper())

    def test_delivery_blocked_when_criterion_unproven(self) -> None:
        for phase in ("PLAN", "MESH", "INTEGRATE", "QC", "RETRO"):
            run_cli("run", "phase", "--to", phase, workspace=self.ws)
        run_cli("criterion", "add", "--id", "build", "--text", "npm run build exits 0",
                workspace=self.ws)
        proc = run_cli("run", "phase", "--to", "DELIVERED", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("GATE_CLOSED", (proc.stdout + proc.stderr).upper())

    def test_delivery_allowed_once_verified(self) -> None:
        for phase in ("PLAN", "MESH", "INTEGRATE", "QC", "RETRO"):
            run_cli("run", "phase", "--to", phase, workspace=self.ws)
        run_cli("criterion", "add", "--id", "build", "--text", "npm run build exits 0",
                workspace=self.ws)
        run_cli("evidence", "add", "--criterion", "build",
                "--command", "npm run build", "--result", "exit 0", workspace=self.ws)
        run_cli("gate", "open", workspace=self.ws)
        run_cli("run", "phase", "--to", "DELIVERED", workspace=self.ws)
        state = store.RunState.load(store.find_latest_run(self.ws))
        self.assertTrue(state.is_finished())
        self.assertEqual(state.phase, "DELIVERED")

    def test_evidence_without_a_source_is_refused(self) -> None:
        run_cli("criterion", "add", "--id", "c", "--text", "t", workspace=self.ws)
        proc = run_cli("evidence", "add", "--criterion", "c", "--result", "looks fine",
                       workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)

    def test_evidence_with_empty_result_is_refused(self) -> None:
        run_cli("criterion", "add", "--id", "c", "--text", "t", workspace=self.ws)
        proc = run_cli("evidence", "add", "--criterion", "c", "--command", "make",
                       workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)

    def test_gate_check_json_is_machine_readable(self) -> None:
        run_cli("criterion", "add", "--id", "c", "--text", "t", workspace=self.ws)
        out = run_cli("--json", "gate", "check", workspace=self.ws, expect=None).stdout
        payload = json.loads(out)
        self.assertIn("results", payload)
        self.assertFalse(payload["open"])


class TestBudgetViaCli(TempWorkspace):
    def test_concurrency_cap_is_enforced_from_the_cli(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        for name in ("a", "b", "c", "d"):
            run_cli("budget", "spawn", "--name", name, workspace=self.ws)
        proc = run_cli("budget", "spawn", "--name", "e", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)

    def test_duplicate_worker_is_refused(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        run_cli("budget", "spawn", "--name", "build", workspace=self.ws)
        proc = run_cli("budget", "spawn", "--name", "build", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)

    def test_tokens_recorded_and_cost_absent_without_pricing(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        run_cli("budget", "tokens", "--in", "1000", "--out", "2000", workspace=self.ws)
        payload = json.loads(run_cli("--json", "budget", "show", workspace=self.ws).stdout)
        self.assertEqual(payload["tokens_in"], 1000)
        self.assertIsNone(payload["estimated_cost_usd"])


class TestScopeViaCli(TempWorkspace):
    def test_disjoint_scopes_pass(self) -> None:
        run_cli("scope", "plan", "--ws", "build=src/a/**", "--ws", "ui=src/b/**",
                "--strict", workspace=self.ws)

    def test_overlapping_scopes_are_refused(self) -> None:
        proc = run_cli("scope", "plan", "--ws", "build=src/a/**", "--ws", "ui=src/a/x.ts",
                       "--strict", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("SCOPE_CONFLICT", (proc.stdout + proc.stderr).upper())

    def test_unbounded_scope_is_refused(self) -> None:
        proc = run_cli("scope", "plan", "--ws", "build=*", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("UNBOUNDED_SCOPE", (proc.stdout + proc.stderr).upper())

    def test_protected_tree_is_refused(self) -> None:
        proc = run_cli("scope", "plan", "--ws", "build=node_modules/**",
                       workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("PROTECTED_SCOPE", (proc.stdout + proc.stderr).upper())


class TestLessonsViaCli(TempWorkspace):
    def setUp(self) -> None:
        super().setUp()
        self.ledger = self.ws / "ledger.md"

    def test_add_without_failure_is_refused(self) -> None:
        # argparse rejects the missing required --failure before the library sees it.
        proc = run_cli("lesson", "add", "--rule", "Never trust exit 0",
                       "--ledger", str(self.ledger), workspace=self.ws, expect=2)
        self.assertIn("--failure", proc.stderr)

    def test_add_then_promote_without_tests_is_refused(self) -> None:
        run_cli("lesson", "add", "--rule", "Never trust exit 0",
                "--failure", "shipped with a red build", "--ledger", str(self.ledger),
                workspace=self.ws)
        proc = run_cli("lesson", "promote", "--rule", "Never trust exit 0",
                       "--evidence", "it sounds right", "--ledger", str(self.ledger),
                       workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("PROMOTION_BLOCKED", (proc.stdout + proc.stderr).upper())

    def test_promote_with_tests_passes(self) -> None:
        run_cli("lesson", "add", "--rule", "Never trust exit 0",
                "--failure", "shipped with a red build", "--ledger", str(self.ledger),
                workspace=self.ws)
        run_cli("lesson", "promote", "--rule", "Never trust exit 0",
                "--evidence", "tests/test_gate.py OK", "--tests-passed",
                "--ledger", str(self.ledger), workspace=self.ws)

    def test_audit_runs(self) -> None:
        run_cli("lesson", "add", "--rule", "Never trust exit 0",
                "--failure", "shipped with a red build", "--ledger", str(self.ledger),
                workspace=self.ws)
        out = run_cli("lesson", "audit", "--ledger", str(self.ledger), workspace=self.ws).stdout
        self.assertIn("lesson(s)", out)
        self.assertIn(f"/{60} lines", out)


if __name__ == "__main__":
    unittest.main()
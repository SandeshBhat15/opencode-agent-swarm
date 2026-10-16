"""Delivery gate: a criterion without evidence can never open the gate."""

from __future__ import annotations

import unittest

from swarmkit import gate as gate_mod
from swarmkit.errors import GateError
from swarmkit.gate import Criterion, Evidence, evaluate, add_criterion, add_evidence
from tests.support import TempWorkspace


def crit(cid: str, text: str = "does the thing") -> Criterion:
    return Criterion(id=cid, text=text)


def ev_pass(cid: str, cmd: str = "npm run build", result: str = "exit 0") -> Evidence:
    return Evidence(criterion=cid, status=gate_mod.PASS, command=cmd, result=result)


class TestCriteria(TempWorkspace):
    def test_add_and_load(self) -> None:
        run = self.make_run()
        add_criterion(run, crit("build"))
        self.assertEqual([c.id for c in gate_mod.load_criteria(run)], ["build"])

    def test_duplicate_id_is_refused(self) -> None:
        run = self.make_run()
        add_criterion(run, crit("build"))
        with self.assertRaises(GateError):
            add_criterion(run, crit("build"))

    def test_blank_id_is_refused(self) -> None:
        run = self.make_run()
        with self.assertRaises(GateError):
            add_criterion(run, crit("  "))


class TestGateClosure(TempWorkspace):
    def test_no_criteria_means_closed(self) -> None:
        run = self.make_run()
        result = evaluate(run)
        self.assertFalse(result.open)
        with self.assertRaises(GateError) as ctx:
            gate_mod.assert_open(run)
        self.assertEqual(ctx.exception.code, "NO_CRITERIA")

    def test_criterion_without_evidence_keeps_gate_closed(self) -> None:
        run = self.make_run()
        add_criterion(run, crit("build"))
        self.assertFalse(evaluate(run).open)

    def test_passing_evidence_opens_gate(self) -> None:
        run = self.make_run()
        add_criterion(run, crit("build"))
        add_evidence(run, ev_pass("build"))
        self.assertTrue(evaluate(run).open)
        gate_mod.assert_open(run)  # must not raise

    def test_failing_evidence_keeps_gate_closed(self) -> None:
        run = self.make_run()
        add_criterion(run, crit("build"))
        add_evidence(run, Evidence(criterion="build", status=gate_mod.FAIL,
                                  command="npm run build", result="exit 1"))
        result = evaluate(run)
        self.assertFalse(result.open)
        with self.assertRaises(GateError) as ctx:
            gate_mod.assert_open(run)
        self.assertEqual(ctx.exception.code, "GATE_CLOSED")

    def test_skip_is_not_a_pass(self) -> None:
        run = self.make_run()
        add_criterion(run, crit("render"))
        add_evidence(run, Evidence(criterion="render", status=gate_mod.SKIP,
                                  result="not run: no GPU"))
        self.assertFalse(evaluate(run).open)

    def test_all_criteria_must_pass(self) -> None:
        run = self.make_run()
        add_criterion(run, crit("build"))
        add_criterion(run, crit("render"))
        add_evidence(run, ev_pass("build"))
        result = evaluate(run)
        self.assertFalse(result.open)
        self.assertEqual(len([r for r in result.results if r.passed]), 1)
        self.assertEqual(len([r for r in result.results if not r.passed]), 1)

    def test_later_failure_overrides_earlier_pass(self) -> None:
        run = self.make_run()
        add_criterion(run, crit("build"))
        add_evidence(run, ev_pass("build"))
        add_evidence(run, Evidence(criterion="build", status=gate_mod.FAIL,
                                  command="npm run build", result="flaky, failed 2nd run"))
        self.assertFalse(evaluate(run).open)

    def test_invalid_status_is_refused(self) -> None:
        run = self.make_run()
        add_criterion(run, crit("build"))
        with self.assertRaises(GateError):
            add_evidence(run, Evidence(criterion="build", status="probably"))

    def test_evidence_for_unknown_criterion_is_flagged_not_fatal(self) -> None:
        run = self.make_run()
        add_criterion(run, crit("build"))
        add_evidence(run, ev_pass("ghost"))
        result = evaluate(run)
        self.assertIn("ghost", result.orphan_evidence)

    def test_render_names_the_failing_criterion(self) -> None:
        run = self.make_run()
        add_criterion(run, crit("build"))
        add_evidence(run, Evidence(criterion="build", status=gate_mod.FAIL, result="boom"))
        text = evaluate(run).render()
        self.assertIn("build", text)
        self.assertIn("boom", text)


if __name__ == "__main__":
    unittest.main()
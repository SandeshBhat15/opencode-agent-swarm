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


class TestSupersededAttemptsCannotOverwrite(TempWorkspace):
    """evidence.jsonl is append-only, so a reclaimed worker can still append
    after its replacement finished. Judged by recency alone the zombie's late
    line wins, which lets a dead attempt overwrite a live one -- or lets a
    dead attempt's PASS mask a live attempt's FAIL."""

    def setUp(self) -> None:
        super().setUp()
        add_criterion(self.ws, crit("c"))

    def test_a_zombie_fail_cannot_overwrite_a_live_pass(self) -> None:
        add_evidence(self.ws, Evidence(criterion="c", status=gate_mod.PASS,
                                       result="attempt 2 ok", worker="w", attempt=2))
        # The dead attempt 1 appends LATER, chronologically newest.
        add_evidence(self.ws, Evidence(criterion="c", status=gate_mod.FAIL,
                                       result="attempt 1 exploded", worker="w", attempt=1))
        self.assertTrue(evaluate(self.ws).open,
                        "the superseded attempt must not decide the gate")

    def test_a_live_fail_is_not_masked_by_an_earlier_pass(self) -> None:
        add_evidence(self.ws, Evidence(criterion="c", status=gate_mod.PASS,
                                       result="attempt 1 ok", worker="w", attempt=1))
        add_evidence(self.ws, Evidence(criterion="c", status=gate_mod.FAIL,
                                       result="attempt 2 broke", worker="w", attempt=2))
        self.assertFalse(evaluate(self.ws).open,
                         "the newest attempt is the one that counts")

    def test_attempt_filtering_is_scoped_to_one_worker(self) -> None:
        """The filter must drop a worker's superseded attempts WITHOUT
        touching another worker's evidence.

        Note what is deliberately NOT asserted: that a later pass from worker
        `a` cancels an earlier fail from worker `b`. That is the pre-existing,
        documented rule -- "the most recent decisive check wins" -- because a
        legitimate re-run has to be able to clear an earlier failure. Two
        workers claiming the same criterion is a planning defect, not
        something the gate should adjudicate.
        """
        add_evidence(self.ws, Evidence(criterion="c", status=gate_mod.FAIL,
                                       result="b attempt 1 broke", worker="b", attempt=1))
        add_evidence(self.ws, Evidence(criterion="c", status=gate_mod.FAIL,
                                       result="a attempt 1 ok-ish", worker="a", attempt=1))
        add_evidence(self.ws, Evidence(criterion="c", status=gate_mod.PASS,
                                       result="a attempt 2 ok", worker="a", attempt=2))
        result = evaluate(self.ws)
        # a's attempt 2 is the last decisive check, so the documented
        # "most recent wins" rule opens the gate. What the fix guarantees is
        # that a's SUPERSEDED attempt 1 line was filtered out before the
        # decision was made.
        self.assertTrue(result.open)
        rendered = result.render()
        self.assertIn("a attempt 2 ok", rendered)
        self.assertNotIn("a attempt 1", rendered)

    def test_unattributed_evidence_is_untouched(self) -> None:
        """No worker means a human, or an older file. It must still count."""
        add_evidence(self.ws, Evidence(criterion="c", status=gate_mod.FAIL,
                                       result="manual check failed"))
        self.assertFalse(evaluate(self.ws).open)

    def test_latest_attempts_ignores_partial_attribution(self) -> None:
        items = [
            Evidence(criterion="c", status=gate_mod.PASS, worker="w", attempt=2),
            Evidence(criterion="c", status=gate_mod.PASS, worker="w"),
            Evidence(criterion="c", status=gate_mod.PASS, worker="x", attempt=1),
        ]
        self.assertEqual(gate_mod.latest_attempts(items), {"w": 2, "x": 1})


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
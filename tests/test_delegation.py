"""Tests for the delegation decision.

Each test pins a behaviour that, if removed, re-opens a specific waste the
swarm was told to eliminate: agents that cannot earn their cost, duplicate
research, repeated summaries, pointless QC, and unbounded retries. The tests
are mutation-minded - delete the guard and the matching test goes red.
"""

import unittest

from swarmkit import delegation, manager
from swarmkit.budget import Limits
from swarmkit.errors import SwarmError


class TestDecide(unittest.TestCase):
    """Whether the manager runs the task or delegates it."""

    @staticmethod
    def _assert_one_sentence(test: unittest.TestCase, reason: str) -> None:
        test.assertTrue(reason.endswith("."), f"reason not a sentence: {reason!r}")
        test.assertNotIn("\n", reason)
        test.assertEqual(reason.count("."), 1, f"reason is not one sentence: {reason!r}")

    def test_a_simple_single_file_task_is_done_directly(self) -> None:
        # The lane R3 shape: one file, no specialist, no parallelism. A worker
        # here would add cost and nothing else.
        signals = delegation.DelegationSignals(
            lane=manager.TRIAGE_SINGLE, independent_workstreams=1
        )
        decision = delegation.decide(signals)
        self.assertFalse(decision.delegate)
        self.assertEqual(decision.code, delegation.CODE_NO_SPECIALIST_VALUE)
        self._assert_one_sentence(self, decision.reason)

    def test_an_answer_lane_request_is_never_delegated(self) -> None:
        # Even with strong benefit signals, an answer-lane request is answered.
        signals = delegation.DelegationSignals(
            lane=manager.TRIAGE_ANSWER,
            specialist_capability=True,
            independent_workstreams=4,
            risk_reduction=True,
        )
        decision = delegation.decide(signals)
        self.assertFalse(decision.delegate)
        self.assertEqual(decision.code, delegation.CODE_ANSWER_DIRECT)

    def test_three_independent_files_delegate_three_ways(self) -> None:
        signals = delegation.DelegationSignals(independent_workstreams=3)
        decision = delegation.decide(signals)
        self.assertTrue(decision.delegate)
        self.assertEqual(decision.code, delegation.CODE_DELEGATE)
        self.assertEqual(decision.worker_count, 3)
        self._assert_one_sentence(self, decision.reason)

    def test_specialist_capability_alone_justifies_delegation(self) -> None:
        signals = delegation.DelegationSignals(specialist_capability=True)
        decision = delegation.decide(signals)
        self.assertTrue(decision.delegate)
        self.assertEqual(decision.worker_count, 1)

    def test_risk_reduction_alone_justifies_delegation(self) -> None:
        signals = delegation.DelegationSignals(risk_reduction=True)
        self.assertTrue(delegation.decide(signals).delegate)

    def test_the_depth_cap_stops_further_delegation(self) -> None:
        policy = delegation.DelegationPolicy(max_depth=2)
        signals = delegation.DelegationSignals(independent_workstreams=3)
        # At the cap even a delegatable task stops nesting.
        decision = delegation.decide(signals, policy=policy, depth=2)
        self.assertFalse(decision.delegate)
        self.assertEqual(decision.code, delegation.CODE_DEPTH_EXCEEDED)
        self._assert_one_sentence(self, decision.reason)

    def test_depth_is_tracked_when_it_delegates(self) -> None:
        signals = delegation.DelegationSignals(specialist_capability=True)
        decision = delegation.decide(signals, depth=0)
        self.assertTrue(decision.delegate)
        self.assertEqual(decision.depth, 1)

    def test_cost_greater_than_benefit_stops_with_a_reason(self) -> None:
        signals = delegation.DelegationSignals(
            specialist_capability=True,  # benefit 0.5
            spawn_cost=0.4,
            context_cost=0.3,
            latency_cost=0.2,
            duplicate_risk=0.2,  # cost 1.1
        )
        decision = delegation.decide(signals)
        self.assertFalse(decision.delegate)
        self.assertEqual(decision.code, delegation.CODE_COST_EXCEEDS_BENEFIT)
        self.assertTrue(decision.reason)
        self._assert_one_sentence(self, decision.reason)

    def test_remaining_work_below_threshold_stops(self) -> None:
        signals = delegation.DelegationSignals(
            specialist_capability=True, remaining_work=0.1
        )
        decision = delegation.decide(signals)
        self.assertFalse(decision.delegate)
        self.assertEqual(decision.code, delegation.CODE_REMAINING_WORK_LOW)

    def test_thresholds_are_configurable(self) -> None:
        # The same signals flip once the threshold is raised: nothing is
        # hard-coded, so a run can loosen or tighten its appetite for fan-out.
        signals = delegation.DelegationSignals(specialist_capability=True, remaining_work=0.5)
        self.assertTrue(delegation.decide(signals).delegate)
        strict = delegation.DelegationPolicy(min_remaining_work=0.9)
        self.assertFalse(delegation.decide(signals, policy=strict).delegate)

    def test_max_net_benefit_margin_is_configurable(self) -> None:
        # Independence of 2 gives benefit 0.35; a 0.5 margin refuses it, a 0.1
        # margin accepts it.
        signals = delegation.DelegationSignals(independent_workstreams=2)
        self.assertTrue(delegation.decide(signals).delegate)
        greedy = delegation.DelegationPolicy(min_net_benefit=0.5)
        self.assertFalse(delegation.decide(signals, policy=greedy).delegate)

    def test_worker_count_never_exceeds_the_policy_cap(self) -> None:
        policy = delegation.DelegationPolicy(max_workers=2)
        signals = delegation.DelegationSignals(independent_workstreams=6)
        decision = delegation.decide(signals, policy=policy)
        self.assertTrue(decision.delegate)
        self.assertEqual(decision.worker_count, 2)

    def test_policy_defaults_track_the_budget_concurrency_cap(self) -> None:
        # Reuses budget.Limits rather than inventing a second number.
        limits = Limits(max_concurrent_agents=3)
        policy = delegation.DelegationPolicy.from_limits(limits)
        self.assertEqual(policy.max_workers, 3)

    def test_benefit_and_cost_scores_are_explicit(self) -> None:
        signals = delegation.DelegationSignals(
            specialist_capability=True, independent_workstreams=3, risk_reduction=True,
            spawn_cost=0.1, context_cost=0.2, latency_cost=0.3, duplicate_risk=0.4,
        )
        self.assertAlmostEqual(delegation.benefit_score(signals), 0.5 + 0.7 + 0.3)
        self.assertAlmostEqual(delegation.cost_score(signals), 1.0)

    def test_decision_serialises(self) -> None:
        decision = delegation.decide(delegation.DelegationSignals(specialist_capability=True))
        as_dict = decision.to_dict()
        self.assertEqual(as_dict["delegate"], True)
        self.assertIn("code", as_dict)


class TestDedupeWorkers(unittest.TestCase):
    """Waste #1 - redundant agents."""

    def test_a_duplicate_worker_for_the_same_objective_is_refused(self) -> None:
        existing = [delegation.WorkerRecord(
            name="b1", role="swarm/builder", objective="wire the API")]
        decision = delegation.dedupe_workers(
            "wire the api", "swarm/builder", existing=existing)  # case/space differ
        self.assertFalse(decision.delegate)
        self.assertEqual(decision.code, delegation.CODE_DUPLICATE_WORKER)

    def test_a_duplicate_worker_name_is_refused(self) -> None:
        existing = [delegation.WorkerRecord(name="ws1", role="swarm/builder",
                                             objective="build the UI")]
        decision = delegation.dedupe_workers(
            "totally different objective", "swarm/builder",
            existing=existing, name="ws1")
        self.assertFalse(decision.delegate)
        self.assertEqual(decision.code, delegation.CODE_DUPLICATE_WORKER)

    def test_a_different_objective_is_allowed(self) -> None:
        existing = [delegation.WorkerRecord(role="swarm/builder", objective="wire the API")]
        decision = delegation.dedupe_workers(
            "build the UI", "swarm/builder", existing=existing)
        self.assertTrue(decision.delegate)
        self.assertEqual(decision.code, delegation.CODE_UNIQUE_WORKER)

    def test_the_same_objective_under_a_different_role_is_allowed(self) -> None:
        # A builder and a verifier on one objective is division of labour, not
        # redundancy.
        existing = [delegation.WorkerRecord(role="swarm/builder", objective="wire the API")]
        decision = delegation.dedupe_workers(
            "wire the API", "swarm/verifier", existing=existing)
        self.assertTrue(decision.delegate)

    def test_dicts_are_accepted_as_worker_records(self) -> None:
        existing = [{"name": "b1", "role": "swarm/builder", "objective": "wire the API"}]
        decision = delegation.dedupe_workers(
            "wire the API", "swarm/builder", existing=existing)
        self.assertFalse(decision.delegate)

    def test_an_empty_objective_is_refused(self) -> None:
        with self.assertRaises(SwarmError) as ctx:
            delegation.dedupe_workers("  ", "swarm/builder")
        self.assertEqual(ctx.exception.code, "EMPTY_OBJECTIVE")


class TestReuseVerified(unittest.TestCase):
    """Waste #2 - duplicate research."""

    def test_an_identical_verified_result_is_reused_not_redone(self) -> None:
        prior = [delegation.VerifiedResult(
            objective="research three.js vs babylon",
            result="three.js wins for a small scene",
            verified=True, evidence="bench.md#L40")]
        decision = delegation.reuse_verified(
            "research three.js vs babylon", verified=prior)
        self.assertTrue(decision.reuse)
        self.assertEqual(decision.code, delegation.CODE_REUSE_VERIFIED)
        self.assertEqual(decision.result, "three.js wins for a small scene")
        self.assertEqual(decision.evidence, "bench.md#L40")

    def test_an_unverified_result_is_not_reused(self) -> None:
        prior = [delegation.VerifiedResult(
            objective="research x", result="a guess", verified=False)]
        decision = delegation.reuse_verified("research x", verified=prior)
        self.assertFalse(decision.reuse)
        self.assertEqual(decision.code, delegation.CODE_NO_VERIFIED_RESULT)

    def test_a_different_objective_is_not_reused(self) -> None:
        prior = [delegation.VerifiedResult(objective="research x", result="r", verified=True)]
        decision = delegation.reuse_verified("research y", verified=prior)
        self.assertFalse(decision.reuse)

    def test_an_empty_objective_is_refused(self) -> None:
        with self.assertRaises(SwarmError) as ctx:
            delegation.reuse_verified("")
        self.assertEqual(ctx.exception.code, "EMPTY_OBJECTIVE")


class TestQC(unittest.TestCase):
    """Waste #3 - unnecessary QC passes."""

    def test_qc_is_skipped_when_nothing_requires_it(self) -> None:
        decision = delegation.should_skip_qc()
        self.assertTrue(decision.skip)
        self.assertEqual(decision.code, delegation.CODE_QC_UNNEEDED)

    def test_qc_is_not_skipped_when_a_criterion_requires_it(self) -> None:
        decision = delegation.should_skip_qc(criteria=["the build passes"])
        self.assertFalse(decision.skip)
        self.assertEqual(decision.code, delegation.CODE_QC_REQUIRED)
        self.assertIn("acceptance criteria", decision.required_by)

    def test_qc_is_not_skipped_when_security_is_required(self) -> None:
        decision = delegation.should_skip_qc(security_required=True)
        self.assertFalse(decision.skip)
        self.assertIn("security check", decision.required_by)

    def test_token_pressure_does_not_waive_a_required_qc(self) -> None:
        decision = delegation.should_skip_qc(criteria=["c"], token_pressure=True)
        self.assertFalse(decision.skip)


class TestSummary(unittest.TestCase):
    """Waste #4 - repeated summaries."""

    def test_an_already_summarised_result_is_not_summarised_again(self) -> None:
        decision = delegation.skip_redundant_summary(already_summarised=True)
        self.assertTrue(decision.skip)
        self.assertEqual(decision.code, delegation.CODE_SUMMARY_REDUNDANT)

    def test_new_material_earns_a_summary(self) -> None:
        decision = delegation.skip_redundant_summary(
            already_summarised=True, new_material=True)
        self.assertFalse(decision.skip)
        self.assertEqual(decision.code, delegation.CODE_SUMMARY_NEEDED)

    def test_an_unsummarised_result_earns_a_summary(self) -> None:
        self.assertFalse(delegation.skip_redundant_summary().skip)


class TestMandatoryChecks(unittest.TestCase):
    """The absolute rule: cost never drops a required check."""

    def test_a_mandatory_security_check_cannot_be_skipped_by_any_cost_argument(self) -> None:
        for saving in (0.0, 1.0, 1_000.0, 1e9, 1e15):
            decision = delegation.mandatory_checks_required(
                security=["secret scan"], token_saving=saving)
            self.assertFalse(decision.skip_allowed)
            self.assertEqual(decision.code, delegation.CODE_MANDATORY_CHECK_REFUSED)
            self.assertIn("security:secret scan", decision.mandatory)

    def test_a_mandatory_test_cannot_be_dropped_for_tokens(self) -> None:
        decision = delegation.mandatory_checks_required(
            tests=["pytest -q"], token_saving=1e9)
        self.assertFalse(decision.skip_allowed)
        self.assertIn("test:pytest -q", decision.mandatory)

    def test_an_acceptance_criterion_cannot_be_dropped_for_tokens(self) -> None:
        decision = delegation.mandatory_checks_required(
            criteria=["the page renders"], token_saving=1e9)
        self.assertFalse(decision.skip_allowed)
        self.assertIn("criterion:the page renders", decision.mandatory)

    def test_nothing_mandatory_means_nothing_is_blocked(self) -> None:
        decision = delegation.mandatory_checks_required()
        self.assertTrue(decision.skip_allowed)
        self.assertEqual(decision.code, delegation.CODE_NO_MANDATORY_CHECK)


class TestRetry(unittest.TestCase):
    """Bounded retry with targeted repair instructions."""

    def test_a_failed_attempt_retries_with_targeted_repair_instructions(self) -> None:
        decision = delegation.retry_decision(1, limit=2, cause="verification_gap")
        self.assertTrue(decision.retry)
        self.assertEqual(decision.code, delegation.CODE_RETRY_WITH_REPAIR)
        joined = " ".join(decision.instructions).lower()
        self.assertIn("evidence", joined)
        self.assertIn("criterion", joined)

    def test_targeted_instructions_differ_from_the_generic_fallback(self) -> None:
        targeted = delegation.retry_decision(1, limit=2, cause="scope_overlap").instructions
        generic = delegation.retry_decision(1, limit=2).instructions
        self.assertNotEqual(targeted, generic)
        self.assertIn("scope", " ".join(targeted).lower())

    def test_the_limit_produces_a_stop(self) -> None:
        decision = delegation.retry_decision(2, limit=2, cause="tool_failure")
        self.assertFalse(decision.retry)
        self.assertEqual(decision.code, delegation.CODE_RETRY_EXHAUSTED)

    def test_retry_never_loops(self) -> None:
        # Drive the decision the way a caller would and prove it terminates.
        outcomes: list[bool] = []
        attempt = 0
        while True:
            attempt += 1
            decision = delegation.retry_decision(attempt, limit=2, cause="tool_failure")
            outcomes.append(decision.retry)
            if not decision.retry:
                break
            self.assertLess(attempt, 50, "retry_decision failed to terminate")
        self.assertEqual(outcomes, [True, False])

    def test_a_zero_or_negative_limit_never_retries(self) -> None:
        for limit in (0, -1):
            decision = delegation.retry_decision(0, limit=limit)
            self.assertFalse(decision.retry)
            self.assertEqual(decision.code, delegation.CODE_RETRY_DISABLED)

    def test_the_limit_defaults_to_the_budget_limits(self) -> None:
        # Reuses budget.Limits.max_retries_per_worker instead of a second number.
        cap = Limits().max_retries_per_worker
        at_cap = delegation.retry_decision(cap, cause="tool_failure")
        self.assertFalse(at_cap.retry)
        self.assertEqual(at_cap.code, delegation.CODE_RETRY_EXHAUSTED)
        before_cap = delegation.retry_decision(cap - 1, cause="tool_failure")
        self.assertTrue(before_cap.retry)

    def test_an_unknown_cause_still_yields_actionable_instructions(self) -> None:
        decision = delegation.retry_decision(1, limit=3, cause="something new broke")
        self.assertTrue(decision.retry)
        self.assertTrue(decision.instructions)
        self.assertIn("reproduce", decision.instructions[0].lower())


if __name__ == "__main__":
    unittest.main()

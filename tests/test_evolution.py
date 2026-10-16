"""Experience capture, memory, analysis, experiments and promotion.

Each test here exists because the corresponding failure is one this system is
supposed to prevent. Several are deliberately adversarial: they try to talk
the engine into promoting something that should never be promoted.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from swarmkit import analysis, evolve, experience, gate, manager, phases, store
from swarmkit.errors import SwarmError
from swarmkit.experience import (Criterion, Experience, ExperienceStore, Memory,
                                 capture_run, redact)
from tests.support import TempWorkspace, run_cli

REQUEST = "build me a Vite app that renders a subway runner game with sprite scrolling"


def exp(run, **kw) -> Experience:
    """A passing experience, overridable field by field."""
    base = dict(
        run=run, request="something", category="web", complexity="medium",
        outcome="delivered",
        criteria=[Criterion("c1", "build exits 0", passed=True)],
        evidence=["npm run build"],
        lessons=["run the build before delivering"],
    )
    base.update(kw)
    return Experience(**base)


class TestRedaction(unittest.TestCase):
    def test_credentials_never_reach_storage(self):
        for secret in ("ghp_abcdefghij1234567890", "sk-abcdefghijklmnopqrst",
                       "password=hunter22", "Bearer aaaaaaaaaaaaaaaa"):
            with self.subTest(secret=secret[:12]):
                self.assertNotIn(secret[:10], redact(secret))

    def test_emails_are_stripped(self):
        self.assertNotIn("bob@example.com", redact("contact bob@example.com now"))

    def test_long_values_are_truncated_not_dropped(self):
        out = redact("x" * 5000)
        self.assertLess(len(out), 700)
        self.assertIn("truncated", out)

    def test_redaction_keeps_the_useful_part(self):
        self.assertIn("build failed", redact("build failed: npm error"))


class TestExperienceRecord(TempWorkspace):
    def test_a_verified_delivery_is_a_lesson(self):
        self.assertEqual(exp("r1").tier, "lesson")

    def test_a_verified_run_with_no_rule_is_not_called_a_lesson(self):
        # Found by running the engine end to end: a delivered run captured
        # with evidence but no distilled rule reported tier="lesson" while
        # `learn lessons` counted zero. The label claimed more than the record
        # held, which is the same over-claiming the whole engine is meant to
        # refuse - just at one tier instead of at the headline level.
        bare = Experience(run="r-norule", request="x", category="web",
                          complexity="small", outcome="delivered",
                          criteria=[Criterion("c1", "builds", passed=True)],
                          evidence=["npm run build"], lessons=[])
        self.assertTrue(bare.verified, "precondition: this run really did verify")
        self.assertNotEqual(bare.tier, "lesson")
        self.assertEqual(bare.tier, "verified")
        memory = Memory(self.ws / "m-norule")
        memory.store.record(bare)
        self.assertEqual(len(memory.lessons()), 0,
                         "no rule was recorded, so no lesson may be reported")
        # And the record is still retrievable - demotion is not deletion.
        self.assertEqual(len(memory.store.all()), 1)
        self.assertIn("verified", memory.tiers())

    def test_adding_a_rule_promotes_the_record_to_a_lesson(self):
        bare = Experience(run="r-later", request="x", outcome="delivered",
                          criteria=[Criterion("c1", "builds", passed=True)],
                          evidence=["npm run build"], lessons=[])
        self.assertEqual(bare.tier, "verified")
        bare.lessons = ["always run the build first"]
        self.assertEqual(bare.tier, "lesson")

    def test_an_unverified_success_is_not_treated_as_a_lesson(self):
        # The single most important property: claiming success without a
        # passing check must never become something the memory learns from.
        boastful = Experience(run="r2", request="x", outcome="delivered",
                              criteria=[Criterion("c1", "builds", passed=None)])
        self.assertFalse(boastful.verified)
        self.assertNotEqual(boastful.tier, "lesson")

    def test_a_hypothesis_is_not_a_lesson(self):
        # Two separate facts, kept separate. A record can be *verified* - a
        # real passing check exists - and still carry speculation. The
        # speculation does not downgrade the verified outcome, and the
        # verified outcome does not promote the speculation to a lesson.
        guess = Experience(run="r3", request="x", outcome="delivered",
                           criteria=[Criterion("c", "t", passed=True)],
                           hypotheses=["maybe caching helps"])
        self.assertNotEqual(guess.tier, "lesson")
        self.assertEqual(guess.tier, "verified")

        # An unverified run carrying the same guess really is a hypothesis,
        # and memory still surfaces it as one rather than losing it.
        unverified = Experience(run="r4", request="x", outcome="failed",
                                hypotheses=["maybe caching helps"])
        self.assertEqual(unverified.tier, "hypothesis")
        memory = Memory(self.ws / "m-hyp")
        memory.store.record(unverified)
        self.assertEqual([rule for rule, _ in memory.hypotheses()],
                         ["maybe caching helps"])

    def test_confidence_rises_with_evidence(self):
        weak = Experience(run="a", request="x", outcome="delivered")
        strong = exp("b", user_accepted=True)
        self.assertGreater(strong.confidence, weak.confidence)

    def test_capture_from_a_real_run_reads_what_actually_happened(self):
        run_dir = store.init_run(self.ws, "cap")
        gate.add_criterion(run_dir, gate.Criterion("build", "build exits 0"))
        gate.add_evidence(run_dir, gate.Evidence(
            criterion="build", status="pass", command="npm run build",
            result="exit 0"))
        state = store.RunState.load(run_dir)
        state.phase = phases.DELIVERED
        state.save(run_dir)
        experience = capture_run(run_dir, request=REQUEST)
        self.assertEqual(experience.outcome, "delivered")
        self.assertTrue(experience.verified)
        self.assertEqual(experience.evidence, ["npm run build"])


class TestMemoryRetrieval(TempWorkspace):
    def test_retrieval_is_scoped_by_category(self):
        store_ = ExperienceStore(self.ws / "mem")
        store_.record(exp("a", category="web", lessons=["pin the vite version"]))
        store_.record(exp("b", category="blender",
                          lessons=["use the posix path form"]))
        memory = Memory(self.ws / "mem")
        web = memory.recall(category="web")
        self.assertEqual([r["rule"] for r in web], ["pin the vite version"])
        # The blender lesson must not reach a web task.
        self.assertEqual(memory.recall(category="other"), [])

    def test_lessons_carry_their_provenance(self):
        store_ = ExperienceStore(self.ws / "mem2")
        store_.record(exp("a", lessons=["always run the build"]))
        rule = Memory(self.ws / "mem2").recall(category="web")[0]
        self.assertEqual(rule["origin"], "a")
        self.assertGreater(rule["confidence"], 0)

    def test_a_challenge_must_reference_a_real_run(self):
        store_ = ExperienceStore(self.ws / "mem3")
        store_.record(exp("real"))
        with self.assertRaises(SwarmError) as ctx:
            Memory(self.ws / "mem3").challenge("imaginary", "seems wrong")
        self.assertEqual(ctx.exception.code, "EXPERIENCE_NOT_FOUND")

    def test_a_real_challenge_is_recorded(self):
        store_ = ExperienceStore(self.ws / "mem4")
        store_.record(exp("real"))
        memory = Memory(self.ws / "mem4")
        memory.challenge("real", "no longer reproduces")
        self.assertEqual(len(memory.challenges()), 1)


class TestMetricsRefuseToOverclaim(TempWorkspace):
    def test_rates_are_withheld_below_the_sample_floor(self):
        metrics = analysis.compute_metrics([exp(f"r{i}") for i in range(2)])
        self.assertIsNone(metrics.first_attempt_success)
        self.assertTrue(any("MIN_SAMPLE" in n for n in metrics.notes))

    def test_rates_appear_once_the_sample_is_sufficient(self):
        metrics = analysis.compute_metrics(
            [exp(f"r{i}") for i in range(analysis.MIN_SAMPLE)])
        self.assertIsNotNone(metrics.first_attempt_success)
        self.assertEqual(metrics.final_delivery, 1.0)

    def test_repeated_causes_need_repetition(self):
        one = [exp("a", outcome="failed", failure_cause="scope_overlap")]
        self.assertEqual(analysis.compute_metrics(one).repeated_failures, {})
        many = [exp(f"r{i}", outcome="failed", failure_cause="scope_overlap")
                for i in range(analysis.MIN_EXAMPLES_FOR_RECURRENCE)]
        self.assertIn("scope_overlap",
                      analysis.compute_metrics(many).repeated_failures)

    def test_counts_are_never_scored_as_achievement(self):
        # A run that made a huge number of agents and lessons must not look
        # better than a run that simply worked.
        noisy = exp("a", agents_used=[f"agent{i}" for i in range(20)],
                    lessons=["x"] * 20)
        clean = exp("b")
        self.assertEqual(analysis.compute_metrics([noisy]).final_delivery,
                         analysis.compute_metrics([clean]).final_delivery)


class TestAnalysisGatesOnEvidence(TempWorkspace):
    def _seed(self, **kw):
        store_ = ExperienceStore(self.ws / "a")
        for i in range(analysis.MIN_SAMPLE):
            store_.record(exp(f"r{i}", **kw))
        return store_

    def test_no_proposals_without_a_baseline(self):
        # One short of MIN_SAMPLE. Seeding exactly MIN_SAMPLE would BE a
        # baseline, which is the point of the threshold.
        self._seed(outcome="failed", failure_cause="unknown")
        self.assertEqual(analysis.MIN_SAMPLE, 5)
        # Truncate the file itself rather than going through `all()`, so this
        # does not depend on what the store happens to hand back.
        path = self.ws / "a" / "experiences.jsonl"
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines(keepends=True) if ln.strip()]
        self.assertEqual(len(lines), analysis.MIN_SAMPLE)
        path.write_text("".join(lines[:-1]), encoding="utf-8")
        result = analysis.analyze(self.ws / "a", category="web")
        self.assertFalse(result["sample_sufficient"])
        self.assertEqual(result["proposals"], [])

    def test_a_weak_baseline_raises_an_evidenced_proposal(self):
        records = []
        for i in range(analysis.MIN_SAMPLE):
            records.append(exp(f"r{i}", outcome="failed",
                               failure_cause="bad_plan", retries=3,
                               criteria=[Criterion("c", "t", passed=False)],
                               failed_checks=["npm run build"]))
        store_ = ExperienceStore(self.ws / "b")
        for record in records:
            store_.record(record)
        result = analysis.analyze(self.ws / "b", category="web")
        self.assertTrue(result["proposals"])
        for proposal in result["proposals"]:
            self.assertTrue(proposal["evidence"])
            self.assertTrue(proposal["evaluation"])
            self.assertTrue(proposal["rollback"])

    def test_trivial_work_does_not_trigger_analysis(self):
        ok, why = analysis.should_analyze([exp("r1"), exp("r2")])
        self.assertFalse(ok)
        # The refusal has to SAY something, or an operator cannot tell a
        # deliberate "not yet" from a broken analyzer.
        self.assertTrue(why.strip())

    def test_a_recurring_failure_does_trigger_it(self):
        records = [exp(f"r{i}", outcome="failed", failure_cause="stale_worker")
                   for i in range(analysis.MIN_EXAMPLES_FOR_RECURRENCE)]
        ok, why = analysis.should_analyze(records)
        self.assertTrue(ok)
        self.assertIn("recurring", why)


class TestProposalPolicy(unittest.TestCase):
    def test_an_unevidenced_proposal_cannot_exist(self):
        with self.assertRaises(SwarmError) as ctx:
            analysis.Proposal(id="p", target="planning", problem="x",
                              evidence=[], change="y", expected_benefit="z",
                              risks=[], evaluation="e", rollback="r")
        self.assertEqual(ctx.exception.code, "PROPOSAL_UNGROUNDED")

    def test_a_proposal_without_a_rollback_is_refused(self):
        with self.assertRaises(SwarmError) as ctx:
            analysis.Proposal(id="p", target="planning", problem="x",
                              evidence=["e"], change="y", expected_benefit="z",
                              risks=[], evaluation="e", rollback="")
        self.assertEqual(ctx.exception.code, "PROPOSAL_INCOMPLETE")

    def test_protected_targets_are_flagged_for_authorization(self):
        for target in ("verification", "recovery"):
            proposal = analysis.Proposal(
                id="p", target=target, problem="x", evidence=["e"], change="y",
                expected_benefit="z", risks=[], evaluation="e", rollback="r")
            self.assertTrue(proposal.needs_authorization, target)

    def test_auto_promotion_cannot_unlock_a_protected_target(self):
        policy = evolve.Policy(auto_promote=True,
                               auto_promote_targets=("verification", "recovery"))
        for target in ("verification", "recovery"):
            self.assertFalse(policy.may_auto_promote(target))


class TestComparisonRejectsBadTrades(TempWorkspace):
    BASE = {"final_delivery": 0.8, "first_attempt_success": 0.6,
            "qc_failure_rate": 0.2, "median_elapsed_s": 300.0}

    def _compare(self, **candidate):
        return evolve.compare(dict(self.BASE, **candidate), self.BASE)

    def test_a_clean_improvement_is_accepted(self):
        verdict = self._compare(final_delivery=0.95, median_elapsed_s=200.0)
        self.assertTrue(verdict["accepted"])
        self.assertGreater(verdict["gain"], 0)

    def test_faster_but_less_correct_is_rejected(self):
        verdict = self._compare(final_delivery=0.5, median_elapsed_s=50.0)
        self.assertFalse(verdict["accepted"])
        self.assertEqual(verdict["reason"], "REGRESSION")
        self.assertIn("final_delivery",
                      [r["metric"] for r in verdict["regressions"]])

    def test_no_baseline_is_rejected(self):
        verdict = evolve.compare({"final_delivery": 1.0}, {})
        self.assertEqual(verdict["reason"], "NO_BASELINE")

    def test_making_nothing_worse_is_not_an_improvement(self):
        verdict = self._compare(final_delivery=0.8)
        self.assertFalse(verdict["accepted"])
        self.assertIn(verdict["reason"], ("NO_MEASURED_GAIN", "GAIN_TOO_SMALL"))

    def test_unmeasured_change_scores_nothing(self):
        self.assertIsNone(evolve.composite_score({"agents_spawned": 99}))


class TestExperimentIsolation(TempWorkspace):
    def test_an_experiment_cannot_touch_the_delivery_gate(self):
        for path in ("swarmkit/gate.py", "./swarmkit/gate.py",
                     "src/swarmkit/gate.py"):
            with self.subTest(path=path):
                with self.assertRaises(SwarmError) as ctx:
                    evolve.assert_scope_allowed([path])
                self.assertEqual(ctx.exception.code, "PROTECTED_SCOPE_CHANGE")

    def test_budget_and_recovery_cannot_be_edited_by_an_experiment(self):
        for path in ("swarmkit/budget.py", "swarmkit/store.py"):
            with self.subTest(path=path):
                with self.assertRaises(SwarmError):
                    evolve.assert_scope_allowed([path])

    def test_an_ordinary_scope_is_allowed(self):
        self.assertEqual(evolve.assert_scope_allowed(["swarmkit/analysis.py"]),
                         ["swarmkit/analysis.py"])

    def test_a_code_change_needs_a_reproduction(self):
        with self.assertRaises(SwarmError) as ctx:
            evolve.CodeChange(id="c1", problem="p", reproduction="",
                              file_scope=["a.py"], fix="f")
        self.assertEqual(ctx.exception.code, "CHANGE_UNREPRODUCIBLE")

    def test_a_code_change_needs_a_scope(self):
        with self.assertRaises(SwarmError) as ctx:
            evolve.CodeChange(id="c2", problem="p", reproduction="r",
                              file_scope=[], fix="f")
        self.assertEqual(ctx.exception.code, "CHANGE_UNSCOPED")

    def test_an_experiment_workspace_is_isolated_from_the_repo(self):
        repo = self.ws / "repo"
        (repo / "swarmkit").mkdir(parents=True)
        (repo / "swarmkit" / "x.py").write_text("ORIGINAL = 1\n", encoding="utf-8")
        space = evolve.isolated_workspace(repo, "exp1")
        copy = evolve.copy_baseline(repo, space)
        (copy / "swarmkit" / "x.py").write_text("TAMPERED = 1\n", encoding="utf-8")
        # The live tree is untouched by anything the experiment did.
        self.assertEqual((repo / "swarmkit" / "x.py").read_text(encoding="utf-8"),
                         "ORIGINAL = 1\n")


class TestPromotionAndRollback(TempWorkspace):
    def _proposal(self, pid="p1", target="planning"):
        return analysis.Proposal(
            id=pid, target=target, problem="slow", evidence=["e"],
            change="do it in smaller pieces", expected_benefit="faster",
            risks=["under-deliver"], evaluation="re-run benchmark",
            rollback="revert policy version")

    def _candidate(self, store_, proposal=None, kind="policy"):
        return evolve.create_candidate(proposal or self._proposal(), store_,
                                        kind=kind)

    def test_a_rejected_candidate_cannot_be_promoted(self):
        store_ = evolve.VersionStore(self.ws / "ev")
        version = self._candidate(store_)
        result = evolve.evaluate("p1", version["id"],
                                 {"final_delivery": 0.1}, {"final_delivery": 0.9},
                                 store_, safety_passed=True)
        self.assertFalse(result["promoted"])
        self.assertEqual(evolve.VersionStore(self.ws / "ev")
                         .load(version["id"]).state, "rejected")

    def test_a_candidate_failing_safety_is_never_promoted_however_good(self):
        store_ = evolve.VersionStore(self.ws / "ev2")
        version = self._candidate(store_)
        result = evolve.evaluate("p1", version["id"],
                                 {"final_delivery": 1.0}, {"final_delivery": 0.1},
                                 store_, safety_passed=False)
        self.assertFalse(result["promoted"])
        self.assertEqual(result["reason"], "SAFETY_GATE_FAILED")

    def test_a_protected_target_needs_explicit_authorization(self):
        store_ = evolve.VersionStore(self.ws / "ev3")
        version = self._candidate(store_, self._proposal(target="verification"),
                                  kind="verification")
        # A large gain, safety clean - and still refused without authorization.
        result = evolve.evaluate("p1", version["id"],
                                 {"final_delivery": 1.0}, {"final_delivery": 0.1},
                                 store_, safety_passed=True, authorized=False)
        self.assertFalse(result["promoted"])
        self.assertEqual(result["reason"], "AUTHORIZATION_REQUIRED")

    def test_an_authorized_protected_change_can_be_promoted(self):
        store_ = evolve.VersionStore(self.ws / "ev4")
        version = self._candidate(store_, self._proposal(target="recovery"),
                                  kind="recovery")
        result = evolve.evaluate("p2", version["id"],
                                 {"final_delivery": 1.0}, {"final_delivery": 0.1},
                                 store_, safety_passed=True, authorized=True)
        self.assertTrue(result["promoted"])

    def test_a_good_candidate_promotes_and_becomes_active(self):
        store_ = evolve.VersionStore(self.ws / "ev5")
        version = self._candidate(store_)
        result = evolve.evaluate("p1", version["id"],
                                 {"final_delivery": 0.95}, {"final_delivery": 0.5},
                                 store_, safety_passed=True)
        self.assertTrue(result["promoted"])
        self.assertEqual(store_.active("policy").id, version["id"])

    def test_a_rejected_proposal_is_not_retried_blindly(self):
        store_ = evolve.VersionStore(self.ws / "ev6")
        version = self._candidate(store_)
        evolve.evaluate("p1", version["id"], {"final_delivery": 0.1},
                        {"final_delivery": 0.9}, store_, safety_passed=True)
        with self.assertRaises(SwarmError) as ctx:
            evolve.create_candidate(self._proposal(), store_)
        self.assertEqual(ctx.exception.code, "PROPOSAL_PREVIOUSLY_REJECTED")

    def test_promotion_supersedes_the_previous_active_version(self):
        store_ = evolve.VersionStore(self.ws / "ev7")
        first = self._candidate(store_, self._proposal("a1"))
        evolve.evaluate("a1", first["id"], {"final_delivery": 0.9},
                        {"final_delivery": 0.5}, store_, safety_passed=True)
        second = self._candidate(store_, self._proposal("a2"))
        self.assertEqual(second["parent"], first["id"])
        evolve.evaluate("a2", second["id"], {"final_delivery": 0.95},
                        {"final_delivery": 0.9}, store_, safety_passed=True)
        self.assertEqual(store_.load(first["id"]).state, "superseded")

    def test_rollback_restores_the_previous_version(self):
        store_ = evolve.VersionStore(self.ws / "ev8")
        first = self._candidate(store_, self._proposal("b1"))
        evolve.evaluate("b1", first["id"], {"final_delivery": 0.9},
                        {"final_delivery": 0.5}, store_, safety_passed=True)
        second = self._candidate(store_, self._proposal("b2"))
        evolve.evaluate("b2", second["id"], {"final_delivery": 0.95},
                        {"final_delivery": 0.9}, store_, safety_passed=True)
        result = evolve.rollback(second["id"], store_, reason="regressed in prod")
        self.assertEqual(result["restored"], first["id"])
        self.assertEqual(store_.active("policy").id, first["id"])
        self.assertEqual(store_.load(second["id"]).state, "rolled_back")

    def test_a_promoted_regression_triggers_automatic_rollback(self):
        store_ = evolve.VersionStore(self.ws / "ev9")
        first = self._candidate(store_, self._proposal("c1"))
        evolve.evaluate("c1", first["id"], {"final_delivery": 0.9},
                        {"final_delivery": 0.5}, store_, safety_passed=True)
        second = self._candidate(store_, self._proposal("c2"))
        evolve.evaluate("c2", second["id"], {"final_delivery": 0.9},
                        {"final_delivery": 0.5}, store_, safety_passed=True)
        # Delivery collapses after promotion.
        result = evolve.monitor(second["id"], {"final_delivery": 0.3}, store_)
        self.assertFalse(result["ok"])
        self.assertIn("rollback", result)
        self.assertEqual(store_.active("policy").id, first["id"])

    def test_healthy_metrics_do_not_trigger_rollback(self):
        store_ = evolve.VersionStore(self.ws / "ev10")
        first = self._candidate(store_, self._proposal("d1"))
        evolve.evaluate("d1", first["id"], {"final_delivery": 0.9},
                        {"final_delivery": 0.5}, store_, safety_passed=True)
        self.assertTrue(evolve.monitor(first["id"],
                                       {"final_delivery": 0.92}, store_)["ok"])

    def test_the_generation_budget_stops_unbounded_recursion(self):
        # Each round has to genuinely PROMOTE, or the generation counter never
        # advances and the budget is not exercised at all. A gain below
        # DEFAULT_MIN_GAIN is rejected, which is the trap: bumping the metric
        # by 0.01 looks like progress and is silently discarded.
        store_ = evolve.VersionStore(self.ws / "ev11")
        baseline = 0.5
        for i in range(2):
            candidate = round(baseline + 0.1, 4)
            proposal = self._proposal(f"gen{i}")
            version = evolve.create_candidate(proposal, store_, kind="policy")
            result = evolve.evaluate(f"gen{i}", version["id"],
                                     {"final_delivery": candidate},
                                     {"final_delivery": baseline}, store_,
                                     safety_passed=True)
            self.assertTrue(result["promoted"], result)
            baseline = candidate
        self.assertEqual(store_.generations(), 2,
                         "two promotions should sit two generations deep")
        with self.assertRaises(SwarmError) as ctx:
            evolve.create_candidate(self._proposal("gen9"), store_, kind="policy",
                                    max_generations=2)
        self.assertEqual(ctx.exception.code, "EVOLUTION_BUDGET_EXHAUSTED")

    def test_rejected_candidates_do_not_advance_the_generation_counter(self):
        # Two limits, two failures. Promotion depth is the expensive one;
        # losing a benchmark is cheap and must not burn the deep budget,
        # because otherwise one failed idea permanently locks out evolution.
        store_ = evolve.VersionStore(self.ws / "ev11b")
        for i in range(3):
            evolve.create_candidate(self._proposal(f"cold{i}"), store_, kind="policy")
        self.assertEqual(store_.generations(), 0)
        self.assertEqual(store_.candidate_count(), 3)

    def test_rejected_churn_is_still_bounded(self):
        # The corollary of the previous test: rejected work never advances
        # `generations`, so without a separate cap the record directory grows
        # forever while `max_generations` stays satisfied at zero.
        store_ = evolve.VersionStore(self.ws / "ev11c")
        for i in range(3):
            evolve.create_candidate(self._proposal(f"churn{i}"), store_, kind="policy")
        self.assertEqual(store_.generations(), 0)
        with self.assertRaises(SwarmError) as ctx:
            evolve.create_candidate(self._proposal("churn9"), store_,
                                    kind="policy", max_candidates=3)
        self.assertEqual(ctx.exception.code, "EVOLUTION_BUDGET_EXHAUSTED")
        self.assertIn("not learning", str(ctx.exception))

    def test_every_promotion_is_auditable(self):
        store_ = evolve.VersionStore(self.ws / "ev12")
        version = self._candidate(store_)
        evolve.evaluate("p1", version["id"], {"final_delivery": 0.9},
                        {"final_delivery": 0.5}, store_, safety_passed=True)
        events = store_.history()
        self.assertTrue(any(e.get("event") == "promoted" for e in events))
        self.assertTrue(all("at" in e for e in events))


class TestRetryableRefusalsAndMonitoring(TempWorkspace):
    """Regressions for the bugs found by running the engine end to end.

    Every test here exists because something was WRONG on disk, not because
    a specification said a case ought to be covered.
    """

    def _proposal(self, pid="p1", target="planning"):
        return analysis.Proposal(
            id=pid, target=target, problem="slow", evidence=["e"],
            change="do it in smaller pieces", expected_benefit="faster",
            risks=["under-deliver"], evaluation="re-run benchmark",
            rollback="revert policy version")

    def _candidate(self, store_, pid="p1"):
        return evolve.create_candidate(self._proposal(pid), store_, kind="policy")

    # --- a refusal that is not a verdict on the idea -------------------

    def test_a_safety_refused_version_can_still_be_evaluated(self):
        # `_reject` used to set state="rejected" unconditionally, so a
        # version refused by a flaky sandbox could never be retried even
        # though its proposal was still perfectly eligible.
        store_ = evolve.VersionStore(self.ws / "ev-retry")
        version = self._candidate(store_)
        first = evolve.evaluate("p1", version["id"], {"final_delivery": 0.9},
                                {"final_delivery": 0.5}, store_, safety_passed=False)
        self.assertFalse(first["promoted"])
        self.assertEqual(first["reason"], "SAFETY_GATE_FAILED")
        self.assertTrue(first["retryable"])
        self.assertEqual(store_.load(version["id"]).state, "candidate",
                         "a retryable refusal must not make the version terminal")
        second = evolve.evaluate("p1", version["id"], {"final_delivery": 0.9},
                                 {"final_delivery": 0.5}, store_, safety_passed=True)
        self.assertTrue(second["promoted"], second)

    def test_a_safety_refusal_does_not_blacklist_the_proposal(self):
        # Narrowing the blacklist was not enough on its own. One failed
        # safety run used to retire the proposal forever, so a transient
        # environment problem permanently closed a good idea.
        store_ = evolve.VersionStore(self.ws / "ev-blacklist")
        version = self._candidate(store_)
        evolve.evaluate("p1", version["id"], {"final_delivery": 0.9},
                        {"final_delivery": 0.5}, store_, safety_passed=False)
        self.assertEqual(store_.rejected_rules(), set())
        again = evolve.create_candidate(self._proposal("p1"), store_, kind="policy")
        self.assertEqual(again["state"], "candidate")

    def test_a_benchmark_refusal_is_permanent(self):
        # The other half: a change that measurably does not help IS retired.
        store_ = evolve.VersionStore(self.ws / "ev-perm")
        version = self._candidate(store_)
        result = evolve.evaluate("p1", version["id"], {"final_delivery": 0.5},
                                 {"final_delivery": 0.5}, store_, safety_passed=True)
        self.assertEqual(result["reason"], "NO_MEASURED_GAIN")
        self.assertFalse(result["retryable"])
        self.assertEqual(store_.load(version["id"]).state, "rejected")
        self.assertIn("p1", store_.rejected_rules())
        with self.assertRaises(SwarmError):
            evolve.create_candidate(self._proposal("p1"), store_, kind="policy")

    # --- monitoring is the gate that calls the bet ----------------------

    def _promoted(self, name, metrics, baseline):
        store_ = evolve.VersionStore(self.ws / name)
        version = self._candidate(store_)
        result = evolve.evaluate("p1", version["id"], metrics, baseline,
                                 store_, safety_passed=True)
        self.assertTrue(result["promoted"], result)
        return store_, version["id"]

    def test_monitor_judges_a_regression_against_what_was_promised(self):
        # The headline bug. Comparing against the BASELINE made the gate
        # structurally unable to fire: promoted at 0.90, collapsed to 0.60,
        # and 0.60 still "beat" the 0.50 baseline, so the collapse read as
        # health. The reference has to be what the candidate promised.
        store_, vid = self._promoted(
            "ev-m1", {"final_delivery": 0.90, "qc_failure_rate": 0.10},
            {"final_delivery": 0.50, "qc_failure_rate": 0.30})
        bad = evolve.monitor(vid, {"final_delivery": 0.60,
                                   "qc_failure_rate": 0.08}, store_)
        self.assertFalse(bad["ok"], "a collapse below the promoted value is a regression")
        self.assertTrue(bad["breaches"])
        self.assertEqual(bad["breaches"][0]["metric"], "final_delivery")
        self.assertEqual(bad["breaches"][0]["promised"], 0.90)
        self.assertEqual(bad["breaches"][0]["observed"], 0.60)

    def test_an_improvement_is_never_called_a_regression(self):
        # The threshold was inverted (`worse < -threshold` with a negative
        # threshold), which would have flagged anything that got BETTER.
        store_, vid = self._promoted(
            "ev-m2", {"final_delivery": 0.50}, {"final_delivery": 0.20})
        better = evolve.monitor(vid, {"final_delivery": 0.95}, store_)
        self.assertTrue(better["ok"], better)
        self.assertEqual(better["breaches"], [])

    def test_a_lower_is_better_metric_moving_up_is_a_regression(self):
        # Direction comes from GUARDED_METRICS, not from the sign of the
        # delta, so `qc_failure_rate` must not read "up is better".
        store_, vid = self._promoted(
            "ev-m3", {"final_delivery": 0.90, "qc_failure_rate": 0.05},
            {"final_delivery": 0.50, "qc_failure_rate": 0.40})
        worse = evolve.monitor(vid, {"final_delivery": 0.90,
                                     "qc_failure_rate": 0.50}, store_)
        self.assertFalse(worse["ok"])
        self.assertEqual(worse["breaches"][0]["metric"], "qc_failure_rate")

    def test_monitor_reports_absence_rather_than_a_clean_bill(self):
        # "Nothing observed" and "nothing changed" look identical from
        # outside. A gate that cannot tell them apart is not a gate.
        store_ = evolve.VersionStore(self.ws / "ev-m4")
        version = self._candidate(store_)
        result = evolve.monitor(version["id"], {"final_delivery": 0.5}, store_)
        self.assertIn("reason", result)
        self.assertIn("nothing to monitor", result["reason"])
        self.assertNotIn("version", result,
                         "an unpromoted version must not masquerade as a healthy one")

    def test_regression_triggers_are_positive_magnitudes(self):
        # If a trigger is negative the breach test becomes `worse > abs(t)`
        # with t computed from a negative number, which silently disables it.
        for name, threshold in evolve.REGRESSION_TRIGGERS.items():
            with self.subTest(metric=name):
                self.assertGreater(threshold, 0,
                                   "regression triggers are magnitudes, not deltas")


class TestEngineCli(TempWorkspace):
    """The engine has to be reachable from the command surface.

    A mechanism nothing can invoke is the exact defect two earlier audit
    rounds found, so the CLI is treated as part of the feature rather than
    as a thin wrapper over it.
    """

    def _proposal(self, pid="p1"):
        return analysis.Proposal(
            id=pid, target="planning", problem="slow", evidence=["e"],
            change="smaller pieces", expected_benefit="faster",
            risks=["under-deliver"], evaluation="re-run benchmark",
            rollback="revert policy version")

    def _seed_memory(self):
        """Give the analyzer a real baseline so it raises a real proposal."""
        store_ = experience.ExperienceStore(
            Path(self.ws) / experience.MEMORY_ROOT)
        for i in range(analysis.MIN_SAMPLE):
            store_.record(Experience(
                run=f"seed{i}", request="build a thing", category="web",
                outcome="failed", failure_cause="bad_plan", retries=2,
                criteria=[Criterion("c", "build exits 0", passed=False)],
                failed_checks=["npm run build"]))

    def _promote(self, store_, pid="p1", value=0.9):
        version = evolve.create_candidate(self._proposal(pid), store_,
                                          kind="policy")
        result = evolve.evaluate(pid, version["id"], {"final_delivery": value},
                                 {"final_delivery": 0.2}, store_,
                                 safety_passed=True)
        self.assertTrue(result["promoted"], result)
        return version["id"]

    def test_the_engine_is_reachable_from_the_command_surface(self):
        for group in ("experience", "learn", "analyze", "evolve"):
            with self.subTest(group=group):
                proc = run_cli(group, "--help", workspace=self.ws)
                self.assertIn(group, proc.stdout)

    def test_analyze_refuses_to_overclaim_on_an_empty_memory(self):
        payload = json.loads(run_cli("--json", "analyze", workspace=self.ws).stdout)
        self.assertFalse(payload["sample_sufficient"])
        self.assertEqual(payload["proposals"], [])

    def test_analyze_raises_a_real_proposal_once_there_is_a_baseline(self):
        self._seed_memory()
        payload = json.loads(run_cli("--json", "analyze", workspace=self.ws).stdout)
        self.assertTrue(payload["sample_sufficient"])
        self.assertTrue(payload["proposals"])
        for proposal in payload["proposals"]:
            self.assertTrue(proposal["evidence"],
                            "a proposal with no evidence is not a proposal")

    def test_a_candidate_cannot_be_hand_written(self):
        # If an operator can invent a proposal they can invent its evidence,
        # and the loop becomes a machine for believing whatever it is told.
        proc = run_cli("--json", "evolve", "candidate", "--proposal", "invented",
                       workspace=self.ws, expect=1)
        error = json.loads(proc.stdout)["error"]
        self.assertEqual(error["code"], "PROPOSAL_NOT_FOUND")

    def test_evaluate_works_without_an_explicit_min_gain(self):
        # The flag has no argparse default, so it arrived as None and
        # `gain < min_gain` raised TypeError - a benchmark gate that could
        # not be used at all.
        store_ = evolve.VersionStore(
            Path(self.ws) / evolve.EVOLUTION_ROOT)
        version = evolve.create_candidate(self._proposal("p1"), store_,
                                          kind="policy")
        proc = run_cli("--json", "evolve", "evaluate", "--proposal", "p1",
                       "--version", version["id"], "--candidate", "final_delivery=0.9",
                       "--baseline", "final_delivery=0.2", "--safety-passed",
                       workspace=self.ws, expect=0)
        self.assertTrue(json.loads(proc.stdout)["promoted"])

    def test_several_metric_pairs_may_be_passed_as_one_flag(self):
        store_ = evolve.VersionStore(
            Path(self.ws) / evolve.EVOLUTION_ROOT)
        version = evolve.create_candidate(self._proposal("p1"), store_,
                                          kind="policy")
        proc = run_cli("--json", "evolve", "evaluate", "--proposal", "p1",
                       "--version", version["id"],
                       "--candidate", "final_delivery=0.9", "qc_failure_rate=0.05",
                       "--baseline", "final_delivery=0.2", "qc_failure_rate=0.40",
                       "--safety-passed", workspace=self.ws, expect=0)
        self.assertTrue(json.loads(proc.stdout)["promoted"])

    def test_rollback_succeeds_without_crashing_afterwards(self):
        # The rollback happened on disk and *then* the process died with
        # AttributeError while printing its own success line. An operation
        # that works but reports failure is worse than one that fails.
        store_ = evolve.VersionStore(Path(self.ws) / evolve.EVOLUTION_ROOT)
        self._promote(store_, "p1", 0.9)
        self._promote(store_, "p2", 0.95)
        proc = run_cli("--json", "evolve", "rollback", "--version",
                       f"p2-2", "--reason", "test", workspace=self.ws, expect=0)
        self.assertEqual(json.loads(proc.stdout)["rolled_back"], "p2-2")
        self.assertEqual(store_.load("p2-2").state, "rolled_back")
        self.assertEqual(store_.active("policy").id, "p1-1")

    def test_rollback_of_the_first_version_refuses_rather_than_guessing(self):
        store_ = evolve.VersionStore(Path(self.ws) / evolve.EVOLUTION_ROOT)
        self._promote(store_, "p1", 0.9)
        proc = run_cli("--json", "evolve", "rollback", "--version", "p1-1",
                       workspace=self.ws, expect=1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"],
                         "NO_ROLLBACK_TARGET")

    def test_a_protected_path_cannot_be_self_evolved(self):
        proc = run_cli("--json", "evolve", "scope-check",
                       "--path", "swarmkit/gate.py",
                       workspace=self.ws, expect=1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"],
                         "PROTECTED_SCOPE_CHANGE")

    def test_policy_round_trips_through_the_command_surface(self):
        run_cli("--json", "evolve", "policy", "--max-generations", "7",
                workspace=self.ws)
        self.assertEqual(
            json.loads(run_cli("--json", "evolve", "policy",
                               workspace=self.ws).stdout)["max_generations"], 7)

    def test_the_schema_is_published_for_anything_reading_the_store(self):
        schema = json.loads(run_cli("--json", "learn", "schema",
                                    workspace=self.ws).stdout)
        self.assertIn("experience_fields", schema)
        self.assertIn("verified", schema["derived"])
        self.assertIn("verified", schema["tiers"])


if __name__ == "__main__":
    unittest.main()
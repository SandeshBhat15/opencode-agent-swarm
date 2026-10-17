"""Tests for :mod:`swarmkit.decide`.

These pin the four properties the decision engine exists to guarantee:

1. Simple tasks use fewer calls than complex ones.
2. Complex tasks receive appropriate help (delegation + verification).
3. Failures recover safely (bounded, targeted, never skipping safety).
4. Routing never selects an unavailable model.

All tests are pure: no network, no model calls, no LLM.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from swarmkit import decision as decide, delegation, router, manager


def _catalog_with_unavailable(tmp: Path) -> Path:
    """A catalog whose only model reports capability False (unavailable)."""
    reg = tmp / "models"
    reg.mkdir(parents=True, exist_ok=True)
    path = tmp / "catalog.json"
    path.write_text(
        '{"models": [{"provider": "p", "model": "ghost", "status": "active", '
        '"capabilities": {"text": false, "code": false, "tools": false}}]}',
        encoding="utf-8",
    )
    router.import_catalog(path, "fixture", registry=reg)
    return reg


def _catalog_with_available(tmp: Path) -> Path:
    reg = tmp / "models"
    reg.mkdir(parents=True, exist_ok=True)
    path = tmp / "catalog.json"
    path.write_text(
        '{"models": [{"provider": "p", "model": "good", "status": "active", '
        '"capabilities": {"text": true, "code": true, "tools": true}}]}',
        encoding="utf-8",
    )
    router.import_catalog(path, "fixture", registry=reg)
    return reg


class TestDisabledByDefault(unittest.TestCase):
    def test_is_enabled_defaults_false(self) -> None:
        os.environ.pop("SWARM_DECISION_ENGINE", None)
        self.assertFalse(decide.is_enabled())

    def test_decide_is_passthrough_when_disabled(self) -> None:
        os.environ.pop("SWARM_DECISION_ENGINE", None)
        d = decide.decide("add a docstring to one function")
        self.assertFalse(d.enabled)
        self.assertEqual(d.code, "DISABLED")
        self.assertFalse(d.delegate)
        self.assertEqual(d.workflow, "direct")
        self.assertEqual(d.predicted_calls, 1)
        self.assertIsNone(d.model)
        self.assertIsNotNone(d.disabled_reason)


class TestSimpleTasksUseFewerCalls(unittest.TestCase):
    def test_simple_task_is_direct_and_single_call(self) -> None:
        d = decide.decide("what is a Python list", enabled=True)
        self.assertEqual(d.workflow, "direct")
        self.assertFalse(d.delegate)
        self.assertEqual(d.predicted_calls, 1)
        self.assertEqual(d.verification, "none")

    def test_complex_task_uses_more_calls_than_simple(self) -> None:
        simple = decide.decide("what is a Python list", enabled=True)
        complex_ = decide.decide(
            "refactor the authentication module across 4 files and add tests",
            enabled=True,
            file_count=4,
            independent_workstreams=3,
        )
        self.assertGreater(complex_.predicted_calls, simple.predicted_calls)
        self.assertTrue(complex_.delegate or complex_.worker_count >= 2)

    def test_simple_never_delegates(self) -> None:
        for req in ("hello", "explain recursion", "list files", "what is X"):
            d = decide.decide(req, enabled=True)
            self.assertFalse(d.delegate, req)
            self.assertEqual(d.worker_count, 0, req)


class TestComplexTasksGetHelp(unittest.TestCase):
    def test_multi_file_project_delegates_and_verifies(self) -> None:
        d = decide.decide(
            "migrate the database schema and deploy to production",
            enabled=True,
            file_count=5,
            independent_workstreams=2,
        )
        self.assertEqual(d.workflow, "project")
        self.assertTrue(d.delegate)
        self.assertGreaterEqual(d.worker_count, 2)
        self.assertEqual(d.verification, "full")
        self.assertEqual(d.profile.risk, "high")

    def test_research_task_routes_to_research_workflow(self) -> None:
        d = decide.decide("find the latest OpenCode docs for agent config", enabled=True)
        self.assertEqual(d.workflow, "research")
        self.assertIn("web", d.profile.required_skills)

    def test_classify_dimensions_are_populated(self) -> None:
        p = decide.classify("deploy the new auth service to production")
        self.assertEqual(p.risk, "high")
        self.assertIn("infra", p.required_skills)
        self.assertEqual(p.verification_needs, "full")


class TestFailuresRecoverSafely(unittest.TestCase):
    def test_retry_is_bounded(self) -> None:
        r = delegation.retry_decision(attempt=0, limit=2, cause="model")
        self.assertTrue(r.retry)
        r2 = delegation.retry_decision(attempt=2, limit=2, cause="model")
        self.assertFalse(r2.retry)

    def test_escalation_only_on_model_cause(self) -> None:
        # Only a 'model' cause may escalate to a different model. Other causes
        # keep the same model (retry/replan/repair-instructions/etc.).
        for cause in ("provider", "orchestration", "instructions", "tools", "budget", "cancelled", "unknown"):
            nxt = router.next(cause, retries=1, max_escalations=2)
            self.assertNotEqual(nxt.get("action"), "escalate", cause)
        # A 'model' cause with retries>=1: with no catalog it raises ConfigError
        # (never guesses a model) - that is the safe outcome.
        try:
            nxt = router.next("model", retries=1, max_escalations=2)
            self.assertIn(nxt.get("action"), ("retry", "escalate", "stop", "blocked"))
        except Exception as exc:
            self.assertEqual(getattr(exc, "code", None), "CONFIG_INVALID")

    def test_safety_checks_are_mandatory(self) -> None:
        # A criterion can never be waived for token savings.
        self.assertFalse(delegation.mandatory_checks_required(criteria=["c"]).skip_allowed)
        self.assertFalse(delegation.should_skip_qc(criteria=["c"], token_pressure=True).skip)
        # No mandatory checks outstanding -> skipping is allowed.
        self.assertTrue(delegation.mandatory_checks_required().skip_allowed)
        self.assertTrue(delegation.should_skip_qc().skip)

    def test_high_risk_task_gets_full_verification(self) -> None:
        d = decide.decide("delete all user data from production", enabled=True)
        self.assertEqual(d.verification, "full")
        self.assertEqual(d.profile.risk, "high")


class TestRoutingNeverSelectsUnavailable(unittest.TestCase):
    def test_unavailable_model_is_not_selected_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = _catalog_with_unavailable(Path(tmp))
            res = router.route("code", registry=reg, accept_unknown_price=False)
            self.assertNotEqual(res.status, "ok")
            self.assertIsNone(res.model)

    def test_decide_does_not_claim_ok_with_no_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp) / "no-such-models"
            d = decide.decide("add a helper function", enabled=True, registry=reg)
            # No catalog => cannot be "ok" with a model.
            self.assertNotEqual(d.model_status, "ok")
            self.assertIsNone(d.model)

    def test_decide_picks_available_model_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = _catalog_with_available(Path(tmp))
            d = decide.decide("add a helper function", enabled=True, registry=reg)
            self.assertEqual(d.model, "good")
            self.assertIn(d.model_status, ("ok", "assumed"))

    def test_router_never_defaults_unknown_capability_to_true(self) -> None:
        # capability unreported must stay None, never become True/False.
        with tempfile.TemporaryDirectory() as tmp:
            reg = tmp_path = Path(tmp) / "models"
            reg.mkdir()
            p = Path(tmp) / "catalog.json"
            p.write_text('{"models": [{"provider": "p", "model": "quiet", "status": "active"}]}', encoding="utf-8")
            router.import_catalog(p, "fixture", registry=reg)
            entry = router.load_catalog(reg)[0]
            self.assertIsNone(entry.capability("code"))
            self.assertIsNone(entry.capability("text"))


class TestClassifyDeterminism(unittest.TestCase):
    def test_classify_is_pure_and_stable(self) -> None:
        a = decide.classify("refactor the auth module")
        b = decide.classify("refactor the auth module")
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_render_does_not_crash(self) -> None:
        d = decide.decide("hello", enabled=True)
        self.assertIn("workflow", decide.render(d))


if __name__ == "__main__":
    unittest.main()

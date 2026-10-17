"""Router: pins, recorded evidence, budgets, diagnosis and escalation.

Every test here runs against a *recorded* catalog fixture and a temporary
registry - no network, no model, no API spend. The assertions are written to
fail if the router ever invents a price, defaults an unknown capability, or
routes around a pin.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from swarmkit import router
from swarmkit.errors import BudgetExceeded, ConfigError
from tests.support import TempWorkspace


def _model(
    provider: str,
    name: str,
    *,
    status: str = "active",
    caps: dict[str, object] | None = None,
    price_in: float | None = None,
    price_out: float | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {"provider": provider, "model": name, "status": status}
    if caps is not None:
        record["capabilities"] = caps
    if price_in is not None:
        record["price_in_per_mtok"] = price_in
    if price_out is not None:
        record["price_out_per_mtok"] = price_out
    return record


class RouterCase(TempWorkspace):
    """A temp workspace with a registry path and a catalog-writing helper."""

    def setUp(self) -> None:
        super().setUp()
        self.registry = self.ws / "models"

    def import_models(self, models: list[dict[str, object]], source: str = "fixture") -> dict:
        path = self.ws / "catalog.json"
        path.write_text(json.dumps({"models": models}), encoding="utf-8")
        return router.import_catalog(path, source, registry=self.registry)


class TestImportPreservesUnknowns(RouterCase):
    def test_unreported_capability_is_none_not_defaulted(self) -> None:
        """A field the host stayed silent on must read as None, never False/True."""
        self.import_models(
            [
                _model("p", "quiet", caps={"text": True}),
                _model("p", "silent"),  # no capabilities key at all
            ]
        )
        entries = {e.model: e for e in router.load_catalog(self.registry)}
        self.assertIs(entries["quiet"].capability("text"), True)
        self.assertIsNone(entries["quiet"].capability("code"))
        self.assertIsNone(entries["quiet"].capability("vision"))
        for key in ("text", "tools", "vision", "reasoning", "code", "audio"):
            self.assertIsNone(entries["silent"].capability(key), key)

    def test_unreported_price_stays_none(self) -> None:
        self.import_models([_model("p", "unpriced", caps={"text": True})])
        entry = router.load_catalog(self.registry)[0]
        self.assertIsNone(entry.price_in_per_mtok)
        self.assertIsNone(entry.price_out_per_mtok)
        self.assertFalse(entry.priced)

    def test_capability_list_reads_as_supported_only(self) -> None:
        path = self.ws / "catalog.json"
        path.write_text(
            json.dumps({"models": [{"provider": "p", "model": "listed", "capabilities": ["text", "tools"]}]}),
            encoding="utf-8",
        )
        router.import_catalog(path, "fixture", registry=self.registry)
        entry = router.load_catalog(self.registry)[0]
        self.assertIs(entry.capability("text"), True)
        # absence from the list is not proof of absence of the capability
        self.assertIsNone(entry.capability("vision"))

    def test_missing_catalog_is_refused(self) -> None:
        with self.assertRaises(ConfigError):
            router.load_catalog(self.registry)


class TestPins(RouterCase):
    def test_pin_is_honoured(self) -> None:
        self.import_models([_model("p", "active", caps={"code": True}, price_in=1.0, price_out=1.0)])
        decision = router.route("code", registry=self.registry, pin="p/active")
        self.assertEqual(decision.status, "ok")
        self.assertEqual((decision.provider, decision.model), ("p", "active"))
        self.assertTrue(router.is_one_sentence(decision.reason))

    def test_missing_pin_returns_pinned_missing(self) -> None:
        self.import_models([_model("p", "active", caps={"code": True})])
        decision = router.route("code", registry=self.registry, pin="ghost/none")
        self.assertEqual(decision.status, "pinned-missing")
        self.assertIn("ghost/none", decision.reason)
        self.assertIn("p/active", decision.reason)
        self.assertTrue(router.is_one_sentence(decision.reason))

    def test_disabled_pin_returns_pinned_missing(self) -> None:
        self.import_models(
            [
                _model("p", "dead", status="deprecated", caps={"code": True}),
                _model("p", "live", caps={"code": True}),
            ]
        )
        decision = router.route("code", registry=self.registry, pin="p/dead")
        self.assertEqual(decision.status, "pinned-missing")
        self.assertIn("deprecated", decision.reason)

    def test_incapable_pin_returns_pinned_incompatible(self) -> None:
        self.import_models(
            [
                _model("p", "blind", caps={"vision": False}),
                _model("p", "seer", caps={"vision": True}),
            ]
        )
        decision = router.route("visual", registry=self.registry, pin="p/blind")
        self.assertEqual(decision.status, "pinned-incompatible")
        self.assertIn("vision", decision.reason)
        self.assertIn("p/seer", decision.reason)

    def test_unknown_capability_pin_is_assumed(self) -> None:
        self.import_models([_model("p", "mystery", price_in=1.0, price_out=1.0)])
        decision = router.route("code", registry=self.registry, pin="p/mystery")
        self.assertEqual(decision.status, "assumed")
        self.assertEqual(decision.assumed, ["code"])
        self.assertIn("code", decision.reason)


class TestBudget(RouterCase):
    def test_unknown_price_blocks_unless_accepted(self) -> None:
        """An unpriced model is not free: it must block until risk is accepted."""
        self.import_models([_model("p", "unpriced", caps={"code": True})])
        blocked = router.route("code", registry=self.registry, pin="p/unpriced")
        self.assertEqual(blocked.status, "blocked")
        self.assertIn("price", blocked.reason.lower())
        accepted = router.route(
            "code", registry=self.registry, pin="p/unpriced", accept_unknown_price=True
        )
        self.assertEqual(accepted.status, "ok")

    def test_budget_refusal_names_the_model_and_the_limit(self) -> None:
        self.import_models([_model("p", "costly", caps={"code": True}, price_in=1.0, price_out=1.0)])
        decision = router.route(
            "code",
            registry=self.registry,
            pin="p/costly",
            est_tokens_in=2_000_000,
            budget_remaining=1.0,
        )
        self.assertEqual(decision.status, "blocked")
        self.assertIn("p/costly", decision.reason)
        self.assertIn("1.0000", decision.reason)

    def test_budget_function_refuses_a_charge_over_ceiling(self) -> None:
        router.budget(max_cost=5.0, registry=self.registry)
        router.budget(registry=self.registry, charge_cost=3.0)
        snapshot = router.budget(registry=self.registry)
        self.assertAlmostEqual(snapshot["remaining_cost"], 2.0)
        with self.assertRaises(BudgetExceeded):
            router.budget(registry=self.registry, charge_cost=3.0)


class TestEvidenceScoring(RouterCase):
    def test_too_few_attempts_defers_to_default(self) -> None:
        """Below the evidence floor, the reasoned answer is 'too few to prefer'."""
        self.import_models(
            [
                _model("solo", "one", caps={"code": True}, price_in=1.0, price_out=1.0),
                _model("backup", "two", caps={"code": True}, price_in=1.0, price_out=1.0),
            ]
        )
        for _ in range(3):
            router.record("solo", "one", "code", "verified", registry=self.registry)
        decision = router.route("code", registry=self.registry, default="backup/two")
        self.assertEqual(decision.status, "ok")
        self.assertEqual(decision.model, "two")
        self.assertIn("too few to prefer", decision.reason)

    def test_verified_completions_outrank_a_cheaper_failing_model(self) -> None:
        self.import_models(
            [
                _model("pricey", "good", caps={"code": True}, price_in=10.0, price_out=10.0),
                _model("cheap", "bad", caps={"code": True}, price_in=0.1, price_out=0.1),
            ]
        )
        for _ in range(5):
            router.record("pricey", "good", "code", "verified", registry=self.registry)
        for _ in range(5):
            router.record("cheap", "bad", "code", "failed", registry=self.registry)
        decision = router.route("code", registry=self.registry, default="cheap/bad")
        self.assertEqual(decision.status, "ok")
        self.assertEqual(decision.model, "good")
        self.assertIn("5 verified completions", decision.reason)

    def test_unverified_runs_are_not_attempts(self) -> None:
        self.import_models([_model("p", "shy", caps={"code": True}, price_in=1.0, price_out=1.0)])
        for _ in range(9):
            router.record("p", "shy", "code", "unverified", registry=self.registry)
        rows = router.history("code", registry=self.registry)
        self.assertEqual(rows[0]["attempts"], 0)
        self.assertFalse(rows[0]["enough_evidence"])
        self.assertIn("too few to prefer", router.route("code", registry=self.registry, default="p/shy").reason)

    def test_self_judged_verification_is_not_evidence(self) -> None:
        self.import_models([_model("p", "judge", caps={"code": True})])
        for _ in range(5):
            router.record(
                "p", "judge", "code", "verified", registry=self.registry, judge="p/judge"
            )
        rows = router.history("code", registry=self.registry)
        self.assertEqual(rows[0]["verified"], 0)
        self.assertEqual(rows[0]["verdict"], "failing")

    def test_history_shows_belief_and_evidence(self) -> None:
        self.import_models([_model("p", "m", caps={"code": True})])
        for _ in range(5):
            router.record("p", "m", "code", "verified", registry=self.registry)
        row = router.history("code", registry=self.registry)[0]
        self.assertEqual(row["verified"], 5)
        self.assertEqual(row["attempts"], 5)
        self.assertEqual(row["verdict"], "proven")
        self.assertTrue(row["enough_evidence"])


class TestDiagnose(unittest.TestCase):
    def test_mappings(self) -> None:
        cases = {
            "429 rate limit exceeded": "provider",
            "unknown tool: apply_patch": "tools",
            "bad plan": "orchestration",
            "a vague and ambiguous request": "instructions",
            "connection timed out after 30s": "provider",
            "budget exceeded": "budget",
            "run aborted by operator": "cancelled",
            "model does not accept image input": "model",
            "asdf qwer zxcv": "unknown",
            "": "unknown",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(router.diagnose(text, retries=0), expected)

    def test_unknown_is_never_attributed_to_the_model(self) -> None:
        self.assertEqual(router.diagnose("zomg a potato", retries=4), "unknown")


class TestEscalation(RouterCase):
    def test_next_does_not_switch_model_on_single_provider_failure(self) -> None:
        decision = router.next("provider", tried=["p/a"], retries=0, registry=self.registry)
        self.assertEqual(decision["status"], "ok")
        self.assertEqual(decision["action"], "retry")
        self.assertEqual(decision["model"], "a")
        self.assertIsNotNone(decision["backoff_s"])

    def test_single_model_failure_retries_same_model(self) -> None:
        decision = router.next("model", tried=["p/a"], retries=0, registry=self.registry)
        self.assertEqual(decision["action"], "retry")

    def test_repeat_model_failure_escalates_to_a_different_model(self) -> None:
        self.import_models(
            [
                _model("p", "a", caps={"code": True}, price_in=1.0, price_out=1.0),
                _model("p", "b", caps={"code": True}, price_in=1.0, price_out=1.0),
            ]
        )
        decision = router.next(
            "model", category="code", tried=["p/a"], retries=1, registry=self.registry
        )
        self.assertEqual(decision["status"], "ok")
        self.assertEqual(decision["action"], "escalate")
        self.assertEqual(decision["model"], "b")

    def test_exhausting_escalation_budget_returns_blocked(self) -> None:
        decision = router.next(
            "model",
            tried=["p/a"],
            retries=1,
            escalations_used=3,
            max_escalations=3,
            registry=self.registry,
        )
        self.assertEqual(decision["status"], "blocked")
        self.assertIn("escalation budget", decision["reason"])

    def test_pin_is_never_routed_around(self) -> None:
        decision = router.next(
            "model", tried=["p/a"], retries=2, pin="p/a", registry=self.registry
        )
        self.assertEqual(decision["status"], "blocked")
        self.assertIn("pin", decision["reason"])


class TestJudging(RouterCase):
    def test_model_authored_verification_change_never_promotes(self) -> None:
        verdict = router.promotion_gate(
            {"target": "tests/test_gate.py", "authored_by": "p/m", "verification": True}
        )
        self.assertEqual(verdict["status"], "refused")
        self.assertIn("never auto-promote", verdict["reason"])

    def test_unverified_change_is_not_evidence(self) -> None:
        verdict = router.promotion_gate(
            {"target": "src/app.py", "authored_by": "p/m", "verification": False}
        )
        self.assertEqual(verdict["status"], "refused")
        self.assertIn("not evidence", verdict["reason"])

    def test_plain_verified_change_may_promote(self) -> None:
        verdict = router.promotion_gate(
            {"target": "src/app.py", "authored_by": "p/m", "verification": True}
        )
        self.assertEqual(verdict["status"], "ok")


if __name__ == "__main__":
    unittest.main()

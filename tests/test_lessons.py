"""Lesson ledger: grounding, dedup, contradiction, capping, and promotion."""

from __future__ import annotations

import unittest

from swarmkit.errors import LessonError
from swarmkit.lessons import (
    DEFAULT_CAP_LINES,
    Ledger,
    add_lesson,
    audit,
    polarity,
    promote,
    subject_key,
)


class TestPolarity(unittest.TestCase):
    def test_negative_polarity(self) -> None:
        for rule in ("Never pin a model", "Avoid worktrees", "Don't guess"):
            self.assertEqual(polarity(rule), -1, rule)

    def test_positive_polarity(self) -> None:
        for rule in ("Must record evidence", "Always use worktrees"):
            self.assertEqual(polarity(rule), 1, rule)

    def test_subject_key_ignores_leading_modal(self) -> None:
        self.assertEqual(
            subject_key("Never use worktrees"), subject_key("Always use worktrees")
        )


class TestAddLesson(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self.dir = Path(tempfile.mkdtemp())
        self.ledger_path = self.dir / "lessons.md"

    def test_lesson_without_failure_is_refused(self) -> None:
        with self.assertRaises(LessonError) as ctx:
            add_lesson(self.ledger_path, "Never trust exit 0")
        self.assertEqual(ctx.exception.code, "LESSON_WITHOUT_ORIGIN")

    def test_lesson_records_origin_and_starts_unverified(self) -> None:
        add_lesson(
            self.ledger_path,
            "Always run the build before delivering",
            failure="shipped a broken bundle",
        )
        led = Ledger.load(self.ledger_path)
        self.assertEqual(len(led.lessons), 1)
        lesson = led.lessons[0]
        self.assertEqual(lesson.rule, "Always run the build before delivering")
        self.assertEqual(lesson.failure, "shipped a broken bundle")
        self.assertFalse(lesson.verified)

    def test_header_is_created_on_first_write(self) -> None:
        add_lesson(self.ledger_path, "Never guess", failure="guessed once")
        self.assertTrue(self.ledger_path.read_text(encoding="utf-8").strip())

    def test_duplicate_is_refused(self) -> None:
        add_lesson(self.ledger_path, "Never trust exit 0", failure="shipped red")
        result = add_lesson(self.ledger_path, "Never trust exit 0", failure="shipped red again")
        self.assertFalse(result["added"])
        self.assertEqual(result["reason"], "duplicate")
        self.assertEqual(len(Ledger.load(self.ledger_path).lessons), 1)

    def test_contradiction_is_refused_by_default(self) -> None:
        add_lesson(self.ledger_path, "Never use worktrees", failure="merge conflict")
        result = add_lesson(
            self.ledger_path,
            "Always use worktrees",
            failure="concurrent edits clobbered each other",
        )
        self.assertFalse(result["added"])
        self.assertEqual(result["reason"], "contradiction")
        self.assertIn("hint", result)

    def test_contradiction_can_be_overridden_explicitly(self) -> None:
        add_lesson(self.ledger_path, "Never use worktrees", failure="merge conflict")
        result = add_lesson(
            self.ledger_path,
            "Always use worktrees",
            failure="concurrent edits clobbered each other",
            allow_contradiction=True,
        )
        self.assertTrue(result["added"])
        led = Ledger.load(self.ledger_path)
        self.assertEqual(len(led.lessons), 2)
        self.assertEqual(polarity(led.lessons[0].rule), -1)
        self.assertEqual(polarity(led.lessons[1].rule), 1)

    def test_cap_stops_growth(self) -> None:
        reasons = []
        for i in range(DEFAULT_CAP_LINES * 3):
            result = add_lesson(
                self.ledger_path,
                f"Never skip verification gate number {i}",
                failure=f"shipped broken artifact {i}",
            )
            if not result["added"]:
                reasons.append(result["reason"])
                break
        self.assertEqual(reasons, ["cap_reached"])
        self.assertLessEqual(Ledger.load(self.ledger_path).line_count(), DEFAULT_CAP_LINES + 20)


class TestPromotion(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self.dir = Path(tempfile.mkdtemp())
        self.ledger_path = self.dir / "lessons.md"
        add_lesson(
            self.ledger_path,
            "Never skip the delivery gate",
            failure="delivered with an unproven criterion",
        )

    def test_promotion_requires_passing_tests(self) -> None:
        with self.assertRaises(LessonError) as ctx:
            promote(
                self.ledger_path,
                "Never skip the delivery gate",
                tests_passed=False,
                evidence="it sounds right",
            )
        self.assertEqual(ctx.exception.code, "PROMOTION_BLOCKED")

    def test_promotion_requires_evidence(self) -> None:
        with self.assertRaises(LessonError):
            promote(
                self.ledger_path,
                "Never skip the delivery gate",
                tests_passed=True,
                evidence="",
            )

    def test_unknown_rule_cannot_be_promoted(self) -> None:
        with self.assertRaises(LessonError):
            promote(self.ledger_path, "Some rule nobody wrote", tests_passed=True, evidence="x")

    def test_promotion_flips_the_status(self) -> None:
        promote(
            self.ledger_path,
            "Never skip the delivery gate",
            tests_passed=True,
            evidence="tests/test_gate.py::test_delivery_allowed_once_verified OK",
        )
        led = Ledger.load(self.ledger_path)
        self.assertTrue(led.lessons[0].verified)
        self.assertIn("verified", self.ledger_path.read_text(encoding="utf-8"))


class TestAudit(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self.dir = Path(tempfile.mkdtemp())
        self.ledger_path = self.dir / "lessons.md"

    def test_ungrounded_lessons_are_reported(self) -> None:
        self.ledger_path.write_text(
            "## Lessons\n\n"
            "## 2026-01-01 run-a\n"
            "Never ship on a red build\n"
            "- status: UNVERIFIED (not promoted)\n",
            encoding="utf-8",
        )
        report = audit(self.ledger_path)
        self.assertEqual(len(report["ungrounded"]), 1)
        self.assertEqual(report["ungrounded"][0]["rule"], "Never ship on a red build")

    def test_audit_is_quiet_on_a_healthy_ledger(self) -> None:
        add_lesson(self.ledger_path, "Never ship on a red build", failure="it was red")
        report = audit(self.ledger_path)
        self.assertFalse(report["ungrounded"])
        self.assertFalse(report["duplicates"])
        self.assertFalse(report["contradictions"])
        self.assertEqual(report["total"], 1)

    def test_audit_reports_line_count_against_cap(self) -> None:
        add_lesson(self.ledger_path, "Never ship on a red build", failure="it was red")
        report = audit(self.ledger_path)
        self.assertEqual(report["cap"], DEFAULT_CAP_LINES)
        self.assertGreater(report["lines"], 0)


if __name__ == "__main__":
    unittest.main()
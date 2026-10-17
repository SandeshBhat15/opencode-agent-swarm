"""Context discipline: minimal packets, compact reports, refuse-don't-truncate.

Every test here exists to pin the two failures the module was written to make
impossible: a spawn carrying context it must not have, and a summary that hits a
token target by dropping a constraint.
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from swarmkit import context
from swarmkit.context import (
    Checkpoint,
    CommandResult,
    ContextRefusal,
    DependencyRef,
    FileChange,
    FileRef,
    TruncationLog,
    WorkerReport,
)
from swarmkit.errors import ConfigError
from tests.support import TempWorkspace

REQUIRED = context.REQUIRED_SECTIONS


def _report(**over: object) -> WorkerReport:
    """A minimal well-formed report; tests override one field at a time."""
    base: dict[str, object] = {
        "task_id": "ws1-build",
        "outcome": "ok",
        "commands": [CommandResult("python -m unittest tests.test_context", "OK", 0)],
    }
    base.update(over)
    return WorkerReport(**base)  # type: ignore[arg-type]


def _sections(**over: object) -> dict[str, object]:
    """All five required sections present, minimal content."""
    base: dict[str, object] = {
        "requirements": ["build swarmkit/context.py"],
        "constraints": ["stdlib only", "no network"],
        "decisions": ["allowlist over denylist"],
        "unresolved": ["none"],
        "evidence": ["tests/test_context.py passes"],
    }
    base.update(over)
    return base


class TestBuildPacketExcludes(unittest.TestCase):
    def test_packet_excludes_the_forbidden_categories_and_says_why(self) -> None:
        packet = context.build_packet(
            "enforce context discipline in swarmkit/context.py",
            task_id="ws1",
            criteria=["imports cleanly"],
            dependencies=["ws0"],
            instructions=["stdlib only"],
            files=["swarmkit/context.py:1-40"],
        )
        categories = {e.category for e in packet.excluded()}
        self.assertIn("conversation_history", categories)
        self.assertIn("full_repository", categories)
        self.assertIn("task_history", categories)
        self.assertIn("other_workers_reports", categories)
        # every exclusion carries its WHY, not just a name
        for exclusion in packet.excluded():
            self.assertTrue(exclusion.reason.strip(), exclusion.category)

    def test_packet_includes_criteria_dependencies_and_files(self) -> None:
        packet = context.build_packet(
            "do the thing",
            task_id="ws1",
            deliverable="a module",
            criteria=["c1", "c2"],
            dependencies=[DependencyRef("ws0", "exported the schema")],
            instructions=["rule a"],
            files=["a.py:1-10", "b/c.py"],
        )
        self.assertEqual(packet.criteria, ("c1", "c2"))
        self.assertEqual([d.id for d in packet.dependencies], ["ws0"])
        self.assertEqual(packet.instructions, ("rule a",))
        self.assertEqual([(f.path, f.start, f.end) for f in packet.files],
                         [("a.py", 1, 10), ("b/c.py", None, None)])
        rendered = packet.render()
        self.assertIn("ACCEPTANCE CRITERIA:", rendered)
        self.assertIn("DEPENDENCIES:", rendered)
        self.assertIn("TARGETED FILES:", rendered)

    def test_a_withheld_field_is_refused_by_name(self) -> None:
        """Pasting the conversation in must fail, not silently succeed."""
        with self.assertRaises(ConfigError) as ctx:
            context.build_packet("do it", conversation=[{"role": "user"}])  # type: ignore[call-arg]
        self.assertEqual(ctx.exception.code, "CONTEXT_OVERREACH")
        self.assertIn("conversation_history", str(ctx.exception))

    def test_repository_and_reports_fields_are_refused(self) -> None:
        for field in ("repository", "file_contents", "other_reports", "history"):
            with self.subTest(field=field):
                with self.assertRaises(ConfigError):
                    context.build_packet("do it", **{field: "..."})  # type: ignore[call-arg]

    def test_unknown_field_is_refused(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            context.build_packet("do it", wat="x")  # type: ignore[call-arg]
        self.assertEqual(ctx.exception.code, "CONTEXT_UNKNOWN_FIELD")

    def test_packet_to_dict_has_no_open_bag(self) -> None:
        """No extra/raw/meta field: there is nowhere to hide a transcript."""
        packet = context.build_packet("do it")
        self.assertEqual(
            set(packet.to_dict()),
            {"task_id", "task", "deliverable", "criteria", "dependencies",
             "instructions", "files", "excluded"},
        )
        for banned in ("extra", "raw", "meta", "conversation", "messages"):
            self.assertFalse(hasattr(packet, banned), banned)

    def test_a_file_entry_carrying_a_body_is_refused(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            context.build_packet("do it", files=["a.py\nprint('whole repo')"])
        self.assertEqual(ctx.exception.code, "CONTEXT_OVERREACH")

    def test_a_repository_sized_file_list_is_refused(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            context.build_packet("do it", files=[f"f{i}.py" for i in range(200)])
        self.assertEqual(ctx.exception.code, "CONTEXT_TOO_BROAD")

    def test_a_dependency_report_is_refused(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            context.build_packet(
                "do it", dependencies=[DependencyRef("ws0", "x" * 500)]
            )
        self.assertEqual(ctx.exception.code, "CONTEXT_OVERREACH")

    def test_an_over_long_task_reads_like_a_transcript_and_is_refused(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            context.build_packet("line\n" * 2000)
        self.assertEqual(ctx.exception.code, "CONTEXT_OVERREACH")

    def test_empty_task_is_refused(self) -> None:
        with self.assertRaises(ConfigError):
            context.build_packet("   ")

    def test_an_optional_ceiling_refuses_rather_than_shipping_a_huge_packet(self) -> None:
        packet = context.build_packet(
            "do it", criteria=["c"] * 20, instructions=["i"] * 20
        )
        with self.assertRaises(ContextRefusal):
            context.build_packet(
                "do it", criteria=["c"] * 20, instructions=["i"] * 20,
                max_estimated_tokens=5,
            )
        self.assertGreater(packet.estimated_tokens(), 5)


class TestFileRef(unittest.TestCase):
    def test_ranges_are_parsed_and_rendered(self) -> None:
        self.assertEqual(FileRef.from_string("a.py:10").start, 10)
        self.assertEqual(FileRef.from_string("a.py:10-20").end, 20)
        self.assertIsNone(FileRef.from_string("a.py").start)
        # a Windows drive path keeps its colon
        self.assertEqual(FileRef.from_string(r"C:\x\y.py").path, r"C:\x\y.py")
        self.assertEqual(FileRef.from_string("a.py:1-3").render(), "a.py:1-3")


class TestWorkerReport(unittest.TestCase):
    def test_a_complete_report_has_no_problems(self) -> None:
        report = _report(
            summary="built it",
            files=[FileChange("swarmkit/context.py", "created")],
            artifacts=["artifact .opencode/logs/build.txt"],
            decisions=["allowlist over denylist"],
            assumptions=["chars/4 is good enough"],
            issues=["none"],
            blockers=[],
            evidence=["tests/test_context.py"],
        )
        self.assertEqual(context.validate_report(report), [])

    def test_a_report_with_no_outcome_is_rejected(self) -> None:
        problems = context.validate_report(_report(outcome=""))
        self.assertTrue(any("outcome" in p for p in problems), problems)

    def test_a_command_with_no_recorded_result_is_rejected(self) -> None:
        report = _report(commands=[CommandResult("npm test", "   ", 0)])
        problems = context.validate_report(report)
        self.assertTrue(any("no recorded result" in p for p in problems), problems)

    def test_a_command_with_no_text_is_rejected(self) -> None:
        problems = context.validate_report(_report(commands=[CommandResult("", "ok")]))
        self.assertTrue(any("no command text" in p for p in problems), problems)

    def test_an_unknown_outcome_is_rejected(self) -> None:
        problems = context.validate_report(_report(outcome="mostly fine"))
        self.assertTrue(any("unknown outcome" in p for p in problems), problems)

    def test_a_report_without_a_task_id_is_rejected(self) -> None:
        problems = context.validate_report(_report(task_id=""))
        self.assertTrue(any("task_id" in p for p in problems), problems)

    def test_an_unknown_file_change_is_rejected(self) -> None:
        problems = context.validate_report(_report(files=[FileChange("a.py", "renamed")]))
        self.assertTrue(any("unknown change" in p for p in problems), problems)

    def test_report_round_trips_through_render_and_dict(self) -> None:
        report = _report(files=[FileChange("a.py")])
        self.assertIn("REPORT ws1-build outcome=ok", report.render())
        self.assertEqual(report.to_dict()["files"], [{"path": "a.py", "change": "modified"}])
        self.assertGreater(report.estimated_tokens(), 0)


class TestSummarise(unittest.TestCase):
    def test_a_long_input_preserves_all_five_categories(self) -> None:
        sections = {
            "requirements": [f"requirement {i} " + "x" * 40 for i in range(40)],
            "constraints": [f"constraint {i} " + "y" * 40 for i in range(40)],
            "decisions": [f"decision {i} " + "z" * 40 for i in range(40)],
            "unresolved": [f"open issue {i} " + "u" * 40 for i in range(40)],
            "evidence": [f"check {i} passed (exit 0) " + "e" * 40 for i in range(40)],
        }
        checkpoint = context.summarise(sections, max_tokens=100000)
        for name in REQUIRED:
            with self.subTest(section=name):
                self.assertEqual(set(getattr(checkpoint, name)), set(sections[name]))
        rendered = checkpoint.render()
        for name in REQUIRED:
            self.assertIn(f"{name.upper()}:", rendered)
        self.assertLessEqual(checkpoint.estimated_tokens, 100000)

    def test_exact_duplicates_are_deduped_but_nothing_distinct_is_lost(self) -> None:
        sections = _sections(constraints=["stdlib only", "stdlib only", "no network"])
        checkpoint = context.summarise(sections, max_tokens=100000)
        self.assertEqual(checkpoint.constraints, ("stdlib only", "no network"))
        self.assertEqual(set(checkpoint.constraints), {"stdlib only", "no network"})

    def test_a_tiny_budget_refuses_and_names_what_it_could_not_preserve(self) -> None:
        sections = {
            "requirements": ["system must answer 200 on localhost:5173"],
            "constraints": ["must stay offline"],
            "decisions": ["chose Vite over CRA"],
            "unresolved": ["no browser screenshot yet"],
            "evidence": ["curl -I returned 200"],
        }
        with self.assertRaises(ContextRefusal) as ctx:
            context.summarise(sections, max_tokens=5)
        refusal = ctx.exception
        self.assertEqual(refusal.code, "CONTEXT_BUDGET_EXCEEDED")
        self.assertEqual(set(refusal.unpreserved), set(REQUIRED))
        message = str(refusal)
        for name in ("constraints", "evidence", "requirements"):
            self.assertIn(name, message)
        # it names the largest item too, so the caller knows where the weight is
        self.assertIn("largest item", message)

    def test_a_missing_required_section_is_refused(self) -> None:
        """Forgetting 'constraints' must not look like 'there were none'."""
        sections = _sections()
        del sections["constraints"]
        with self.assertRaises(ConfigError) as ctx:
            context.summarise(sections, max_tokens=100000)
        self.assertEqual(ctx.exception.code, "CONTEXT_SECTION_MISSING")

    def test_an_unknown_section_is_refused(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            context.summarise(_sections(vibes=["..."]), max_tokens=100000)
        self.assertEqual(ctx.exception.code, "CONTEXT_UNKNOWN_SECTION")

    def test_optional_sections_are_the_only_thing_shed_to_fit(self) -> None:
        sections = _sections(notes=[f"note {i} " + "n" * 60 for i in range(50)])
        checkpoint = context.summarise(sections, max_tokens=200)
        self.assertEqual(checkpoint.dropped_optional, ("notes",))
        self.assertLessEqual(checkpoint.estimated_tokens, 200)
        # the required five survived intact
        for name in REQUIRED:
            with self.subTest(section=name):
                self.assertEqual(set(getattr(checkpoint, name)), set(sections[name]))

    def test_a_non_positive_budget_is_a_config_error(self) -> None:
        for bad in (0, -1, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigError):
                    context.summarise(_sections(), max_tokens=bad)  # type: ignore[arg-type]

    def test_a_checkpoint_cannot_be_built_without_a_required_section(self) -> None:
        """The refusal is in the type, not only in the function."""
        with self.assertRaises(TypeError):
            Checkpoint(  # type: ignore[call-arg]
                requirements=("a",), decisions=("b",), unresolved=(), evidence=()
            )


class TestTokenEstimate(unittest.TestCase):
    def test_estimate_is_a_labelled_heuristic(self) -> None:
        method = context.TOKEN_ESTIMATE_METHOD
        self.assertIn("ESTIMATE", method.upper())
        self.assertIn("NOT", method.upper())
        self.assertIn("heuristic", method.lower())

    def test_estimate_basics(self) -> None:
        self.assertEqual(context.estimate_tokens(""), 0)
        self.assertEqual(context.estimate_tokens("abcd"), 1)
        self.assertGreater(context.estimate_tokens("a" * 400), 50)

    def test_token_estimate_carries_the_label_with_the_number(self) -> None:
        estimate = context.token_estimate("hello world")
        self.assertTrue(estimate.is_estimate)
        self.assertEqual(estimate.method, context.TOKEN_ESTIMATE_METHOD)
        self.assertIn("tokens", estimate.render())
        self.assertIn("not a tokenizer", estimate.render().lower())

    def test_fits_accepts_a_payload_under_the_ceiling(self) -> None:
        result = context.fits(1000, "a small payload")
        self.assertTrue(result.ok)
        self.assertGreater(result.headroom(), 0)
        self.assertIn("estimate, not a measurement", result.render())

    def test_fits_refuses_an_over_ceiling_request_and_records_it(self) -> None:
        log = TruncationLog()
        with self.assertRaises(ContextRefusal) as ctx:
            context.fits(50, "x" * 1000, log=log)
        self.assertEqual(ctx.exception.code, "CONTEXT_LIMIT_EXCEEDED")
        self.assertEqual(len(log.events), 1)
        event = log.events[0]
        self.assertEqual(event.limit, 50)
        self.assertGreater(event.estimated_tokens, 50)
        self.assertIs(ctx.exception.event, event)
        self.assertTrue(event.at)

    def test_reserved_budget_can_push_a_fitting_payload_over(self) -> None:
        with self.assertRaises(ContextRefusal):
            context.fits(100, "y" * 200, reserved=100)

    def test_fits_renders_a_packet_payload(self) -> None:
        packet = context.build_packet("do it", criteria=["c1"])
        result = context.fits(1000, packet)
        self.assertEqual(result.tokens, packet.estimated_tokens())

    def test_fits_validates_its_ceiling(self) -> None:
        with self.assertRaises(ConfigError):
            context.fits(0, "x")
        with self.assertRaises(ConfigError):
            context.fits(1000, "x", reserved=-5)


class TestArtifacts(TempWorkspace):
    def test_store_artifact_returns_a_reference_whose_bytes_round_trip(self) -> None:
        content = "line one\nline two\n"
        target = self.ws / "logs" / "build.txt"
        ref = context.store_artifact(target, content)
        self.assertEqual(Path(ref.path).read_text(encoding="utf-8"), content)
        self.assertEqual(ref.bytes, len(content.encode("utf-8")))
        self.assertEqual(ref.lines, 2)
        self.assertEqual(ref.sha256, hashlib.sha256(content.encode("utf-8")).hexdigest())
        self.assertEqual(context.read_artifact(ref), content)
        self.assertIn("artifact", ref.render())

    def test_binary_content_is_stored_and_hashed(self) -> None:
        ref = context.store_artifact(self.ws / "out.bin", b"\x00\x01\x02")
        self.assertEqual(Path(ref.path).read_bytes(), b"\x00\x01\x02")
        self.assertEqual(ref.bytes, 3)

    def test_a_path_outside_the_root_is_refused(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            context.store_artifact(self.ws.parent / "outside.txt", "x", root=self.ws)
        self.assertEqual(ctx.exception.code, "ARTIFACT_OUTSIDE_ROOT")


class TestSectionsFromPacket(unittest.TestCase):
    def test_a_packet_becomes_a_checkpoint_input_without_missing_a_category(self) -> None:
        packet = context.build_packet(
            "do it",
            criteria=["c1"],
            dependencies=[DependencyRef("ws0", "schema done")],
            instructions=["rule a"],
            files=["a.py:1-5"],
        )
        sections = context.sections_from_packet(packet)
        self.assertEqual(set(sections), set(REQUIRED))
        checkpoint = context.summarise(sections, max_tokens=100000)
        self.assertIn("do it", checkpoint.requirements)
        self.assertIn("c1", checkpoint.requirements)
        self.assertIn("rule a", checkpoint.constraints)


if __name__ == "__main__":
    unittest.main()

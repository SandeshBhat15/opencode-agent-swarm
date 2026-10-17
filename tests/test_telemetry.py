"""Tests for :mod:`swarmkit.telemetry`.

The cases that matter are the ones that keep a measurement honest:

* an unmeasured number must not become a zero;
* a *reported* zero must stay a zero (the two are different claims);
* a PowerShell-redirected log must still parse;
* a cheaper run that broke the outcome must be refused, not promoted.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from swarmkit import telemetry as T


def step_finish(inp: int, out: int, reasoning: int, cached: int, cost: float = 0.0) -> dict:
    return {
        "type": "step_finish",
        "timestamp": 2,
        "sessionID": "ses_x",
        "part": {
            "type": "step-finish",
            "reason": "tool-calls",
            "cost": cost,
            "tokens": {
                "input": inp,
                "output": out,
                "reasoning": reasoning,
                "cache": {"read": cached, "write": 0},
            },
        },
    }


def tool_use(tool: str, status: str, error: str | None = None) -> dict:
    return {
        "type": "tool_use",
        "timestamp": 3,
        "sessionID": "ses_x",
        "part": {
            "partID": "p1",
            "messageID": "m1",
            "type": "tool",
            "id": "call_1",
            "tool": tool,
            "state": {"status": status, "error": error, "input": {}, "output": ""},
        },
    }


def as_log(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def session(
    sid: str,
    *,
    parent: str | None = None,
    inp: int = 0,
    out: int = 0,
    cost: float = 0.0,
    outcome: str = "succeeded",
    elapsed_ms: int = 1000,
    title: str = "t",
) -> dict:
    row = {
        "id": sid,
        "agent": "swarm/builder",
        "model": {"id": "m", "providerID": "p", "variant": None},
        "cost": cost,
        "tokens": {"input": inp, "output": out, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        "outcome": outcome,
        "time": {"created": 1_000_000, "updated": 1_000_000 + elapsed_ms},
        "title": title,
    }
    if parent:
        row["parentID"] = parent
    return row


class TestUnmeasuredIsNone(unittest.TestCase):
    def test_empty_record_reports_nothing_measured(self) -> None:
        usage = T.from_session_record({})
        for name in T.MEASURED_FIELDS:
            self.assertIsNone(getattr(usage, name), name)
        self.assertIsNone(usage.total_tokens)
        self.assertIn("input_tokens", usage.unmeasured())

    def test_reported_zero_stays_zero(self) -> None:
        usage = T.from_session_record(session("ses_a", inp=10, out=2))
        # cache.write was reported as 0 - that is a measurement, not a gap.
        self.assertEqual(usage.cache_write_tokens, 0)
        self.assertNotIn("cache_write_tokens", usage.unmeasured())
        # cost was reported as 0.0 - also a measurement.
        self.assertEqual(usage.cost_usd, 0.0)

    def test_non_numeric_value_is_unmeasured_not_zero(self) -> None:
        usage = T.from_session_record({"tokens": {"input": "many"}})
        self.assertIsNone(usage.input_tokens)

    def test_non_mapping_record_is_refused(self) -> None:
        with self.assertRaises(T.TelemetryError):
            T.from_session_record(["not", "a", "record"])  # type: ignore[arg-type]


class TestObservedBaselineRecord(unittest.TestCase):
    """The real shape measured on this host, parsed for the real numbers."""

    RECORD = {
        "id": "ses_eb6ac0d77ffecmR4BDd3Zu5U9m",
        "title": "Create calc.py with add function",
        "agent": None,
        "model": {"id": "deepseek-v4.1-flash-free", "providerID": "opencode", "variant": None},
        "cost": 0,
        "tokens": {
            "input": 23870,
            "output": 405,
            "reasoning": 254,
            "cache": {"read": 104384, "write": 0},
        },
        "outcome": "succeeded",
        "time": {"created": 1_792_231_000_000, "updated": 1_792_231_156_800},
    }

    def test_numbers_match_the_measurement(self) -> None:
        usage = T.from_session_record(self.RECORD)
        self.assertEqual(usage.input_tokens, 23870)
        self.assertEqual(usage.output_tokens, 405)
        self.assertEqual(usage.reasoning_tokens, 254)
        self.assertEqual(usage.cache_read_tokens, 104384)
        self.assertEqual(usage.cache_write_tokens, 0)
        self.assertEqual(usage.cost_usd, 0.0)
        self.assertEqual(usage.outcome, "succeeded")
        self.assertAlmostEqual(usage.elapsed_s or 0, 156.8, places=3)
        self.assertEqual(usage.model, "opencode/deepseek-v4.1-flash-free")
        self.assertEqual(usage.total_tokens, 23870 + 405 + 254)

    def test_model_calls_are_not_invented_from_a_session_record(self) -> None:
        # A session record carries no per-call count. Reporting 0 here would be
        # a fabricated measurement; None says "ask the run log".
        usage = T.from_session_record(self.RECORD)
        self.assertIsNone(usage.model_calls)


class TestRunLog(unittest.TestCase):
    def test_counts_model_calls_and_sums_tokens(self) -> None:
        log = as_log(
            step_finish(19085, 100, 50, 3072),
            step_finish(3357, 120, 60, 22144),
            step_finish(1117, 185, 144, 25472),
        )
        parsed = T.parse_run_log(log)
        self.assertEqual(parsed.usage.model_calls, 3)
        self.assertEqual(parsed.usage.input_tokens, 19085 + 3357 + 1117)
        self.assertEqual(parsed.usage.output_tokens, 100 + 120 + 185)
        self.assertEqual(parsed.usage.cache_read_tokens, 3072 + 22144 + 25472)
        self.assertEqual(parsed.malformed_lines, 0)

    def test_powershell_utf16_redirect_still_parses(self) -> None:
        # A `1> file` redirect in Windows PowerShell 5.1 writes UTF-16LE. Read as
        # UTF-8 this log looks empty, which is indistinguishable from "no work".
        raw = as_log(step_finish(10, 1, 1, 0)).encode("utf-16")
        parsed = T.parse_run_log(raw)
        self.assertEqual(parsed.usage.model_calls, 1)
        self.assertEqual(parsed.usage.input_tokens, 10)

    def test_utf8_bom_still_parses(self) -> None:
        raw = ("\ufeff" + as_log(step_finish(5, 1, 0, 0))).encode("utf-8")
        self.assertEqual(T.parse_run_log(raw).usage.input_tokens, 5)

    def test_wasted_steps_are_counted_and_keep_their_error(self) -> None:
        log = as_log(
            tool_use("read", "error", "File not found: .opencode/swarm/state.json"),
            tool_use("write", "completed"),
            tool_use("glob", "completed"),
        )
        parsed = T.parse_run_log(log)
        self.assertEqual(parsed.usage.wasted_steps, 1)
        failed = [s for s in parsed.steps if s.get("wasted")]
        self.assertEqual(len(failed), 1)
        self.assertIn("state.json", failed[0]["error"])

    def test_no_step_finish_means_unmeasured_not_zero(self) -> None:
        log = as_log({"type": "text", "timestamp": 9, "part": {"type": "text", "text": "hi"}})
        parsed = T.parse_run_log(log)
        self.assertIsNone(parsed.usage.model_calls)
        self.assertIsNone(parsed.usage.input_tokens)
        self.assertTrue(any("step_finish" in n for n in parsed.usage.notes))

    def test_malformed_lines_are_counted_not_swallowed(self) -> None:
        log = "{ this is not json }\n" + as_log(step_finish(4, 1, 0, 0))
        parsed = T.parse_run_log(log)
        self.assertEqual(parsed.malformed_lines, 1)
        self.assertEqual(parsed.usage.model_calls, 1)

    def test_reported_cost_is_summed(self) -> None:
        log = as_log(step_finish(1, 1, 0, 0, cost=0.25), step_finish(1, 1, 0, 0, cost=0.75))
        self.assertAlmostEqual(T.parse_run_log(log).usage.cost_usd or 0, 1.0)


class TestCombine(unittest.TestCase):
    def test_sums_only_reported_values(self) -> None:
        merged = T.combine([T.Usage(input_tokens=10), T.Usage(input_tokens=None)])
        # Not 5 (a silent average) and not 0 (silent absence).
        self.assertEqual(merged.input_tokens, 10)

    def test_all_unreported_stays_unmeasured(self) -> None:
        self.assertIsNone(T.combine([T.Usage(), T.Usage()]).input_tokens)

    def test_outcome_is_the_worst_not_the_last(self) -> None:
        merged = T.combine([T.Usage(outcome="succeeded"), T.Usage(outcome="failed")])
        self.assertEqual(merged.outcome, "failed")

    def test_empty_input_is_not_an_error(self) -> None:
        self.assertIsNone(T.combine([]).input_tokens)


class TestRollup(unittest.TestCase):
    def test_children_are_attributed_by_parent_id(self) -> None:
        rows = [
            session("root", inp=100, out=10),
            session("kid1", parent="root", inp=20, out=2),
            session("kid2", parent="root", inp=30, out=3),
        ]
        rolled = T.rollup(rows)
        self.assertEqual(list(rolled), ["root"])
        root = rolled["root"]
        self.assertEqual(root.delegations, 2)
        self.assertEqual(root.input_tokens, 150)
        self.assertEqual(root.output_tokens, 15)

    def test_nothing_delegated_is_stated(self) -> None:
        rolled = T.rollup([session("root")])
        self.assertEqual(rolled["root"].delegations, 0)
        self.assertTrue(any("nothing was delegated" in n for n in rolled["root"].notes))

    def test_load_sessions_accepts_envelope_and_list(self) -> None:
        row = session("a")
        self.assertEqual(len(T.load_sessions({"data": [row]})), 1)
        self.assertEqual(len(T.load_sessions([row])), 1)
        self.assertEqual(len(T.load_sessions(json.dumps({"data": [row]}))), 1)

    def test_pricing_available_detects_an_unpriced_host(self) -> None:
        self.assertFalse(T.pricing_available([session("a"), session("b")]))
        self.assertTrue(T.pricing_available([session("a"), session("b", cost=0.01)]))


class TestCompare(unittest.TestCase):
    def test_cheaper_but_broken_is_refused(self) -> None:
        before = T.Usage(outcome="succeeded", input_tokens=1000, output_tokens=100)
        after = T.Usage(outcome="failed", input_tokens=10, output_tokens=1)
        verdict = T.compare(before, after, category="r3")
        self.assertEqual(verdict.verdict, "refused")
        self.assertIn("outcome", verdict.regressions[0])

    def test_guardrail_outranks_savings(self) -> None:
        before = T.Usage(outcome="succeeded", input_tokens=1000, qc_failures=0)
        after = T.Usage(outcome="succeeded", input_tokens=100, qc_failures=1)
        verdict = T.compare(before, after)
        self.assertEqual(verdict.verdict, "refused")
        self.assertIn("qc_failures", verdict.regressions)

    def test_improvement_needs_a_measured_pair(self) -> None:
        before = T.Usage(outcome="succeeded", input_tokens=1000, output_tokens=100, elapsed_s=60.0)
        after = T.Usage(outcome="succeeded", input_tokens=400, output_tokens=40, elapsed_s=30.0)
        verdict = T.compare(before, after)
        self.assertEqual(verdict.verdict, "improved")
        self.assertIn("input_tokens", verdict.improvements)

    def test_unmeasured_metric_is_excluded_not_treated_as_zero(self) -> None:
        before = T.Usage(outcome="succeeded", input_tokens=1000)
        after = T.Usage(outcome="succeeded", input_tokens=400)
        verdict = T.compare(before, after)
        self.assertIn("elapsed_s", verdict.not_measured)
        self.assertNotIn("elapsed_s", verdict.metrics)
        self.assertEqual(verdict.verdict, "improved")

    def test_one_metric_worsening_makes_it_mixed(self) -> None:
        before = T.Usage(outcome="succeeded", input_tokens=1000, output_tokens=100)
        after = T.Usage(outcome="succeeded", input_tokens=400, output_tokens=300)
        verdict = T.compare(before, after)
        self.assertEqual(verdict.verdict, "mixed")
        self.assertIn("output_tokens", verdict.regressions)

    def test_small_move_inside_tolerance_is_unchanged(self) -> None:
        before = T.Usage(outcome="succeeded", input_tokens=1000)
        after = T.Usage(outcome="succeeded", input_tokens=950)
        self.assertEqual(T.compare(before, after).verdict, "unchanged")

    def test_nothing_comparable_is_unknown(self) -> None:
        verdict = T.compare(T.Usage(outcome="succeeded"), T.Usage(outcome="succeeded"))
        self.assertEqual(verdict.verdict, "unknown")
        self.assertTrue(verdict.not_measured)


class TestBaselines(unittest.TestCase):
    def test_roundtrip_and_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baselines.jsonl"
            T.write_baseline(path, "r3", T.Usage(label="before", input_tokens=1000))
            T.write_baseline(path, "r3", T.Usage(label="after", input_tokens=400))
            T.write_baseline(path, "r4", T.Usage(label="other", input_tokens=1))
            self.assertEqual(len(T.load_baselines(path)), 3)
            rows = T.load_baselines(path, "r3")
            self.assertEqual(len(rows), 2)
            # append-only: the earlier baseline still reads as it was written
            self.assertEqual(rows[0]["usage"]["input_tokens"], 1000)

    def test_missing_file_reads_as_empty(self) -> None:
        self.assertEqual(T.load_baselines(Path(tempfile.gettempdir()) / "nope-not-here.jsonl"), [])


class TestRender(unittest.TestCase):
    def test_report_states_the_gaps(self) -> None:
        text = T.render(T.Usage(label="calc", input_tokens=23870, cost_usd=0.0))
        self.assertIn("23870", text)
        self.assertIn("not measured:", text)
        self.assertIn("model_calls", text)

    def test_zero_cost_is_called_out_as_an_absence_of_price(self) -> None:
        text = T.render(T.Usage(label="calc", cost_usd=0.0))
        self.assertIn("absence of one", text)


if __name__ == "__main__":
    unittest.main()

"""The five wired engines are reachable from the CLI and behave as documented.

These tests exist because the failure they guard against already happened once:
``router``, ``context``, ``delegation``, ``knowledge`` and ``telemetry`` were
implemented and passing their own unit tests while being unreachable from the
command surface and from ``swarmkit`` itself. Each test drives the real CLI in a
subprocess so a missing registration or a broken exit code cannot hide.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.support import run_cli
from tests.support import TempWorkspace

CATALOG = {
    "models": [
        {
            "provider": "local",
            "model": "fast",
            "status": "active",
            "capabilities": {"code": True, "text": True},
            "price_in_per_mtok": 0.1,
            "price_out_per_mtok": 0.2,
        }
    ]
}


def jload(proc) -> dict:
    return json.loads(proc.stdout)


class TestReExports(unittest.TestCase):
    def test_every_public_name_is_reachable(self) -> None:
        import swarmkit

        for name in swarmkit.__all__:
            self.assertTrue(hasattr(swarmkit, name), f"swarmkit.{name} missing")

    def test_module_objects_are_reachable(self) -> None:
        import swarmkit

        for name in ("router", "context", "delegation", "knowledge", "telemetry"):
            self.assertTrue(hasattr(swarmkit, name), f"swarmkit.{name} missing")

    def test_flat_functions_are_reachable(self) -> None:
        import swarmkit

        for name in ("route", "next", "record", "history", "decide", "build_packet",
                     "summarise", "fits", "cache_doc", "lookup", "compare",
                     "parse_run_log", "rollup"):
            self.assertTrue(callable(getattr(swarmkit, name)), name)

    def test_router_budget_is_reachable_without_shadowing_the_module(self) -> None:
        import swarmkit
        import swarmkit.budget as budget_module

        self.assertTrue(callable(swarmkit.router.budget))
        # the module must still win the flat name: every caller imports it
        self.assertTrue(hasattr(budget_module, "Limits"))


class TestHelp(TempWorkspace):
    def test_group_help_exits_zero(self) -> None:
        for group in ("model", "context", "delegate", "knowledge", "telemetry"):
            proc = run_cli(group, "--help", workspace=self.ws, expect=None)
            self.assertEqual(proc.returncode, 0, f"{group}: {proc.stderr}")

    def test_model_route_help_exits_zero(self) -> None:
        proc = run_cli("model", "route", "--help", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_selftest_still_ok(self) -> None:
        self.assertIn("selftest ok", run_cli("selftest", workspace=self.ws).stdout)


class TestModel(TempWorkspace):
    def _catalog(self) -> Path:
        path = self.ws / "catalog.json"
        path.write_text(json.dumps(CATALOG), encoding="utf-8")
        return path

    def test_import_counts_models(self) -> None:
        proc = run_cli("model", "import", "--file", str(self._catalog()),
                       "--source", "test", "--json", workspace=self.ws)
        payload = jload(proc)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["source"], "test")
        self.assertEqual(payload["models"], ["local/fast"])

    def test_route_honours_a_capable_pin(self) -> None:
        self._catalog_import()
        proc = run_cli("model", "route", "--task", "t1", "--objective", "do it",
                       "--role", "swarm/builder", "--category", "code",
                       "--pin", "local/fast", "--json", workspace=self.ws)
        payload = jload(proc)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["provider"], "local")
        self.assertTrue(payload["reason"].endswith("."))

    def test_route_without_evidence_is_a_blocked_decision_not_an_error(self) -> None:
        self._catalog_import()
        proc = run_cli("model", "route", "--task", "t1", "--objective", "do it",
                       "--role", "swarm/builder", "--category", "code", "--json",
                       workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(jload(proc)["status"], "blocked")

    def test_route_without_a_catalog_refuses_cleanly(self) -> None:
        proc = run_cli("model", "route", "--task", "t1", "--objective", "x",
                       "--role", "swarm/builder", "--json",
                       workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        self.assertEqual(jload(proc)["error"]["code"], "CONFIG_INVALID")

    def test_record_then_history(self) -> None:
        run_cli("model", "record", "--provider", "local", "--model", "fast",
                "--category", "code", "--outcome", "verified", "--json",
                workspace=self.ws)
        payload = jload(run_cli("model", "history", "--category", "code", "--json",
                                workspace=self.ws))
        self.assertEqual(payload["category"], "code")
        self.assertEqual(payload["history"][0]["key"], "local/fast")
        self.assertEqual(payload["history"][0]["verdict"], "not-yet")

    def test_enough_evidence_makes_a_model_proven(self) -> None:
        self._catalog_import()
        for _ in range(5):
            run_cli("model", "record", "--provider", "local", "--model", "fast",
                    "--category", "code", "--outcome", "verified", workspace=self.ws)
        proc = run_cli("model", "route", "--task", "t1", "--objective", "do it",
                       "--role", "swarm/builder", "--category", "code", "--json",
                       workspace=self.ws)
        self.assertEqual(jload(proc)["status"], "ok")

    def test_diagnose_attributes_a_cause(self) -> None:
        payload = jload(run_cli("model", "diagnose", "--error", "429 rate limit",
                                "--json", workspace=self.ws))
        self.assertEqual(payload["cause"], "provider")

    def test_next_from_an_error_derives_the_cause(self) -> None:
        payload = jload(run_cli("model", "next", "--task", "t",
                                "--error", "429 rate limit", "--json", workspace=self.ws))
        self.assertEqual(payload["cause"], "provider")
        self.assertEqual(payload["action"], "retry")

    def test_next_model_fault_retries_before_switching(self) -> None:
        payload = jload(run_cli("model", "next", "--task", "t", "--cause", "model",
                                "--retries", "0", "--json", workspace=self.ws))
        self.assertEqual(payload["action"], "retry")

    def test_next_budget_is_blocked_but_exits_zero(self) -> None:
        proc = run_cli("model", "next", "--task", "t", "--cause", "budget", "--json",
                       workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(jload(proc)["status"], "blocked")

    def test_budget_declares_ceilings(self) -> None:
        payload = jload(run_cli("model", "budget", "--max-cost", "5",
                                "--max-tokens", "1000", "--json", workspace=self.ws))
        self.assertEqual(payload["max_cost"], 5.0)
        self.assertEqual(payload["max_tokens"], 1000)
        self.assertEqual(payload["remaining_tokens"], 1000)

    def _catalog_import(self) -> None:
        run_cli("model", "import", "--file", str(self._catalog()),
                "--source", "test", workspace=self.ws)


class TestContext(TempWorkspace):
    def test_packet_carries_criteria_files_and_sections(self) -> None:
        proc = run_cli("context", "packet", "--task", "T1", "--objective", "add a docstring",
                       "--criteria", "no behaviour change", "tests pass",
                       "--dep", "T0", "--file", "src/a.py", "--json", workspace=self.ws)
        payload = jload(proc)
        self.assertEqual(payload["criteria"], ["no behaviour change", "tests pass"])
        self.assertEqual(payload["files"][0]["path"], "src/a.py")
        # the checkpoint sections every spawn must carry are provided
        for section in ("requirements", "constraints", "decisions", "unresolved", "evidence"):
            self.assertIn(section, payload["sections"])
        self.assertGreater(payload["estimated_tokens"], 0)

    def test_summarise_builds_a_checkpoint(self) -> None:
        path = self.ws / "sections.json"
        path.write_text(json.dumps({
            "requirements": ["ship it"],
            "constraints": [],
            "decisions": ["use stdlib"],
            "unresolved": [],
            "evidence": ["pytest: 12 passed"],
        }), encoding="utf-8")
        payload = jload(run_cli("context", "summarise", "--file", str(path),
                                "--json", workspace=self.ws))
        self.assertEqual(payload["requirements"], ["ship it"])
        self.assertEqual(payload["evidence"], ["pytest: 12 passed"])

    def test_summarise_refuses_rather_than_truncating(self) -> None:
        path = self.ws / "sections.json"
        path.write_text(json.dumps({
            "requirements": ["x" * 4000],
            "constraints": [], "decisions": [], "unresolved": [], "evidence": [],
        }), encoding="utf-8")
        proc = run_cli("context", "summarise", "--file", str(path), "--max-tokens", "1",
                       "--json", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        self.assertEqual(jload(proc)["error"]["code"], "CONTEXT_BUDGET_EXCEEDED")

    def test_fits_reports_headroom(self) -> None:
        path = self.ws / "payload.txt"
        path.write_text("short payload\n", encoding="utf-8")
        payload = jload(run_cli("context", "fits", "--file", str(path),
                                "--limit", "1000", "--json", workspace=self.ws))
        self.assertTrue(payload["ok"])
        self.assertGreater(payload["headroom"], 0)

    def test_fits_refuses_over_the_ceiling(self) -> None:
        path = self.ws / "payload.txt"
        path.write_text("x" * 400, encoding="utf-8")
        proc = run_cli("context", "fits", "--file", str(path), "--limit", "1",
                       "--json", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(jload(proc)["error"]["code"], "CONTEXT_LIMIT_EXCEEDED")


class TestDelegate(TempWorkspace):
    def test_answer_lane_is_answered_directly(self) -> None:
        proc = run_cli("delegate", "decide", "--objective", "what is x?",
                       "--lane", "R1", "--json", workspace=self.ws)
        payload = jload(proc)
        self.assertFalse(payload["decision"]["delegate"])
        self.assertEqual(payload["decision"]["code"], "ANSWER_DIRECT")

    def test_independent_files_delegate(self) -> None:
        proc = run_cli("delegate", "decide", "--objective", "touch two files",
                       "--lane", "R3", "--files", "2", "--json", workspace=self.ws)
        payload = jload(proc)
        self.assertTrue(payload["decision"]["delegate"])
        self.assertGreaterEqual(payload["decision"]["worker_count"], 2)

    def test_retry_is_bounded_with_repair_instructions(self) -> None:
        payload = jload(run_cli("delegate", "retry", "--attempt", "1",
                                "--limit", "3", "--cause", "tool_failure",
                                "--json", workspace=self.ws))
        self.assertTrue(payload["retry"])
        self.assertTrue(payload["instructions"])

    def test_retry_exhausted_stops(self) -> None:
        payload = jload(run_cli("delegate", "retry", "--attempt", "3",
                                "--limit", "3", "--json", workspace=self.ws))
        self.assertFalse(payload["retry"])
        self.assertEqual(payload["code"], "RETRY_EXHAUSTED")

    def test_unknown_lane_is_a_usage_error(self) -> None:
        proc = run_cli("delegate", "decide", "--objective", "x", "--lane", "R9",
                       workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 2)


class TestKnowledge(TempWorkspace):
    def _content(self, text: str = "cached body") -> Path:
        path = self.ws / "page.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def test_cache_then_lookup(self) -> None:
        run_cli("knowledge", "cache", "--url", "https://example.com/a",
                "--content-file", str(self._content()), "--json", workspace=self.ws)
        payload = jload(run_cli("knowledge", "lookup", "--url", "https://example.com/a",
                                "--json", workspace=self.ws))
        self.assertTrue(payload["hit"])
        self.assertEqual(payload["matched_on"], "url")

    def test_unchanged_refetch_says_so(self) -> None:
        run_cli("knowledge", "cache", "--url", "https://example.com/a",
                "--content-file", str(self._content()), workspace=self.ws)
        payload = jload(run_cli("knowledge", "cache", "--url", "https://example.com/a",
                                "--content-file", str(self._content()), "--json",
                                workspace=self.ws))
        self.assertEqual(payload["reason"], "unchanged")

    def test_lookup_of_an_unknown_url_is_a_miss_not_an_error(self) -> None:
        proc = run_cli("knowledge", "lookup", "--url", "https://example.com/never",
                       "--json", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(jload(proc)["hit"])

    def test_freshness_of_a_cached_url(self) -> None:
        run_cli("knowledge", "cache", "--url", "https://example.com/a",
                "--content-file", str(self._content()), workspace=self.ws)
        payload = jload(run_cli("knowledge", "freshness", "--url", "https://example.com/a",
                                "--json", workspace=self.ws))
        self.assertIn("stale", payload)
        self.assertFalse(payload["stale"])

    def test_freshness_of_an_uncached_url_refuses(self) -> None:
        proc = run_cli("knowledge", "freshness", "--url", "https://example.com/never",
                       "--json", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(jload(proc)["error"]["code"], "NO_SUCH_DOC")

    def test_store_wide_freshness(self) -> None:
        run_cli("knowledge", "cache", "--url", "https://example.com/a",
                "--content-file", str(self._content()), workspace=self.ws)
        payload = jload(run_cli("knowledge", "freshness", "--json", workspace=self.ws))
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["fresh"], 1)


class TestTelemetry(TempWorkspace):
    def _file(self, name: str, text: str) -> Path:
        path = self.ws / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_run_log_parses(self) -> None:
        log = self._file("run.jsonl",
                         '{"type":"step_finish","part":{"tokens":{"input":10,"output":5},'
                         '"cost":0.5}}\n'
                         '{"type":"tool_use","part":{"tool":"bash","state":{"status":"completed"}}}\n')
        payload = jload(run_cli("telemetry", "run-log", "--file", str(log), "--json",
                                workspace=self.ws))
        self.assertEqual(payload["step_count"], 2)
        self.assertEqual(payload["usage"]["input_tokens"], 10)
        self.assertEqual(payload["usage"]["model_calls"], 1)

    def test_sessions_from_a_file(self) -> None:
        path = self._file("sessions.json", json.dumps({
            "data": [{"id": "s1", "title": "task", "agent": "swarm/builder",
                      "tokens": {"input": 100, "output": 50},
                      "model": {"providerID": "local", "id": "fast"}, "cost": 0.2}]
        }))
        payload = jload(run_cli("telemetry", "sessions", "--file", str(path), "--json",
                                workspace=self.ws))
        self.assertEqual(payload["sessions"], 1)
        self.assertTrue(payload["pricing_available"])

    def test_sessions_rollup_attributes_children(self) -> None:
        path = self._file("sessions.json", json.dumps({
            "data": [
                {"id": "root", "title": "task", "tokens": {"input": 10}},
                {"id": "kid", "parentID": "root", "tokens": {"input": 5}},
            ]
        }))
        payload = jload(run_cli("telemetry", "sessions", "--file", str(path),
                                "--rollup", "--json", workspace=self.ws))
        self.assertEqual(payload["rollup"]["root"]["delegations"], 1)
        self.assertEqual(payload["rollup"]["root"]["input_tokens"], 15)

    def test_compare_improved(self) -> None:
        before = self._file("before.json", json.dumps(
            {"input_tokens": 100, "outcome": "succeeded"}))
        after = self._file("after.json", json.dumps(
            {"input_tokens": 50, "outcome": "succeeded"}))
        payload = jload(run_cli("telemetry", "compare", "--before", str(before),
                                "--after", str(after), "--json", workspace=self.ws))
        self.assertEqual(payload["verdict"], "improved")

    def test_compare_refuses_a_correctness_regression(self) -> None:
        before = self._file("before.json", json.dumps(
            {"input_tokens": 100, "outcome": "succeeded"}))
        after = self._file("after.json", json.dumps(
            {"input_tokens": 50, "outcome": "failed"}))
        proc = run_cli("telemetry", "compare", "--before", str(before),
                       "--after", str(after), "--json", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(jload(proc)["verdict"], "refused")

    def test_baseline_record_then_list(self) -> None:
        usage = self._file("usage.json", json.dumps(
            {"input_tokens": 42, "outcome": "succeeded"}))
        ledger = self.ws / "baselines.jsonl"
        recorded = jload(run_cli("telemetry", "baseline", "--file", str(ledger),
                                 "--category", "code", "--task-log", str(usage),
                                 "--json", workspace=self.ws))
        self.assertTrue(recorded["recorded"])
        listed = jload(run_cli("telemetry", "baseline", "--file", str(ledger),
                               "--category", "code", "--json", workspace=self.ws))
        self.assertEqual(listed["count"], 1)
        self.assertFalse(listed["recorded"])

    def test_a_malformed_session_payload_refuses_cleanly(self) -> None:
        path = self._file("bad.json", "not json at all")
        proc = run_cli("telemetry", "sessions", "--file", str(path), "--json",
                       workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)


class TestGlobalJsonStillWorks(TempWorkspace):
    def test_top_level_json_flag_still_accepted(self) -> None:
        # `--json` before the subcommand is the existing idiom and must keep working.
        proc = run_cli("--json", "delegate", "decide", "--objective", "x",
                       "--lane", "R1", workspace=self.ws)
        self.assertFalse(jload(proc)["decision"]["delegate"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

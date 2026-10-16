"""CLI behaviour: exit codes, refusal messages, and the end-to-end pipeline."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from swarmkit import store
from tests.support import GitRepo
from tests.support import REPO_ROOT
from tests.support import git
from tests.support import run_cli
from tests.support import TempWorkspace


class TestUsage(TempWorkspace):
    def test_phases_table_exits_zero(self) -> None:
        out = run_cli("run", "phases", workspace=self.ws).stdout
        self.assertIn("INTAKE", out)
        self.assertIn("DELIVERED", out)

    def test_lint_of_this_repo_exits_zero(self) -> None:
        # Must lint the REAL repo. Linting self.ws.parent (the OS temp dir) finds
        # zero pages and returns 0 for any content whatsoever, so it passed while
        # the agent pages were broken.
        proc = run_cli("lint", "--root", str(REPO_ROOT), expect=None)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("0 page(s)", proc.stdout)

    def test_selftest_exits_zero(self) -> None:
        self.assertIn("selftest ok", run_cli("selftest", workspace=self.ws).stdout)


class TestRunLifecycle(TempWorkspace):
    def test_init_then_show(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        out = run_cli("run", "show", workspace=self.ws, expect=None)
        self.assertIn("INTAKE", out.stdout)

    def test_list_prefers_unfinished(self) -> None:
        run_cli("run", "init", "--slug", "a", workspace=self.ws)
        run_cli("run", "init", "--slug", "b", workspace=self.ws)
        out = run_cli("--json", "run", "list", workspace=self.ws).stdout
        self.assertIn("a", out)
        self.assertIn("b", out)

    def test_phase_moves_forward(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        run_cli("run", "phase", "--to", "PLAN", workspace=self.ws)
        state = store.RunState.load(store.find_run(self.ws, "demo"))
        self.assertEqual(state.phase, "PLAN")

    def test_illegal_transition_exits_nonzero_and_explains(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        proc = run_cli("run", "phase", "--to", "DELIVERED", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ILLEGAL", (proc.stdout + proc.stderr).upper())

    def test_status_is_written_on_phase_change(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        run_cli("run", "phase", "--to", "PLAN", workspace=self.ws)
        status = (store.find_run(self.ws, "demo") / store.STATUS_FILE).read_text(encoding="utf-8")
        self.assertIn("PLAN", status)

    def test_abort_is_allowed_from_anywhere(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        run_cli("run", "phase", "--to", "PLAN", workspace=self.ws)
        proc = run_cli("run", "phase", "--to", "ABORTED", "--reason", "blocked",
                       workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(store.RunState.load(store.find_run(self.ws, "demo")).phase, "ABORTED")


class TestInterruptedRunViaCli(TempWorkspace):
    def test_recover_unblocks_a_stalled_run(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        run = store.find_run(self.ws, "demo")
        # Simulate a process killed mid-mutation.
        store.atomic_write_json(
            store.journal(run), {"op": "phase", "target": "PLAN", "pid": 1, "at": "x"}
        )
        proc = run_cli("run", "phase", "--to", "PLAN", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("RECOVERY_REQUIRED", (proc.stdout + proc.stderr).upper())
        run_cli("run", "recover", "--note", "reconciled by hand", workspace=self.ws)
        run_cli("run", "phase", "--to", "PLAN", workspace=self.ws)  # now works

    def test_bare_recover_works_and_writes_a_truthful_note(self) -> None:
        # agents/swarm.md documents a bare `run recover`. The previous test asserted
        # argparse refused a missing --note, which proved nothing about swarmkit.
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        run = store.find_run(self.ws, "demo")
        store.atomic_write_json(
            store.journal(run), {"op": "phase", "target": "PLAN", "pid": 1, "at": "x"}
        )
        proc = run_cli("run", "recover", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(store.pending_journal(run), "journal must be cleared")
        history = (run / store.HISTORY_FILE).read_text(encoding="utf-8")
        # The auto-note must name the op it cleared, so history.md stays truthful.
        self.assertIn("phase", history)
        self.assertIn("PLAN", history)

    def test_recover_note_is_recorded_when_supplied(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        run_cli("run", "recover", "--note", "reconciled by hand", workspace=self.ws)
        history = (store.find_run(self.ws, "demo") / store.HISTORY_FILE).read_text(encoding="utf-8")
        self.assertIn("reconciled by hand", history)


class TestGateViaCli(TempWorkspace):
    def setUp(self) -> None:
        super().setUp()
        run_cli("run", "init", "--slug", "gate-demo", workspace=self.ws)

    def test_delivery_blocked_with_no_criteria(self) -> None:
        for phase in ("PLAN", "MESH", "INTEGRATE", "QC", "RETRO"):
            run_cli("run", "phase", "--to", phase, workspace=self.ws)
        proc = run_cli("run", "phase", "--to", "DELIVERED", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("NO_CRITERIA", (proc.stdout + proc.stderr).upper())

    def test_delivery_blocked_when_criterion_unproven(self) -> None:
        for phase in ("PLAN", "MESH", "INTEGRATE", "QC", "RETRO"):
            run_cli("run", "phase", "--to", phase, workspace=self.ws)
        run_cli("criterion", "add", "--id", "build", "--text", "npm run build exits 0",
                workspace=self.ws)
        proc = run_cli("run", "phase", "--to", "DELIVERED", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("GATE_CLOSED", (proc.stdout + proc.stderr).upper())

    def test_delivery_allowed_once_verified(self) -> None:
        for phase in ("PLAN", "MESH", "INTEGRATE", "QC", "RETRO"):
            run_cli("run", "phase", "--to", phase, workspace=self.ws)
        run_cli("criterion", "add", "--id", "build", "--text", "npm run build exits 0",
                workspace=self.ws)
        run_cli("evidence", "add", "--criterion", "build",
                "--command", "npm run build", "--result", "exit 0", workspace=self.ws)
        run_cli("gate", "open", workspace=self.ws)
        run_cli("run", "phase", "--to", "DELIVERED", workspace=self.ws)
        state = store.RunState.load(store.find_latest_run(self.ws))
        self.assertTrue(state.is_finished())
        self.assertEqual(state.phase, "DELIVERED")

    def test_evidence_without_a_source_is_refused(self) -> None:
        run_cli("criterion", "add", "--id", "c", "--text", "t", workspace=self.ws)
        proc = run_cli("--json", "evidence", "add", "--criterion", "c", "--result", "looks fine",
                       workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        # Assert the specific refusal, not merely "some error happened".
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "UNSUPPORTED_EVIDENCE")

    def test_evidence_with_empty_result_is_refused(self) -> None:
        run_cli("criterion", "add", "--id", "c", "--text", "t", workspace=self.ws)
        proc = run_cli("--json", "evidence", "add", "--criterion", "c", "--command", "make",
                       workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "EMPTY_EVIDENCE")

    def test_refused_evidence_is_not_written(self) -> None:
        run_cli("criterion", "add", "--id", "c", "--text", "t", workspace=self.ws)
        run_cli("--json", "evidence", "add", "--criterion", "c", "--result", "looks fine",
                workspace=self.ws, expect=1)
        evidence = store.find_latest_run(self.ws) / "evidence.jsonl"
        self.assertEqual(store.read_jsonl(evidence), [])

    def test_gate_check_json_is_machine_readable(self) -> None:
        run_cli("criterion", "add", "--id", "c", "--text", "t", workspace=self.ws)
        out = run_cli("--json", "gate", "check", workspace=self.ws, expect=None).stdout
        payload = json.loads(out)
        self.assertIn("results", payload)
        self.assertFalse(payload["open"])


class TestBudgetViaCli(TempWorkspace):
    def test_concurrency_cap_is_enforced_from_the_cli(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        for name in ("a", "b", "c", "d"):
            run_cli("budget", "spawn", "--name", name, workspace=self.ws)
        proc = run_cli("--json", "budget", "spawn", "--name", "e",
                       workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "CONCURRENCY_LIMIT")
        # The refusal must not have consumed a slot or a spawn.
        payload = json.loads(run_cli("--json", "budget", "show", workspace=self.ws).stdout)
        self.assertEqual(payload["inflight"], ["a", "b", "c", "d"])
        self.assertEqual(payload["spawns"], 4)

    def test_duplicate_worker_is_refused(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        run_cli("budget", "spawn", "--name", "build", workspace=self.ws)
        proc = run_cli("--json", "budget", "spawn", "--name", "build",
                       workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "DUPLICATE_WORKER")

    def test_worker_flag_is_an_alias_for_name(self) -> None:
        # agents/swarm.md documents `--worker`, not `--name`.
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        run_cli("budget", "spawn", "--worker", "build", workspace=self.ws)
        payload = json.loads(run_cli("--json", "budget", "show", workspace=self.ws).stdout)
        self.assertEqual(payload["inflight"], ["build"])
        run_cli("budget", "retry", "--worker", "build", workspace=self.ws)
        run_cli("budget", "release", "--worker", "build", workspace=self.ws)
        payload = json.loads(run_cli("--json", "budget", "show", workspace=self.ws).stdout)
        self.assertEqual(payload["inflight"], [])
        self.assertEqual(payload["retries"]["build"], 1)

    def test_tokens_recorded_and_cost_absent_without_pricing(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        run_cli("budget", "tokens", "--in", "1000", "--out", "2000", workspace=self.ws)
        payload = json.loads(run_cli("--json", "budget", "show", workspace=self.ws).stdout)
        self.assertEqual(payload["tokens_in"], 1000)
        self.assertIsNone(payload["estimated_cost_usd"])


class TestScopeViaCli(TempWorkspace):
    def test_disjoint_scopes_pass(self) -> None:
        proc = run_cli("--json", "scope", "plan", "--ws", "build=src/a/**", "--ws", "ui=src/b/**",
                       "--strict", workspace=self.ws)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["possible_conflicts"], [])

    def test_overlapping_scopes_are_refused(self) -> None:
        proc = run_cli("scope", "plan", "--ws", "build=src/a/**", "--ws", "ui=src/a/x.ts",
                       "--strict", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("SCOPE_CONFLICT", (proc.stdout + proc.stderr).upper())

    def test_unbounded_scope_is_refused(self) -> None:
        proc = run_cli("scope", "plan", "--ws", "build=*", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("UNBOUNDED_SCOPE", (proc.stdout + proc.stderr).upper())

    def test_protected_tree_is_refused(self) -> None:
        proc = run_cli("scope", "plan", "--ws", "build=node_modules/**",
                       workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("PROTECTED_SCOPE", (proc.stdout + proc.stderr).upper())


class TestLessonsViaCli(TempWorkspace):
    def setUp(self) -> None:
        super().setUp()
        self.ledger = self.ws / "ledger.md"

    def test_add_without_failure_is_a_usage_error(self) -> None:
        # argparse rejects the missing required --failure before the library sees it.
        # The library-level LESSON_WITHOUT_ORIGIN guard is covered in test_lessons.py;
        # this test only pins that the CLI does not silently accept an ungrounded rule.
        proc = run_cli("lesson", "add", "--rule", "Never trust exit 0",
                       "--ledger", str(self.ledger), workspace=self.ws, expect=2)
        self.assertIn("--failure", proc.stderr)
        self.assertFalse(self.ledger.exists(), "nothing may be written to the ledger")

    def test_add_then_promote_without_tests_is_refused(self) -> None:
        run_cli("lesson", "add", "--rule", "Never trust exit 0",
                "--failure", "shipped with a red build", "--ledger", str(self.ledger),
                workspace=self.ws)
        proc = run_cli("lesson", "promote", "--rule", "Never trust exit 0",
                       "--evidence", "it sounds right", "--ledger", str(self.ledger),
                       workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("PROMOTION_BLOCKED", (proc.stdout + proc.stderr).upper())

    def test_promote_with_tests_passes(self) -> None:
        run_cli("lesson", "add", "--rule", "Never trust exit 0",
                "--failure", "shipped with a red build", "--ledger", str(self.ledger),
                workspace=self.ws)
        run_cli("lesson", "promote", "--rule", "Never trust exit 0",
                "--evidence", "tests/test_gate.py OK", "--tests-passed",
                "--ledger", str(self.ledger), workspace=self.ws)

    def test_audit_runs(self) -> None:
        run_cli("lesson", "add", "--rule", "Never trust exit 0",
                "--failure", "shipped with a red build", "--ledger", str(self.ledger),
                workspace=self.ws)
        payload = json.loads(
            run_cli("--json", "lesson", "audit", "--ledger", str(self.ledger),
                    workspace=self.ws).stdout
        )
        # Assert real counts, not just the template. An audit that returned nothing
        # would previously have printed the same banner and passed.
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["duplicates"], [])
        self.assertEqual(payload["contradictions"], [])
        self.assertEqual(payload["ungrounded"], [])
        self.assertEqual([e["rule"] for e in payload["unverified"]], ["Never trust exit 0"])


class TestDocumentedCommandForms(TempWorkspace):
    """Every command an agent page or the README tells the orchestrator to run.

    An earlier audit found 11 of these were unrunnable: the pages documented
    `run phase DELIVERED`, `budget spawn --worker build`, `iso patch --ws build`,
    but the parser only accepted `--to`, `--name`, and `--wt`+`--out`. The whole
    pipeline was unreachable while `lint` still reported 0 errors. These tests
    execute the documented strings verbatim so that class of rot cannot return.
    """

    def _to_retro(self) -> None:
        for phase in ("PLAN", "MESH", "INTEGRATE", "QC", "RETRO"):
            run_cli("run", "phase", phase, workspace=self.ws)

    def test_run_init_accepts_goal(self) -> None:
        proc = run_cli("run", "init", "--slug", "demo",
                       "--goal", "a running app answering 200", workspace=self.ws)
        self.assertIn("demo", proc.stdout)

    def test_positional_phase_form_advances_and_is_persisted(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        run_cli("run", "phase", "PLAN", workspace=self.ws)
        state = store.RunState.load(store.find_run(self.ws, "demo"))
        self.assertEqual(state.phase, "PLAN")

    def test_positional_form_still_reaches_the_delivery_gate(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        run_cli("criterion", "add", "--id", "build", "--text", "exits 0", workspace=self.ws)
        self._to_retro()
        proc = run_cli("run", "phase", "DELIVERED", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("GATE_CLOSED", (proc.stdout + proc.stderr).upper())
        run_cli("evidence", "add", "--criterion", "build",
                "--command", "npm run build", "--result", "exit 0", workspace=self.ws)
        run_cli("run", "phase", "DELIVERED", workspace=self.ws)
        self.assertEqual(store.RunState.load(store.find_latest_run(self.ws)).phase, "DELIVERED")

    def test_abort_is_accepted_positionally(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        run_cli("run", "phase", "ABORTED", "--reason", "blocked on credentials",
                workspace=self.ws)
        state = store.RunState.load(store.find_run(self.ws, "demo"))
        self.assertEqual(state.phase, "ABORTED")
        self.assertEqual(state.abort_reason, "blocked on credentials")

    def test_to_flag_and_positional_must_agree(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        proc = run_cli("--json", "run", "phase", "PLAN", "--to", "MESH",
                       workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "BAD_USAGE")

    def test_missing_target_is_reported_not_silently_accepted(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        proc = run_cli("--json", "run", "phase", workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "BAD_USAGE")
        self.assertEqual(store.RunState.load(store.find_run(self.ws, "demo")).phase, "INTAKE")

    def test_run_show_reports_next_action_and_evidence_gaps(self) -> None:
        run_cli("run", "init", "--slug", "demo", workspace=self.ws)
        payload = json.loads(run_cli("--json", "run", "show", workspace=self.ws).stdout)
        self.assertIn("INTAKE", payload["next_action"])
        self.assertEqual(payload["criteria"], [])
        self.assertFalse(payload["gate_open"])

        run_cli("run", "phase", "PLAN", workspace=self.ws)
        run_cli("criterion", "add", "--id", "build", "--text", "exits 0", workspace=self.ws)
        payload = json.loads(run_cli("--json", "run", "show", workspace=self.ws).stdout)
        self.assertEqual(payload["criteria"], ["build"])
        self.assertEqual(payload["criteria_needing_evidence"], ["build"])
        self.assertFalse(payload["gate_open"])
        self.assertIn("PLAN", payload["next_action"])

        run_cli("evidence", "add", "--criterion", "build",
                "--command", "npm run build", "--result", "exit 0", workspace=self.ws)
        payload = json.loads(run_cli("--json", "run", "show", workspace=self.ws).stdout)
        self.assertEqual(payload["criteria_needing_evidence"], [])
        self.assertTrue(payload["gate_open"])


class TestIsoViaCli(GitRepo):
    """`iso create --ws build --ws ui` silently dropped every workstream but the
    last because --ws lacked action="append". Both trees must exist."""

    def test_create_makes_one_worktree_per_workstream(self) -> None:
        proc = run_cli("--json", "iso", "create", "--ws", "build", "--ws", "ui",
                       "--repo", str(self.ws), workspace=self.ws)
        payload = json.loads(proc.stdout)
        names = sorted(w["name"] for w in payload["worktrees"])
        self.assertEqual(names, ["build", "ui"], "a workstream was silently dropped")
        self.assertTrue((self.ws / ".swarm-trees" / "build").is_dir())
        self.assertTrue((self.ws / ".swarm-trees" / "ui").is_dir())

    def test_manifest_records_every_workstream(self) -> None:
        run_cli("iso", "create", "--ws", "build", "--ws", "ui",
                "--repo", str(self.ws), workspace=self.ws)
        manifest = json.loads(
            (self.ws / ".swarm-trees" / "swarm-worktrees.json").read_text(encoding="utf-8")
        )
        self.assertEqual(sorted(manifest), ["build", "ui"])

    def test_patch_integrate_remove_resolve_by_workstream_name(self) -> None:
        run_cli("iso", "create", "--ws", "build", "--repo", str(self.ws), workspace=self.ws)
        wt = self.ws / ".swarm-trees" / "build"
        (wt / "new.py").write_text("x = 1\n", encoding="utf-8")
        git("add", "-A", cwd=wt)
        git("commit", "-m", "worker change", cwd=wt)

        proc = run_cli("--json", "iso", "patch", "--ws", "build",
                       "--repo", str(self.ws), workspace=self.ws)
        patch_path = Path(json.loads(proc.stdout)["patch"])
        self.assertTrue(patch_path.exists())

        run_cli("iso", "integrate", "--ws", "build", "--repo", str(self.ws), workspace=self.ws)
        self.assertTrue((self.ws / "new.py").exists(), "workstream patch never landed")
        run_cli("iso", "remove", "--ws", "build", "--repo", str(self.ws), workspace=self.ws)

    def test_unknown_workstream_is_refused_not_guessed(self) -> None:
        run_cli("iso", "create", "--ws", "build", "--repo", str(self.ws), workspace=self.ws)
        proc = run_cli("--json", "iso", "patch", "--ws", "nope",
                       "--repo", str(self.ws), workspace=self.ws, expect=None)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "NO_SUCH_WORKTREE")

    def test_explicit_path_flags_still_work(self) -> None:
        run_cli("iso", "create", "--ws", "build", "--repo", str(self.ws), workspace=self.ws)
        wt = self.ws / ".swarm-trees" / "build"
        out = self.ws / "explicit.patch"
        run_cli("iso", "patch", "--wt", str(wt), "--out", str(out),
                "--repo", str(self.ws), workspace=self.ws)
        self.assertTrue(out.exists())


class TestDocumentedClaimsStayTrue(TempWorkspace):
    """Guard the two documented facts that were simply false.

    Both of these shipped in the agent pages while ``lint`` reported zero
    errors: the linter only reads frontmatter, so it cannot see a wrong CLI
    invocation. These tests execute the documented text verbatim so the
    documentation cannot drift away from the code again.
    """

    def test_run_dir_is_date_prefixed_as_documented(self) -> None:
        # agents/swarm.md: ".opencode/swarm/<YYYY-MM-DD>-<short-slug>/"
        proc = run_cli("--json", "run", "init", "--slug", "demo-run",
                       "--goal", "ship it", workspace=self.ws)
        payload = json.loads(proc.stdout)
        self.assertRegex(
            payload["slug"], r"^\d{4}-\d{2}-\d{2}-demo-run$",
            "run init must prefix today's date, as swarm.md documents",
        )
        self.assertTrue(Path(payload["run_dir"]).is_dir())

    def test_the_bare_slug_still_resolves_the_dated_run(self) -> None:
        """Date-prefixing must not break `--slug` as a handle for every later
        command; the caller typed `demo`, not `2026-10-16-demo`."""
        run_cli("run", "init", "--slug", "demo", "--goal", "x", workspace=self.ws)
        proc = run_cli("--json", "run", "show", "--slug", "demo",
                       workspace=self.ws)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertRegex(json.loads(proc.stdout)["slug"], r"-demo$")

    def test_an_existing_date_prefix_is_not_doubled(self) -> None:
        proc = run_cli("--json", "run", "init", "--slug", "2020-01-02-older",
                       "--goal", "x", workspace=self.ws)
        self.assertEqual(json.loads(proc.stdout)["slug"], "2020-01-02-older")

    def test_the_scope_example_in_swarm_md_actually_passes(self) -> None:
        # Verbatim from agents/swarm.md PLAN. It used to name build=src/** and
        # ui=src/ui/**, which is a DEFINITE overlap, so the command the
        # orchestrator is told to run always refused with SCOPE_CONFLICT.
        proc = run_cli("--json", "scope", "plan",
                       "--ws", "build=src/build/**,vite.config.ts",
                       "--ws", "ui=src/ui/**",
                       "--strict", workspace=self.ws)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["possible_conflicts"], [])

    def test_the_nested_scope_example_still_refuses(self) -> None:
        """Guard the other side: fixing the example must not have made
        genuinely overlapping scopes acceptable."""
        proc = run_cli("--json", "scope", "plan",
                       "--ws", "build=src/**",
                       "--ws", "ui=src/ui/**",
                       "--strict", workspace=self.ws, expect=1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "SCOPE_CONFLICT")

    def test_an_ambiguous_slug_is_refused_rather_than_guessed(self) -> None:
        """Two runs ending in `-dup` must not let `find_run` pick one."""
        run_cli("run", "init", "--slug", "a-dup", "--goal", "x", workspace=self.ws)
        run_cli("run", "init", "--slug", "b-dup", "--goal", "x", workspace=self.ws)
        proc = run_cli("--json", "run", "show", "--slug", "dup",
                       workspace=self.ws, expect=1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "AMBIGUOUS_RUN")


    def test_a_corrupt_state_file_reports_a_code_not_a_traceback(self) -> None:
        """state.json is regenerated by `run init`; losing the run is cheap.
        A silent reset would be the dangerous outcome, not a lost run."""
        run_cli("run", "init", "--slug", "corrupt", "--goal", "x", workspace=self.ws)
        run_dir = Path(store.find_run(self.ws, "corrupt"))
        (run_dir / "state.json").write_text("{not json", encoding="utf-8")
        proc = run_cli("--json", "run", "show", "--slug", "corrupt",
                       workspace=self.ws, expect=1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "CORRUPT_STATE")

    def test_a_state_file_missing_required_keys_is_refused(self) -> None:
        """slug/phase absent used to raise TypeError out of the dataclass."""
        run_cli("run", "init", "--slug", "nokeys", "--goal", "x", workspace=self.ws)
        run_dir = Path(store.find_run(self.ws, "nokeys"))
        (run_dir / "state.json").write_text('{"fix_cycles": 0}', encoding="utf-8")
        proc = run_cli("--json", "run", "show", "--slug", "nokeys",
                       workspace=self.ws, expect=1)
        payload = json.loads(proc.stdout)["error"]
        self.assertEqual(payload["code"], "CORRUPT_STATE")
        self.assertIn("slug", payload["message"])

    def test_a_corrupt_budget_refuses_rather_than_resetting_caps(self) -> None:
        """budget.json holds the counters that make caps hold across CLI
        invocations. Resetting them silently would let a run bypass its own
        spawn limit, so an unreadable file must be refused."""
        run_cli("run", "init", "--slug", "badbudget", "--goal", "x", workspace=self.ws)
        run_dir = Path(store.find_run(self.ws, "badbudget"))
        (run_dir / "budget.json").write_text('{"spawns": "many"}', encoding="utf-8")
        proc = run_cli("--json", "budget", "show", "--slug", "badbudget",
                       workspace=self.ws, expect=1)
        payload = json.loads(proc.stdout)["error"]
        self.assertEqual(payload["code"], "CORRUPT_BUDGET")
        self.assertIn("max_spawns_total", payload["message"])

    def test_the_fix_cycle_cap_holds_through_the_cli(self) -> None:
        """The cap was enforced in cli.py with zero coverage: deleting the
        check let every test still pass. Drive QC->FIX->QC to the cap."""
        run_cli("run", "init", "--slug", "cycles", "--goal", "x", workspace=self.ws)
        run_cli("criterion", "add", "--id", "build", "--text", "exits 0",
                "--slug", "cycles", workspace=self.ws)
        for phase in ("PLAN", "MESH", "INTEGRATE", "QC"):
            run_cli("run", "phase", phase, "--slug", "cycles", workspace=self.ws)

        # Default max_fix_cycles = 2: two cycles are allowed, the third is not.
        for cycle in (1, 2):
            run_cli("run", "phase", "FIX", "--slug", "cycles", workspace=self.ws)
            run_cli("run", "phase", "QC", "--slug", "cycles", workspace=self.ws)

        proc = run_cli("--json", "run", "phase", "FIX", "--slug", "cycles",
                       workspace=self.ws, expect=1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "FIX_CYCLE_LIMIT")

        # The refusal must not have advanced the phase or burned a cycle.
        shown = json.loads(run_cli("--json", "run", "show", "--slug", "cycles",
                                   workspace=self.ws).stdout)
        self.assertEqual(shown["phase"], "QC")
        self.assertEqual(shown["fix_cycles"], 2)

    def test_a_definite_overlap_refuses_even_without_strict(self) -> None:
        """Only the `possible` verdict is gated behind --strict; a DEFINITE
        overlap must be refused either way. This had no coverage at all."""
        proc = run_cli("--json", "scope", "plan",
                       "--ws", "build=src/**", "--ws", "ui=src/ui/**",
                       workspace=self.ws, expect=1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "SCOPE_CONFLICT")


if __name__ == "__main__":
    unittest.main()

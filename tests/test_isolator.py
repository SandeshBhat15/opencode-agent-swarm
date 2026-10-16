"""Scope validation, overlap detection, and git worktree isolation.

Overlap detection is deliberately conservative: anything it cannot *prove*
disjoint is reported as "possible" rather than "none", so the planner fails
closed instead of waving two workers through onto the same file.
"""

from __future__ import annotations

import os
import unittest

from swarmkit.errors import ScopeConflict
from swarmkit.isolator import (
    find_conflicts,
    integrate_patch,
    overlap_kind,
    plan_scopes,
    repo_root,
    validate_entry,
    worktree_patch,
    create_worktrees,
    remove_worktree,
)
from tests.support import GitRepo
from tests.support import git


class TestValidateEntry(unittest.TestCase):
    def test_plain_and_glob_paths_are_accepted(self) -> None:
        validate_entry("src/app.ts")
        validate_entry("src/ui/**/*.tsx")

    def test_unbounded_glob_is_refused(self) -> None:
        for entry in ("*", "**", "**/*"):
            with self.subTest(entry=entry):
                with self.assertRaises(ScopeConflict) as ctx:
                    validate_entry(entry)
                self.assertEqual(ctx.exception.code, "UNBOUNDED_SCOPE")

    def test_escape_is_refused(self) -> None:
        with self.assertRaises(ScopeConflict) as ctx:
            validate_entry("../outside.txt")
        self.assertEqual(ctx.exception.code, "SCOPE_ESCAPE")

    def test_protected_trees_are_refused(self) -> None:
        for entry in (".git/config", "node_modules/x/index.js", "dist/bundle.js"):
            with self.subTest(entry=entry):
                with self.assertRaises(ScopeConflict) as ctx:
                    validate_entry(entry)
                self.assertEqual(ctx.exception.code, "PROTECTED_SCOPE")

    def test_swarm_files_are_refused_by_default(self) -> None:
        for entry in (".opencode/swarm/x/state.json", ".config/opencode/agents/swarm.md"):
            with self.subTest(entry=entry):
                with self.assertRaises(ScopeConflict) as ctx:
                    validate_entry(entry)
                self.assertEqual(ctx.exception.code, "SELF_PROTECTED_SCOPE")

    def test_swarm_files_are_opt_in(self) -> None:
        validate_entry(".opencode/swarm/x/state.json", allow_self_protected=True)


class TestOverlap(unittest.TestCase):
    def test_disjoint_scopes_do_not_overlap(self) -> None:
        self.assertEqual(overlap_kind("src/a", "src/b"), "none")

    def test_different_top_trees_do_not_overlap(self) -> None:
        self.assertEqual(overlap_kind("src/**", "docs/**"), "none")

    def test_identical_paths_definitely_overlap(self) -> None:
        self.assertEqual(overlap_kind("src/a.ts", "src/a.ts"), "definite")

    def test_nested_prefix_definitely_overlaps(self) -> None:
        self.assertEqual(overlap_kind("src/ui", "src/ui/x.ts"), "definite")

    def test_glob_under_a_glob_never_reports_none(self) -> None:
        # Fail-closed: a glob that could cover a concrete path must never be
        # reported as disjoint. Assert the *property*, not one allowed value,
        # so a regression that hard-codes 'definite' everywhere is also caught.
        for a, b in [
            ("src/**", "src/ui/x.ts"),
            ("src/**/*.ts", "src/ui/*.ts"),
            ("src/*", "src/a.ts"),
            ("src/**/*.tsx", "src/ui/*.tsx"),
        ]:
            with self.subTest(pair=(a, b)):
                self.assertIn(overlap_kind(a, b), {"definite", "possible"})
                self.assertIn(overlap_kind(b, a), {"definite", "possible"})

    def test_a_concrete_path_under_a_glob_is_actually_covered(self) -> None:
        """Pin the real answer for the simplest case, so the guard above
        cannot pass on a constant."""
        self.assertEqual(overlap_kind("src/**", "src/ui/x.ts"), "possible")
        self.assertEqual(overlap_kind("src/ui", "src/ui/x.ts"), "definite")


class TestPlanScopes(unittest.TestCase):
    def test_disjoint_scopes_pass(self) -> None:
        plan_scopes({"build": ["src/a/**"], "ui": ["src/b/**"]}, strict=True)

    def test_definite_conflict_raises(self) -> None:
        with self.assertRaises(ScopeConflict) as ctx:
            plan_scopes({"build": ["src/a.ts"], "ui": ["src/a.ts"]}, strict=True)
        self.assertEqual(ctx.exception.code, "SCOPE_CONFLICT")

    def test_possible_conflict_raises_only_under_strict(self) -> None:
        scopes = {"build": ["src/**"], "ui": ["src/ui/x.ts"]}
        plan_scopes(scopes, strict=False)  # tolerated, not proven safe
        with self.assertRaises(ScopeConflict) as ctx:
            plan_scopes(scopes, strict=True)
        self.assertEqual(ctx.exception.code, "SCOPE_CONFLICT_POSSIBLE")

    def test_invalid_entry_raises(self) -> None:
        with self.assertRaises(ScopeConflict) as ctx:
            plan_scopes({"build": ["*"]}, strict=True)
        self.assertEqual(ctx.exception.code, "UNBOUNDED_SCOPE")

    def test_conflict_report_names_both_owners(self) -> None:
        report = find_conflicts({"build": ["src/a.ts"], "ui": ["src/a.ts"]})
        self.assertTrue(report)
        rendered = report[0].render()
        self.assertIn("build", rendered)
        self.assertIn("ui", rendered)


class TestWorktrees(GitRepo):
    def test_repo_root_and_base_ref(self) -> None:
        root = repo_root(self.ws)
        # git may report the Windows 8.3 short path; compare by identity.
        self.assertTrue(os.path.samefile(root, self.ws))

    def test_create_worktrees_is_idempotent(self) -> None:
        first = create_worktrees(self.ws, self.ws / ".swarm-trees", ["build", "ui"])
        second = create_worktrees(self.ws, self.ws / ".swarm-trees", ["build", "ui"])
        self.assertEqual([w.name for w in first], ["build", "ui"])
        self.assertEqual([w.created for w in second], [False, False])

    def test_worktrees_get_distinct_branches_and_paths(self) -> None:
        trees = create_worktrees(self.ws, self.ws / ".swarm-trees", ["build", "ui"])
        self.assertNotEqual(trees[0].branch, trees[1].branch)
        self.assertNotEqual(trees[0].path, trees[1].path)

    def test_patch_captures_worker_changes(self) -> None:
        trees = create_worktrees(self.ws, self.ws / ".swarm-trees", ["build"])
        wt = trees[0].path
        (wt / "new.py").write_text("x = 1\n", encoding="utf-8")
        git("add", "-A", cwd=wt)
        git("commit", "-m", "worker change", cwd=wt)
        patch = worktree_patch(self.ws, wt)
        self.assertIn("new.py", patch)
        self.assertIn("x = 1", patch)

    def test_integrate_applies_cleanly(self) -> None:
        trees = create_worktrees(self.ws, self.ws / ".swarm-trees", ["build"])
        wt = trees[0].path
        (wt / "new.py").write_text("x = 1\n", encoding="utf-8")
        git("add", "-A", cwd=wt)
        git("commit", "-m", "worker change", cwd=wt)
        patch_file = self.ws / "p.patch"
        patch_file.write_text(worktree_patch(self.ws, wt), encoding="utf-8")
        result = integrate_patch(self.ws, patch_file)
        self.assertTrue(result["applied"], result)
        self.assertTrue((self.ws / "new.py").exists())

    def test_integrate_refuses_to_clobber(self) -> None:
        trees = create_worktrees(self.ws, self.ws / ".swarm-trees", ["build"])
        wt = trees[0].path
        (wt / "clash.txt").write_text("worker\n", encoding="utf-8")
        git("add", "-A", cwd=wt)
        git("commit", "-m", "worker change", cwd=wt)
        patch_file = self.ws / "p.patch"
        patch_file.write_text(worktree_patch(self.ws, wt), encoding="utf-8")
        # Someone else already put a different file there.
        (self.ws / "clash.txt").write_text("other\n", encoding="utf-8")
        result = integrate_patch(self.ws, patch_file)
        self.assertFalse(result["applied"])
        self.assertIn("clash.txt", str(result))

    def test_remove_worktree(self) -> None:
        trees = create_worktrees(self.ws, self.ws / ".swarm-trees", ["build"])
        remove_worktree(self.ws, trees[0].path)
        self.assertFalse(trees[0].path.exists())


if __name__ == "__main__":
    unittest.main()
"""Contract linter regressions.

These lock in the three confirmed bugs (inert `bash` rules, subagent model
pin, unverified self-promotion) so they cannot come back silently, plus the
false positives I hit while fixing them.
"""

from __future__ import annotations

import textwrap
import unittest

from swarmkit import contracts, manager as manager_mod
from swarmkit.contracts import validate_repo
from tests.support import REPO_ROOT, TempWorkspace

PAGE_OK = textwrap.dedent(
    """\
    ---
    description: does a thing
    mode: subagent
    permissions:
      - action: shell
        resource: "*"
        effect: allow
      - action: shell
        resource: "git push*"
        effect: deny
    ---

    Body.
    """
)


def write(root, rel: str, text: str):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def codes(root) -> set[str]:
    return {i.code for page in validate_repo(root) for i in page.issues}


class TestBashRegression(TempWorkspace):
    def test_bash_action_is_flagged_as_inert(self) -> None:
        """BUG: `bash` is not a V2 action, so every `bash` deny blocked nothing."""
        write(self.ws, "agents/a.md", PAGE_OK.replace("action: shell", "action: bash"))
        self.assertIn("SW003", codes(self.ws))

    def test_shell_is_accepted(self) -> None:
        write(self.ws, "agents/a.md", PAGE_OK)
        self.assertNotIn("SW003", codes(self.ws))

    def test_legacy_permission_field_is_flagged(self) -> None:
        # BUG: a V1 `permission:` mapping was silently dropped by the reader.
        # It must still be reported, even though the shape is unreadable.
        write(self.ws, "agents/a.md",
              '---\ndescription: x\nmode: subagent\npermission:\n'
              '  bash: ask\n---\n\nbody\n')
        found = codes(self.ws)
        self.assertIn("SW002", found)
        self.assertIn("SW001", found)


class TestOrdering(TempWorkspace):
    def test_deny_shadowed_by_later_same_action_allow(self) -> None:
        text = PAGE_OK.replace(
            '  - action: shell\n    resource: "*"\n    effect: allow\n',
            '  - action: shell\n    resource: "git push*"\n    effect: deny\n'
            '  - action: shell\n    resource: "*"\n    effect: allow\n',
        )
        write(self.ws, "agents/a.md", text)
        self.assertIn("SW004", codes(self.ws))

    def test_different_action_allow_does_not_shadow(self) -> None:
        """Regression: a `webfetch` allow must not be reported as shadowing a
        `shell` deny, even though both use resource '*'."""
        text = textwrap.dedent(
            """\
            ---
            description: research unit
            mode: subagent
            permissions:
              - action: shell
                resource: "*"
                effect: deny
              - action: webfetch
                resource: "*"
                effect: allow
            ---

            Body.
            """
        )
        write(self.ws, "agents/a.md", text)
        self.assertNotIn("SW004", codes(self.ws))

    def test_allow_above_deny_is_correct_and_quiet(self) -> None:
        write(self.ws, "agents/a.md", PAGE_OK)
        self.assertNotIn("SW004", codes(self.ws))


class TestModelPin(TempWorkspace):
    def test_subagent_model_pin_is_flagged(self) -> None:
        write(self.ws, "agents/a.md", PAGE_OK.replace(
            "mode: subagent", "mode: subagent\nmodel: tokenharbor/deepseek-v4.1-flash:free"
        ))
        self.assertIn("SW005", codes(self.ws))

    def test_malformed_model_reference_is_flagged(self) -> None:
        write(self.ws, "agents/a.md", PAGE_OK.replace(
            "mode: subagent", "mode: subagent\nmodel: someprovider:free-model"
        ))
        self.assertIn("SW006", codes(self.ws))

    def test_wellformed_model_on_a_subagent_is_still_a_pin(self) -> None:
        write(self.ws, "agents/a.md", PAGE_OK.replace(
            "mode: subagent", "mode: subagent\nmodel: opencode/some-model"
        ))
        self.assertIn("SW005", codes(self.ws))

    def test_no_pin_is_quiet(self) -> None:
        write(self.ws, "agents/a.md", PAGE_OK)
        self.assertNotIn("SW005", codes(self.ws))
        self.assertNotIn("SW006", codes(self.ws))


class TestNonPagesAndCommands(TempWorkspace):
    def test_changelog_is_not_an_agent_page(self) -> None:
        """Regression: CHANGELOG.md under agents/ was reported as a broken page."""
        write(self.ws, "agents/CHANGELOG.md", "# Changelog\n\nno frontmatter here\n")
        write(self.ws, "agents/a.md", PAGE_OK)
        self.assertNotIn("SW001", codes(self.ws))

    def test_readme_is_not_an_agent_page(self) -> None:
        write(self.ws, "agents/README.md", "# Agents\n")
        write(self.ws, "agents/a.md", PAGE_OK)
        self.assertNotIn("SW001", codes(self.ws))

    def test_command_page_needs_no_mode(self) -> None:
        """Regression: command pages bind with `agent:`, not `mode:`."""
        write(self.ws, "agents/a.md", PAGE_OK)
        write(self.ws, "commands/go.md", "---\ndescription: go\nagent: a\n---\n\nGo.\n")
        found = codes(self.ws)
        self.assertNotIn("SW008", found)
        self.assertNotIn("SW009", found)

    def test_command_bound_to_unknown_agent_is_flagged(self) -> None:
        write(self.ws, "agents/a.md", PAGE_OK)
        write(self.ws, "commands/go.md", "---\ndescription: go\nagent: ghost\n---\n\nGo.\n")
        self.assertIn("SW009", codes(self.ws))

    def test_nested_ids_use_slashes(self) -> None:
        write(self.ws, "agents/swarm/a.md", PAGE_OK)
        write(self.ws, "agents/swarm/b.md", PAGE_OK)
        self.assertEqual(
            contracts.collect_agent_ids(self.ws / "agents", self.ws), {"swarm/a", "swarm/b"}
        )


class TestRealRepoIsClean(unittest.TestCase):
    """The regression guard: this repo's own pages must lint clean."""

    def test_repo_lints_without_errors(self) -> None:
        summary = contracts.summarise(validate_repo(REPO_ROOT))
        if summary["errors"]:
            self.fail(
                "repo contract lint failed:\n"
                + "\n".join(
                    f"  {i['code']} {i['path']}: {i['message']}" for i in summary["issues"]
                )
            )

    def test_every_specialist_page_exists(self) -> None:
        ids = contracts.collect_agent_ids(REPO_ROOT / "agents", REPO_ROOT)
        for expected in ("swarm", "swarm/researcher", "swarm/builder", "swarm/visual",
                         "swarm/qc", "swarm/improve"):
            self.assertIn(expected, ids)

    def test_no_specialist_pins_a_model(self) -> None:
        for page in validate_repo(REPO_ROOT):
            if page.rel.startswith("agents/swarm/"):
                self.assertNotIn("model", page.frontmatter, f"{page.rel} pins a model")

    def test_no_page_uses_the_bash_action(self) -> None:
        for page in validate_repo(REPO_ROOT):
            for rule in page.frontmatter.get("permissions", []) or []:
                if isinstance(rule, dict):
                    self.assertNotEqual(str(rule.get("action")), "bash", page.rel)

    def test_improve_cannot_edit_pages(self) -> None:
        """The self-promotion hole must stay shut."""
        page = next(
            p for p in validate_repo(REPO_ROOT) if p.rel == "agents/swarm/improve.md"
        )
        rules = page.frontmatter.get("permissions", [])
        wildcard_deny = any(
            isinstance(r, dict) and r.get("action") == "edit" and r.get("resource") == "*"
            and r.get("effect") == "deny"
            for r in rules
        )
        self.assertTrue(wildcard_deny, "improve.md must deny edit on * with no page exception")


class TestAgentPagesReachEveryCommandTheyNeed(unittest.TestCase):
    """The orchestrator can only run commands its own pages mention.

    Two rounds of worker-liveness work (heartbeats, progress/wedge detection,
    lease reclaim, crash reconcile) were unreachable because `agents/swarm.md`
    still told the orchestrator to use `budget spawn`, which takes a lease but
    writes no `workers.json` record. Nothing beat, nothing reported progress,
    nothing reclaimed, and the run deadlocked on CONCURRENCY_LIMIT. The linter
    passed the whole time: it reads frontmatter, not page bodies.

    So this reads the BODIES. If a mechanism exists in `swarmkit` and the pages
    never tell the orchestrator to call it, that is a defect, not a doc nit.
    """

    # subcommand -> the pages that must document it.
    REQUIRED = {
        "spawn": ["agents/swarm.md"],
        "beat": ["agents/swarm.md"],
        "progress": ["agents/swarm.md"],
        "finish": ["agents/swarm.md"],
        "status": ["agents/swarm.md"],
        "reclaim": ["agents/swarm.md"],
        "reconcile": ["agents/swarm.md"],
    }

    # The manager entry points. Same failure mode, so same guard: a command
    # that exists but that no page tells the orchestrator to run is unreachable
    # no matter how well it works.
    MANAGER_ENTRY = {
        # Matched on the flag alone, not the whole invocation: a page is free
        # to wrap the command across lines for readability, and a guard that
        # demands an exact string match would just push authors into ugly
        # one-liners. What matters is that the flag is present.
        "--request": ["agents/swarm.md", "commands/swarm.md"],
        "run state": ["agents/swarm.md", "commands/swarm.md"],
        "run triage": ["agents/swarm.md"],
        "approve": ["agents/swarm.md", "commands/swarm.md"],
    }

    # The improvement engine. Same reasoning as MANAGER_ENTRY: a mechanism no
    # agent is told to invoke is a mechanism that does not run, which is the
    # defect two audit rounds already found once each.
    IMPROVEMENT_ENGINE = {
        "experience record": ["agents/swarm.md"],
        "experience challenge": ["agents/swarm.md", "agents/swarm/improve.md"],
        "learn recall": ["agents/swarm.md", "agents/swarm/improve.md"],
        "analyze": ["agents/swarm.md", "agents/swarm/improve.md"],
        "evolve candidate": ["agents/swarm.md"],
        "evolve evaluate": ["agents/swarm.md"],
        "evolve monitor": ["agents/swarm.md"],
        "evolve rollback": ["agents/swarm.md"],
        "evolve scope-check": ["agents/swarm.md"],
    }

    # The session-bound task lifecycle. The same defect one mechanism later:
    # `lifecycle start` / `checkpoint` / `cancel` / `resume` enforce ownership,
    # fencing and durable progress, and an orchestrator that no page ever names
    # will leave work running after its session closes. Matched on the exact
    # fragment, so the page has to name the subcommand as the CLI spells it.
    LIFECYCLE = {
        "lifecycle start": ["agents/swarm.md"],
        "lifecycle checkpoint": ["agents/swarm.md"],
        "lifecycle status": ["agents/swarm.md"],
        "lifecycle cancel": ["agents/swarm.md"],
        "lifecycle resume": ["agents/swarm.md"],
        "lifecycle resume-plan": ["agents/swarm.md"],
        "lifecycle session-close": ["agents/swarm.md"],
        "lifecycle shutdown": ["agents/swarm.md"],
        "lifecycle recover": ["agents/swarm.md"],
    }

    # The plugin's in-session control surface, which is how the orchestrator
    # reaches the same engine from inside a session rather than from a shell.
    # Keys are the `key=value` forms of the tool's own input schema; the plugin
    # forwards only the flags each action accepts, so a page that invents a flag
    # here documents a call that cannot happen.
    LIFECYCLE_TOOL = {
        "action=start": ["agents/swarm.md"],
        "action=checkpoint": ["agents/swarm.md"],
        "action=status": ["agents/swarm.md"],
        "action=cancel": ["agents/swarm.md"],
        "action=resume": ["agents/swarm.md"],
        "action=list": ["agents/swarm.md"],
    }

    # Every spawned worker needs to know how to keep its lease alive.
    WORKERS_NEEDING_LIVENESS = [
        "agents/swarm/builder.md",
        "agents/swarm/researcher.md",
        "agents/swarm/visual.md",
        "agents/swarm/qc.md",
        "agents/swarm/improve.md",
    ]

    @staticmethod
    def _body(rel: str) -> str:
        # Frontmatter is stripped: these assertions are about instructions.
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        parts = text.split("---", 2)
        return parts[2] if len(parts) == 3 else text

    def test_orchestrator_documents_every_workers_command(self) -> None:
        body = self._body("agents/swarm.md")
        for subcommand, pages in self.REQUIRED.items():
            for rel in pages:
                with self.subTest(command=subcommand, page=rel):
                    self.assertIn(
                        f"workers {subcommand}", body,
                        f"{rel} never mentions `workers {subcommand}`; "
                        "the orchestrator cannot run a command it is not told about",
                    )

    def test_orchestrator_tells_workers_to_keep_beating(self) -> None:
        body = self._body("agents/swarm.md")
        for needle in ("--epoch", "EPOCH_MISMATCH", "workers finish"):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)

    def test_every_worker_page_documents_its_own_liveness(self) -> None:
        for rel in self.WORKERS_NEEDING_LIVENESS:
            body = self._body(rel)
            with self.subTest(page=rel):
                for needle in ("workers beat", "workers progress", "workers finish"):
                    self.assertIn(
                        needle, body,
                        f"{rel} never mentions `{needle}`; a worker that is not "
                        "told to beat goes stale and its slot is reclaimed",
                    )

    def test_the_manager_entry_points_are_documented_where_an_agent_reads(self) -> None:
        for command, pages in self.MANAGER_ENTRY.items():
            for page in pages:
                with self.subTest(command=command, page=page):
                    self.assertIn(command, self._body(page))

    def test_the_improvement_engine_is_documented_where_an_agent_reads(self) -> None:
        for command, pages in self.IMPROVEMENT_ENGINE.items():
            for page in pages:
                with self.subTest(command=command, page=page):
                    self.assertIn(command, self._body(page))

    def test_the_lifecycle_commands_are_documented_where_an_agent_reads(self) -> None:
        for command, pages in self.LIFECYCLE.items():
            for page in pages:
                with self.subTest(command=command, page=page):
                    self.assertIn(
                        command, self._body(page),
                        f"{page} never mentions `{command}`; task ownership, "
                        "durable checkpoints and fencing are unreachable unless "
                        "the orchestrator is told the command exists",
                    )

    def test_the_lifecycle_tool_actions_are_documented_where_an_agent_reads(self) -> None:
        for command, pages in self.LIFECYCLE_TOOL.items():
            for page in pages:
                with self.subTest(command=command, page=page):
                    self.assertIn(
                        command, self._body(page),
                        f"{page} never shows `{command}`; an action the plugin "
                        "exposes but no page names is an action nobody calls",
                    )

    def test_the_lifecycle_names_all_nine_stages_on_the_orchestrator_page(self) -> None:
        body = self._body("agents/swarm.md")
        for stage in manager_mod.LIFECYCLE:
            with self.subTest(stage=stage):
                self.assertIn(stage, body)

    def test_the_liveness_contract_reaches_the_delegation_template(self) -> None:
        """The spawn-time contract is where a worker first learns its epoch."""
        body = self._body("agents/swarm.md")
        self.assertIn("LIVENESS:", body)
        self.assertIn("WORKER NAME:", body)


if __name__ == "__main__":
    unittest.main()
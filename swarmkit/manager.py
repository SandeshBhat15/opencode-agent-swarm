"""Autonomous, manager-led orchestration.

The orchestrator protocol in ``agents/swarm.md`` describes a lifecycle. Before
this module existed, describing it was all that happened: nothing took a plain
request and produced a run you could inspect, resume, or prove happened. Two
rounds of audit found exactly that shape of bug - a documented command that
nothing invoked - so the entry point has to be a command, not a paragraph.

What this module owns:

* **Triage** - which lane a request belongs in, so simple work stays cheap.
* **Request intake** - one request becomes one resumable run on disk.
* **Task planning** - a dependency graph with a parallel frontier, so the
  orchestrator can tell what may start now and what genuinely must wait.
* **Approval boundaries** - what the manager may decide alone, and what it must
  stop and ask about. Encoded, so "ask before deploying" is a refusal with a
  code rather than an intention.

Nothing here calls a model. Planning is a deterministic function of the task
graph, so the whole lifecycle is testable without spending tokens.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import phases, store
from .errors import SwarmError

#: A run of this shape must not fan out. Cheap, and answerable directly.
TRIAGE_ANSWER = "R1"
TRIAGE_RESEARCH = "R2"
TRIAGE_SINGLE = "R3"
TRIAGE_FULL = "R4"
TRIAGE_LANES = (TRIAGE_ANSWER, TRIAGE_RESEARCH, TRIAGE_SINGLE, TRIAGE_FULL)

#: One sentence that names both the artifact and the evidence for it.
_ARTIFACT_RE = ("artifact", "deliverable", "build", "app", "site", "page", "script",
                "model", "scene", "file", "feature", "fix", "report", "plugin",
                "extension", "test", "render", "image", "diagram", "doc")

#: A task that is mostly "go and find out" needs researchers, not builders.
_RESEARCH_VERBS = ("research", "investigate", "find out", "compare", "explain",
                   "what is", "how does", "why does", "audit", "review", "find")

#: Verbs that mean the result must be checked by running something.
_VERIFY_WORDS = ("build", "test", "lint", "typecheck", "compile", "run",
                 "install", "deploy", "benchmark", "verify")

#: Things you BUILD, not just answer. A request naming one of these is real
#: deliverable work even when it is a single sentence and names no file.
_DELIVERABLE_KINDS = (
    "app", "application", "website", "site", "page", "game", "component",
    "library", "package", "plugin", "extension", "tool", "cli", "api",
    "service", "server", "endpoint", "dashboard", "model", "scene",
    "animation", "video", "render", "pipeline", "workflow", "feature",
    "system", "automation", "bot", "script", "generator", "converter",
)


@dataclass
class TaskNode:
    """One unit of work in the plan."""

    id: str
    objective: str
    role: str = "swarm/builder"
    depends_on: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    deliverable: str = ""
    criteria: list[str] = field(default_factory=list)
    retries: int = 2
    parallel_group: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TaskNode":
        known = {f for f in cls.__dataclass_fields__}  # noqa: F821 - dataclass attr
        return cls(**{k: v for k, v in raw.items() if k in known})

    def render(self) -> str:
        deps = ", ".join(self.depends_on) or "-"
        return (f"{self.id}  role={self.role}  depends_on=[{deps}]  "
                f"files={','.join(self.files) or '-'}  retries={self.retries}\n"
                f"    objective: {self.objective}\n"
                f"    deliverable: {self.deliverable or '(unspecified)'}")


@dataclass
class ApprovalDecision:
    approved: bool
    reason: str = ""
    code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: Actions the manager may never take on its own. Each entry is a lowercased
#: substring; a match anywhere in the requested action blocks it. Deliberately
#: broad: a false refusal costs one question, an unreviewed deploy costs far
#: more.
#:
#: Matched against the ACTION the manager wants to take, never against the
#: request text. "Add a deploy button to the dashboard" asks for no deployment,
#: and refusing it would train the user to ignore refusals - which is exactly
#: how a real `rm -rf` gets waved through later.
NEEDS_APPROVAL: tuple[str, ...] = (
    "deploy", "publish", "release", "push", "npm publish", "pip install",
    "purchase", "buy", "payment", "charge", "delete", "rm -rf", "drop table",
    "truncate", "force-push", "production", "secret", "rotate key",
    "send email", "email to", "post to", "merge to main", "prune", "revoke",
    "terraform apply", "kubectl apply", "systemctl", "choco install",
)

#: Things the manager may always decide alone. Present so the rule is two-sided:
#: silence here means "allowed", and a stray word in a task description must not
#: turn an ordinary local edit into an escalation. Checked FIRST, so "add a
#: deploy button" stays allowed while a bare "deploy" is still gated.
ALWAYS_ALLOWED: tuple[str, ...] = (
    "read", "grep", "glob", "search", "edit", "write", "create", "add", "update",
    "refactor", "rename", "test", "lint", "format", "build", "install dependency",
    "comment", "docstring", "type hint", "implement", "fix", "document",
    "scaffold", "generate", "migrate", "wire up", "hook up",
)

#: Cues that the manager is about to take a consequential action rather than
#: merely mention one. Checked against the action string.
_ACTION_INTENT: tuple[str, ...] = ("run", "execute", "perform", "apply", "push",
                                   "publish", "deploy", "send", "delete", "remove")


def needs_approval(action: str) -> ApprovalDecision:
    """Decide whether ``action`` requires the user before the manager proceeds.

    Encoded rather than described because the orchestrator's prose said "ask
    before anything destructive" for several rounds without it ever being
    checked. A refusal carries a code so it can be asserted in a test.

    A bare imperative like ``"deploy"`` is gated. A phrase like ``"add a deploy
    button"`` is not, because it asks for code, not for a deployment - and a
    refusal there would push the user towards blanket approval.
    """
    text = (action or "").lower().strip()
    if not text:
        return ApprovalDecision(True, "nothing to do")
    if any(token in text for token in ALWAYS_ALLOWED):
        return ApprovalDecision(True, "ordinary local work")
    intentful = any(text == verb or text.startswith(verb + " ") or
                    (" " + verb + " ") in text or text.endswith(" " + verb)
                    for verb in _ACTION_INTENT)
    for token in NEEDS_APPROVAL:
        if token in text:
            # Only gate when the request is to DO it, not to describe it.
            if not intentful and len(text.split()) > 3:
                return ApprovalDecision(True, "mentions the action, does not request it")
            return ApprovalDecision(
                False,
                f"'{token}' is a consequential action; the manager may not "
                f"do it without the user's explicit go-ahead",
                code="APPROVAL_REQUIRED",
            )
    return ApprovalDecision(True, "no approval-gated token in the action")


def _names_several_kinds(words: list[str]) -> bool:
    """True when a request names two or more different deliverable kinds.

    "build me a Vite app that renders a subway runner game" is one short
    sentence with no "then"/"and then"/";" marker, so the conjunction check
    misses it and it routes to the cheap single-file lane. But naming an app
    AND a game is two deliverables that need two scopes, two criteria and
    (almost certainly) two workers. Counting distinct kinds is what separates
    a genuine multi-part brief from a one-liner.
    """
    return len({w.strip(".,?!:;") for w in words} & set(_DELIVERABLE_KINDS)) >= 2


def triage(request: str) -> str:
    """Route a request to a lane.

    Cheap work should stay cheap. Spawning a swarm to answer a question burns
    tokens and adds failure modes for no gain, so the lanes exist to keep the
    fan-out proportional to the request.
    """
    text = (request or "").strip().lower()
    if not text:
        raise SwarmError("cannot triage an empty request", code="EMPTY_REQUEST")

    words = text.split()
    asks_artifact = any(w.strip(".,?!:;") in _ARTIFACT_RE for w in words)
    asks_kind = any(w.strip(".,?!:;") in _DELIVERABLE_KINDS for w in words)
    asks_research = any(v in text for v in _RESEARCH_VERBS)
    asks_verify = any(v in text for v in _VERIFY_WORDS)

    if len(words) <= 18 and not asks_artifact and not asks_kind and not asks_verify:
        return TRIAGE_ANSWER
    if asks_research and not (asks_artifact or asks_kind):
        return TRIAGE_RESEARCH
    if not asks_artifact and not asks_kind and not asks_verify:
        return TRIAGE_RESEARCH
    if asks_research:
        return TRIAGE_FULL
    # Names something to build or to check by running it: real deliverable work.
    # A single-file scope keeps it cheap; anything wider earns the full pipeline.
    if _looks_multi_file(request) or _names_several_kinds(words):
        return TRIAGE_FULL
    return TRIAGE_SINGLE


def _looks_multi_file(request: str) -> bool:
    """Heuristic: does this ask for more than one thing to change?"""
    markers = (" and then ", " then ", " plus ", " also ", " as well ", ";")
    lowered = (request or "").lower()
    if any(m in lowered for m in markers):
        return True
    return lowered.count(".") >= 2


def _idify(text: str, index: int) -> str:
    slug = store.slugify(text or f"task-{index}")
    return f"ws{index}-{slug[:32]}" if slug else f"ws{index}"


def plan_tasks(
    objectives: Iterable[str],
    *,
    roles: dict[str, str] | None = None,
    retry_limit: int = 2,
) -> list[TaskNode]:
    """Turn a list of objectives into a dependency-ordered task graph.

    Each objective becomes one task. Ordering follows the order given, which
    keeps planning deterministic and reviewable: the same request produces the
    same plan every run, so a diff in the plan means the request changed.
    Independent tasks land in the same ``parallel_group``, which is what tells
    the orchestrator they may be spawned together.
    """
    roles = roles or {}
    nodes: list[TaskNode] = []
    for index, objective in enumerate(objectives, start=1):
        text = str(objective or "").strip()
        if not text:
            raise SwarmError(f"objective {index} is empty", code="EMPTY_OBJECTIVE")
        node = TaskNode(
            id=_idify(text, index),
            objective=text,
            role=roles.get(text) or ("swarm/researcher" if _reads_like_research(text)
                                     else "swarm/builder"),
            depends_on=[],
            # Every task needs something to prove. A criterion-free task makes
            # the whole run unprovable: `gate.assert_open` refuses with
            # NO_CRITERIA, so an auto-planned run could never reach DELIVERED
            # and the manager would deadlock against its own plan.
            criteria=[f"{text} is complete and verified"],
            retries=retry_limit,
            parallel_group=0,
        )
        nodes.append(node)
    # Everything declared together is treated as independent. Sequencing is the
    # orchestrator's call, expressed by declaring fewer objectives per batch;
    # guessing dependencies from wording produced plans nobody could verify.
    return nodes


def _reads_like_research(text: str) -> bool:
    lowered = text.lower()
    return any(v in lowered for v in ("find out", "research", "investigate",
                                      "determine", "compare", "list", "explain"))


def parallel_frontier(nodes: Iterable[TaskNode]) -> list[TaskNode]:
    """Tasks whose dependencies are all satisfied right now.

    This is what makes "which can run at the same time" answerable instead of a
    judgement call: a task is ready when nothing it depends on is still open.
    """
    todo = {n.id: n for n in nodes}
    ready: list[TaskNode] = []
    for node in todo.values():
        if all(dep not in todo for dep in node.depends_on):
            ready.append(node)
    return ready


def blocked_by_cycles(nodes: Iterable[TaskNode]) -> list[list[str]]:
    """Report dependency cycles, which would otherwise deadlock the run.

    A plan with a cycle never becomes runnable, and the failure would surface
    far from its cause. Refusing the plan up front keeps the error next to the
    thing that made it.
    """
    todo = {n.id: list(n.depends_on) for n in nodes}
    cycles: list[list[str]] = []
    state: dict[str, int] = {}

    def visit(node_id: str, stack: list[str]) -> None:
        state[node_id] = 1
        stack.append(node_id)
        for dep in todo.get(node_id, []):
            if state.get(dep) == 1:
                cycles.append(stack[stack.index(dep):] + [dep])
            elif state.get(dep, 0) == 0:
                visit(dep, stack)
        stack.pop()
        state[node_id] = 2

    for node_id in todo:
        if state.get(node_id, 0) == 0:
            visit(node_id, [])
    return cycles


#: Instructions the manager hands back to the orchestrator. Keeping them here
#: rather than only in prose means the required sequence is testable.
LIFECYCLE = (
    "UNDERSTAND", "PLAN", "DELEGATE", "EXECUTE", "MONITOR",
    "INTEGRATE", "VERIFY", "REPAIR", "DELIVER",
)


def lifecycle_state(run_dir: Path) -> dict[str, Any]:
    """Where a run is and what the manager must do next.

    Single source of truth for "resume this run". ``run show`` already reports a
    next action, but only for the phase; this adds the outstanding obligations
    - workers still holding slots, criteria with no evidence, a gate that is
    shut - so a resumed session is told what is unfinished rather than
    rediscovering it.
    """
    from . import budget, gate, workers

    run = store.RunState.load(run_dir)
    obligations: list[str] = []
    alive: list[dict[str, Any]] = []
    try:
        status = workers.Registry(run_dir).status(stale_seconds=workers.DEFAULT_STALE_SECONDS)
        alive = status.get("alive", []) + status.get("slow", [])
        for bucket in ("stale", "timeout", "wedged"):
            # These buckets hold worker NAMES, like `alive` does. Indexing
            # them as records raised TypeError instead of reporting the stall.
            for name in status.get(bucket, []):
                obligations.append(f"worker {name} is {bucket} - reclaim or restart it")
    except SwarmError as exc:
        obligations.append(f"worker registry unreadable: {exc}")

    try:
        charged = budget.Budget.load(run_dir)
        if charged.inflight:
            charged_names = set(charged.snapshot()["inflight"])
            # `status()["alive"]` holds worker NAMES, not records. Indexing
            # them as dicts raised TypeError, which turned every orphan check
            # into a crash rather than a report.
            live_names = set(alive)
            for name in sorted(charged_names - live_names):
                obligations.append(f"lease held by {name} with no live worker - run 'workers reconcile'")
    except SwarmError as exc:
        obligations.append(f"budget unreadable: {exc}")

    try:
        checked = gate.evaluate(run_dir)
        if checked.failed:
            for res in checked.results:
                if not res.passed:
                    obligations.append(f"criterion {res.criterion.id} unproven: {res.reasons}")
        elif not checked.open:
            obligations.append("no acceptance criteria declared yet")
    except SwarmError as exc:
        obligations.append(f"gate unreadable: {exc}")

    if store.pending_journal(run_dir) is not None:
        obligations.append("an interrupted mutation left a journal - run 'run recover'")

    return {
        "run": run.slug,
        "phase": run.phase,
        "unfinished": run.phase in phases.UNFINISHED,
        # `alive` holds worker NAMES already.
        "live_workers": sorted(alive),
        "obligations": obligations,
        # Only a pending journal blocks continuation. Outstanding obligations
        # are the manager's work list, not a hard stop, so treating them as a
        # block would stall the very run that could clear them.
        "clean_to_continue": not any(
            o.startswith("an interrupted mutation") for o in obligations
        ),
    }


def write_plan(run_dir: Path, nodes: Iterable[TaskNode], *, goal: str = "") -> Path:
    """Persist the plan so QC, a resumed session, and the user all read the
    same thing rather than three reconstructions of it."""
    nodes = list(nodes)
    cycles = blocked_by_cycles(nodes)
    if cycles:
        raise SwarmError(
            "plan has a dependency cycle: " + " -> ".join(cycles[0]),
            code="PLAN_CYCLE",
        )
    plan = {
        "goal": goal,
        "created_at": store.utcnow(),
        "lifecycle": list(LIFECYCLE),
        "tasks": [n.to_dict() for n in nodes],
        "parallel": [[n.id for n in parallel_frontier(nodes)]],
    }
    store.atomic_write_json(run_dir / store.PLAN_TASKS_FILE, plan)
    text = ["GOAL: " + (goal or "(unset)"), "", "LIFECYCLE: " + " -> ".join(LIFECYCLE), ""]
    for node in nodes:
        text.append(node.render())
    store.atomic_write_text(run_dir / store.WORKSTREAMS_FILE, "\n".join(text) + "\n")
    return run_dir / store.PLAN_TASKS_FILE
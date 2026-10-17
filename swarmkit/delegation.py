"""Delegate-or-not as a measurable, testable decision.

Triage (:func:`swarmkit.manager.triage`) already answers *which lane* a request
belongs in, but a lane is only a guess about shape. The expensive failure this
module exists to stop is narrower and more common: spawning a worker that
cannot earn its cost. A specialist who repeats research somebody already
verified, a second agent pointed at the same objective, a QC pass over a result
with nothing to check, a fresh summary of an unchanged result - each is a spawn
that burns tokens and adds a failure mode for no gain.

So the decision here is arithmetic, not vibes. Benefit signals (specialist
capability, genuine independence, risk reduction) and cost signals (spawn,
context handover, latency, duplicate-work risk) are summed and compared against
configurable thresholds, and every outcome carries a one-sentence reason and a
stable code so it can be asserted in a test rather than argued about.

Three rules are deliberately absolute, because they are the ways a
token-minded system quietly ships something broken:

* A mandatory test, security check, or acceptance criterion is **never** waived
  to save tokens. :func:`mandatory_checks_required` encodes that as a refusal
  that accepts a cost argument and ignores it.
* Retries are bounded by the existing budget ceiling
  (:class:`swarmkit.budget.Limits`); :func:`retry_decision` either returns
  targeted repair instructions or stops, and can never loop on its own.
* A verified result is reused, not reproduced; the four named wastes
  (redundant agents, duplicate research, repeated summaries, unnecessary QC)
  each have a refusing function below.

Nothing here calls a model or touches the network. It is a deterministic
function of the signals it is handed, which is what makes the whole swarm's
fan-out testable without spending a token.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .budget import Limits
from .errors import SwarmError
from .manager import TRIAGE_ANSWER

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Why a decision was refused. Kept stable so the CLI and the tests can match on
#: a code instead of a sentence that a later edit might reword.
CODE_ANSWER_DIRECT = "ANSWER_DIRECT"
CODE_REMAINING_WORK_LOW = "REMAINING_WORK_LOW"
CODE_DEPTH_EXCEEDED = "DEPTH_EXCEEDED"
CODE_NO_SPECIALIST_VALUE = "NO_SPECIALIST_VALUE"
CODE_COST_EXCEEDS_BENEFIT = "COST_EXCEEDS_BENEFIT"
CODE_DELEGATE = "DELEGATE"
CODE_DUPLICATE_WORKER = "DUPLICATE_WORKER"
CODE_UNIQUE_WORKER = "UNIQUE_WORKER"
CODE_REUSE_VERIFIED = "REUSE_VERIFIED"
CODE_NO_VERIFIED_RESULT = "NO_VERIFIED_RESULT"
CODE_QC_REQUIRED = "QC_REQUIRED"
CODE_QC_UNNEEDED = "QC_UNNEEDED"
CODE_SUMMARY_REDUNDANT = "SUMMARY_REDUNDANT"
CODE_SUMMARY_NEEDED = "SUMMARY_NEEDED"
CODE_MANDATORY_CHECK_REFUSED = "MANDATORY_CHECK_REFUSED"
CODE_NO_MANDATORY_CHECK = "NO_MANDATORY_CHECK"
CODE_RETRY_WITH_REPAIR = "RETRY_WITH_REPAIR"
CODE_RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
CODE_RETRY_DISABLED = "RETRY_DISABLED"


def _norm(text: Any) -> str:
    """Case- and whitespace-insensitive key for comparing objectives/roles.

    Two objectives that differ only in case or spacing describe the same work;
    treating them as different is exactly how a duplicate worker slips through.
    """
    return " ".join(str("" if text is None else text).strip().lower().split())


# ---------------------------------------------------------------------------
# The decision inputs
# ---------------------------------------------------------------------------


@dataclass
class DelegationSignals:
    """The measurable inputs to a delegate-or-not decision.

    Benefit signals say a specialist would *add* something; cost signals say
    handing off would *subtract* something. Keeping them on one object makes the
    trade explicit at the call site instead of buried in a judgement call.

    ``lane`` is the :func:`swarmkit.manager.triage` lane when known. It is
    informational for most decisions, but an answer-lane request is answered
    directly without needing a worker at all.
    """

    # -- benefit ---------------------------------------------------------
    specialist_capability: bool = False
    """A named specialist is genuinely required (a skill the manager lacks)."""
    independent_workstreams: int = 1
    """How many units of work can proceed without touching each other."""
    risk_reduction: bool = False
    """An independent check would remove real risk, not just add ceremony."""

    # -- cost ------------------------------------------------------------
    spawn_cost: float = 0.0
    """Relative cost to start one more worker (tokens + slot + setup)."""
    context_cost: float = 0.0
    """Relative cost of handing the worker enough context to be useful."""
    latency_cost: float = 0.0
    """Relative cost of the round trip: hand-off, wait, integrate."""
    duplicate_risk: float = 0.0
    """0..1-ish risk the worker repeats work that is already done."""

    # -- scope -----------------------------------------------------------
    remaining_work: float = 1.0
    """Fraction of the task still ahead. Small means "just finish it"."""
    lane: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DelegationPolicy:
    """Configurable thresholds. Every gate in :func:`decide` reads from here.

    Defaults are stated as named fields rather than magic numbers so a caller
    can tighten or loosen the swarm's appetite for fan-out without editing
    logic. ``max_workers`` mirrors the budget's concurrency cap so delegation
    can never propose more workers than the run is allowed to hold at once.
    """

    max_depth: int = 2
    max_workers: int = 4
    min_remaining_work: float = 0.25
    min_net_benefit: float = 0.1
    weight_specialist: float = 0.5
    weight_parallel: float = 0.35
    weight_risk: float = 0.3

    @classmethod
    def from_limits(cls, limits: Limits, **overrides: Any) -> "DelegationPolicy":
        """Derive a policy from the run's existing budget ceilings.

        Reuses :class:`swarmkit.budget.Limits` rather than inventing a second
        concurrency number, so a run cannot delegate into a limit it already
        promised not to exceed.
        """
        base: dict[str, Any] = {"max_workers": int(limits.max_concurrent_agents)}
        base.update(overrides)
        return cls(**base)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def benefit_score(signals: DelegationSignals, policy: DelegationPolicy | None = None) -> float:
    """Sum of the benefit signals, weighted by ``policy``.

    Independence is counted from the *second* workstream onward: one workstream
    is what the manager was going to do anyway, so it is not a benefit that the
    hand-off buys. Extra workstreams are capped at what the run may hold.
    """
    policy = policy or DelegationPolicy()
    score = 0.0
    if signals.specialist_capability:
        score += policy.weight_specialist
    extra = max(0, int(signals.independent_workstreams) - 1)
    if extra:
        score += policy.weight_parallel * min(extra, max(0, policy.max_workers - 1))
    if signals.risk_reduction:
        score += policy.weight_risk
    return score


def cost_score(signals: DelegationSignals, policy: DelegationPolicy | None = None) -> float:
    """Sum of the cost signals. ``policy`` is accepted for symmetry/future use."""
    del policy  # no weights yet; kept so callers do not branch on signature
    return (
        float(signals.spawn_cost)
        + float(signals.context_cost)
        + float(signals.latency_cost)
        + float(signals.duplicate_risk)
    )


# ---------------------------------------------------------------------------
# 1. decide: run it here, or hand it off?
# ---------------------------------------------------------------------------


@dataclass
class DelegationDecision:
    """Whether to delegate, why, and (when delegating) how wide.

    ``reason`` is a single sentence by construction, so it can be shown to the
    user as-is; ``code`` is the assertable part.
    """

    delegate: bool
    reason: str
    code: str = ""
    worker_count: int = 0
    depth: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _delegation_clauses(signals: DelegationSignals) -> list[str]:
    """The concrete benefit(s) present, as clause fragments for the reason."""
    clauses: list[str] = []
    if signals.specialist_capability:
        clauses.append("a specialist capability is required")
    if signals.independent_workstreams >= 2:
        clauses.append("the workstreams run independently")
    if signals.risk_reduction:
        clauses.append("an independent check removes real risk")
    return clauses


def decide(
    signals: DelegationSignals,
    *,
    policy: DelegationPolicy | None = None,
    depth: int = 0,
) -> DelegationDecision:
    """Choose between the manager doing the work and delegating it.

    The checks are ordered cheapest-reason-first so the reported reason is the
    first thing that actually settles the question:

    1. An answer-lane request is answered directly - no worker adds anything.
    2. Too little work left to be worth a hand-off.
    3. The delegation depth cap is reached.
    4. No benefit signal is present at all.
    5. Cost meets or exceeds benefit.

    Only when none of those fire does the manager delegate, which is what keeps
    simple work cheap and fan-out proportional to the request.
    """
    policy = policy or DelegationPolicy()
    depth = max(0, int(depth))

    if _norm(signals.lane) == _norm(TRIAGE_ANSWER):
        return DelegationDecision(
            False,
            "This is an answer-lane request, so the manager answers it directly "
            "rather than paying for a hand-off.",
            code=CODE_ANSWER_DIRECT,
            depth=depth,
        )

    if float(signals.remaining_work) < policy.min_remaining_work:
        return DelegationDecision(
            False,
            "So little work remains that spawning a worker would cost more than "
            "finishing it directly.",
            code=CODE_REMAINING_WORK_LOW,
            depth=depth,
        )

    if depth >= policy.max_depth:
        return DelegationDecision(
            False,
            "Delegation has reached its configured maximum depth, so the manager "
            "finishes this level directly instead of nesting further.",
            code=CODE_DEPTH_EXCEEDED,
            depth=depth,
        )

    benefit = benefit_score(signals, policy)
    if benefit <= 0:
        return DelegationDecision(
            False,
            "No specialist capability, genuine independence, or risk reduction "
            "is in play, so a worker would add cost without adding value.",
            code=CODE_NO_SPECIALIST_VALUE,
            depth=depth,
        )

    cost = cost_score(signals, policy)
    if benefit - cost < policy.min_net_benefit:
        return DelegationDecision(
            False,
            "The cost of spawning, handing over context, waiting, and risking "
            "duplicate work outweighs the benefit a specialist would add.",
            code=CODE_COST_EXCEEDS_BENEFIT,
            depth=depth,
        )

    workers = max(1, min(int(signals.independent_workstreams), policy.max_workers))
    clauses = _delegation_clauses(signals) or ["a specialist adds measurable value"]
    return DelegationDecision(
        True,
        f"Delegating to {workers} worker(s) because "
        f"{' and '.join(clauses)} justifies the hand-off cost.",
        code=CODE_DELEGATE,
        worker_count=workers,
        depth=depth + 1,
    )


# ---------------------------------------------------------------------------
# 2. Waste #1 - redundant agents
# ---------------------------------------------------------------------------


@dataclass
class WorkerRecord:
    """One already-running or already-planned worker."""

    name: str = ""
    role: str = ""
    objective: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WorkerRecord":
        return cls(
            name=str(raw.get("name", "")),
            role=str(raw.get("role", "")),
            objective=str(raw.get("objective", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _as_worker(item: WorkerRecord | dict[str, Any]) -> WorkerRecord:
    return item if isinstance(item, WorkerRecord) else WorkerRecord.from_dict(item)


def dedupe_workers(
    objective: str,
    role: str,
    *,
    existing: Iterable[WorkerRecord | dict[str, Any]] = (),
    name: str = "",
) -> DelegationDecision:
    """Refuse a worker that would repeat one already assigned this objective.

    A duplicate is the same objective *and* the same role (or the same worker
    name). The same objective under a *different* role is allowed: splitting one
    objective across a builder and a verifier is a real division of labour, not
    redundancy.
    """
    goal = _norm(objective)
    if not goal:
        raise SwarmError("cannot dedupe a worker with no objective", code="EMPTY_OBJECTIVE")
    want_role = _norm(role)
    for raw in existing:
        seen = _as_worker(raw)
        same_name = bool(name) and _norm(seen.name) == _norm(name)
        same_goal = _norm(seen.objective) == goal
        same_role = _norm(seen.role) == want_role
        if same_name or (same_goal and same_role):
            who = seen.name or seen.role or "an existing worker"
            return DelegationDecision(
                False,
                f"{who} already covers this objective and role, so spawning "
                "another would duplicate the work.",
                code=CODE_DUPLICATE_WORKER,
            )
    return DelegationDecision(
        True,
        "No existing worker covers this objective and role, so this one is not "
        "redundant.",
        code=CODE_UNIQUE_WORKER,
    )


# ---------------------------------------------------------------------------
# 3. Waste #2 - duplicate research
# ---------------------------------------------------------------------------


@dataclass
class VerifiedResult:
    """An earlier result plus whether it was actually proved."""

    objective: str = ""
    result: str = ""
    verified: bool = False
    evidence: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "VerifiedResult":
        return cls(
            objective=str(raw.get("objective", "")),
            result=str(raw.get("result", "")),
            verified=bool(raw.get("verified", False)),
            evidence=str(raw.get("evidence", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReuseDecision:
    """Whether an existing verified result answers the objective."""

    reuse: bool
    reason: str
    code: str = ""
    result: str = ""
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _as_result(item: VerifiedResult | dict[str, Any]) -> VerifiedResult:
    return item if isinstance(item, VerifiedResult) else VerifiedResult.from_dict(item)


def reuse_verified(
    objective: str,
    *,
    verified: Iterable[VerifiedResult | dict[str, Any]] = (),
) -> ReuseDecision:
    """Reuse a verified result instead of re-running identical work.

    Only a result flagged ``verified`` counts. An unverified result is a claim,
    and reusing a claim is how the swarm ships something nobody checked - so it
    is treated as absent and the work runs again.
    """
    goal = _norm(objective)
    if not goal:
        raise SwarmError("cannot reuse a result for an empty objective", code="EMPTY_OBJECTIVE")
    for raw in verified:
        found = _as_result(raw)
        if found.verified and _norm(found.objective) == goal:
            return ReuseDecision(
                True,
                "A verified result already answers this objective, so the work "
                "is reused rather than repeated.",
                code=CODE_REUSE_VERIFIED,
                result=found.result,
                evidence=found.evidence,
            )
    return ReuseDecision(
        False,
        "No verified result answers this objective, so the work has to be done.",
        code=CODE_NO_VERIFIED_RESULT,
    )


# ---------------------------------------------------------------------------
# 4. Waste #3 - unnecessary QC passes
# ---------------------------------------------------------------------------


@dataclass
class QCDecision:
    """Whether a QC pass can be skipped."""

    skip: bool
    reason: str
    code: str = ""
    required_by: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def should_skip_qc(
    *,
    criteria: Iterable[str] = (),
    security_required: bool = False,
    token_pressure: bool = False,
) -> QCDecision:
    """Decide whether QC may be skipped for lack of anything to check.

    QC is skipped only when *nothing declared a reason to run it*: no acceptance
    criterion and no security check. When either is present the pass is
    mandatory and ``token_pressure`` cannot waive it - that argument exists
    precisely so the refusal is exercised by a test rather than assumed.
    """
    required_by: list[str] = []
    if list(criteria):
        required_by.append("acceptance criteria")
    if security_required:
        required_by.append("security check")
    if required_by:
        return QCDecision(
            False,
            f"A QC pass is required by {' and '.join(required_by)}, and no token "
            "pressure may waive it.",
            code=CODE_QC_REQUIRED,
            required_by=tuple(required_by),
        )
    return QCDecision(
        True,
        "Nothing declared an acceptance criterion or a security check, so a QC "
        "pass would only re-read the same result.",
        code=CODE_QC_UNNEEDED,
    )


# ---------------------------------------------------------------------------
# 5. Waste #4 - repeated summaries
# ---------------------------------------------------------------------------


@dataclass
class SummaryDecision:
    """Whether another summary would actually add anything."""

    skip: bool
    reason: str
    code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def skip_redundant_summary(
    *,
    already_summarised: bool = False,
    new_material: bool = False,
) -> SummaryDecision:
    """Refuse to re-summarise a result that is already summarised.

    A summary is only worth its tokens when there is something new to fold in
    or nothing has been summarised yet. Re-summarising unchanged material is
    pure repetition that grows the transcript without moving the work.
    """
    if already_summarised and not new_material:
        return SummaryDecision(
            True,
            "This result is already summarised and no new material has arrived, "
            "so another summary would merely repeat it.",
            code=CODE_SUMMARY_REDUNDANT,
        )
    return SummaryDecision(
        False,
        "There is either no summary yet or new material to fold in, so a "
        "summary adds something.",
        code=CODE_SUMMARY_NEEDED,
    )


# ---------------------------------------------------------------------------
# 6. The absolute rule - cost never waives a required check
# ---------------------------------------------------------------------------


@dataclass
class MandatoryCheckDecision:
    """The refusal that lets cost pressure be passed in and ignored."""

    skip_allowed: bool
    reason: str
    code: str = ""
    mandatory: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def mandatory_checks_required(
    *,
    tests: Iterable[str] = (),
    security: Iterable[str] = (),
    criteria: Iterable[str] = (),
    token_saving: float = 0.0,
) -> MandatoryCheckDecision:
    """Refuse to drop a mandatory check to save tokens.

    A necessary test, a security check, or an acceptance criterion is never
    traded for tokens. ``token_saving`` is accepted so a caller cannot claim it
    was ignored by accident, and is deliberately not consulted when a mandatory
    check exists: that is the whole point of the refusal.
    """
    del token_saving  # accepted on purpose; never used to relax the rule
    mandatory: list[str] = []
    mandatory += [f"test:{_norm(t)}" for t in tests]
    mandatory += [f"security:{_norm(s)}" for s in security]
    mandatory += [f"criterion:{_norm(c)}" for c in criteria]
    if mandatory:
        return MandatoryCheckDecision(
            False,
            "Skipping a mandatory test, security check, or acceptance criterion "
            "to save tokens is refused, because cost pressure never overrides a "
            "required check.",
            code=CODE_MANDATORY_CHECK_REFUSED,
            mandatory=tuple(mandatory),
        )
    return MandatoryCheckDecision(
        True,
        "No test, security check, or acceptance criterion is outstanding, so "
        "there is nothing mandatory to skip.",
        code=CODE_NO_MANDATORY_CHECK,
    )


# ---------------------------------------------------------------------------
# 7. Retry, bounded, with targeted repair
# ---------------------------------------------------------------------------

#: Cause -> the two most useful next actions. A retry that repeats the action
#: that just failed is not a repair; naming the cause is what makes the retry
#: targeted. Unknown causes fall back to a reproduce-then-narrow default.
_REPAIR_PLAYBOOK: dict[str, tuple[str, str]] = {
    "tool_failure": (
        "Re-run the exact command that failed and read its stderr to the end.",
        "Fix the failing tool invocation itself, not the test that caught it.",
    ),
    "verification_gap": (
        "Add the missing evidence for the criterion that failed.",
        "Re-run that criterion's own check and attach its raw output.",
    ),
    "scope_overlap": (
        "Shrink this worker's write scope to the files it owns.",
        "Re-run only after the overlapping writer has released the shared file.",
    ),
    "dependency_conflict": (
        "Pin or upgrade the conflicting dependency to a compatible version.",
        "Re-run only the failing check to confirm the conflict is gone.",
    ),
    "bad_plan": (
        "Re-plan the step that was wrong before executing it again.",
        "Re-run the smallest command that shows the new plan holds.",
    ),
    "stale_worker": (
        "Reclaim the stale worker and restart it under a fresh epoch.",
        "Re-run the abandoned step from its last checkpoint.",
    ),
    "agent_selection": (
        "Route the step to the role named in the failure.",
        "Re-run the step under that role and capture the result.",
    ),
    "duplicate_execution": (
        "Drop the duplicate worker and let the surviving one finish.",
        "Re-run only the work the surviving worker has not yet covered.",
    ),
}


def _repair_instructions(cause: str) -> tuple[str, ...]:
    """Turn a failure cause into concrete next actions."""
    key = _norm(cause).replace(" ", "_")
    mapped = _REPAIR_PLAYBOOK.get(key)
    if mapped:
        return mapped
    if _norm(cause):
        return (
            f"Reproduce the failure and read the exact error reported by {cause!r}.",
            "Change only what that error names, then re-run the failing check.",
        )
    return (
        "Reproduce the failure and read the exact error.",
        "Change only what that error names, then re-run the failing check.",
    )


@dataclass
class RetryDecision:
    """Whether to retry, and with what."""

    retry: bool
    reason: str
    code: str = ""
    attempt: int = 0
    limit: int = 0
    instructions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def retry_decision(
    attempt: int,
    limit: int | None = None,
    cause: str = "",
) -> RetryDecision:
    """Decide between a targeted repair and a stop.

    ``attempt`` is how many attempts have already failed (1 after the first
    failure); ``limit`` is the maximum allowed. When ``limit`` is omitted the
    budget's ``max_retries_per_worker`` is used, so retries are bounded by the
    same ceiling that already stops the run.

    The function is pure and compares two integers, so it cannot loop: a caller
    that keeps feeding it the same ``attempt`` gets the same stop, and the only
    way to reach a retry is to have a strictly smaller ``attempt`` than
    ``limit``. That is the property :func:`test_delegation` pins down.
    """
    if limit is None:
        limit = int(Limits().max_retries_per_worker)
    attempt = max(0, int(attempt))
    limit = int(limit)

    if limit <= 0:
        return RetryDecision(
            False,
            "Retries are disabled for this task, so the manager stops and "
            "escalates instead of looping.",
            code=CODE_RETRY_DISABLED,
            attempt=attempt,
            limit=max(0, limit),
        )
    if attempt >= limit:
        return RetryDecision(
            False,
            f"The retry limit of {limit} is reached, so the manager stops and "
            "escalates with the evidence instead of looping.",
            code=CODE_RETRY_EXHAUSTED,
            attempt=attempt,
            limit=limit,
        )
    return RetryDecision(
        True,
        f"Attempt {attempt} of {limit} failed, so the manager retries with "
        "targeted repair instructions rather than repeating the same action.",
        code=CODE_RETRY_WITH_REPAIR,
        attempt=attempt,
        limit=limit,
        instructions=_repair_instructions(cause),
    )


__all__ = [
    "DelegationSignals",
    "DelegationPolicy",
    "DelegationDecision",
    "benefit_score",
    "cost_score",
    "decide",
    "WorkerRecord",
    "dedupe_workers",
    "VerifiedResult",
    "ReuseDecision",
    "reuse_verified",
    "QCDecision",
    "should_skip_qc",
    "SummaryDecision",
    "skip_redundant_summary",
    "MandatoryCheckDecision",
    "mandatory_checks_required",
    "RetryDecision",
    "retry_decision",
]

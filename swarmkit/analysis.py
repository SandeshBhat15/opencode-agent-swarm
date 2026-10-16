"""Performance analysis and improvement proposals.

The analyzer answers one question: *where is the swarm actually losing?* Every
metric here is computed from recorded experiences, and every proposal must
name the metric it targets and the evidence behind it.

Three rules make this an analyzer rather than a dashboard that congratulates
itself:

**No denominator, no metric.** A rate is not reported until the sample is big
enough to mean something, and the threshold is a constant rather than a
tuning knob. Below it the metric is returned as ``None`` with a stated reason,
because a 100% success rate from two runs is noise dressed as performance.

**Counts are not achievement.** Agent count, code volume, test count and lesson
count appear nowhere in the scoring. A change that adds three subagents and one
lesson is not progress; it is cost.

**Impact is earned by baselines.** A proposal is only raised once the
category has a baseline. Improving against nothing is indistinguishable from
being bad.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import SwarmError
from .experience import Experience, ExperienceStore, redact

#: Below this many comparable records a rate is withheld. Chosen deliberately
#: high: three runs of the same task is an anecdote.
MIN_SAMPLE = 5

#: A repair that neither fixes the failure nor is retried differently is churn.
#: Surfacing this is the point - it is the signature of a self-reinforcing loop.
MIN_EXAMPLES_FOR_RECURRENCE = 3

#: Categories cheaper than this skip the whole analysis path.
COMPLEXITY_FOR_ANALYSIS = frozenset({"medium", "large", "epic"})

TARGETS = (
    "planning",
    "agent_selection",
    "scheduling",
    "prompt_construction",
    "model_selection",
    "verification",
    "recovery",
    "memory",
    "efficiency",
)

#: Improvements touching these require explicit human authorization. A learner
#: that can weaken the gates that judge it has no evidence left to lose.
PROTECTED_TARGETS = frozenset({"verification", "recovery"})


def _rate(hits: int, total: int) -> float | None:
    return round(hits / total, 4) if total else None


@dataclass
class Metrics:
    """Measured performance for one category, or a stated reason for absence."""

    category: str
    runs: int = 0
    first_attempt_success: float | None = None
    final_delivery: float | None = None
    qc_failure_rate: float | None = None
    repair_success_rate: float | None = None
    median_elapsed_s: float | None = None
    median_retries: float | None = None
    median_cost_usd: float | None = None
    repeated_failures: dict[str, int] = field(default_factory=dict)
    wrong_agent_selections: int = 0
    recoveries: int = 0
    duplicates_prevented: int = 0
    corrections: int = 0
    notes: list[str] = field(default_factory=list)

    def insufficient(self, reason: str) -> "Metrics":
        self.notes.append(reason)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "runs": self.runs,
            "first_attempt_success": self.first_attempt_success,
            "final_delivery": self.final_delivery,
            "qc_failure_rate": self.qc_failure_rate,
            "repair_success_rate": self.repair_success_rate,
            "median_elapsed_s": self.median_elapsed_s,
            "median_retries": self.median_retries,
            "median_cost_usd": self.median_cost_usd,
            "repeated_failures": self.repeated_failures,
            "wrong_agent_selections": self.wrong_agent_selections,
            "recoveries": self.recoveries,
            "duplicates_prevented": self.duplicates_prevented,
            "corrections": self.corrections,
            "notes": self.notes,
        }


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 2)
    return round((ordered[mid - 1] + ordered[mid]) / 2, 2)


def compute_metrics(experiences: list[Experience]) -> Metrics:
    """Aggregate one comparable set of records.

    "Comparable" means the caller already filtered by category or failure
    cause. Mixing a CSS tweak with a five-day migration produces averages that
    describe nothing, so this function never widens its own input.
    """
    if not experiences:
        return Metrics(category="none", runs=0).insufficient("no records")

    category = experiences[0].category or "unknown"
    metrics = Metrics(category=category, runs=len(experiences))
    sufficient = len(experiences) >= MIN_SAMPLE

    first_try = sum(1 for e in experiences
                    if e.outcome in ("delivered", "repaired") and e.retries == 0)
    metrics.final_delivery = _rate(sum(1 for e in experiences if e.verified),
                                   len(experiences))
    metrics.qc_failure_rate = _rate(sum(1 for e in experiences if e.failed_checks),
                                    len(experiences))
    metrics.wrong_agent_selections = sum(len(e.agents_selected_wrong) for e in experiences)
    metrics.recoveries = sum(e.workers_recovered for e in experiences)
    metrics.duplicates_prevented = sum(e.duplicates_prevented for e in experiences)
    metrics.corrections = sum(len(e.user_corrections) for e in experiences)

    causes: dict[str, int] = {}
    for e in experiences:
        if e.failure_cause and e.failure_cause != "none":
            causes[e.failure_cause] = causes.get(e.failure_cause, 0) + 1
    metrics.repeated_failures = {k: v for k, v in causes.items()
                                 if v >= MIN_EXAMPLES_FOR_RECURRENCE}

    repaired = [e for e in experiences
                if any(a.kind == "repair" for a in e.attempts)]
    if repaired:
        worked = sum(1 for e in repaired
                     if any(a.kind == "repair" and a.worked for a in e.attempts))
        metrics.repair_success_rate = _rate(worked, len(repaired))

    metrics.median_elapsed_s = _median([e.elapsed_s for e in experiences
                                        if e.elapsed_s is not None])
    metrics.median_retries = _median([float(e.retries) for e in experiences])
    costs = [e.cost_usd for e in experiences if e.cost_usd is not None]
    metrics.median_cost_usd = _median(costs) if costs else None

    if not sufficient:
        metrics.first_attempt_success = None
        metrics.insufficient(
            f"{len(experiences)} record(s) < MIN_SAMPLE={MIN_SAMPLE}: "
            "rates withheld to avoid reporting noise as performance")
    else:
        metrics.first_attempt_success = _rate(first_try, len(experiences))

    return metrics


@dataclass
class Proposal:
    """A proposed change to how the swarm works.

    Carries its own falsification. ``evaluation`` and ``rollback`` are not
    decoration: a proposal that cannot say how it would be measured or undone
    has not been thought through, and :func:`rank` refuses to score it.
    """

    id: str
    target: str
    problem: str
    evidence: list[str]
    change: str
    expected_benefit: str
    risks: list[str]
    evaluation: str
    rollback: str
    metrics: Metrics | None = None
    priority: float = 0.0
    tier: str = "candidate"
    created_at: str = ""

    def __post_init__(self) -> None:
        if self.target not in TARGETS:
            raise SwarmError(f"unknown improvement target {self.target!r}",
                             code="UNKNOWN_TARGET")
        if not self.evidence:
            # The single most important rule in this module. An unevidenced
            # proposal is a guess with a rollback plan, and guessing at your own
            # architecture is how a system degrades itself.
            raise SwarmError(f"proposal {self.id!r} has no evidence",
                             code="PROPOSAL_UNGROUNDED")
        if not self.evaluation or not self.rollback:
            raise SwarmError(f"proposal {self.id!r} needs an evaluation and "
                             "a rollback", code="PROPOSAL_INCOMPLETE")
        if not self.created_at:
            from .store import utcnow
            self.created_at = utcnow()

    @property
    def needs_authorization(self) -> bool:
        """True when this touches a protected target.

        Weakening verification or recovery is never learned automatically. A
        learner that can remove the gates judging it has no ground truth left.
        """
        return self.target in PROTECTED_TARGETS

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id, "target": self.target, "problem": self.problem,
            "evidence": self.evidence, "change": self.change,
            "expected_benefit": self.expected_benefit, "risks": self.risks,
            "evaluation": self.evaluation, "rollback": self.rollback,
            "priority": self.priority, "tier": self.tier,
            "created_at": self.created_at,
            "needs_authorization": self.needs_authorization,
        }
        if self.metrics is not None:
            data["metrics"] = self.metrics.to_dict()
        return data


def _regression_risk(target: str, metrics: Metrics) -> float:
    """Heuristic 0.0-1.0. Verification and recovery carry the most risk."""
    base = {"verification": 0.9, "recovery": 0.8, "scheduling": 0.5,
            "planning": 0.45, "agent_selection": 0.4, "memory": 0.35,
            "prompt_construction": 0.3, "model_selection": 0.25,
            "efficiency": 0.2}.get(target, 0.5)
    # High final delivery means there is something to break. A low baseline
    # means little to lose.
    delivery = metrics.final_delivery if metrics.final_delivery is not None else 0.0
    return round(min(1.0, base * (0.4 + 0.6 * delivery)), 3)


def _impact(metrics: Metrics) -> float:
    delivery = metrics.final_delivery
    qc_fail = metrics.qc_failure_rate
    if delivery is None or qc_fail is None:
        return 0.0
    # Headroom: how much is currently failing, plus how often QC catches it.
    return round(min(1.0, (1.0 - delivery) * 0.7 + qc_fail * 0.3), 3)


def _confidence(metrics: Metrics) -> float:
    if metrics.first_attempt_success is None:
        return 0.0
    return round(min(1.0, min(metrics.runs / (MIN_SAMPLE * 3), 1.0)), 3)


def rank(proposal: Proposal) -> Proposal:
    """Score by impact, confidence, cost and regression risk.

    Weighted so that a high-impact idea with thin evidence cannot outrank a
    moderate one that has been measured: confidence multiplies rather than
    adds, which means thin evidence halves a proposal instead of nudging it.
    """
    if proposal.metrics is None:
        proposal.priority = 0.0
        return proposal
    impact = _impact(proposal.metrics)
    confidence = _confidence(proposal.metrics)
    risk = _regression_risk(proposal.target, proposal.metrics)
    cost = 0.3 + 0.1 * len(proposal.change) / 500
    raw = (impact * (0.4 + 0.6 * confidence)) / (1.0 + risk + cost)
    proposal.priority = round(min(1.0, raw), 4)
    return proposal


def analyze(root: Path | None = None, *,
            category: str | None = None) -> dict[str, Any]:
    """Measure a category and raise proposals for whatever is measurably weak.

    Returns the metrics always, and proposals only where a metric is both
    present and bad. No evidence, no proposal.
    """
    store = ExperienceStore(root)
    experiences = store.search(category=category, limit=1000)
    metrics = compute_metrics(experiences)
    proposals: list[Proposal] = []

    def raise_proposal(**kwargs: Any) -> None:
        try:
            proposals.append(rank(Proposal(metrics=metrics, **kwargs)))
        except SwarmError:
            # A proposal that cannot meet the evidence bar is not reported as
            # a failure; it simply is not raised.
            pass

    if metrics.runs >= MIN_SAMPLE:
        if metrics.first_attempt_success is not None and metrics.first_attempt_success < 0.6:
            raise_proposal(
                id=f"first-attempt-{metrics.category}",
                target="planning",
                problem=(f"only {metrics.first_attempt_success:.0%} of runs succeed "
                         f"first try across {metrics.runs} {metrics.category} runs"),
                evidence=[f"first_attempt_success={metrics.first_attempt_success} "
                          f"over n={metrics.runs}",
                          f"median_retries={metrics.median_retries}"],
                change="Break objectives into smaller tasks with explicit acceptance "
                       "criteria at plan time, so a wrong assumption is caught in PLAN "
                       "rather than at QC.",
                expected_benefit="Higher first-attempt success, fewer repair cycles.",
                risks=["Smaller tasks can under-deliver on genuinely large work."],
                evaluation="Re-run the fixed benchmark and compare first-attempt "
                           "success and final delivery; reject if either drops.",
                rollback="Revert the planning policy to its previous version.",
            )

        if metrics.qc_failure_rate and metrics.qc_failure_rate > 0.4:
            raise_proposal(
                id=f"qc-failure-{metrics.category}",
                target="verification",
                problem=(f"{metrics.qc_failure_rate:.0%} of {metrics.category} runs "
                         "produced a failing check"),
                evidence=[f"qc_failure_rate={metrics.qc_failure_rate} over n={metrics.runs}"],
                change="Require a passing check per criterion at integration time, "
                       "not only at QC.",
                expected_benefit="Failures caught earlier, fewer full repair cycles.",
                risks=["Earlier checks cost time per iteration."],
                evaluation="Compare qc_failure_rate on the fixed benchmark.",
                rollback="Revert the verification policy version.",
            )

        for cause, count in metrics.repeated_failures.items():
            raise_proposal(
                id=f"recurring-{cause}-{metrics.category}",
                target="recovery" if cause in ("stale_worker", "duplicate_execution") else "scheduling",
                problem=f"{cause} recurred {count} times in {metrics.category}",
                evidence=[f"failure_cause={cause} in {count} of {metrics.runs} runs"],
                change=(f"Add a guard specific to {cause} and surface it in run state "
                        "so it is visible before it recurs."),
                expected_benefit="Fewer repeats of a known failure shape.",
                risks=["A guard that is too aggressive blocks legitimate work."],
                evaluation="Re-run the failure-injection cases and confirm they stop "
                           "failing while the benchmark is unaffected.",
                rollback="Remove the guard; it is additive and separately versioned.",
            )

        if metrics.wrong_agent_selections:
            raise_proposal(
                id=f"agent-selection-{metrics.category}",
                target="agent_selection",
                problem=(f"{metrics.wrong_agent_selections} recorded wrong-agent "
                         f"selection(s) in {metrics.category}"),
                evidence=[f"wrong_agent_selections={metrics.wrong_agent_selections} "
                          f"over n={metrics.runs}"],
                change="Derive the agent from the objective's verbs and deliverable "
                       "kind rather than from free choice.",
                expected_benefit="Fewer mismatched specialists.",
                risks=["Verb heuristics can misread an unusual request."],
                evaluation="Compare wrong_agent_selections on the benchmark.",
                rollback="Revert routing to its previous version.",
            )

    return {
        "metrics": metrics.to_dict(),
        "proposals": [p.to_dict() for p in proposals],
        "sample_sufficient": metrics.runs >= MIN_SAMPLE,
        "min_sample": MIN_SAMPLE,
    }


def should_analyze(experiences: list[Experience], *,
                   cooldown_s: float = 0.0,
                   last_analysis_at: str | None = None) -> tuple[bool, str]:
    """Decide whether now is a reasonable moment to analyze.

    Refuses after every trivial task by requiring either a meaningful failure,
    a recurring cause, or an accumulated backlog. This is what stops the engine
    spending more on self-reflection than on the user's work.
    """
    meaningful = [e for e in experiences if e.outcome != "delivered"
                  or e.retries or e.workers_recovered]
    if not meaningful:
        return False, "nothing interesting yet: no failures, retries or recoveries"
    recurring: dict[str, int] = {}
    for e in meaningful:
        if e.failure_cause and e.failure_cause != "none":
            recurring[e.failure_cause] = recurring.get(e.failure_cause, 0) + 1
    if any(v >= MIN_EXAMPLES_FOR_RECURRENCE for v in recurring.values()):
        return True, f"recurring failure: {max(recurring, key=recurring.get)}"
    if len(meaningful) >= MIN_SAMPLE:
        return True, f"backlog of {len(meaningful)} meaningful outcomes"
    return False, (f"{len(meaningful)} meaningful outcome(s) below threshold; "
                   "trivial tasks do not trigger analysis")


def describe_schema() -> dict[str, Any]:
    """Machine-readable schema, used by docs and the CLI."""
    from .experience import FAILURE_CAUSES as causes
    from .experience import OUTCOMES as outcomes
    from .experience import TIERS as tiers

    return {
        "experience_fields": sorted(Experience.__dataclass_fields__),
        "derived": ["verified", "confidence", "tier", "search_keys"],
        "tiers": list(tiers),
        "outcomes": list(outcomes),
        "failure_causes": list(causes),
        "improvement_targets": list(TARGETS),
        "protected_targets": sorted(PROTECTED_TARGETS),
        "min_sample": MIN_SAMPLE,
        "min_examples_for_recurrence": MIN_EXAMPLES_FOR_RECURRENCE,
    }


def redact_all(values: list[str]) -> list[str]:
    return [redact(v) for v in values]
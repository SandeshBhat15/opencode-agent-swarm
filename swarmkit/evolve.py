"""Experiments, promotion policy, versioning and rollback.

This is the part of the engine that can hurt, so it is built to be boring.
A candidate change lives in an isolated version record, is evaluated against
a fixed benchmark, and is promoted only when it passes both the quality bar and
the safety bar. Nothing here edits a live agent page, a run directory, or the
active configuration.

Four invariants hold no matter what a proposal claims:

**Isolation.** A candidate is data in ``versions/``. Applying one is an
explicit operator action, never a side effect of analysis.

**Baseline first.** :func:`evaluate` refuses a candidate with no baseline.
A comparison against nothing is indistinguishable from being bad.

**Multi-metric rejection.** A candidate that wins one metric while degrading
delivery or safety is rejected, not celebrated. Speeding a failing pipeline is
not an improvement.

**Bounded recursion.** ``max_generations`` and a cooldown stop an engine from
improving at the improvement engine's expense.
"""

from __future__ import annotations

import shutil
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .analysis import PROTECTED_TARGETS, Proposal
from .errors import SwarmError
from .experience import Memory, redact
from .store import (LOCK_STALE_SECONDS, LOCK_WAIT_SECONDS, atomic_write_json,
                    file_lock, read_json, utcnow)

EVOLUTION_ROOT = Path(".opencode") / "swarm-evolution"
VERSIONS_DIR = "versions"
HISTORY_FILE = "history.json"
LOCK_FILE = ".evolve.lock"
LOCK_STALE_SECONDS = 900

ACTIVE_POINTER = "active"

#: A candidate must beat the baseline by this much to be worth its risk.
DEFAULT_MIN_GAIN = 0.02

#: Metrics that may not regress by more than this at promotion time.
DEFAULT_REGRESSION_TOLERANCE = 0.0

#: Terminal states for a version record.
STATES = ("candidate", "evaluating", "promoted", "rejected", "superseded", "rolled_back")


def _protected_kind(kind: str) -> bool:
    """Protected *kinds* mirror the protected improvement targets.

    A version's ``kind`` is a caller-supplied string, so it is normalized here
    rather than trusted: ``Verification`` must not slip past a check that
    compares against ``verification``.
    """
    return str(kind).strip().lower() in PROTECTED_TARGETS

REQUIRED_GATES = ("benchmark", "safety", "authorization")


# --- versioning -----------------------------------------------------------


@dataclass
class Gate:
    """One check a candidate must pass."""

    name: str
    passed: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Version:
    """One version of something the swarm can evolve.

    ``kind`` is open on purpose: agent instruction pages, planning policies and
    routing strategies are the obvious cases, and the same promotion rules apply
    to each. Nothing in this module assumes markdown.
    """

    id: str
    kind: str
    proposal_id: str
    parent: str
    label: str
    state: str = "candidate"
    generation: int = 0
    change: str = ""
    rationale: str = ""
    rollback_to: str = ""
    gates: list[Gate] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    baseline_metrics: dict[str, Any] = field(default_factory=dict)
    score: float | None = None
    baseline_score: float | None = None
    rejection: str = ""
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise SwarmError(f"unknown version state {self.state!r}",
                             code="BAD_VERSION_STATE")

    @property
    def is_active(self) -> bool:
        return self.state == "promoted"

    def gate(self, name: str) -> Gate | None:
        for gate in self.gates:
            if gate.name == name:
                return gate
        return None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["gates"] = [g.to_dict() for g in self.gates]
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Version":
        known = set(cls.__dataclass_fields__)
        data = {k: v for k, v in raw.items() if k in known}
        data["gates"] = [Gate(**g) for g in raw.get("gates", [])]
        return cls(**data)


# --- storage --------------------------------------------------------------


class VersionStore:
    """File-backed version registry.

    One JSON file per version plus an ``active`` pointer, so a candidate can be
    inspected, diffed and reverted by hand without this code running.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else EVOLUTION_ROOT
        self.versions = self.root / VERSIONS_DIR

    def _ensure(self) -> None:
        self.versions.mkdir(parents=True, exist_ok=True)
        guard = self.root / ".gitignore"
        if not guard.exists():
            guard.write_text("*\n", encoding="utf-8")

    def _path(self, version_id: str) -> Path:
        safe = _safe_id(version_id)
        return self.versions / f"{safe}.json"

    def save(self, version: Version) -> dict[str, Any]:
        self._ensure()
        version.updated_at = utcnow()
        atomic_write_json(self._path(version.id), version.to_dict())
        self._log({"event": "saved", "version": version.id, "state": version.state})
        return version.to_dict()

    def load(self, version_id: str) -> Version | None:
        data = read_json(self._path(version_id), default=None)
        return Version.from_dict(data) if data else None

    def all(self) -> list[Version]:
        if not self.versions.exists():
            return []
        out = []
        for path in sorted(self.versions.glob("*.json")):
            data = read_json(path, default=None)
            if data:
                out.append(Version.from_dict(data))
        return out

    def active(self, kind: str | None = None) -> Version | None:
        pointer = read_json(self.root / f"{ACTIVE_POINTER}.json", default={}) or {}
        candidates = [v for v in self.all() if v.state == "promoted"]
        if kind:
            candidates = [v for v in candidates if v.kind == kind]
        if candidates:
            return sorted(candidates, key=lambda v: v.updated_at)[-1]
        return None

    def history(self) -> list[dict[str, Any]]:
        return read_json(self.root / HISTORY_FILE, default=[]) or []

    def _log(self, entry: dict[str, Any]) -> None:
        entry = {"at": utcnow(), **entry}
        events = self.history()
        events.append(entry)
        # Bounded. An unbounded audit log is itself an unbounded context cost.
        atomic_write_json(self.root / HISTORY_FILE, events[-500:])

    def generations(self) -> int:
        """Depth of the *promoted* chain - not a count of everything tried.

        Only promoted versions contribute. This is deliberate: ``max_generations``
        exists to stop an engine auto-promoting itself arbitrarily deep, and a
        rejected experiment is not a step toward that. A cheap candidate that
        loses its benchmark should cost almost nothing.

        The corollary is that rejected work does not advance this counter, so it
        is bounded separately by ``max_candidates`` in :func:`create_candidate`.
        Two limits, two different failure modes.
        """
        promoted = [v for v in self.all() if v.state == "promoted"]
        return max((v.generation for v in promoted), default=0)

    def candidate_count(self) -> int:
        """Every version record on disk, whatever state it ended in."""
        return len(self.all())

    def rejected_rules(self) -> set[str]:
        """Proposals that LOST their benchmark, so they are not retried blindly.

        Deliberately narrower than "every version in state rejected" - see
        ``PERMANENT_REJECTIONS``. A refused safety gate or a missing
        authorization leaves the version in ``rejected`` state for the record,
        but does not blacklist the proposal, because both conditions are
        fixable and must stay re-evaluable.
        """
        out: set[str] = set()
        for version in self.all():
            if version.state != "rejected":
                continue
            reason = (version.rejection or "").split(":", 1)[0].strip()
            if reason in PERMANENT_REJECTIONS:
                out.add(version.proposal_id)
        return out

#: bad". A safety or authorization refusal describes the attempt, not the
#: proposal: the candidate has not been made safe *yet*, which is a different
#: statement from "it must never be evaluated again". Folding the two together
#: means one flaky sandbox permanently retires a good proposal, and because
#: `evaluate` refuses a previously-rejected proposal outright there is then no
#: way back except hand-editing the version directory.
PERMANENT_REJECTIONS = frozenset({
    "NO_BASELINE",
    "NO_MEASURED_GAIN",
    "REGRESSION",
    "NOT_COMPARABLE",
    "GAIN_TOO_SMALL",
})


@contextmanager
def version_lock(store: VersionStore) -> Any:
    """Serialize version reads and writes.

    Promotion is read-compare-write: it reads the active pointer, decides, then
    writes the pointer. Two candidates evaluated at once can both read the same
    previous and both decide they are the successor. The lock makes promotion a
    single critical section. ``evolve evaluate`` deliberately does NOT take it -
    an operator holding the lock while waiting for benchmark numbers would wedge
    every other promotion behind a long-running test.
    """
    with file_lock(store.root / LOCK_FILE, timeout=LOCK_WAIT_SECONDS,
                   stale_after=LOCK_STALE_SECONDS):
        yield


def version_at(store: VersionStore) -> dict[str, Any] | None:
    """The active version's recorded metrics, for use as a baseline."""
    active = store.active()
    return dict(active.metrics) if active else None

def _safe_id(value: str) -> str:
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in str(value))
    cleaned = cleaned.strip(".-") or "version"
    return cleaned[:80]


# --- candidate creation ---------------------------------------------------


def create_candidate(proposal: Proposal, store: VersionStore, *,
                     kind: str = "policy", label: str = "",
                     max_generations: int = 5,
                     max_candidates: int = 50) -> dict[str, Any]:
    """Register a proposal as an isolated candidate.

    Refuses to re-register a proposal that was already rejected, unless the
    caller passes ``force`` with a reason. Re-running a refused experiment
    unchanged is how an engine burns its budget proving the same thing twice.

    Two independent budgets apply, because they bound different failures:

    ``max_generations``
        How deep the *promoted* chain may go. Promotions are the expensive,
        irreversible kind of progress, so this is the tight budget.

    ``max_candidates``
        How many version records may exist at all. Rejected candidates never
        advance the generation counter, so without this an engine stuck in a
        reject loop - proposing slightly reworded versions of an idea that has
        already lost three times - would write records forever and never trip
        a limit. Cheap is fine; unbounded is not.
    """
    if proposal.id in store.rejected_rules() and not label:
        raise SwarmError(
            f"proposal {proposal.id!r} was already rejected; "
            "change it or pass an explicit new label",
            code="PROPOSAL_PREVIOUSLY_REJECTED")
    existing = store.candidate_count()
    if existing >= max_candidates:
        raise SwarmError(
            f"{existing} candidate version(s) already on disk; "
            f"max_candidates={max_candidates}. Prune the rejected ones or raise "
            "the budget deliberately - an engine that keeps generating ideas "
            "which lose their benchmark is not learning, it is churning",
            code="EVOLUTION_BUDGET_EXHAUSTED")
    generation = store.generations() + 1
    if generation > max_generations:
        # Bounded recursion. An engine that keeps evolving itself has stopped
        # serving the tasks it exists to complete.
        raise SwarmError(
            f"generation {generation} exceeds max_generations={max_generations}; "
            "stop evolving and review the active version by hand",
            code="EVOLUTION_BUDGET_EXHAUSTED")
    parent = store.active(kind)
    version = Version(
        id=f"{proposal.id}-{generation}",
        kind=kind,
        proposal_id=proposal.id,
        parent=parent.id if parent else "",
        label=label or proposal.id,
        state="candidate",
        generation=generation,
        change=proposal.change,
        rationale=proposal.rationale if hasattr(proposal, "rationale")
        else proposal.problem,
        rollback_to=parent.id if parent else "",
        metrics=dict(proposal.to_dict().get("metrics") or {}),
    )
    return store.save(version)


# --- evaluation -----------------------------------------------------------


#: Metrics a candidate may not regress, and the direction that counts as better.
GUARDED_METRICS = {
    "final_delivery": 1,          # higher is better
    "first_attempt_success": 1,
    "repair_success_rate": 1,
    "qc_failure_rate": -1,        # lower is better
    "median_elapsed_s": -1,       # lower is better
    "median_cost_usd": -1,
}


def composite_score(metrics: dict[str, Any]) -> float | None:
    """A single comparable number, or None when there is nothing to compare.

    Only guarded metrics contribute. Unmeasured improvement is not improvement,
    so a candidate that adds a mechanism nobody measured scores exactly zero.

    The denominator is the weight of the metrics actually MEASURED, not every
    weight there is. Averaging against the full set would silently shrink the
    score of a run that measured less, which is precisely backwards: a thin
    sample should not look better than a thick one.
    """
    weights = {"final_delivery": 3.0, "first_attempt_success": 2.0,
               "repair_success_rate": 1.5, "qc_failure_rate": 1.5,
               "median_elapsed_s": 0.5, "median_cost_usd": 0.5}
    total = 0.0
    weight_used = 0.0
    for name, direction in GUARDED_METRICS.items():
        value = metrics.get(name)
        if value is None:
            continue
        weight = weights.get(name, 1.0)
        total += weight * float(value) * direction
        weight_used += weight
    if weight_used == 0.0:
        return None
    return round(total / weight_used, 4)


def compare(candidate: dict[str, Any], baseline: dict[str, Any],
            *, min_gain: float = DEFAULT_MIN_GAIN,
            tolerance: float = DEFAULT_REGRESSION_TOLERANCE) -> dict[str, Any]:
    """Compare candidate metrics against a baseline on every guarded metric.

    Returns a verdict plus the per-metric deltas, so a rejection can be
    explained rather than merely asserted.
    """
    if not baseline:
        return {"accepted": False,
                "reason": "NO_BASELINE",
                "detail": "improvement cannot be established without a baseline"}
    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []

    for name, direction in GUARDED_METRICS.items():
        new = candidate.get(name)
        old = baseline.get(name)
        if new is None or old is None:
            continue
        delta = (new - old) * direction
        record = {"metric": name, "baseline": old, "candidate": new,
                  "delta": round(delta, 4)}
        (improvements if delta > 0 else regressions if delta < 0 else []).append(record)

    hard = [r for r in regressions if r["delta"] < -tolerance]
    new_score = composite_score(candidate)
    old_score = composite_score(baseline)

    if not improvements:
        return {"accepted": False, "reason": "NO_MEASURED_GAIN",
                "detail": "no guarded metric improved",
                "regressions": regressions, "improvements": improvements}
    if hard:
        names = ", ".join(r["metric"] for r in hard)
        return {"accepted": False, "reason": "REGRESSION",
                "detail": f"regressed beyond tolerance: {names}",
                "regressions": regressions, "improvements": improvements}
    if new_score is None or old_score is None:
        return {"accepted": False, "reason": "NOT_COMPARABLE",
                "detail": "metrics present but not comparable",
                "regressions": regressions, "improvements": improvements}
    gain = new_score - old_score
    if gain < min_gain:
        return {"accepted": False, "reason": "GAIN_TOO_SMALL",
                "detail": f"gain {gain:.4f} < min_gain {min_gain}",
                "regressions": regressions, "improvements": improvements}

    return {"accepted": True, "reason": "", "gain": round(gain, 4),
            "score": new_score, "baseline_score": old_score,
            "regressions": regressions, "improvements": improvements}


def evaluate(proposal_id: str, version_id: str,
             candidate_metrics: dict[str, Any],
             baseline_metrics: dict[str, Any],
             store: VersionStore, *,
             safety_passed: bool = False,
             authorized: bool = False,
             min_gain: float = DEFAULT_MIN_GAIN) -> dict[str, Any]:
    """Run the full promotion decision for one candidate.

    Gate order matters. Safety is checked before benefit, so a candidate that
    improves throughput while failing a safety check is rejected as unsafe,
    not promoted with a caveat.
    """
    version = store.load(version_id)
    if version is None:
        raise SwarmError(f"no such version {version_id!r}", code="NO_SUCH_VERSION")

    if proposal_id in store.rejected_rules():
        return {"promoted": False, "reason": "PREVIOUSLY_REJECTED",
                "detail": f"proposal {proposal_id!r} already lost its benchmark",
                "version": version_id}

    # -- safety gate ----------------------------------------------------
    if not safety_passed:
        return _reject(version, store, "SAFETY_GATE_FAILED",
                       "candidate did not pass the safety gate")
    version.gates.append(Gate("safety", True, "safety checks reported clean"))

    # -- benchmark gate --------------------------------------------------
    with version_lock(store):
        verdict = compare(candidate_metrics, baseline_metrics, min_gain=min_gain)
        version.gates.append(Gate("benchmark", verdict["accepted"],
                                  verdict.get("detail") or f"gain {verdict.get('gain')}"))
        version.metrics = candidate_metrics
        version.baseline_metrics = baseline_metrics
        version.score = composite_score(candidate_metrics)
        version.baseline_score = composite_score(baseline_metrics)

        if not verdict["accepted"]:
            detail = verdict.get("detail", "")
            if verdict.get("regressions"):
                detail += " | " + "; ".join(
                    f"{r['metric']} {r['baseline']}->{r['candidate']}"
                    for r in verdict["regressions"])
            return _reject(version, store, verdict["reason"], detail, verdict=verdict)

        # -- authorization gate -------------------------------------------
        # Verification and recovery targets never auto-promote, whatever the
        # policy says. That refusal is structural: a learner that can weaken the
        # components judging it has no ground truth left to improve against.
        protected = _protected_kind(version.kind)
        if protected and not authorized:
            return _reject(version, store, "AUTHORIZATION_REQUIRED",
                           f"{version.kind} is protected; a human must authorize this change")
        version.gates.append(Gate(
            "authorization", True,
            "explicit authorization supplied" if protected else "not required"))

        version.state = "promoted"
        previous = store.active(version.kind)
        if previous is not None and previous.id != version.id:
            previous.state = "superseded"
            store.save(previous)
        atomic_write_json(store.root / f"{ACTIVE_POINTER}.json",
                         {"kind": version.kind, "version": version.id})
    store.save(version)
    store._log({"event": "promoted", "version": version.id,
                "gain": verdict.get("gain"), "proposal": proposal_id})
    return {"promoted": True, "version": version_id, "gain": verdict.get("gain"),
            "score": version.score, "gates": [g.to_dict() for g in version.gates]}


def _reject(version: Version, store: VersionStore, reason: str, detail: str,
            verdict: dict[str, Any] | None = None) -> dict[str, Any]:
    """Record a refusal.

    Only a *permanent* refusal - losing the benchmark, regressing a guarded
    metric, having no baseline - is terminal. A safety or authorization
    refusal leaves the version as a candidate, because both are fixable: the
    suite passes on the next run, and a human authorizes the next proposal.

    Marking those terminal is what made a single flaky sandbox end the
    experiment permanently. Narrowing the proposal blacklist was not enough on
    its own: the version itself also went terminal, so even a proposal that was
    still eligible had nothing left to be evaluated as. The record keeps the
    refusal for audit, and the event is logged as ``refused`` rather than
    ``rejected`` so the trail does not imply a verdict that was never reached.
    """
    permanent = reason in PERMANENT_REJECTIONS
    version.rejection = f"{reason}: {detail}"
    version.state = "rejected" if permanent else "candidate"
    store.save(version)
    store._log({"event": "rejected" if permanent else "refused",
                "version": version.id, "reason": reason, "detail": detail,
                "retryable": not permanent})
    return {"promoted": False, "reason": reason, "detail": detail,
            "version": version.id, "retryable": not permanent,
            "gates": [g.to_dict() for g in version.gates],
            "comparison": verdict}


# --- rollback -------------------------------------------------------------


def rollback(version_id: str, store: VersionStore, *, reason: str = "") -> dict[str, Any]:
    """Return the active pointer to a previous version.

    Rollback is always available and never needs a benchmark run: when
    something is actively harmful, waiting for evidence is the wrong default.
    """
    version = store.load(version_id)
    if version is None:
        raise SwarmError(f"no such version {version_id!r}", code="NO_SUCH_VERSION")
    if version.state != "promoted":
        raise SwarmError(f"version {version_id!r} is {version.state}, not promoted",
                         code="NOT_PROMOTED")
    previous = store.load(version.rollback_to) if version.rollback_to else None
    if previous is None:
        raise SwarmError("no previous version to roll back to",
                         code="NO_ROLLBACK_TARGET")

    previous.state = "promoted"
    version.state = "rolled_back"
    store.save(previous)
    store.save(version)
    atomic_write_json(store.root / f"{ACTIVE_POINTER}.json",
                     {"kind": previous.kind, "version": previous.id})
    store._log({"event": "rolled_back", "version": version.id,
                "restored": previous.id, "reason": redact(reason)})
    return {"rolled_back": version.id, "restored": previous.id,
            "reason": redact(reason)}


# --- regression monitoring ------------------------------------------------

#: Post-promotion drop that triggers an automatic rollback, as a POSITIVE
#: magnitude. Direction comes from :data:`GUARDED_METRICS`, so a metric that
#: is better when smaller needs no sign juggling here.
REGRESSION_TRIGGERS = {
    "final_delivery": 0.10,          # 10 points of delivery is a real drop
    "qc_failure_rate": 0.15,
    "median_elapsed_s": 0.50,
}


def monitor(version_id: str, observed: dict[str, Any],
            store: VersionStore) -> dict[str, Any]:
    """Compare post-promotion results against what was actually promoted.

    A promotion is a bet. This is how the bet is called: if the metrics move
    the wrong way past a threshold, the change is withdrawn rather than
    defended.

    The comparison is against ``version.metrics`` - what the candidate
    *promised* - not against ``baseline_metrics``, what it beat. Comparing to
    the baseline makes this gate structurally unable to fire for the change it
    exists to police: a candidate promoted at 0.90 that then collapses to 0.20
    still beats the 0.10 baseline, so the regression reads as health.
    """
    version = store.load(version_id)
    if version is None:
        raise SwarmError(f"no such version {version_id!r}", code="NO_SUCH_VERSION")
    promised = dict(version.metrics or {})
    if not promised:
        # Report the absence rather than a clean bill of health. "Nothing
        # observed" and "nothing changed" look identical from outside.
        return {"ok": True, "reason": "no promoted metrics recorded; "
                                     "nothing to monitor against",
                "breaches": []}

    breaches = []
    for name, threshold in REGRESSION_TRIGGERS.items():
        before = promised.get(name)
        now = observed.get(name)
        if before is None or now is None:
            continue
        # Normalise so a positive `worse_by` always means "worse than at
        # promotion", whatever the metric's natural direction happens to be.
        direction = GUARDED_METRICS.get(name, 1)
        worse_by = (before - now) * direction
        if worse_by > abs(threshold):
            breaches.append({"metric": name, "promised": before, "observed": now,
                             "worse_by": round(worse_by, 4),
                             "threshold": abs(threshold)})

    if not breaches:
        return {"ok": True, "breaches": [], "version": version_id}

    result: dict[str, Any] = {"ok": False, "breaches": breaches,
                              "version": version_id}
    if version.rollback_to:
        try:
            result["rollback"] = rollback(version_id, store,
                                          reason="regression threshold crossed")
        except SwarmError as exc:
            result["rollback_error"] = str(exc)
    return result


# --- code self-evolution --------------------------------------------------


@dataclass
class CodeChange:
    """A proposed change to the swarm's own source.

    Separated from :class:`Version` because code carries a different risk: it
    can change behaviour rather than only behaviour-on-a-task, so it demands a
    reproduction and a regression test before it is even measurable.
    """

    id: str
    problem: str
    reproduction: str
    file_scope: list[str]
    fix: str
    regression_test: str = ""
    worktree: str = ""
    branch: str = ""
    status: str = "proposed"
    notes: list[str] = field(default_factory=list)

    VALID_STATUS = ("proposed", "reproduced", "in_worktree", "implemented",
                    "tested", "reviewed", "promoted", "rejected")

    def __post_init__(self) -> None:
        if self.status not in self.VALID_STATUS:
            raise SwarmError(f"bad code-change status {self.status!r}",
                             code="BAD_CODE_CHANGE_STATUS")
        if not self.reproduction:
            # Cannot fix what cannot be reproduced. This is the single most
            # common cause of speculative refactors.
            raise SwarmError(f"code change {self.id!r} has no reproduction",
                             code="CHANGE_UNREPRODUCIBLE")
        if not self.file_scope:
            raise SwarmError(f"code change {self.id!r} has no file scope",
                             code="CHANGE_UNSCOPED")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: Paths an experiment may never write, whatever a proposal says. These are the
#: components that judge the experiment itself; a learner that can edit them
#: can grade its own homework.
PROTECTED_PATHS = (
    ".git",
    "swarmkit/gate.py",           # the delivery gate
    "swarmkit/budget.py",         # budget enforcement
    "swarmkit/store.py",          # state integrity and locks
    "swarmkit/lessons.py",        # the ledger
    "tests/test_security.py",
    "agents/swarm/CHANGELOG.md",
)


def assert_scope_allowed(paths: list[str]) -> list[str]:
    """Refuse any scope that touches a protected or in-place target.

    Experiments run in a worktree. A change aimed at the active tree is not an
    experiment, it is an edit to production, and it is refused.
    """
    violations = []
    for raw in paths:
        normalised = str(raw).replace("\\", "/").lstrip("./")
        for protected in PROTECTED_PATHS:
            if normalised == protected or normalised.endswith("/" + protected) \
                    or normalised.startswith(protected):
                violations.append(f"{raw} touches protected {protected}")
    if violations:
        raise SwarmError("; ".join(violations), code="PROTECTED_SCOPE_CHANGE")
    return paths


def isolated_workspace(repo: Path, name: str) -> Path:
    """Create an isolated experiment directory beside the run dirs.

    A plain directory rather than a git worktree: the engine must work in a
    repo that is not under git at all, and a worktree would fail there. Git
    isolation remains available through the existing ``iso`` commands.
    """
    if not name or name in (".", ".."):
        raise SwarmError("experiment name must be a real name",
                         code="BAD_WORKSTREAM_NAME")
    safe = _safe_id(name)
    path = repo / ".opencode" / "swarm-experiments" / safe
    path.mkdir(parents=True, exist_ok=True)
    (path / ".gitignore").write_text("*\n", encoding="utf-8")
    return path


def copy_baseline(repo: Path, workspace: Path) -> Path:
    """Copy the tree under experiment so the original is never edited in place."""
    import os

    def ignore(_dir: str, names: list[str]) -> set[str]:
        skip = {".git", "__pycache__", ".venv", "node_modules", ".opencode"}
        return {n for n in names if n in skip}

    dest = workspace / "baseline"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(repo, dest, ignore=ignore, symlinks=False)
    return dest


# --- orchestration --------------------------------------------------------


@dataclass
class Policy:
    """Operator-tunable limits for the improvement loop itself."""

    min_new_experiences: int = 5
    cooldown_s: float = 300.0
    max_candidates: int = 3
    max_generations: int = 5
    min_gain: float = DEFAULT_MIN_GAIN
    auto_promote: bool = False
    auto_promote_targets: tuple[str, ...] = ()

    def may_auto_promote(self, target: str) -> bool:
        if not self.auto_promote:
            return False
        if target in PROTECTED_TARGETS:
            # Structural refusal, not a default. Enabling auto_promotion does
            # not unlock the gates.
            return False
        return target in self.auto_promote_targets

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_policy(root: Path | None = None) -> Policy:
    data = read_json((Path(root) if root else EVOLUTION_ROOT) / "policy.json",
                     default={}) or {}
    known = set(Policy.__dataclass_fields__)
    return Policy(**{k: v for k, v in data.items() if k in known})


def save_policy(policy: Policy, root: Path | None = None) -> dict[str, Any]:
    target = Path(root) if root else EVOLUTION_ROOT
    atomic_write_json(target / "policy.json", policy.to_dict())
    return policy.to_dict()


def memory_bridge(root: Path, ledger_path: Path | None = None) -> dict[str, Any]:
    """Connect experience memory to the existing lessons ledger.

    Experiences propose; the ledger disposes. This does NOT write lessons
    directly - promotion still goes through ``lessons.promote`` so a rule can
    never enter the ledger without ``--tests-passed``.
    """
    memory = Memory(root, ledger_path)
    return {
        "tiers": {tier: len(items) for tier, items in memory.tiers().items()},
        "lessons": memory.lessons(),
        "hypotheses": memory.hypotheses(),
        "challenges": memory.challenges(),
    }
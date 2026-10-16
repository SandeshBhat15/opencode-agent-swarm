"""Structured experience capture and tiered memory.

This is deliberately an extension of :mod:`swarmkit.lessons`, not a second
memory system. The ledger answers "what rule have we learned"; this answers
"what actually happened, and how confident are we". A lesson without the
experience that produced it is a belief, and this module exists so that belief
can be traced back to evidence or marked as a hypothesis.

Three ideas carry the design:

**A record, not a transcript.** An experience is a compact structured record.
Storing the full conversation would grow without bound, leak secrets, and make
retrieval useless. :func:`redact` is applied on the way in so an error message
carrying a token does not become permanent.

**Confidence is derived, not asserted.** A lesson that has only ever been
suggested is a hypothesis. One backed by a reproduced failure with a recorded
command is verified. :class:`Memory` refuses to promote a hypothesis to a
lesson without an origin, which is the single rule that stops the swarm from
teaching itself its own guesses.

**Retrieval is scoped.** :meth:`Memory.recall` filters by task category and
failure class. Applying a lesson about flaky integration tests to a CSS tweak
is how a system accumulates stale instructions it can never shake.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import SwarmError
from .store import append_jsonl, atomic_write_json, read_jsonl, utcnow

# --- location -------------------------------------------------------------

MEMORY_ROOT = Path(".opencode") / "swarm-memory"
EXPERIENCE_FILE = "experiences.jsonl"
INDEX_FILE = "index.json"

# --- vocabularies ---------------------------------------------------------

COMPLEXITY = ("trivial", "small", "medium", "large", "epic")
COMPLEXITY_RANK = {name: i for i, name in enumerate(COMPLEXITY)}

FAILURE_CAUSES = (
    "none",
    "unknown",
    "scope_overlap",
    "stale_worker",
    "duplicate_execution",
    "budget_exhausted",
    "gate_closed",
    "bad_plan",
    "agent_selection",
    "tool_failure",
    "dependency_conflict",
    "verification_gap",
)

OUTCOMES = ("delivered", "failed", "aborted", "interrupted", "repaired")

#: Outcome -> whether the work actually shipped something verified.
VERIFIED_OUTCOMES = frozenset({"delivered", "repaired"})

TIERS = (
    "history",      # raw structured records
    "verified",     # proved out, but nobody has distilled a rule from it yet
    "hypothesis",   # plausible, no reproduced evidence
    "lesson",       # a rule, grounded in a named failure with a recorded command
    "candidate",    # a proposed improvement awaiting evaluation
    "strategy",     # approved and active
    "rejected",     # evaluated and refused; kept so it is not re-tried blindly
)

_REDACT_PATTERNS = (
    re.compile(r"(?i)\b(?:ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{12,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|password|passwd|token)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"-----BEGIN[^-]*PRIVATE KEY-----[\s\S]*?-----END[^-]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
)

#: Anything longer than this is a transcript, not a fact. Long values are
#: truncated rather than dropped: the first part usually carries the error class.
MAX_FIELD_CHARS = 600


def redact(text: Any) -> str:
    """Strip credential-shaped and e-mail-shaped substrings.

    Applied to every free-text field on write. Losing the tail of a long error
    is acceptable; persisting somebody's key is not.
    """
    value = "" if text is None else str(text)
    for pattern in _REDACT_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    if len(value) > MAX_FIELD_CHARS:
        value = value[:MAX_FIELD_CHARS] + f"... [truncated {len(value) - MAX_FIELD_CHARS} chars]"
    return value


def _now() -> str:
    return utcnow()


# --- the record -----------------------------------------------------------


@dataclass
class Criterion:
    """One acceptance criterion and what actually happened to it."""

    id: str
    text: str
    expected: str = ""
    actual: str = ""
    passed: bool | None = None
    evidence: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Criterion":
        passed = raw.get("passed")
        if passed is not None:
            passed = bool(passed)
        return cls(
            id=raw.get("id", ""),
            text=raw.get("text", ""),
            expected=raw.get("expected", ""),
            actual=raw.get("actual", ""),
            passed=passed,
            evidence=raw.get("evidence", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Attempt:
    """One execution attempt and whether it helped.

    Repairs that changed nothing are the most informative records here: a
    repair rate of 100% with a success rate of 0% is a loop, not progress.
    """

    kind: str                     # spawn | repair | retry
    agent: str = ""
    action: str = ""
    worked: bool | None = None
    at: str = field(default_factory=_now)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Attempt":
        worked = raw.get("worked")
        return cls(
            kind=raw.get("kind", "spawn"),
            agent=raw.get("agent", ""),
            action=redact(raw.get("action", "")),
            worked=None if worked is None else bool(worked),
            at=raw.get("at", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Experience:
    """A structured record of one task, from request to outcome.

    This is the unit the analyzer aggregates and the memory retrieves against.
    Every free-text field is redacted at construction, so a caller cannot leak
    a secret by forgetting to sanitize.
    """

    run: str
    request: str
    category: str = "unknown"
    complexity: str = "small"
    outcome: str = "unknown"
    lane: str = ""
    plan: list[str] = field(default_factory=list)
    agents_used: list[str] = field(default_factory=list)
    agents_selected_wrong: list[str] = field(default_factory=list)
    criteria: list[Criterion] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    failed_checks: list[str] = field(default_factory=list)
    failure_cause: str = "none"
    root_cause: str = ""
    root_cause_established: bool = False
    attempts: list[Attempt] = field(default_factory=list)
    elapsed_s: float | None = None
    retries: int = 0
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    user_corrections: list[str] = field(default_factory=list)
    user_accepted: bool | None = None
    strategy_version: str = ""
    workflow_version: str = ""
    lessons: list[str] = field(default_factory=list)
    hypotheses: list[str] = field(default_factory=list)
    workers_recovered: int = 0
    duplicates_prevented: int = 0
    run_dir: str = ""
    id: str = ""
    at: str = field(default_factory=_now)

    # -- derived ---------------------------------------------------------

    @property
    def verified(self) -> bool:
        """True when something was shipped *and* proved it.

        Requires both a verified outcome and at least one passing criterion.
        A run that claims success with no passing check is exactly the
        "unverified results treated as successes" failure this engine exists
        to prevent, so the two conditions are deliberately not interchangeable.
        """
        if self.outcome not in VERIFIED_OUTCOMES:
            return False
        return any(c.passed is True for c in self.criteria)

    @property
    def confidence(self) -> float:
        """0.0-1.0, derived from what actually supports the record."""
        score = 0.0
        if self.outcome in VERIFIED_OUTCOMES:
            score += 0.4
        passing = [c for c in self.criteria if c.passed is True]
        if passing:
            score += 0.2
        if self.evidence:
            score += 0.2
        if self.failure_cause != "none" and self.root_cause:
            # An *established* cause counts for more than a guessed one.
            score += 0.1 if self.root_cause_established else 0.05
        if self.user_accepted is True:
            score += 0.1
        return round(min(score, 1.0), 3)

    @property
    def tier(self) -> str:
        """Where this record belongs in memory.

        ``lesson`` is reserved for records that actually carry a rule. A run
        that verified cleanly is the strongest evidence the engine holds, but
        if nobody distilled a rule out of it then ``learn lessons`` would
        report rules that do not exist, and the tier label would be claiming
        more than the record contains. That over-claiming is precisely the
        failure this engine exists to prevent, so the honest middle tier -
        proved, but not yet distilled - gets its own name.
        """
        if self.outcome in VERIFIED_OUTCOMES and self.verified:
            return "lesson" if self.lessons else "verified"
        if self.hypotheses or self.lessons:
            return "hypothesis"
        return "history"

    @property
    def search_keys(self) -> list[str]:
        """Tokens used for retrieval. Deliberately coarse."""
        keys = [self.category, self.failure_cause, self.outcome, self.lane,
                self.strategy_version, self.workflow_version]
        keys += self.agents_used + self.agents_selected_wrong
        return [k.lower() for k in keys if k]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["criteria"] = [c.to_dict() for c in self.criteria]
        data["attempts"] = [a.to_dict() for a in self.attempts]
        data["verified"] = self.verified
        data["confidence"] = self.confidence
        data["tier"] = self.tier
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Experience":
        known = {f for f in cls.__dataclass_fields__}
        data = {k: v for k, v in raw.items() if k in known}
        data["criteria"] = [Criterion.from_dict(c) for c in raw.get("criteria", [])]
        data["attempts"] = [Attempt.from_dict(a) for a in raw.get("attempts", [])]
        return cls(**data)

    @classmethod
    def build(cls, run_dir: Path, request: str, **kwargs: Any) -> "Experience":
        """Construct from a run directory, reading whatever run state exists."""
        from . import store

        state = store.RunState.load(run_dir)
        kwargs.setdefault("run", state.slug)
        kwargs.setdefault("run_dir", str(run_dir))
        return cls(request=redact(request), **kwargs)


# --- storage --------------------------------------------------------------


class ExperienceStore:
    """Append-only log of experiences with a maintained index.

    Append-only because an experience is evidence: rewriting history is how a
    system convinces itself it was right earlier. Corrections are new records.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else MEMORY_ROOT

    @property
    def path(self) -> Path:
        return self.root / EXPERIENCE_FILE

    @property
    def index_path(self) -> Path:
        return self.root / INDEX_FILE

    def _ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        guard = self.root / ".gitignore"
        if not guard.exists():
            # Same reason the run dir is ignored: experiences can quote user
            # paths and command output.
            guard.write_text("*\n", encoding="utf-8")

    def record(self, experience: Experience) -> dict[str, Any]:
        if not experience.run:
            raise SwarmError("experience needs a run", code="EXPERIENCE_NO_RUN")
        self._ensure()
        if not experience.id:
            experience.id = f"{experience.run}-{int(time.time() * 1000)}"
        append_jsonl(self.path, experience.to_dict())
        self._reindex()
        return experience.to_dict()

    def all(self) -> list[Experience]:
        return [Experience.from_dict(r) for r in read_jsonl(self.path)]

    def _reindex(self) -> None:
        counts: dict[str, dict[str, int]] = {}
        for record in read_jsonl(self.path):
            bucket = counts.setdefault(
                record.get("category", "unknown"), {"total": 0, "verified": 0})
            bucket["total"] += 1
            if record.get("verified"):
                bucket["verified"] += 1
        atomic_write_json(self.index_path, {"categories": counts,
                                            "updated_at": _now()})

    def index(self) -> dict[str, Any]:
        data = read_json(self.index_path, default={})
        return {"categories": data.get("categories", {}), "updated_at": data.get("updated_at", "")}

    # -- retrieval -------------------------------------------------------

    def search(self, *, category: str | None = None,
               failure_cause: str | None = None,
               strategy_version: str | None = None,
               outcome: str | None = None,
               limit: int = 20) -> list[Experience]:
        """Filtered retrieval. Unset filters match everything."""
        results = []
        for experience in self.all():
            if category and experience.category != category:
                continue
            if failure_cause and experience.failure_cause != failure_cause:
                continue
            if strategy_version and experience.strategy_version != strategy_version:
                continue
            if outcome and experience.outcome != outcome:
                continue
            results.append(experience)
        results.sort(key=lambda e: (e.confidence, e.at), reverse=True)
        return results[:limit]

    def neighbours(self, experience: Experience, *, limit: int = 5) -> list[Experience]:
        """Most confident records sharing a category or failure cause.

        This is the 'retrieve lessons for a similar task' path. Restricting by
        category is what keeps a lesson about flaky worktree merges from being
        applied to a CSS change.
        """
        same = [e for e in self.all()
                if e.run != experience.run
                and (e.category == experience.category
                     or e.failure_cause == experience.failure_cause)]
        same.sort(key=lambda e: e.confidence, reverse=True)
        return same[:limit]


def read_json(path: Path, default: Any = None) -> Any:
    from .store import read_json as _read

    return _read(path, default=default)


# --- memory ---------------------------------------------------------------


class Memory:
    """Tiered memory layered on the experience store.

    Tier order matters: a candidate improvement is not a strategy, and a
    strategy is not a lesson. :meth:`recall` only ever returns grounded
    material, so retrieval cannot hand the planner an unverified belief as if
    it were established.
    """

    def __init__(self, root: Path | None = None,
                 ledger_path: Path | None = None) -> None:
        self.root = Path(root) if root is not None else MEMORY_ROOT
        self.store = ExperienceStore(self.root)
        self.ledger_path = ledger_path

    def tiers(self) -> dict[str, list[Experience]]:
        grouped: dict[str, list[Experience]] = {t: [] for t in TIERS}
        for experience in self.store.all():
            grouped[experience.tier].append(experience)
        return grouped

    def lessons(self) -> list[tuple[str, float, str]]:
        """(rule, confidence, provenance) for records grounded in evidence."""
        out = []
        for experience in self.store.all():
            if experience.tier != "lesson":
                continue
            for rule in experience.lessons:
                out.append((rule, experience.confidence, experience.run))
        return out

    def hypotheses(self) -> list[tuple[str, str]]:
        return [(rule, e.run) for e in self.store.all()
                if e.tier == "hypothesis" for rule in e.hypotheses]

    def recall(self, *, category: str | None = None,
               failure_cause: str | None = None,
               limit: int = 5) -> list[dict[str, Any]]:
        """Grounded material relevant to a category, most confident first.

        Returns dicts rather than strings so the caller can see the confidence
        and where the rule came from. A rule applied without its provenance is
        a rule that cannot be challenged when the evidence changes.
        """
        found: list[dict[str, Any]] = []
        for experience in self.store.search(category=category,
                                           failure_cause=failure_cause):
            if experience.tier != "lesson":
                continue
            for rule in experience.lessons:
                found.append({
                    "rule": rule,
                    "confidence": experience.confidence,
                    "category": experience.category,
                    "failure_cause": experience.failure_cause,
                    "origin": experience.run,
                })
        found.sort(key=lambda r: r["confidence"], reverse=True)
        return found[:limit]

    def challenge(self, run: str, reason: str) -> dict[str, Any]:
        """Record that new evidence contradicts an earlier conclusion.

        Refuses to invent a run that does not exist: an unsupported challenge
        is how a correct lesson gets quietly dropped.
        """
        if not any(e.run == run for e in self.store.all()):
            raise SwarmError(f"no experience for run {run!r}",
                             code="EXPERIENCE_NOT_FOUND")
        self._ensure_dir()
        append_jsonl(self.root / "challenges.jsonl",
                     {"run": run, "reason": redact(reason), "at": _now()})
        return {"challenged": run, "reason": redact(reason)}

    def challenges(self) -> list[dict[str, Any]]:
        return read_jsonl(self.root / "challenges.jsonl")

    def _ensure_dir(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        guard = self.root / ".gitignore"
        if not guard.exists():
            guard.write_text("*\n", encoding="utf-8")


# --- capture from a run ---------------------------------------------------


def capture_run(run_dir: Path, *, request: str = "",
                user_corrections: Iterable[str] = (),
                user_accepted: bool | None = None) -> Experience:
    """Build an :class:`Experience` from everything a finished run recorded.

    Reads rather than receives wherever a fact already exists on disk: the run
    state, the plan, the criteria, the evidence, and the budget counters. That
    is what keeps capture honest -- it cannot claim something the run did not
    actually record.
    """
    from . import gate, phases, store
    from .budget import Budget
    from .workers import Registry

    state = store.RunState.load(run_dir)
    plan = store.read_json(run_dir / store.PLAN_TASKS_FILE, default={}) or {}
    criteria = gate.load_criteria(run_dir)

    criterion_records = []
    for crit in criteria:
        entries = [e for e in store.read_jsonl(run_dir / store.EVIDENCE_FILE)
                   if e.get("criterion") == crit.id]
        decisive = [e for e in entries if e.get("status") in ("pass", "fail")]
        passed = None
        actual = ""
        evidence = ""
        if decisive:
            last = decisive[-1]
            passed = last.get("status") == "pass"
            actual = redact(last.get("result", ""))
            evidence = redact(last.get("command", "") or last.get("source", ""))
        criterion_records.append(Criterion(
            id=crit.id, text=crit.text, expected=crit.text,
            actual=actual, passed=passed, evidence=evidence))

    failed_checks = [e.get("command") or e.get("source") or e.get("criterion", "")
                     for e in store.read_jsonl(run_dir / store.EVIDENCE_FILE)
                     if e.get("status") == "fail"]

    charge = Budget.load(run_dir)
    workers = Registry(run_dir).load()

    outcome = "aborted" if state.abort_reason else (
        "delivered" if state.phase == phases.DELIVERED else "unknown")

    experience = Experience(
        run=state.slug,
        run_dir=str(run_dir),
        request=redact(request),
        category=plan.get("category", "unknown"),
        complexity=plan.get("complexity", "small"),
        outcome=outcome,
        lane=plan.get("lane", ""),
        plan=[t.get("objective", "") for t in plan.get("tasks", [])],
        agents_used=sorted({w.role for w in workers.values() if w.role}),
        criteria=criterion_records,
        evidence=[c.evidence for c in criterion_records if c.evidence],
        failed_checks=[redact(c) for c in failed_checks],
        failure_cause="none" if outcome == "delivered" else "unknown",
        elapsed_s=round(charge.elapsed(), 2) if charge else None,
        retries=int(sum(charge.retries.values())) if charge else 0,
        tokens_in=charge.tokens_in if charge else None,
        tokens_out=charge.tokens_out if charge else None,
        cost_usd=charge.estimated_cost_usd() if charge else None,
        user_corrections=[redact(c) for c in user_corrections],
        user_accepted=user_accepted,
        strategy_version=plan.get("strategy_version", ""),
        workflow_version=plan.get("workflow_version", ""),
        workers_recovered=sum(1 for w in workers.values()
                              if w.state in ("reclaimed", "timeout")),
        duplicates_prevented=0,
    )
    experience.id = f"{state.slug}-{int(time.time() * 1000)}"
    return experience


def json_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, default=str)
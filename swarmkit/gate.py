"""The delivery gate.

"Never mark a task DELIVERED without verifying its required acceptance
criteria" only holds if the criteria and their evidence are machine-readable.
Prose in ``plan.md`` cannot be checked, so the gate reads two structured
sidecars written alongside it:

* ``criteria.json`` - one entry per objectively checkable acceptance criterion.
* ``evidence.jsonl`` - append-only records of what was actually run and what
  it actually said.

A criterion with no passing evidence keeps the gate shut.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import GateError
from .store import CRITERIA_FILE, EVIDENCE_FILE, append_jsonl, read_json, read_jsonl, utcnow

PASS = "pass"
FAIL = "fail"
SKIP = "skip"
VALID_STATUSES = (PASS, FAIL, SKIP)


@dataclass
class Criterion:
    id: str
    text: str
    workstream: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Criterion":
        if not isinstance(raw, dict):
            raise GateError("each criterion must be an object", code="BAD_CRITERION")
        cid = str(raw.get("id", "")).strip()
        text = str(raw.get("text", "")).strip()
        if not cid:
            raise GateError("criterion is missing an id", code="BAD_CRITERION")
        if not text:
            raise GateError(f"criterion {cid!r} is missing text", code="BAD_CRITERION")
        ws = raw.get("workstream")
        return cls(id=cid, text=text, workstream=str(ws) if ws else None)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "workstream": self.workstream}


@dataclass
class Evidence:
    criterion: str
    status: str
    command: str = ""
    result: str = ""
    source: str = ""
    at: str = field(default_factory=utcnow)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Evidence":
        status = str(raw.get("status", "")).lower()
        if status not in VALID_STATUSES:
            raise GateError(
                f"evidence status must be one of {VALID_STATUSES}, got {status!r}",
                code="BAD_EVIDENCE",
            )
        return cls(
            criterion=str(raw.get("criterion", "")),
            status=status,
            command=str(raw.get("command", "")),
            result=str(raw.get("result", "")),
            source=str(raw.get("source", "")),
            at=str(raw.get("at", "")),
        )


def load_criteria(run_dir: Path) -> list[Criterion]:
    raw = read_json(Path(run_dir) / CRITERIA_FILE, default=[]) or []
    if not isinstance(raw, list):
        raise GateError(f"{CRITERIA_FILE} must be a JSON array", code="BAD_CRITERION")
    criteria = [Criterion.from_dict(item) for item in raw]
    seen: set[str] = set()
    for crit in criteria:
        if crit.id in seen:
            raise GateError(f"duplicate criterion id {crit.id!r}", code="DUPLICATE_CRITERION")
        seen.add(crit.id)
    return criteria


def add_criterion(run_dir: Path, crit: Criterion) -> Criterion:
    run_dir = Path(run_dir)
    # Round-trip through from_dict so a hand-built Criterion gets the same
    # validation as one read back off disk. Without this a blank id or blank
    # text lands in criteria.json and only fails much later, at delivery.
    crit = Criterion.from_dict(crit.to_dict())
    criteria = load_criteria(run_dir)
    if any(c.id == crit.id for c in criteria):
        raise GateError(f"criterion {crit.id!r} already exists", code="DUPLICATE_CRITERION")
    from .store import atomic_write_json  # local import keeps module import graph flat

    atomic_write_json(run_dir / CRITERIA_FILE, [c.to_dict() for c in criteria + [crit]])
    return crit


def add_evidence(run_dir: Path, ev: Evidence) -> Evidence:
    # Same reason as add_criterion: validate before append. evidence.jsonl is
    # append-only, so bad status text written here could never be taken back.
    ev = Evidence.from_dict(ev.__dict__)
    ev.at = ev.at or utcnow()
    append_jsonl(Path(run_dir) / EVIDENCE_FILE, ev.__dict__)
    return ev


@dataclass
class CriterionResult:
    criterion: Criterion
    passed: bool
    evidence_count: int
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.criterion.id,
            "text": self.criterion.text,
            "passed": self.passed,
            "evidence_count": self.evidence_count,
            "reasons": self.reasons,
        }


@dataclass
class GateResult:
    results: list[CriterionResult]
    orphan_evidence: list[str] = field(default_factory=list)

    @property
    def open(self) -> bool:
        """True when every criterion has passing evidence."""
        return bool(self.results) and all(r.passed for r in self.results)

    @property
    def failed(self) -> list[CriterionResult]:
        return [r for r in self.results if not r.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "open": self.open,
            "criteria_total": len(self.results),
            "criteria_passed": sum(1 for r in self.results if r.passed),
            "results": [r.to_dict() for r in self.results],
            "orphan_evidence": self.orphan_evidence,
        }

    def render(self) -> str:
        lines = ["DELIVERY GATE: " + ("OPEN" if self.open else "CLOSED"), ""]
        for res in self.results:
            mark = "PASS" if res.passed else "FAIL"
            lines.append(f"  [{mark}] {res.criterion.id}  {res.criterion.text}")
            for reason in res.reasons:
                lines.append(f"         - {reason}")
        if self.orphan_evidence:
            lines.append("")
            lines.append(
                "  orphan evidence (criterion id not declared): "
                + ", ".join(sorted(set(self.orphan_evidence)))
            )
        return "\n".join(lines)


def evaluate(run_dir: Path) -> GateResult:
    """Score every criterion against the evidence recorded so far."""
    run_dir = Path(run_dir)
    criteria = load_criteria(run_dir)
    evidence = [Evidence.from_dict(e) for e in read_jsonl(run_dir / EVIDENCE_FILE)]

    by_criterion: dict[str, list[Evidence]] = {}
    orphans: list[str] = []
    known = {c.id for c in criteria}
    for ev in evidence:
        if ev.criterion in known:
            by_criterion.setdefault(ev.criterion, []).append(ev)
        else:
            orphans.append(ev.criterion)

    results: list[CriterionResult] = []
    for crit in criteria:
        items = by_criterion.get(crit.id, [])
        # A `skip` is neither a pass nor a failure: it records that a check was
        # unavailable, which stays visible without pretending either way.
        decisive = [e for e in items if e.status in (PASS, FAIL)]
        reasons: list[str] = []
        if not items:
            reasons.append("no evidence recorded")
        elif not decisive:
            reasons.append(f"{len(items)} record(s), all skipped, none verified")
        elif decisive[-1].status == PASS:
            reasons.extend(
                f"pass via {ev.command or ev.source or '(unspecified source)'}: "
                f"{ev.result or 'no result text'}"
                for ev in decisive
                if ev.status == PASS
            )
        else:
            # The LAST decisive record wins. An earlier pass must not open the
            # gate once a later run of the same check failed — that ordering is
            # a regression, and a stale green is the exact thing the gate exists
            # to stop being fooled by.
            last = decisive[-1]
            reasons.append(
                f"last check FAILED via {last.command or last.source or '(unspecified source)'}: "
                f"{last.result or 'no result text'}"
            )
            for ev in decisive[:-1]:
                if ev.status == FAIL:
                    reasons.append(
                        f"earlier failure via "
                        f"{ev.command or ev.source or '(unspecified source)'}: "
                        f"{ev.result or 'no result text'}"
                    )
        results.append(
            CriterionResult(
                criterion=crit,
                passed=bool(decisive) and decisive[-1].status == PASS,
                evidence_count=len(items),
                reasons=reasons,
            )
        )
    return GateResult(results=results, orphan_evidence=orphans)


def assert_open(run_dir: Path) -> GateResult:
    """Raise unless the gate is open. Used to block the DELIVERED transition."""
    result = evaluate(run_dir)
    if not result.open:
        if not result.results:
            raise GateError(
                "delivery gate closed: no acceptance criteria declared. "
                "Add them with `swarm criterion add` before delivering.",
                code="NO_CRITERIA",
            )
        raise GateError(
            "delivery gate closed:\n" + result.render(),
            code="GATE_CLOSED",
        )
    return result
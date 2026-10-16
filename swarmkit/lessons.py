"""Safe self-improvement.

The previous design let ``swarm/improve`` silently rewrite the agent pages -
including ``qc.md``, the gate that judges its own work - with no evidence
requirement, no regression check and no dedup. This module makes the ledger
append-only-with-provenance and refuses to promote anything unverified:

* every rule records the failure it came from and the evidence for it;
* near-duplicates are refused rather than appended twice;
* rules that contradict an existing rule are flagged, never merged silently;
* the line cap is enforced instead of aspirational;
* nothing is auto-promoted. :func:`promote` demands passing regression tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import LessonError
from .store import atomic_write_text, utcnow

LEDGER_HEADER = "# Swarm lessons ledger"
DEFAULT_CAP_LINES = 60

#: Polarity markers. Two rules on the same subject with opposite polarity
#: contradict; this catches "always X" landing next to "never X".
POLARITY = {
    "never": -1,
    "avoid": -1,
    "don't": -1,
    "do not": -1,
    "no longer": -1,
    "always": +1,
    "must": +1,
    "require": +1,
}

_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "at", "for", "with",
    "is", "are", "be", "do", "not", "that", "this", "it", "by", "from", "as",
}

# Metadata keys `Lesson.render()` emits. Anything else stays part of the rule,
# so a rule that happens to contain a colon is not mistaken for metadata.
_META_KEYS = frozenset({"origin", "evidence", "status"})

# Section headings that are structure, not lessons.
_DIVIDERS = frozenset({"lessons", "index", "rules", "contents", "toc"})


def normalise_rule(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return " ".join(w for w in words if w not in _STOPWORDS)


def polarity(text: str) -> int | None:
    lowered = text.lower()
    found = [POLARITY[k] for k in POLARITY if re.search(rf"\b{re.escape(k)}\b", lowered)]
    if not found:
        return None
    return 1 if sum(found) > 0 else -1


def subject_key(text: str) -> str:
    """Loose subject identity so near-duplicates collapse.

    The polarity modal is stripped on purpose: contradiction detection pairs
    "never use worktrees" with "always use worktrees" by subject, and keeps
    them apart only by sign. Leaving the modal in makes the two keys differ,
    so they never compare and the contradiction is silently missed.
    """
    stripped = text.lower()
    for marker in sorted(POLARITY, key=len, reverse=True):
        stripped = re.sub(rf"\b{re.escape(marker)}\b\s*", " ", stripped, count=1)
    return normalise_rule(stripped)


@dataclass
class Lesson:
    rule: str
    date: str = ""
    run: str = ""
    failure: str = ""
    evidence: str = ""
    verified: bool = False

    def render(self) -> str:
        head = f"## {self.date} {self.run}".rstrip()
        lines = [head, self.rule.strip()]
        if self.failure:
            lines.append(f"- origin: {self.failure.strip()}")
        if self.evidence:
            lines.append(f"- evidence: {self.evidence.strip()}")
        lines.append(
            "- status: verified" if self.verified else "- status: UNVERIFIED (not promoted)"
        )
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Ledger:
    path: Path
    header: str = ""
    lessons: list[Lesson] = field(default_factory=list)
    trailing: str = ""

    @classmethod
    def load(cls, path: Path) -> "Ledger":
        path = Path(path)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if not text.strip():
            return cls(path=path, header=default_header(), lessons=[], trailing="")
        blocks = re.split(r"(?m)^##\s+", text)
        header = blocks[0].rstrip("\n")
        lessons: list[Lesson] = []
        for block in blocks[1:]:
            if not block.strip():
                continue
            lines = [ln.rstrip() for ln in block.splitlines()]
            head_meta = lines[0].strip()
            if head_meta.lower() in _DIVIDERS:
                continue
            # Metadata lines are recognised wherever they appear: `render()`
            # writes the rule FIRST and the metadata after it, so parsing them
            # only in leading position silently folds them into the rule text.
            rule_lines: list[str] = []
            meta: dict[str, str] = {}
            for line in lines[1:]:
                stripped = line.strip()
                if not stripped:
                    continue
                m = re.match(r"^-\s*([a-z_]+)\s*:\s*(.*)$", stripped, re.IGNORECASE)
                if m and m.group(1).lower() in _META_KEYS:
                    meta[m.group(1).lower()] = m.group(2).strip()
                    continue
                if stripped.startswith("- "):
                    stripped = stripped[2:]
                rule_lines.append(stripped)
            rule = " ".join(x for x in rule_lines if x).strip()
            if not rule and not meta:
                # A divider or an empty heading is not a lesson.
                continue
            status = meta.get("status", "")
            lessons.append(
                Lesson(
                    rule=rule,
                    date=head_meta.split()[0] if head_meta else "",
                    run=head_meta.split(" ", 1)[1].strip() if " " in head_meta else "",
                    failure=meta.get("origin", ""),
                    evidence=meta.get("evidence", ""),
                    verified=status.strip().lower() == "verified",
                )
            )
        return cls(path=path, header=header or default_header(), lessons=lessons)

    def line_count(self) -> int:
        return len(self.path.read_text(encoding="utf-8").splitlines()) if self.path.exists() else 0

    def render(self) -> str:
        body = "\n".join(lesson.render() for lesson in self.lessons)
        return f"{self.header.rstrip()}\n\n## Lessons\n\n{body}".rstrip() + "\n"

    def save(self) -> None:
        atomic_write_text(self.path, self.render())


def default_header() -> str:
    return (
        f"{LEDGER_HEADER}\n\n"
        "Standing rules distilled from past swarm runs. The orchestrator reads "
        "this at INTAKE and obeys it. Maintained by `swarm/improve`.\n"
        "Every rule must name the failure it came from. Unverified rules are "
        "marked and are never promoted automatically."
    )


def find_duplicate(ledger: Ledger, rule: str) -> Lesson | None:
    """Same subject *and* same polarity. An opposite polarity is a contradiction,
    not a duplicate, so it must fall through to find_contradiction()."""
    key = subject_key(rule)
    if not key:
        return None
    want = polarity(rule)
    for lesson in ledger.lessons:
        if subject_key(lesson.rule) == key and polarity(lesson.rule) == want:
            return lesson
    return None


def find_contradiction(ledger: Ledger, rule: str) -> Lesson | None:
    """Same subject, opposite polarity."""
    key = subject_key(rule)
    pol = polarity(rule)
    if not key or pol is None:
        return None
    for lesson in ledger.lessons:
        if subject_key(lesson.rule) != key:
            continue
        other = polarity(lesson.rule)
        if other is not None and other != pol:
            return lesson
    return None


def add_lesson(
    path: Path,
    rule: str,
    *,
    run: str = "",
    failure: str = "",
    evidence: str = "",
    date: str | None = None,
    cap: int = DEFAULT_CAP_LINES,
    allow_contradiction: bool = False,
) -> dict[str, Any]:
    """Append one lesson, or explain why it was refused."""
    rule = rule.strip()
    if not rule:
        raise LessonError("lesson text is empty", code="EMPTY_LESSON")
    if not failure.strip():
        raise LessonError(
            "every lesson must name the failure that produced it "
            "(--failure is required); ungrounded rules are noise",
            code="LESSON_WITHOUT_ORIGIN",
        )
    ledger = Ledger.load(path)
    dup = find_duplicate(ledger, rule)
    if dup is not None:
        return {
            "added": False,
            "reason": "duplicate",
            "existing": dup.to_dict(),
        }
    contra = find_contradiction(ledger, rule)
    if contra is not None and not allow_contradiction:
        return {
            "added": False,
            "reason": "contradiction",
            "existing": contra.to_dict(),
            "hint": "resolve the conflict by hand, or pass allow_contradiction=True "
            "after deciding which rule wins",
        }
    projected = ledger.line_count() + 5
    if projected > cap:
        return {
            "added": False,
            "reason": "cap_reached",
            "detail": f"ledger would reach ~{projected} lines, cap is {cap}. "
            "Consolidate near-duplicates before adding more.",
        }
    lesson = Lesson(
        rule=rule,
        date=date or utcnow()[:10],
        run=run,
        failure=failure,
        evidence=evidence,
        verified=False,
    )
    ledger.lessons.append(lesson)
    ledger.save()
    return {"added": True, "lesson": lesson.to_dict()}


def promote(path: Path, rule: str, *, tests_passed: bool, evidence: str) -> dict[str, Any]:
    """Promote a lesson to verified. Requires a passing regression run."""
    if not tests_passed:
        raise LessonError(
            "refusing to promote: regression tests did not pass. "
            "Run the suite, then promote.",
            code="PROMOTION_BLOCKED",
        )
    if not evidence.strip():
        raise LessonError(
            "refusing to promote: --evidence is required", code="PROMOTION_BLOCKED"
        )
    ledger = Ledger.load(path)
    target = find_duplicate(ledger, rule)
    if target is None:
        raise LessonError(f"no lesson matching {rule!r} to promote", code="NO_SUCH_LESSON")
    target.verified = True
    if evidence.strip() not in target.evidence:
        target.evidence = f"{target.evidence}; {evidence.strip()}".strip("; ")
    ledger.save()
    return {"promoted": True, "lesson": target.to_dict()}


def audit(path: Path) -> dict[str, Any]:
    """Report ungrounded, duplicate, contradictory and unverified entries."""
    ledger = Ledger.load(path)
    seen: dict[str, list[int]] = {}
    duplicates: list[dict[str, Any]] = []
    for idx, lesson in enumerate(ledger.lessons):
        key = subject_key(lesson.rule)
        if key in seen:
            duplicates.append(
                {"duplicate_of_index": seen[key], "index": idx, "rule": lesson.rule}
            )
        else:
            seen[key] = idx
    contradictions: list[dict[str, Any]] = []
    for i, a in enumerate(ledger.lessons):
        for j, b in enumerate(ledger.lessons[i + 1 :], start=i + 1):
            if subject_key(a.rule) == subject_key(b.rule):
                pa, pb = polarity(a.rule), polarity(b.rule)
                if pa is not None and pb is not None and pa != pb:
                    contradictions.append({"a": i, "b": j, "subject": subject_key(a.rule)})
    ungrounded = [
        {"index": i, "rule": ls.rule}
        for i, ls in enumerate(ledger.lessons)
        if not ls.failure.strip()
    ]
    unverified = [
        {"index": i, "rule": ls.rule} for i, ls in enumerate(ledger.lessons) if not ls.verified
    ]
    return {
        "path": str(path),
        "total": len(ledger.lessons),
        "lines": ledger.line_count(),
        "cap": DEFAULT_CAP_LINES,
        "ungrounded": ungrounded,
        "duplicates": duplicates,
        "contradictions": contradictions,
        "unverified": unverified,
    }
"""Context discipline: the minimal packet in, the compact report out, and the
refusals that stop a summary from eating the truth.

Context discipline was prose in the agent pages and enforced nowhere. Prose
cannot stop the two failures that actually wreck a long run:

* a spawn that carries the whole conversation, the whole repository and every
  other worker's report because pasting was cheaper than choosing; and
* a summariser that quietly drops the constraint nobody restated, so the run
  "passes" a review that no longer remembers what it was asked to guarantee.

Every rule below is therefore a refusal with a code rather than a guideline:

* :func:`build_packet` is an **allowlist**. The packet is a frozen dataclass
  with a closed set of fields, so there is no ``extra`` bag a transcript can be
  smuggled through, and every withheld category is reported by ``excluded()``
  with its reason instead of being silently forgotten.
* :func:`summarise` **refuses**. It never truncates a required category to hit a
  number, it will not even build a checkpoint that omits one, and it verifies
  that no distinct item vanished before it returns - so "it dropped the
  constraint" is not a bug you can write by accident.
* :func:`estimate_tokens` is labelled as a heuristic everywhere it surfaces,
  because a character count dressed up as a provider measurement is how a run
  ends up over a real model ceiling and truncates in the one place it cannot
  see.

Stdlib only, no network and no model call. This module decides what a worker is
handed and what it must hand back; it never talks to a provider itself.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .errors import BudgetExceeded, ConfigError, SwarmError
from .store import atomic_write_text, utcnow

# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class ContextRefusal(BudgetExceeded):
    """A context operation refused instead of dropping required material.

    Subclasses :class:`swarmkit.errors.BudgetExceeded` rather than inventing a
    new base, because the reason is the same one the budget module exists for:
    a ceiling was reached and the honest response is a hard stop. Callers that
    already ``except SwarmError`` keep working, and ``.code`` still names the
    exact condition.

    ``unpreserved`` names what could not be kept, ``detail`` carries the coarse
    sizes, and ``event`` carries the recorded truncation when one applies. A
    refusal that does not say *what* was lost is just a number.
    """

    code = "CONTEXT_BUDGET_EXCEEDED"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        unpreserved: Sequence[str] = (),
        detail: Mapping[str, object] | None = None,
        event: "TruncationEvent | None" = None,
    ) -> None:
        super().__init__(message, code=code)
        self.unpreserved: list[str] = list(unpreserved)
        self.detail: dict[str, object] = dict(detail or {})
        self.event = event


# --------------------------------------------------------------------------
# token estimation (heuristic, labelled, never a measurement)
# --------------------------------------------------------------------------

#: The heuristic behind :func:`estimate_tokens`, named so a caller can put it in
#: a log line. An estimate that does not say how it was produced gets read as a
#: measurement, and that is the mistake this constant exists to prevent.
TOKEN_ESTIMATE_METHOD = (
    "heuristic: sum of ceil(chars/4) per line plus 1 token per newline; "
    "an ESTIMATE, NOT a tokenizer measurement"
)

#: Four characters per token is the usual English rule of thumb. It is wrong for
#: code, CJK and base64, which is exactly why the result is labelled and why
#: :func:`fits` refuses rather than sends when the estimate is over.
CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class TokenEstimate:
    """A labelled estimate.

    ``is_estimate`` is carried on the value, not just the docstring, so a caller
    that renders it into a status line cannot present it as a provider's count.
    """

    tokens: int
    chars: int
    method: str = TOKEN_ESTIMATE_METHOD
    is_estimate: bool = True

    def render(self) -> str:
        return f"~{self.tokens} tokens ({self.method})"


def estimate_tokens(text: str) -> int:
    """Estimate the token count of ``text``.

    HEURISTIC, NOT A MEASUREMENT: ``ceil(len(line) / CHARS_PER_TOKEN)`` summed
    per line, plus one token per newline. See :data:`TOKEN_ESTIMATE_METHOD`.

    Use it to budget and to refuse. Never report the result as a provider's
    token count - compare :func:`token_estimate`, which returns the same number
    wrapped with the label so the two cannot be confused.
    """
    if not text:
        return 0
    total = text.count("\n")
    for line in text.split("\n"):
        if line:
            total += max(1, math.ceil(len(line) / CHARS_PER_TOKEN))
    return total


def token_estimate(text: str) -> TokenEstimate:
    """Like :func:`estimate_tokens` but returns the labelled value object."""
    return TokenEstimate(tokens=estimate_tokens(text), chars=len(text or ""))


# --------------------------------------------------------------------------
# the context packet (allowlist by construction)
# --------------------------------------------------------------------------

#: Context material a packet NEVER carries, with the WHY.
#:
#: This table is the enforcement, not documentation. ``excluded()`` renders it
#: for every packet so the omission is explicit, and ``build_packet`` refuses
#: any attempt to supply one of these fields - so "we did not include the
#: conversation" is a property of the code, not a promise in a prompt.
NEVER_INCLUDED: tuple[tuple[str, str], ...] = (
    (
        "conversation_history",
        "the worker did not take part in the conversation; replaying it is the "
        "'rewrite the whole window every turn' failure this module exists to stop",
    ),
    (
        "full_repository",
        "a worker is handed the paths it must touch, not a file tree or file "
        "bodies; reading the repository is a tool call, not context",
    ),
    (
        "task_history",
        "earlier tasks' transcripts do not change this task; what matters from "
        "them arrives as a one-line dependency summary",
    ),
    (
        "other_workers_reports",
        "another workstream's report is a QC input, not this task's input; "
        "cross-workstream bleed is what the file boundary exists to prevent",
    ),
    (
        "prior_summaries",
        "an earlier checkpoint is referenced by path or id, not inlined; only "
        "the reader that needs it should fetch it",
    ),
)

#: Field names a caller might reasonably try, and the withheld category each
#: maps to. Matched case-insensitively against every key ``build_packet`` is
#: given. Accepting any of these would let the whole-conversation case back in.
REFUSED_FIELDS: dict[str, str] = {
    "context": "conversation_history",
    "conversation": "conversation_history",
    "messages": "conversation_history",
    "chat": "conversation_history",
    "transcript": "conversation_history",
    "history": "task_history",
    "task_history": "task_history",
    "repo": "full_repository",
    "repository": "full_repository",
    "tree": "full_repository",
    "file_tree": "full_repository",
    "files_tree": "full_repository",
    "contents": "full_repository",
    "file_contents": "full_repository",
    "reports": "other_workers_reports",
    "other_reports": "other_workers_reports",
    "peer_reports": "other_workers_reports",
    "summaries": "prior_summaries",
    "checkpoints": "prior_summaries",
}

#: The complete set of fields a packet may contain. Anything else is refused.
ALLOWED_FIELDS: tuple[str, ...] = (
    "task_id",
    "task",
    "deliverable",
    "criteria",
    "dependencies",
    "instructions",
    "files",
)

#: Content caps. The allowlist is the first line of defence; these stop an
#: allowed field being used as a drain for bulk text. A description longer than
#: this reads like a transcript, and a dependency "summary" longer than this is
#: the dependency's report, not its interface.
MAX_TASK_CHARS = 8000
MAX_INSTRUCTION_CHARS = 4000
MAX_DEPENDENCY_SUMMARY_CHARS = 280
MAX_CRITERIA = 64
MAX_INSTRUCTIONS = 64
MAX_FILES = 64

_EXCLUDE_REASON = {name: reason for name, reason in NEVER_INCLUDED}

_RANGE_RE = re.compile(r"^(?P<path>.+):(?P<start>\d+)(?:-(?P<end>\d+))?$")


@dataclass(frozen=True)
class Exclusion:
    """One withheld category and the reason it is withheld."""

    category: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"category": self.category, "reason": self.reason}


@dataclass(frozen=True)
class FileRef:
    """A targeted file, optionally narrowed to a line range.

    A path plus a range, never a body: if a caller wants the worker to see the
    contents, that is a read tool call the worker makes, not bytes shipped in
    the packet.
    """

    path: str
    start: int | None = None
    end: int | None = None
    note: str = ""

    @classmethod
    def from_string(cls, text: str) -> "FileRef":
        """Parse ``path`` or ``path:START`` or ``path:START-END``.

        The range is only parsed when the trailing ``:digits`` shape matches, so
        a Windows drive path (``C:\\x\\y.py``) keeps its colon.
        """
        raw = str(text or "").strip()
        match = _RANGE_RE.match(raw)
        if not match:
            return cls(path=raw)
        start = int(match.group("start"))
        end_text = match.group("end")
        end = int(end_text) if end_text else start
        return cls(path=match.group("path"), start=start, end=end)

    def render(self) -> str:
        span = ""
        if self.start is not None:
            span = f":{self.start}"
            if self.end is not None and self.end != self.start:
                span += f"-{self.end}"
        tail = f"  # {self.note}" if self.note else ""
        return f"{self.path}{span}{tail}"

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "start": self.start, "end": self.end, "note": self.note}


@dataclass(frozen=True)
class DependencyRef:
    """A dependency, reduced to its interface.

    ``summary`` is one line about what the dependency *produced* (a signature, a
    path, a verified outcome), not the dependency's report. The length cap in
    ``build_packet`` is what keeps that promise honest.
    """

    id: str
    summary: str = ""

    def render(self) -> str:
        return f"{self.id}: {self.summary}" if self.summary else self.id

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "summary": self.summary}


@dataclass(frozen=True)
class ContextPacket:
    """The minimal context for exactly one worker.

    Frozen, with a closed field set and no ``extra``/``raw``/``meta`` mapping:
    there is no attribute a transcript could be attached to, which is what makes
    "the whole conversation cannot be included" structural rather than a rule
    somebody has to remember.
    """

    task_id: str
    task: str
    deliverable: str
    criteria: tuple[str, ...]
    dependencies: tuple[DependencyRef, ...]
    instructions: tuple[str, ...]
    files: tuple[FileRef, ...]

    # -- what was deliberately left out ---------------------------------
    def excluded(self) -> list[Exclusion]:
        """The withheld categories and why, rendered into every packet.

        Static by design: these categories are absent because the packet's shape
        cannot hold them, so the list is the same for every packet. Making it
        depend on what the caller passed would turn a guarantee into a report.
        """
        return [Exclusion(category=name, reason=reason) for name, reason in NEVER_INCLUDED]

    # -- rendering ------------------------------------------------------
    def render(self) -> str:
        sections = (
            ("ACCEPTANCE CRITERIA:", [c for c in self.criteria], "  (none declared)"),
            ("DEPENDENCIES:", [d.render() for d in self.dependencies], "  (none)"),
            ("PROJECT INSTRUCTIONS:", [i for i in self.instructions], "  (none)"),
            ("TARGETED FILES:", [f.render() for f in self.files], "  (none)"),
        )
        lines = [f"TASK {self.task_id or '(unnamed)'}: {self.task}"]
        if self.deliverable:
            lines.append(f"DELIVERABLE: {self.deliverable}")
        for header, values, empty in sections:
            lines.append("")
            lines.append(header)
            if values:
                lines.extend(f"  - {value}" for value in values)
            else:
                lines.append(empty)
        lines.append("")
        lines.append("EXCLUDED (deliberately not in this packet):")
        lines.extend(f"  - {e.category}: {e.reason}" for e in self.excluded())
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task": self.task,
            "deliverable": self.deliverable,
            "criteria": list(self.criteria),
            "dependencies": [d.to_dict() for d in self.dependencies],
            "instructions": list(self.instructions),
            "files": [f.to_dict() for f in self.files],
            "excluded": [e.to_dict() for e in self.excluded()],
        }

    def estimated_tokens(self) -> int:
        """Heuristic size of the rendered packet (see :func:`estimate_tokens`)."""
        return estimate_tokens(self.render())


def _as_str_tuple(values: Iterable[Any], *, what: str, cap: int) -> tuple[str, ...]:
    """Normalise an iterable to a non-empty, deduped, capped tuple of strings."""
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            raise ConfigError(f"{what} entry is empty", code="CONTEXT_EMPTY_ENTRY")
        if text not in out:
            out.append(text)
        if len(out) > cap:
            raise ConfigError(
                f"{what} carries more than {cap} entries; a packet is a "
                f"selection, not a dump",
                code="CONTEXT_TOO_BROAD",
            )
    return tuple(out)


def build_packet(
    task: str,
    *,
    task_id: str = "",
    deliverable: str = "",
    criteria: Iterable[str] = (),
    dependencies: Iterable[DependencyRef | Mapping[str, Any] | str] = (),
    instructions: Iterable[str] = (),
    files: Iterable[FileRef | str] = (),
    max_estimated_tokens: int | None = None,
    **extra: Any,
) -> ContextPacket:
    """Build the minimal packet for one worker.

    Only the task description, its acceptance criteria, its dependencies, the
    project instructions it needs and the file paths/ranges it may touch are
    accepted. Two refusals make the rest impossible by construction:

    * any key outside :data:`ALLOWED_FIELDS` is refused. A key that names a
      withheld category (:data:`REFUSED_FIELDS` -> :data:`NEVER_INCLUDED`) is
      refused with ``CONTEXT_OVERREACH`` and the reason, so nobody can paste the
      conversation in and call it a packet;
    * oversized or body-shaped values are refused rather than included, so an
      allowed field cannot be used as a drain for bulk text.

    ``excluded()`` on the result always lists what was left out and why.
    """
    if extra:
        for key in sorted(extra):
            normalised = str(key).strip().lower()
            category = REFUSED_FIELDS.get(normalised)
            if category:
                raise ConfigError(
                    f"field {key!r} is withheld from a context packet "
                    f"({category}): {_EXCLUDE_REASON.get(category, 'not part of a packet')}. "
                    f"A packet is an allowlist: {', '.join(ALLOWED_FIELDS)}.",
                    code="CONTEXT_OVERREACH",
                )
            raise ConfigError(
                f"field {key!r} is not a packet field; a packet is an allowlist: "
                f"{', '.join(ALLOWED_FIELDS)}",
                code="CONTEXT_UNKNOWN_FIELD",
            )

    text = str(task or "").strip()
    if not text:
        raise ConfigError("a context packet needs a task description", code="CONTEXT_EMPTY_TASK")
    if len(text) > MAX_TASK_CHARS:
        raise ConfigError(
            f"task description is {len(text)} chars (limit {MAX_TASK_CHARS}); this "
            f"reads like a transcript, not a task",
            code="CONTEXT_OVERREACH",
        )

    packet_files: list[FileRef] = []
    for entry in files:
        if isinstance(entry, FileRef):
            ref = entry
        else:
            raw = str(entry or "")
            if "\n" in raw or "\r" in raw:
                raise ConfigError(
                    "a file entry carries a body, not a path; a packet ships "
                    "paths, the worker reads contents with a tool",
                    code="CONTEXT_OVERREACH",
                )
            ref = FileRef.from_string(raw)
        if not ref.path:
            raise ConfigError("file entry has no path", code="CONTEXT_EMPTY_ENTRY")
        if "\n" in ref.path or "\r" in ref.path:
            raise ConfigError(
                "a file path contains a newline; that is a body, not a path",
                code="CONTEXT_OVERREACH",
            )
        packet_files.append(ref)
    if len(packet_files) > MAX_FILES:
        raise ConfigError(
            f"packet names {len(packet_files)} files (limit {MAX_FILES}); target "
            f"the files this task touches, not the repository",
            code="CONTEXT_TOO_BROAD",
        )

    deps: list[DependencyRef] = []
    seen_deps: set[str] = set()
    for entry in dependencies:
        if isinstance(entry, DependencyRef):
            ref = entry
        elif isinstance(entry, Mapping):
            dep_id = str(entry.get("id") or entry.get("task_id") or "").strip()
            summary = str(entry.get("summary") or entry.get("result") or "").strip()
            ref = DependencyRef(id=dep_id, summary=summary)
        else:
            ref = DependencyRef(id=str(entry or "").strip())
        if not ref.id:
            raise ConfigError("dependency entry has no id", code="CONTEXT_EMPTY_ENTRY")
        if len(ref.summary) > MAX_DEPENDENCY_SUMMARY_CHARS:
            raise ConfigError(
                f"dependency {ref.id!r} summary is {len(ref.summary)} chars "
                f"(limit {MAX_DEPENDENCY_SUMMARY_CHARS}); a dependency arrives as "
                f"its interface, not its report",
                code="CONTEXT_OVERREACH",
            )
        if ref.id in seen_deps:
            continue
        seen_deps.add(ref.id)
        deps.append(ref)

    instruction_list: list[str] = []
    for value in _as_str_tuple(instructions, what="instructions", cap=MAX_INSTRUCTIONS):
        if len(value) > MAX_INSTRUCTION_CHARS:
            raise ConfigError(
                f"instruction is {len(value)} chars (limit {MAX_INSTRUCTION_CHARS}); "
                f"project instructions are rules, not documents",
                code="CONTEXT_OVERREACH",
            )
        instruction_list.append(value)

    packet = ContextPacket(
        task_id=str(task_id or "").strip(),
        task=text,
        deliverable=str(deliverable or "").strip(),
        criteria=_as_str_tuple(criteria, what="criteria", cap=MAX_CRITERIA),
        dependencies=tuple(deps),
        instructions=tuple(instruction_list),
        files=tuple(packet_files),
    )

    if max_estimated_tokens is not None:
        if max_estimated_tokens <= 0:
            raise ConfigError(
                "max_estimated_tokens must be positive", code="CONFIG_INVALID"
            )
        used = packet.estimated_tokens()
        if used > max_estimated_tokens:
            raise ContextRefusal(
                f"packet is ~{used} estimated tokens (limit {max_estimated_tokens}); "
                f"narrow the task, criteria or files instead of shipping it whole",
                code="CONTEXT_BUDGET_EXCEEDED",
                unpreserved=["task", "criteria", "dependencies", "instructions", "files"],
                detail={"estimated_tokens": used, "limit": max_estimated_tokens},
            )
    return packet


def sections_from_packet(packet: ContextPacket) -> dict[str, list[str]]:
    """Required checkpoint sections for a packet, so a spawn summary is uniform.

    Provided so a caller does not hand-roll the mapping and accidentally omit a
    required section (which :func:`summarise` refuses anyway).
    """
    return {
        "requirements": [packet.task, *packet.criteria],
        "constraints": list(packet.instructions),
        "decisions": [d.render() for d in packet.dependencies],
        "unresolved": [],
        "evidence": [f.render() for f in packet.files],
    }


# --------------------------------------------------------------------------
# the worker report (the compact handoff)
# --------------------------------------------------------------------------

#: Outcomes a report may claim. Deliberately small: a free-text outcome is the
#: one thing QC cannot branch on.
REPORT_OUTCOMES: tuple[str, ...] = ("ok", "partial", "failed", "blocked")

#: Change kinds for a file entry.
FILE_CHANGES: tuple[str, ...] = ("created", "modified", "deleted")


@dataclass(frozen=True)
class CommandResult:
    """A check that was actually run, and what it actually returned."""

    command: str
    result: str
    exit_code: int | None = None

    def render(self) -> str:
        code = "" if self.exit_code is None else f" [exit {self.exit_code}]"
        return f"{self.command}{code} -> {self.result}"

    def to_dict(self) -> dict[str, Any]:
        return {"command": self.command, "result": self.result, "exit_code": self.exit_code}


@dataclass(frozen=True)
class FileChange:
    """A file the worker created, modified or deleted."""

    path: str
    change: str = "modified"

    def render(self) -> str:
        return f"{self.change} {self.path}"

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "change": self.change}


@dataclass
class WorkerReport:
    """The compact structured handoff every worker returns.

    Six things, and only six, because a report that carries more becomes a
    transcript nobody reads: the task and its outcome; the files and artifacts
    it touched; the decisions and assumptions it made; the commands it ran with
    their actual results; what is still unresolved; and references to detailed
    logs (see :func:`store_artifact`) that are fetched only when needed.
    """

    task_id: str
    outcome: str = ""
    summary: str = ""
    files: list[FileChange] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    commands: list[CommandResult] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "outcome": self.outcome,
            "summary": self.summary,
            "files": [f.to_dict() for f in self.files],
            "artifacts": list(self.artifacts),
            "decisions": list(self.decisions),
            "assumptions": list(self.assumptions),
            "commands": [c.to_dict() for c in self.commands],
            "issues": list(self.issues),
            "blockers": list(self.blockers),
            "evidence": list(self.evidence),
        }

    def render(self) -> str:
        lines = [
            f"REPORT {self.task_id or '(unnamed)'} outcome={self.outcome or '(missing)'}",
        ]
        if self.summary:
            lines.append(f"  {self.summary}")
        for label, values in (
            ("files", [f.render() for f in self.files]),
            ("artifacts", list(self.artifacts)),
            ("decisions", list(self.decisions)),
            ("assumptions", list(self.assumptions)),
            ("commands", [c.render() for c in self.commands]),
            ("issues", list(self.issues)),
            ("blockers", list(self.blockers)),
            ("evidence", list(self.evidence)),
        ):
            for value in values:
                lines.append(f"  {label}: {value}")
        return "\n".join(lines)

    def estimated_tokens(self) -> int:
        return estimate_tokens(self.render())


def validate_report(report: WorkerReport) -> list[str]:
    """Return every problem with ``report``; empty list means it is acceptable.

    Returns rather than raises so QC can show all the problems at once instead
    of one per round trip. Two rules are non-negotiable:

    * **no outcome** - a handoff that does not say what happened cannot be acted
      on or branched on;
    * **a command with an empty result** - a check with no recorded result is
      not a result. Recording the command and forgetting its output is how a
      passing test and a failed one become indistinguishable.
    """
    problems: list[str] = []

    if not str(report.task_id or "").strip():
        problems.append("report has no task_id; a handoff cannot be attributed")

    outcome = str(report.outcome or "").strip()
    if not outcome:
        problems.append("report has no outcome; say ok, partial, failed or blocked")
    elif outcome not in REPORT_OUTCOMES:
        problems.append(
            f"unknown outcome {outcome!r}; expected one of {', '.join(REPORT_OUTCOMES)}"
        )

    for change in report.files:
        if not str(change.path or "").strip():
            problems.append("a file entry has no path")
        elif change.change not in FILE_CHANGES:
            problems.append(
                f"file {change.path!r} has unknown change {change.change!r}; "
                f"expected one of {', '.join(FILE_CHANGES)}"
            )

    for index, command in enumerate(report.commands, start=1):
        name = str(command.command or "").strip()
        result = "" if command.result is None else str(command.result)
        if not name:
            problems.append(f"command #{index} has no command text")
        if not result.strip():
            problems.append(
                f"command {name or f'#{index}'!r} has no recorded result; a check "
                f"with no result is not a result"
            )

    for index, artifact in enumerate(report.artifacts, start=1):
        if not str(artifact or "").strip():
            problems.append(f"artifact #{index} is empty")

    for index, reference in enumerate(report.evidence, start=1):
        if not str(reference or "").strip():
            problems.append(f"evidence reference #{index} is empty")

    return problems


# --------------------------------------------------------------------------
# summarise: compress, or refuse
# --------------------------------------------------------------------------

#: The categories a checkpoint MUST carry. A summary that loses one of these is
#: the exact failure this module exists to make impossible, so ``summarise``
#: refuses rather than dropping any of them. Order is the render order.
REQUIRED_SECTIONS: tuple[str, ...] = (
    "requirements",
    "constraints",
    "decisions",
    "unresolved",
    "evidence",
)

#: Sections a caller may add. These are the ONLY thing a summary is allowed to
#: shed to meet a token target, and even then their removal is named on the
#: result - required material is never the thing that gets cut.
OPTIONAL_SECTIONS: tuple[str, ...] = ("notes", "timeline", "context")


@dataclass(frozen=True)
class Checkpoint:
    """A concise structured checkpoint of completed phases.

    The five required sections have **no defaults**: a checkpoint that omits
    ``constraints`` cannot be constructed at all. That is what turns "do not
    drop the constraint" from a promise into a type.
    """

    requirements: tuple[str, ...]
    constraints: tuple[str, ...]
    decisions: tuple[str, ...]
    unresolved: tuple[str, ...]
    evidence: tuple[str, ...]
    #: Optional sections dropped to fit, named so the drop is visible.
    dropped_optional: tuple[str, ...] = ()
    #: Heuristic size of the rendered checkpoint (see :func:`estimate_tokens`).
    estimated_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {name: list(getattr(self, name)) for name in REQUIRED_SECTIONS}
        data["dropped_optional"] = list(self.dropped_optional)
        data["estimated_tokens"] = self.estimated_tokens
        return data

    def render(self) -> str:
        lines = ["CHECKPOINT"]
        for name in REQUIRED_SECTIONS:
            values = getattr(self, name)
            lines.append(f"{name.upper()}:")
            if values:
                lines.extend(f"  - {value}" for value in values)
            else:
                lines.append("  (none)")
        if self.dropped_optional:
            lines.append("DROPPED OPTIONAL: " + ", ".join(self.dropped_optional))
        return "\n".join(lines)


def _normalise_section(value: Any) -> tuple[str, ...]:
    """Strip, drop blanks and dedupe an exact-duplicate line.

    Deduplication is the only reduction applied to a required section, and it is
    information-preserving: an exact duplicate adds nothing to the set of facts
    the checkpoint carries.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        raw: Iterable[Any] = value.splitlines()
    else:
        raw = value
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        if text not in out:
            out.append(text)
    return tuple(out)


def summarise(sections: Mapping[str, Any], max_tokens: int) -> Checkpoint:
    """Compress completed phases into a checkpoint, or refuse.

    Preserves requirements, constraints, decisions, unresolved issues and
    verification evidence. It **never** discards any of those to meet
    ``max_tokens``: if they cannot fit, it raises :class:`ContextRefusal` naming
    what could not be preserved. The only reduction it performs is deduping
    exact duplicates and normalising whitespace; the only thing it will shed is
    an optional section, and it names that on the result.

    Every required section must be present as a key (it may be empty), so a
    caller cannot forget the constraints category and have the omission look
    like an absence of constraints.
    """
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        raise ConfigError("max_tokens must be a positive integer", code="CONFIG_INVALID")
    if not isinstance(sections, Mapping):
        raise ConfigError("sections must be a mapping of category -> lines", code="CONFIG_INVALID")

    known = set(REQUIRED_SECTIONS) | set(OPTIONAL_SECTIONS)
    unknown = set(sections) - known
    if unknown:
        raise ConfigError(
            f"unknown section(s): {', '.join(sorted(unknown))}; known: "
            f"{', '.join((*REQUIRED_SECTIONS, *OPTIONAL_SECTIONS))}",
            code="CONTEXT_UNKNOWN_SECTION",
        )
    missing = [name for name in REQUIRED_SECTIONS if name not in sections]
    if missing:
        raise ConfigError(
            f"section(s) {', '.join(missing)} missing; a checkpoint must "
            f"declare every required category even when it is empty, otherwise "
            f"a forgotten category is indistinguishable from an empty one",
            code="CONTEXT_SECTION_MISSING",
        )

    normalised = {name: _normalise_section(sections.get(name)) for name in REQUIRED_SECTIONS}
    optional = {name: _normalise_section(sections.get(name)) for name in OPTIONAL_SECTIONS}

    def build(dropped: tuple[str, ...]) -> Checkpoint:
        return Checkpoint(
            requirements=normalised["requirements"],
            constraints=normalised["constraints"],
            decisions=normalised["decisions"],
            unresolved=normalised["unresolved"],
            evidence=normalised["evidence"],
            dropped_optional=dropped,
        )

    full_text = _render_sections(normalised, optional)
    used = estimate_tokens(full_text)
    if used <= max_tokens:
        return _finalise(build(()), normalised, used)

    # Over budget: the required five are not negotiable, so shed the optional
    # sections (the only non-essential content) and try again.
    present_optional = tuple(name for name in OPTIONAL_SECTIONS if optional.get(name))
    pruned_text = _render_sections(normalised, {})
    pruned_used = estimate_tokens(pruned_text)
    if pruned_used <= max_tokens:
        return _finalise(build(present_optional), normalised, pruned_used)

    # Even without the optional sections it does not fit. Refuse and name what
    # could not be preserved - never truncate.
    unpreserved = [name for name in REQUIRED_SECTIONS if normalised[name]]
    detail: dict[str, object] = {
        "limit": max_tokens,
        "rendered_estimated_tokens": pruned_used,
        "sections": {
            name: {
                "items": len(normalised[name]),
                "estimated_tokens": estimate_tokens("\n".join(normalised[name])),
            }
            for name in REQUIRED_SECTIONS
        },
    }
    breakdown = "; ".join(
        f"{name} ({detail['sections'][name]['items']} items, "  # type: ignore[index]
        f"~{detail['sections'][name]['estimated_tokens']} tokens)"  # type: ignore[index]
        for name in unpreserved
    )
    largest = max(
        ((name, normalised[name]) for name in unpreserved),
        key=lambda pair: estimate_tokens("\n".join(pair[1])),
        default=("", ()),
    )
    example = f" largest item in {largest[0]}: {largest[1][0][:120]!r}" if largest[1] else ""
    raise ContextRefusal(
        f"cannot fit the checkpoint into max_tokens={max_tokens} (~{pruned_used} "
        f"estimated tokens needed) without dropping required material. Could not "
        f"preserve: {breakdown};{example}. Refusing rather than truncating: "
        f"requirements, constraints, decisions, unresolved issues and evidence "
        f"must all survive a summary.",
        code="CONTEXT_BUDGET_EXCEEDED",
        unpreserved=unpreserved,
        detail=detail,
    )


def _render_sections(
    required: Mapping[str, tuple[str, ...]],
    optional: Mapping[str, tuple[str, ...]],
) -> str:
    """Render required (and, when kept, optional) sections for a size check."""
    lines: list[str] = []
    for name in (*REQUIRED_SECTIONS, *OPTIONAL_SECTIONS):
        values = required.get(name) if name in required else optional.get(name)
        values = values or ()
        lines.append(f"{name.upper()}:")
        lines.extend(f"- {value}" for value in values)
    return "\n".join(lines)


def _finalise(
    checkpoint: Checkpoint, normalised: Mapping[str, tuple[str, ...]], used: int
) -> Checkpoint:
    """Attach the size and assert nothing required vanished.

    The assertion is the last line of defence: if a future edit introduces a
    code path that drops or truncates a required item, this raises before the
    checkpoint is ever returned rather than after a review decided to trust it.
    """
    for name in REQUIRED_SECTIONS:
        before = set(normalised[name])
        after = set(getattr(checkpoint, name))
        if before != after:
            lost = sorted(before - after)
            raise SwarmError(
                f"internal: summariser dropped {len(lost)} required item(s) from "
                f"{name}: {lost[:3]}. This is the exact bug this module exists to "
                f"make impossible; refusing to return the checkpoint.",
                code="CONTEXT_DROPPED_REQUIRED",
            )
    return Checkpoint(
        requirements=checkpoint.requirements,
        constraints=checkpoint.constraints,
        decisions=checkpoint.decisions,
        unresolved=checkpoint.unresolved,
        evidence=checkpoint.evidence,
        dropped_optional=checkpoint.dropped_optional,
        estimated_tokens=used,
    )


# --------------------------------------------------------------------------
# artifacts: pass a reference, fetch the bytes only when needed
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactRef:
    """A small pointer to a large log written to disk.

    Summaries and reports carry this instead of the bytes, so a 40k-line build
    log costs a line of context until somebody investigating a failure asks for
    the file.
    """

    path: str
    bytes: int
    lines: int
    sha256: str

    def render(self) -> str:
        return f"artifact {self.path} ({self.bytes} bytes, sha256:{self.sha256[:12]})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "bytes": self.bytes,
            "lines": self.lines,
            "sha256": self.sha256,
        }


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """Temp-then-replace for binary payloads, mirroring ``store``'s guarantee."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def store_artifact(
    path: Path | str,
    content: str | bytes,
    *,
    root: Path | str | None = None,
) -> ArtifactRef:
    """Write a large log or verbose output to ``path`` and return a reference.

    ``root``, when given, confines the write: a path that resolves outside it is
    refused, so a caller cannot be talked into overwriting something else by a
    value that arrived from the workstream it is logging for.
    """
    target = Path(path)
    if root is not None:
        anchor = Path(root).resolve()
        resolved = target.resolve()
        if resolved != anchor and anchor not in resolved.parents:
            raise ConfigError(
                f"artifact path {resolved} is outside {anchor}; refusing to write it",
                code="ARTIFACT_OUTSIDE_ROOT",
            )

    if isinstance(content, bytes):
        data = content
        text_for_lines = data.decode("utf-8", errors="replace")
        _write_bytes_atomic(target, data)
    else:
        text_for_lines = str(content)
        data = text_for_lines.encode("utf-8")
        atomic_write_text(target, text_for_lines)

    lines = text_for_lines.count("\n") + (0 if text_for_lines.endswith("\n") or not text_for_lines else 1)
    return ArtifactRef(
        path=str(target),
        bytes=len(data),
        lines=lines,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def read_artifact(reference: ArtifactRef | Path | str) -> str:
    """Fetch the full text of a stored artifact.

    The explicit "go and get it" step: a summary carries an :class:`ArtifactRef`
    and only a failure investigation or a QC request turns it back into bytes.
    """
    path = Path(reference.path if isinstance(reference, ArtifactRef) else reference)
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# fits: refuse an over-ceiling request and record the truncation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TruncationEvent:
    """The record of a request that was refused for exceeding a ceiling.

    Recorded rather than commented because a truncation that leaves no trace is
    indistinguishable from a request that was never made.
    """

    limit: int
    estimated_tokens: int
    reserved: int
    label: str
    reason: str
    at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "estimated_tokens": self.estimated_tokens,
            "reserved": self.reserved,
            "label": self.label,
            "reason": self.reason,
            "at": self.at,
        }


class TruncationLog:
    """Where refusals are recorded.

    Explicit rather than module-global so two runs inside one process cannot see
    each other's truncations, and so a test can assert that the event happened
    instead of reading a log file.
    """

    def __init__(self) -> None:
        self.events: list[TruncationEvent] = []

    def record(self, event: TruncationEvent) -> TruncationEvent:
        self.events.append(event)
        return event

    def __len__(self) -> int:
        return len(self.events)

    def render(self) -> str:
        return "\n".join(f"{e.at} {e.label}: {e.estimated_tokens}>{e.limit}" for e in self.events)


@dataclass(frozen=True)
class FitResult:
    """The outcome of a ceiling check that passed."""

    tokens: int
    limit: int
    reserved: int = 0
    label: str = "payload"

    @property
    def ok(self) -> bool:
        return True

    def headroom(self) -> int:
        return self.limit - self.tokens - self.reserved

    def render(self) -> str:
        return (
            f"{self.label}: ~{self.tokens} estimated tokens "
            f"(+{self.reserved} reserved) fits {self.limit} "
            f"({self.headroom()} left) - estimate, not a measurement"
        )


def _payload_text(payload: Any) -> str:
    """Render anything the caller might hand ``fits`` into a string."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    for method in ("render",):
        renderer = getattr(payload, method, None)
        if callable(renderer):
            return str(renderer())
    return str(payload)


def fits(
    context_limit: int,
    payload: Any = None,
    *,
    reserved: int = 0,
    label: str = "payload",
    log: TruncationLog | None = None,
) -> FitResult:
    """Check a payload against a model's context ceiling. Refuses, never sends.

    An estimate over the ceiling is a hard stop: it records a
    :class:`TruncationEvent` (into ``log`` when one is supplied) and raises
    :class:`ContextRefusal`, because sending it anyway is how a run gets silently
    truncated at a point where nobody is looking.

    ``reserved`` accounts for output/system budget the caller keeps aside.
    """
    if not isinstance(context_limit, int) or isinstance(context_limit, bool) or context_limit <= 0:
        raise ConfigError("context_limit must be a positive integer", code="CONFIG_INVALID")
    if reserved < 0 or not isinstance(reserved, int) or isinstance(reserved, bool):
        raise ConfigError("reserved must be a non-negative integer", code="CONFIG_INVALID")

    text = _payload_text(payload)
    used = estimate_tokens(text)
    if used + reserved > context_limit:
        event = TruncationEvent(
            limit=context_limit,
            estimated_tokens=used,
            reserved=reserved,
            label=label,
            reason=(
                f"~{used} estimated tokens (+{reserved} reserved) exceeds "
                f"context_limit={context_limit}"
            ),
            at=utcnow(),
        )
        if log is not None:
            log.record(event)
        raise ContextRefusal(
            f"{label} is ~{used} estimated tokens (+{reserved} reserved), over "
            f"context_limit={context_limit}. Refusing to send it; record the "
            f"truncation and reduce the payload instead.",
            code="CONTEXT_LIMIT_EXCEEDED",
            unpreserved=[label],
            detail={
                "estimated_tokens": used,
                "reserved": reserved,
                "limit": context_limit,
                "method": TOKEN_ESTIMATE_METHOD,
            },
            event=event,
        )
    return FitResult(tokens=used, limit=context_limit, reserved=reserved, label=label)


__all__ = [
    "ALLOWED_FIELDS",
    "ArtifactRef",
    "Checkpoint",
    "CommandResult",
    "ContextPacket",
    "ContextRefusal",
    "DependencyRef",
    "Exclusion",
    "FILE_CHANGES",
    "FileChange",
    "FileRef",
    "FitResult",
    "NEVER_INCLUDED",
    "OPTIONAL_SECTIONS",
    "REFUSED_FIELDS",
    "REPORT_OUTCOMES",
    "REQUIRED_SECTIONS",
    "TOKEN_ESTIMATE_METHOD",
    "TruncationEvent",
    "TruncationLog",
    "TokenEstimate",
    "WorkerReport",
    "build_packet",
    "estimate_tokens",
    "fits",
    "read_artifact",
    "sections_from_packet",
    "store_artifact",
    "summarise",
    "token_estimate",
    "validate_report",
]

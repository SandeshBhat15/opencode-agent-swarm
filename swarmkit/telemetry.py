"""Per-task and per-worker measurement, taken from telemetry that already exists.

Three rules shape this module.

1. **Measure from records that are already written.** OpenCode emits token and
   cost accounting into its session records and into its `run --format json`
   log, so taking a measurement never costs a model call. Spending a request to
   discover how many requests were spent is the waste this module exists to
   remove. Where native telemetry is available, use it.

2. **A number that was not reported is ``None``, never ``0``.** "Unmeasured" and
   "zero" are different claims, and conflating them is how a budget reports
   success it never verified. That is why every field here defaults to ``None``
   and why :meth:`Usage.unmeasured` exists: the gaps are part of the report.

3. **Two sources, because they answer different questions.** A session record
   gives one session's aggregate - and its ``parentID``, which is how a worker
   is attributed back to the task that spawned it, with no name matching and no
   heuristics. A run log gives per-step granularity, which is what separates
   "the task was large" from "the agent wasted steps".

Nothing here talks to the network, and nothing here calls a model.
"""

from __future__ import annotations

import collections
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

#: Fields that hold a measurement. A ``None`` in any of them means "not reported".
MEASURED_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "model_calls",
    "retries",
    "delegations",
    "elapsed_s",
    "cost_usd",
    "context_tokens",
    "truncations",
    "qc_failures",
    "repair_cycles",
    "wasted_steps",
)

#: A smaller number is a better number for all of these.
LOWER_IS_BETTER: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "model_calls",
    "retries",
    "delegations",
    "elapsed_s",
    "wasted_steps",
)

#: Correctness guards. A change that worsens one of these is refused, not
#: promoted with a caveat: faster at failing is not better.
GUARDED_FIELDS: tuple[str, ...] = ("outcome", "qc_failures", "repair_cycles")


class TelemetryError(ValueError):
    """Raised when a telemetry record cannot be read as written."""

    def __init__(self, message: str, *, code: str = "TELEMETRY_INVALID") -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# coercion helpers - the only place a missing value is allowed to become None
# --------------------------------------------------------------------------- #
def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# --------------------------------------------------------------------------- #
# the measurement itself
# --------------------------------------------------------------------------- #
@dataclass
class Usage:
    """What one task, or one worker, actually consumed.

    Every numeric field defaults to ``None``: unmeasured, not zero.
    """

    label: str = ""
    session_id: str | None = None
    agent: str | None = None
    model: str | None = None
    outcome: str | None = None

    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None

    model_calls: int | None = None
    retries: int | None = None
    delegations: int | None = None

    elapsed_s: float | None = None
    cost_usd: float | None = None

    context_tokens: int | None = None
    truncations: int | None = None

    qc_failures: int | None = None
    repair_cycles: int | None = None

    wasted_steps: int | None = None

    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @property
    def total_tokens(self) -> int | None:
        """Input + output + reasoning, or ``None`` if none of them were reported."""
        parts = [self.input_tokens, self.output_tokens, self.reasoning_tokens]
        if all(p is None for p in parts):
            return None
        return sum(p or 0 for p in parts)

    def unmeasured(self) -> list[str]:
        """Names of the measurements this record does not have.

        Exposed rather than hidden: a comparison built on two unmeasured
        numbers is not a comparison.
        """
        return [name for name in MEASURED_FIELDS if getattr(self, name) is None]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sum_field(usages: Sequence[Usage], name: str) -> int | float | None:
    values = [getattr(u, name) for u in usages]
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present)


def combine(usages: Iterable[Usage], *, label: str = "") -> Usage:
    """Sum several measurements into one.

    Each field is summed over the records that reported it and is ``None`` if
    none did - so combining one measured record with one silent record yields a
    measured number, not a silently halved one.
    """
    items = list(usages)
    if not items:
        return Usage(label=label)

    combined = Usage(label=label or items[0].label)
    for name in MEASURED_FIELDS:
        setattr(combined, name, _sum_field(items, name))

    # Outcomes do not sum. The worst one is the honest summary, because a rollup
    # that contains a failure has not succeeded.
    outcomes = [u.outcome for u in items if u.outcome]
    if outcomes:
        combined.outcome = "succeeded" if all(o == "succeeded" for o in outcomes) else "failed"
    combined.session_id = items[0].session_id
    combined.agent = items[0].agent
    combined.model = items[0].model
    for u in items:
        combined.notes.extend(u.notes)
    return combined


# --------------------------------------------------------------------------- #
# source 1: an OpenCode session record
# --------------------------------------------------------------------------- #
def from_session_record(record: Mapping[str, Any]) -> Usage:
    """Read one ``/api/session`` entry.

    Missing keys become ``None``. A key that is present but not a number is also
    ``None``: a value we cannot read is not a value we can report.
    """
    if not isinstance(record, Mapping):
        raise TelemetryError("session record must be a mapping", code="RECORD_INVALID")

    tokens = _mapping(record.get("tokens"))
    cache = _mapping(tokens.get("cache"))
    model = _mapping(record.get("model"))
    timing = _mapping(record.get("time"))

    created = _int_or_none(timing.get("created"))
    updated = _int_or_none(timing.get("updated"))
    elapsed = None
    if created is not None and updated is not None:
        elapsed = (updated - created) / 1000.0

    model_name = None
    parts = [model.get("providerID"), model.get("id")]
    joined = "/".join(str(p) for p in parts if p)
    if joined:
        model_name = joined
        variant = model.get("variant")
        if variant:
            model_name = f"{model_name}#{variant}"

    usage = Usage(
        label=str(record.get("title") or record.get("id") or ""),
        session_id=record.get("id") if isinstance(record.get("id"), str) else None,
        agent=record.get("agent") if isinstance(record.get("agent"), str) else None,
        model=model_name,
        outcome=record.get("outcome") if isinstance(record.get("outcome"), str) else None,
        input_tokens=_int_or_none(tokens.get("input")),
        output_tokens=_int_or_none(tokens.get("output")),
        reasoning_tokens=_int_or_none(tokens.get("reasoning")),
        cache_read_tokens=_int_or_none(cache.get("read")),
        cache_write_tokens=_int_or_none(cache.get("write")),
        elapsed_s=elapsed,
        cost_usd=_float_or_none(record.get("cost")),
    )
    return usage


def load_sessions(payload: Any) -> list[Mapping[str, Any]]:
    """Accept either the ``{"data": [...]}`` envelope or a bare list."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise TelemetryError(f"session payload is not JSON: {exc}", code="JSON_INVALID") from exc
    if isinstance(payload, Mapping):
        payload = payload.get("data", [])
    if not isinstance(payload, list):
        raise TelemetryError("session payload must be a list or an envelope", code="PAYLOAD_INVALID")
    return [row for row in payload if isinstance(row, Mapping)]


def pricing_available(sessions: Sequence[Mapping[str, Any]]) -> bool:
    """Whether any record carried a non-zero cost.

    Every session on a free model reports ``cost: 0``, and a column of zeros is
    not evidence that the work was free - it is evidence that nobody priced it.
    This lets a report say "cost not measurable on this host" instead of
    quietly printing ``$0.00``.
    """
    for row in sessions:
        value = _float_or_none(row.get("cost")) if isinstance(row, Mapping) else None
        if value is not None and value > 0:
            return True
    return False


def rollup(sessions: Sequence[Mapping[str, Any]]) -> dict[str, Usage]:
    """Group sessions into one :class:`Usage` per root task.

    A worker is a session whose ``parentID`` names another session, so worker
    attribution is a lookup rather than a guess, and the count of children *is*
    the delegation count.
    """
    rows = [r for r in sessions if isinstance(r, Mapping)]
    by_id = {r.get("id"): r for r in rows if isinstance(r.get("id"), str)}
    children: dict[str, list[Mapping[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        parent = row.get("parentID")
        if isinstance(parent, str) and parent:
            children[parent].append(row)

    rolled: dict[str, Usage] = {}
    for session_id, record in by_id.items():
        if record.get("parentID"):
            continue  # a worker, not a task root
        own = from_session_record(record)
        kids = children.get(session_id, [])
        merged = combine([own, *(from_session_record(k) for k in kids)], label=own.label)
        merged.session_id = session_id
        merged.agent = own.agent
        merged.model = own.model
        merged.delegations = len(kids)
        if not kids:
            merged.notes.append("no child sessions: nothing was delegated")
        rolled[session_id] = merged
    return rolled


# --------------------------------------------------------------------------- #
# source 2: an `opencode run --format json` log
# --------------------------------------------------------------------------- #
@dataclass
class RunLog:
    """Per-step view of one run."""

    steps: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    malformed_lines: int = 0


def _decode(raw: bytes | str) -> str:
    """Decode a run log written by any of the ways a shell writes one.

    A PowerShell ``1>`` redirect emits UTF-16LE, and reading that as UTF-8
    yields text in which every line fails to parse - which looks exactly like an
    empty log. Sniffing the BOM is cheaper than re-running the task to find out.
    """
    if isinstance(raw, str):
        return raw[1:] if raw.startswith("\ufeff") else raw
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8")
    return raw.decode("utf-8", errors="replace")


def parse_run_log(raw: bytes | str) -> RunLog:
    """Read an ``opencode run --format json`` log (JSON-lines).

    Only two event types carry a fact worth keeping:

    * ``step_finish`` closes exactly one model call and is the *only* event with
      ``tokens`` and ``cost``. Counting those is what makes "model calls" a
      measured number rather than an estimate.
    * ``tool_use`` records a tool and its status. A tool that ended in an error
      is a wasted step, and surfacing that count is how a wasted step stays
      visible instead of being averaged away inside a total.
    """
    text = _decode(raw)
    result = RunLog()
    input_tot = output_tot = reasoning_tot = cache_read_tot = cache_write_tot = None
    cost_tot: float | None = None
    calls = 0
    wasted = 0

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            result.malformed_lines += 1
            continue
        if not isinstance(event, Mapping):
            result.malformed_lines += 1
            continue

        kind = event.get("type")
        part = _mapping(event.get("part"))

        if kind == "step_finish":
            calls += 1
            tokens = _mapping(part.get("tokens"))
            cache = _mapping(tokens.get("cache"))
            step = {
                "kind": kind,
                "timestamp": event.get("timestamp"),
                "reason": part.get("reason"),
                "cost": _float_or_none(part.get("cost")),
                "tokens": {
                    "input": _int_or_none(tokens.get("input")),
                    "output": _int_or_none(tokens.get("output")),
                    "reasoning": _int_or_none(tokens.get("reasoning")),
                    "cache_read": _int_or_none(cache.get("read")),
                    "cache_write": _int_or_none(cache.get("write")),
                },
            }
            result.steps.append(step)

            def _add(current: int | None, fresh: int | None) -> int | None:
                if fresh is None:
                    return current
                return (current or 0) + fresh

            input_tot = _add(input_tot, step["tokens"]["input"])
            output_tot = _add(output_tot, step["tokens"]["output"])
            reasoning_tot = _add(reasoning_tot, step["tokens"]["reasoning"])
            cache_read_tot = _add(cache_read_tot, step["tokens"]["cache_read"])
            cache_write_tot = _add(cache_write_tot, step["tokens"]["cache_write"])
            cost_tot = _add(cost_tot, step["cost"])  # type: ignore[arg-type]

        elif kind == "tool_use":
            state = _mapping(part.get("state"))
            status = state.get("status")
            failed = status not in (None, "completed") or bool(state.get("error"))
            if failed:
                wasted += 1
            result.steps.append(
                {
                    "kind": kind,
                    "timestamp": event.get("timestamp"),
                    "tool": part.get("tool"),
                    "status": status,
                    "error": state.get("error"),
                    "wasted": failed,
                }
            )

    # Only claim a measurement if the events that carry it were present.
    result.usage = Usage(
        input_tokens=input_tot,
        output_tokens=output_tot,
        reasoning_tokens=reasoning_tot,
        cache_read_tokens=cache_read_tot,
        cache_write_tokens=cache_write_tot,
        model_calls=calls if calls else None,
        wasted_steps=wasted if result.steps else None,
        cost_usd=cost_tot,
    )
    if calls == 0:
        result.usage.notes.append("no step_finish events: token totals not measured")
    return result


# --------------------------------------------------------------------------- #
# baselines and comparison
# --------------------------------------------------------------------------- #
@dataclass
class Comparison:
    """Before/after for one task category."""

    category: str
    verdict: str
    reason: str
    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    regressions: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)
    not_measured: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"[{self.category}] {self.verdict}: {self.reason}"]
        for name, row in sorted(self.metrics.items()):
            pct = row.get("pct")
            shown = "n/a" if pct is None else f"{pct:+.1f}%"
            lines.append(
                f"  {name:<20} {row['before']} -> {row['after']}  ({shown})"
            )
        if self.regressions:
            lines.append("  regressions: " + ", ".join(self.regressions))
        if self.not_measured:
            lines.append("  not measured (excluded from the verdict): " + ", ".join(self.not_measured))
        return "\n".join(lines)


def compare(
    before: Usage,
    after: Usage,
    *,
    category: str = "task",
    tolerance: float = 0.10,
) -> Comparison:
    """Judge a change against a fixed baseline.

    Order matters, and it is the whole point:

    1. **Correctness first.** A change that breaks the verified outcome, or that
       adds QC failures or repair cycles, is ``refused`` - not promoted with a
       caveat. Cheaper at failing is not cheaper.
    2. **Then the resource comparison**, using only fields measured on *both*
       sides. Anything unmeasured is listed and excluded rather than treated as
       zero.
    3. A resource regression does not disqualify an improvement, but it does
       stop the verdict from being a clean ``improved``.
    """
    before_outcome, after_outcome = before.outcome, after.outcome
    if before_outcome == "succeeded" and after_outcome not in (None, "succeeded"):
        return Comparison(
            category=category,
            verdict="refused",
            reason=f"verified outcome regressed from succeeded to {after_outcome}",
            regressions=["outcome"],
        )

    guard_breaches = []
    for name in GUARDED_FIELDS:
        if name == "outcome":
            continue
        b, a = getattr(before, name), getattr(after, name)
        if b is not None and a is not None and a > b:
            guard_breaches.append(name)
    if guard_breaches:
        return Comparison(
            category=category,
            verdict="refused",
            reason="regressed on " + ", ".join(guard_breaches) + "; correctness outranks savings",
            regressions=guard_breaches,
        )

    metrics: dict[str, dict[str, Any]] = {}
    improvements: list[str] = []
    regressions: list[str] = []
    not_measured: list[str] = []

    for name in LOWER_IS_BETTER:
        b, a = getattr(before, name), getattr(after, name)
        if b is None or a is None:
            not_measured.append(name)
            continue
        if b == 0:
            # A relative change from zero is undefined; report the absolute move.
            metrics[name] = {"before": b, "after": a, "delta": a - b, "pct": None}
            if a > 0:
                regressions.append(name)
            continue
        pct = (a - b) / b * 100.0
        metrics[name] = {"before": b, "after": a, "delta": a - b, "pct": pct}
        if pct < -tolerance * 100.0:
            improvements.append(name)
        elif pct > tolerance * 100.0:
            regressions.append(name)

    if not metrics:
        return Comparison(
            category=category,
            verdict="unknown",
            reason="nothing comparable was measured on both runs",
            not_measured=not_measured,
        )
    if improvements and not regressions:
        verdict, reason = "improved", "reduced " + ", ".join(improvements) + " with no measured regression"
    elif improvements and regressions:
        verdict, reason = "mixed", "improved " + ", ".join(improvements) + " but worsened " + ", ".join(regressions)
    elif regressions:
        verdict, reason = "regressed", "worsened " + ", ".join(regressions)
    else:
        verdict, reason = "unchanged", "no metric moved beyond the tolerance"
    return Comparison(
        category=category,
        verdict=verdict,
        reason=reason,
        metrics=metrics,
        regressions=regressions,
        improvements=improvements,
        not_measured=not_measured,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_baseline(path: Path, category: str, usage: Usage, *, note: str = "") -> Path:
    """Append a baseline row. Append-only, so an earlier baseline cannot be
    rewritten by a later, more flattering one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"category": category, "recorded_at": _now(), "note": note, "usage": usage.as_dict()}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    return path


def load_baselines(path: Path, category: str | None = None) -> list[dict[str, Any]]:
    """Read baseline rows, optionally filtered to one category."""
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if category is not None and row.get("category") != category:
            continue
        rows.append(row)
    return rows


def render(usage: Usage) -> str:
    """Human-readable measurement, with the gaps stated."""
    lines = [f"measurement: {usage.label or '(unlabelled)'}"]
    if usage.agent:
        lines.append(f"  agent: {usage.agent}")
    if usage.model:
        lines.append(f"  model: {usage.model}")
    if usage.outcome:
        lines.append(f"  outcome: {usage.outcome}")
    for name in MEASURED_FIELDS:
        value = getattr(usage, name)
        if value is not None:
            lines.append(f"  {name:<20} {value}")
    gaps = usage.unmeasured()
    if gaps:
        lines.append(f"  not measured: {', '.join(gaps)}")
    if usage.cost_usd is not None and usage.cost_usd == 0:
        lines.append("  note: cost reported as 0 - if the model is a free tier this is not a price, it is an absence of one")
    return "\n".join(lines)

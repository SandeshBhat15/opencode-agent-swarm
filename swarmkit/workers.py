"""Worker liveness: heartbeats, staleness, timeouts and safe reclaim.

A worker that dies silently is indistinguishable from one that is still
thinking, and that ambiguity is how a swarm deadlocks: a slot stays leased, the
run never finishes, and nothing reports why. This module makes liveness
observable and recoverable.

Liveness is classified into four states, and the distinction that matters is
*dead* versus *slow*:

``alive``
    Running, and a heartbeat landed recently.
``slow``
    Running, still beating, but close to its timeout. A slow worker that keeps
    beating never becomes stale - only the wall clock can condemn it.
``stale``
    Running with no heartbeat for ``stale_seconds``. Probably dead.
``timeout``
    Past ``timeout_s`` since spawn. Definitive, whatever the heartbeat says.

The last point is why heartbeats alone are insufficient, and why a heartbeat
alone is never enough: a worker stuck in an infinite loop can beat forever.

Every attempt carries an opaque ``epoch``. A reclaimed worker's replacement gets
a new one, so a zombie that wakes up and beats or releases cannot interfere with
the generation that replaced it. Duplicate execution is prevented on
``(name, attempt)`` identity rather than name alone.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .budget import COUNTERS_LOCK, Budget, Limits
from .errors import SwarmError
from .store import LOCK_WAIT_SECONDS, atomic_write_json, file_lock, read_json, utcnow

REGISTRY_FILE = "workers.json"

#: Terminal worker states. A worker in one of these no longer occupies a slot.
DONE = ("ok", "failed", "reclaimed", "timeout")

#: Liveness verdicts produced by :func:`classify`.
ALIVE = "alive"
SLOW = "slow"
STALE = "stale"
TIMEOUT = "timeout"

#: Beating, but not advancing. Deliberately *not* a reclaim trigger: see
#: :func:`_is_wedged` for why this must not kill a long legitimate job.
WEDGED = "wedged"

VALID_STATES = ("running",) + DONE

DEFAULT_TIMEOUT_S = 900.0

#: No beat for this long means the worker is presumed dead.
DEFAULT_STALE_SECONDS = 120.0

#: Beating but no forward progress for this long means the worker is spinning.
#: Set well above ``stale_seconds`` on purpose: a worker that goes quiet is
#: handled by ``stale_seconds``; this only ever fires on a worker that is
#: demonstrably still running.
DEFAULT_PROGRESS_STALE_SECONDS = 600.0

#: A beating worker this close to its timeout is reported as slow, so an
#: operator sees pressure building before the worker is condemned.
SLOW_FRACTION = 0.8

#: Retry backoff: delay = base * factor ** (attempt - 1), capped at max.
DEFAULT_RETRY_BACKOFF_SECONDS = 2.0
DEFAULT_RETRY_BACKOFF_FACTOR = 2.0
MAX_RETRY_BACKOFF_SECONDS = 60.0


def registry_path(run_dir: Path) -> Path:
    return Path(run_dir) / REGISTRY_FILE


def _parse(ts: str | None) -> float:
    """ISO timestamp -> epoch seconds. Missing/unreadable -> 0.0 (very old)."""
    if not ts:
        return 0.0
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _age(now: float, ts: str | None) -> float:
    """Seconds between ``ts`` and ``now``.

    A missing or unreadable timestamp reads as *infinitely old*, never as
    "just now". That is deliberate: a record with no timestamp must never be
    mistaken for a healthy worker, or a crashed worker would look alive.
    """
    parsed = _parse(ts)
    if parsed <= 0.0:
        return float("inf")
    return now - parsed


def _is_wedged(worker: Worker, *, now: float, stale_seconds: float) -> bool:
    """True when a worker is beating but has stopped advancing.

    A heartbeat only proves *something is running*. A worker stuck in an
    infinite retry loop, or one that rewrites a log line on every pass while
    getting nowhere, will beat forever. Waiting for the wall-clock timeout
    means sitting through the whole ``timeout_s`` doing nothing.

    The guard that keeps this from killing legitimate long work: the worker
    must have beaten **at least once since its last progress** and must still
    be inside ``stale_seconds``, so it is provably alive. Progress is a
    caller-reported signal (``Registry.progress``); a worker that never reports
    progress has ``progress_at`` equal to its spawn, and the whole check
    degrades to the wall-clock timeout rather than firing on its own. Callers
    opt in by reporting progress, which is what keeps a silent-but-working
    long job from being called wedged.
    """
    if worker.progress <= 0:
        # No progress has ever been reported, so there is no evidence of a
        # spin. A worker that has simply not reached its first milestone yet
        # is indistinguishable from one that is stuck, and killing it would be
        # destroying legitimate work on a guess.
        return False
    if worker.progress_at == worker.spawned_at:
        # Nothing reported progress since spawn: no evidence of a spin.
        return False
    since_progress = _age(now, worker.progress_at)
    if since_progress < DEFAULT_PROGRESS_STALE_SECONDS:
        return False
    # Beats since the last progress: proof it is running, not merely slow.
    since_beat = _age(now, worker.last_beat)
    return since_beat < stale_seconds


def _stamp(epoch: float) -> str:
    """Epoch seconds -> the same ISO form ``utcnow`` produces.

    Written this way so an injected ``now`` is authoritative for the recorded
    timestamps, not just for the comparison. Otherwise a test could classify a
    record at a fake time while the record itself carried the real clock, and
    the verdict would depend on when the test happened to run.
    """
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def retry_delay(attempt: int, *, base: float = DEFAULT_RETRY_BACKOFF_SECONDS,
                factor: float = DEFAULT_RETRY_BACKOFF_FACTOR,
                cap: float = MAX_RETRY_BACKOFF_SECONDS) -> float:
    """Bounded exponential backoff for attempt ``attempt`` (1 = first try).

    Bounded on purpose: an unbounded backoff turns a dead worker into a run that
    silently waits hours. The cap keeps a retry loop inside a human's patience
    and inside ``max_wall_clock_seconds``.
    """
    if attempt < 1:
        attempt = 1
    delay = base * (factor ** (attempt - 1))
    return float(min(delay, cap))


@dataclass
class Worker:
    name: str
    role: str = ""
    attempt: int = 1
    state: str = "running"
    pid: int | None = None
    epoch: str = ""
    spawned_at: str = ""
    last_beat: str = ""
    beats: int = 0
    progress: int = 0
    progress_at: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "attempt": self.attempt,
            "state": self.state,
            "pid": self.pid,
            "epoch": self.epoch,
            "spawned_at": self.spawned_at,
            "last_beat": self.last_beat,
            "beats": self.beats,
            "progress": self.progress,
            "progress_at": self.progress_at,
            "timeout_s": self.timeout_s,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Worker":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {k: v for k, v in raw.items() if k in known}
        try:
            return cls(**clean)
        except (TypeError, ValueError) as exc:
            raise SwarmError(
                f"malformed worker record: {exc}", code="CORRUPT_REGISTRY"
            ) from exc


def classify(worker: Worker, *, stale_seconds: float = DEFAULT_STALE_SECONDS,
             now: float | None = None) -> str:
    """Decide whether a worker is alive, slow, stale or timed out.

    ``now`` is injectable so tests can classify a fixed timestamp instead of
    sleeping.
    """
    if worker.state != "running":
        return worker.state
    now = time.time() if now is None else now

    age = _age(now, worker.spawned_at)
    timeout_s = worker.timeout_s or DEFAULT_TIMEOUT_S
    if age >= timeout_s:
        return TIMEOUT

    since_beat = _age(now, worker.last_beat or worker.spawned_at)
    if since_beat >= stale_seconds:
        return STALE
    if _is_wedged(worker, now=now, stale_seconds=stale_seconds):
        return WEDGED
    if age >= timeout_s * SLOW_FRACTION:
        return SLOW
    return ALIVE


class Registry:
    """Persistent view of the workers spawned for one run."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)

    # -- persistence ----------------------------------------------------
    def load(self) -> dict[str, Worker]:
        raw = read_json(registry_path(self.run_dir), default={}) or {}
        if not isinstance(raw, dict):
            raise SwarmError(
                f"{REGISTRY_FILE} is unreadable; move it aside to reset the "
                "worker registry",
                code="CORRUPT_REGISTRY",
            )
        workers = raw.get("workers")
        if workers is None:
            return {}
        if not isinstance(workers, dict):
            raise SwarmError(
                f"{REGISTRY_FILE} has a malformed 'workers' section",
                code="CORRUPT_REGISTRY",
            )
        out: dict[str, Worker] = {}
        for name, entry in workers.items():
            if not isinstance(entry, dict):
                raise SwarmError(
                    f"worker {name!r} is not a record", code="CORRUPT_REGISTRY"
                )
            out[name] = Worker.from_dict(entry)
        return out

    def save(self, workers: dict[str, Worker]) -> None:
        atomic_write_json(
            registry_path(self.run_dir),
            {
                "version": 1,
                "updated_at": utcnow(),
                "workers": {n: w.to_dict() for n, w in sorted(workers.items())},
            },
        )

    # -- queries -------------------------------------------------------
    def status(self, *, stale_seconds: float = DEFAULT_STALE_SECONDS,
               now: float | None = None) -> dict[str, Any]:
        workers = self.load()
        rows = []
        for name, worker in sorted(workers.items()):
            verdict = classify(worker, stale_seconds=stale_seconds, now=now)
            rows.append({**worker.to_dict(), "liveness": verdict})
        return {
            "workers": rows,
            "alive": [r["name"] for r in rows if r["liveness"] == ALIVE],
            "slow": [r["name"] for r in rows if r["liveness"] == SLOW],
            "stale": [r["name"] for r in rows if r["liveness"] == STALE],
            "timed_out": [r["name"] for r in rows if r["liveness"] == TIMEOUT],
            "wedged": [r["name"] for r in rows if r["liveness"] == WEDGED],
        }

    # -- mutations -----------------------------------------------------
    def register(self, name: str, *, role: str = "",
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 pid: int | None = None, now: float | None = None,
                 stale_seconds: float = DEFAULT_STALE_SECONDS) -> Worker:
        """Record a new attempt for ``name``.

        Refuses while a previous attempt is still genuinely alive, which is the
        duplicate-execution guard. A stale or timed-out predecessor is allowed
        through and gets a NEW epoch, so the old process cannot interfere.
        """
        now = time.time() if now is None else now
        workers = self.load()
        previous = workers.get(name)
        if previous is not None and previous.state == "running":
            verdict = classify(previous, stale_seconds=stale_seconds, now=now)
            if verdict in (ALIVE, SLOW):
                raise SwarmError(
                    f"worker {name!r} attempt {previous.attempt} is {verdict}; "
                    f"refusing to spawn it again (duplicate execution). "
                    f"Wait for it, or `workers reclaim` it first.",
                    code="DUPLICATE_WORKER",
                )
        attempt = (previous.attempt + 1) if previous is not None else 1
        stamp = _stamp(now)
        worker = Worker(
            name=name,
            role=role or (previous.role if previous else ""),
            attempt=attempt,
            state="running",
            pid=pid,
            epoch=uuid.uuid4().hex,
            spawned_at=stamp,
            last_beat=stamp,
            beats=0,
            progress=0,
            # Seeded equal to spawned_at so a brand-new worker is never
            # immediately judged wedged: no progress has been *missed* yet.
            progress_at=stamp,
            timeout_s=timeout_s or DEFAULT_TIMEOUT_S,
        )
        workers[name] = worker
        self.save(workers)
        return worker

    def heartbeat(self, name: str, epoch: str) -> Worker:
        """Record a beat from the process that holds ``epoch``.

        A mismatched epoch is refused rather than ignored: that is a zombie
        from a reclaimed attempt, and honouring its beat would keep a dead
        worker looking alive forever.
        """
        workers = self.load()
        worker = workers.get(name)
        if worker is None:
            raise SwarmError(f"no worker {name!r} in the registry", code="NO_SUCH_WORKER")
        if epoch and worker.epoch and epoch != worker.epoch:
            raise SwarmError(
                f"worker {name!r} is attempt {worker.attempt} "
                f"(epoch {worker.epoch}), but a beat arrived for epoch {epoch}. "
                f"This process was reclaimed; stop it.",
                code="EPOCH_MISMATCH",
            )
        worker.last_beat = utcnow()
        worker.beats += 1
        workers[name] = worker
        self.save(workers)
        return worker

    def progress(self, name: str, epoch: str, *, units: int = 1) -> Worker:
        """Record forward progress for ``name`` and refresh its beat.

        This is the signal that separates a slow-but-working worker from one
        that is spinning. A worker that reports progress every step keeps its
        ``progress_at`` current and is never classified ``wedged``; one that
        keeps beating without calling this eventually is, and only then.

        The epoch check is the same as :meth:`heartbeat`: a reclaimed zombie
        cannot report progress to resurrect its record.

        Holds ``COUNTERS_LOCK`` with a bounded wait for the same reason
        :func:`spawn` does. Progress arrives from *every* live worker at once,
        so a fail-fast lock would drop most of them on the floor; and
        ``save`` rewrites the whole registry from the snapshot it loaded, so an
        unlocked write both loses its own increment and reverts any concurrent
        record change - which could resurrect a finished worker as ``running``
        and defeat the duplicate-execution guard.
        """
        if units < 1:
            raise SwarmError(
                f"progress units must be >= 1, got {units}", code="BAD_PROGRESS"
            )
        with file_lock(self.run_dir / COUNTERS_LOCK, timeout=LOCK_WAIT_SECONDS):
            workers = self.load()
            worker = workers.get(name)
            if worker is None:
                raise SwarmError(f"no worker {name!r} in the registry", code="NO_SUCH_WORKER")
            if epoch and worker.epoch and epoch != worker.epoch:
                raise SwarmError(
                    f"worker {name!r} is attempt {worker.attempt} "
                    f"(epoch {worker.epoch}), but progress arrived for epoch {epoch}. "
                    f"This process was reclaimed; stop it.",
                    code="EPOCH_MISMATCH",
                )
            stamp = utcnow()
            worker.progress += units
            worker.progress_at = stamp
            # Progress implies a beat: a worker that is advancing is by
            # definition alive, so do not force a second call.
            worker.last_beat = stamp
            worker.beats += 1
            workers[name] = worker
            self.save(workers)
            return worker

    def finish(self, name: str, epoch: str, state: str,
               note: str = "") -> Worker:
        """Mark a worker done. ``state`` must be one of ``ok``/``failed``."""
        if state not in ("ok", "failed"):
            raise SwarmError(
                f"finish() takes 'ok' or 'failed', not {state!r}",
                code="BAD_STATE",
            )
        workers = self.load()
        worker = workers.get(name)
        if worker is None:
            raise SwarmError(f"no worker {name!r} in the registry", code="NO_SUCH_WORKER")
        if epoch and worker.epoch and epoch != worker.epoch:
            raise SwarmError(
                f"worker {name!r} holds epoch {worker.epoch}, not {epoch!r}; "
                f"refusing to finish an attempt it does not own",
                code="EPOCH_MISMATCH",
            )
        worker.state = state
        worker.note = note or worker.note
        worker.last_beat = utcnow()
        workers[name] = worker
        self.save(workers)
        return worker

    def find(self, name: str) -> Worker:
        worker = self.load().get(name)
        if worker is None:
            raise SwarmError(f"no worker {name!r} in the registry", code="NO_SUCH_WORKER")
        return worker

    def owns(self, name: str, epoch: str) -> bool:
        """True when ``epoch`` is the current attempt for ``name``."""
        worker = self.load().get(name)
        return bool(worker and worker.epoch and worker.epoch == epoch)


def reconcile(run_dir: Path, *, limits: Limits | None = None,
              stale_seconds: float = DEFAULT_STALE_SECONDS,
              now: float | None = None) -> dict[str, Any]:
    """Make registry, budget leases and recorded evidence agree. Idempotent.

    A worker can die at four distinct points, each leaving different debris:

    ==========================  ================  ===========  ==========
    crash point                 registry state   budget lease  evidence
    ==========================  ================  ===========  ==========
    after spawn, before running  ``running``      held         none
    while running                ``running``      held         none
    after result, before finish  ``running``      held         present
    after finish                  terminal        free         present
    ==========================  ================  ===========  ==========

    Reconciling answers the only question that matters on resume: does a live
    process still exist for this slot? It releases the lease of every worker
    that is dead (``stale``/``timeout``) **and** of every worker that already
    has evidence recorded but never called ``finish`` - that last case is a
    worker that did its job and died before tidying up, which no liveness check
    could ever distinguish from a slow one.

    Called twice in a row the second call releases nothing, because the first
    already popped the lease and marked the record. That is the property that
    makes it safe to run unconditionally on resume.
    """
    now = time.time() if now is None else now
    path = Path(run_dir)
    registry = Registry(path)
    released: list[dict[str, Any]] = []
    consistent: list[dict[str, Any]] = []

    with file_lock(path / COUNTERS_LOCK, timeout=LOCK_WAIT_SECONDS):
        workers = registry.load()
        budget = Budget.load(path, limits)
        evidence_seen = _evidence_for(path)

        for name, worker in sorted(workers.items()):
            if name not in budget.leased:
                # No slot to free: either already released or never taken.
                if worker.state == "running":
                    # Classify BEFORE mutating. classify() returns worker.state
                    # unchanged for anything not "running", so asking it after
                    # the assignment made every report say "reclaimed" and
                    # erased the crashed-before-starting vs killed-mid-run
                    # distinction this report exists to provide.
                    verdict = classify(worker, stale_seconds=stale_seconds, now=now)
                    worker.state = "reclaimed"
                    worker.note = "reconciled: registered with no lease"
                    released.append({"name": name, "reason": "no_lease",
                                     "liveness": verdict})
                else:
                    consistent.append({"name": name, "state": worker.state})
                continue

            verdict = classify(worker, stale_seconds=stale_seconds, now=now)
            has_evidence = bool(evidence_seen.get(name))

            # A worker that already reached a terminal state cannot be alive,
            # so a lease it still holds is orphaned by definition. Without this
            # branch a crash between `finish` and `release` leaks the slot
            # forever and the run eventually deadlocks on CONCURRENCY_LIMIT.
            if worker.state != "running":
                budget.leased.pop(name, None)
                released.append({"name": name,
                                 "reason": "finished_without_release",
                                 "liveness": verdict,
                                 "evidence": has_evidence})
            elif verdict in (STALE, TIMEOUT) or (has_evidence and verdict == WEDGED):
                reason = ("finished_without_release" if has_evidence
                          else "no_heartbeat")
                worker.state = "reclaimed" if verdict == STALE else "timeout"
                if has_evidence and verdict != TIMEOUT:
                    worker.state = "ok"
                worker.note = f"reconciled: {reason}"
                budget.leased.pop(name, None)
                released.append({"name": name, "reason": reason,
                                 "liveness": verdict,
                                 "evidence": has_evidence})
            else:
                consistent.append({"name": name, "liveness": verdict})

        if released:
            registry.save(workers)
            budget.persist(path)

    return {"released": released, "consistent": consistent}


def _evidence_for(run_dir: Path) -> dict[str, int]:
    """Workstreams that have at least one passing evidence record.

    Used only as a tie-breaker: a worker that has already produced a passing
    result and merely failed to tidy up is finished, not stuck.

    Keyed by **workstream**, not criterion id. A criterion is named for what it
    proves (``build-exits-0``); the thing that produced it is a *worker*. Joining
    the two on the criterion id is what made the headline reconciliation case
    unreachable — lookups by worker name never matched, so "did its job, died
    before tidying up" was reported as nothing to reconcile. Falls back to the
    criterion id only when a criterion declares no workstream.
    """
    from . import gate as _gate  # local import: gate is not otherwise needed here

    try:
        result = _gate.evaluate(Path(run_dir))
    except SwarmError:
        return {}
    seen: dict[str, int] = {}
    for r in result.results:
        if not (r.passed and r.evidence_count):
            continue
        key = getattr(r.criterion, "workstream", None) or r.criterion.id
        seen[key] = seen.get(key, 0) + r.evidence_count
    return seen


def reclaim(run_dir: Path, *, limits: Limits | None = None,
            stale_seconds: float = DEFAULT_STALE_SECONDS,
            names: list[str] | None = None,
            now: float | None = None,
            include_wedged: bool = False) -> dict[str, Any]:
    """Free the slots of dead workers and mark them reclaimed.

    Registry and budget are updated inside one ``counters.lock`` critical
    section, so a reclaim can never interleave with a spawn and leave a worker
    holding a slot that no longer exists (or a slot freed twice).

    ``names`` restricts the sweep to specific workers. With no names, every
    worker currently classified ``stale`` or ``timeout`` is reclaimed. A worker
    that is merely ``slow`` is never reclaimed - beating means alive.

    ``wedged`` is also never reclaimed unless ``include_wedged=True``. A
    wedged worker is provably alive, only unproductive; killing one by default
    is exactly how a legitimate long-running task gets destroyed by a monitor
    that confused "slow" with "stuck". Reclaiming a wedged worker stays a
    deliberate, explicit act.
    """
    now = time.time() if now is None else now
    registry = Registry(run_dir)
    reclaimed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    with file_lock(Path(run_dir) / COUNTERS_LOCK, timeout=LOCK_WAIT_SECONDS):
        workers = registry.load()
        budget = Budget.load(Path(run_dir), limits)
        targets: list[str] = []
        for name, worker in workers.items():
            if names is not None and name not in names:
                continue
            if worker.state != "running":
                continue
            verdict = classify(worker, stale_seconds=stale_seconds, now=now)
            if verdict in (STALE, TIMEOUT) or (
                include_wedged and verdict == WEDGED
            ):
                targets.append(name)
            else:
                skipped.append({"name": name, "liveness": verdict})

        for name in sorted(targets):
            worker = workers[name]
            verdict = classify(worker, stale_seconds=stale_seconds, now=now)
            # Record the truth, not a derived guess. A wedged worker never
            # exceeded its timeout, so recording "timeout" (and a note saying
            # it did) left `workers status` lying about it permanently:
            # classify() short-circuits on a non-running state, so the false
            # record was reported forever.
            if verdict == STALE:
                worker.state, worker.note = "reclaimed", f"no heartbeat for {stale_seconds:.0f}s"
            elif verdict == WEDGED:
                worker.state = "reclaimed"
                worker.note = (
                    f"no forward progress for {DEFAULT_PROGRESS_STALE_SECONDS:.0f}s "
                    "while still beating"
                )
            else:
                worker.state = "timeout"
                worker.note = f"exceeded timeout_s={worker.timeout_s:.0f}s"
            # Force-free the slot: the lease is by definition held by a process
            # that is gone, and the epoch check would refuse our own release.
            budget.leased.pop(name, None)
            reclaimed.append(
                {"name": name, "liveness": verdict, "attempt": worker.attempt}
            )
        if targets:
            registry.save(workers)
            budget.persist(Path(run_dir))

    return {"reclaimed": reclaimed, "skipped": skipped}


@dataclass
class SpawnResult:
    worker: Worker
    lease: dict[str, Any] = field(default_factory=dict)
    #: Bounded exponential backoff before the NEXT attempt of this worker.
    #: Attempt 1 is the first spawn, so it is 0: there is nothing to back off
    #: from yet. Reported here rather than left in a local so the caller
    #: actually waits instead of hot-looping on a doomed worker.
    retry_delay: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker": self.worker.to_dict(),
            "lease": self.lease,
            "retry_delay": self.retry_delay,
        }


def spawn(run_dir: Path, name: str, *, role: str = "",
          timeout_s: float = DEFAULT_TIMEOUT_S,
          limits: Limits | None = None, pid: int | None = None,
          now: float | None = None) -> SpawnResult:
    """Register a worker AND take its concurrency slot, atomically.

    This is the real spawn path. Doing both under one lock is what stops a
    crash between the two steps from leaving a registered worker with no slot,
    or a leased slot with no registry entry - both of which deadlock or leak.
    The retry cap is charged here too, so retries cannot skip the budget.
    """
    run_dir = Path(run_dir)
    with file_lock(run_dir / COUNTERS_LOCK, timeout=LOCK_WAIT_SECONDS):
        budget = Budget.load(run_dir, limits)
        registry = Registry(run_dir)
        existing = registry.load().get(name)
        if existing is not None and existing.state == "running":
            verdict = classify(existing, now=now)
            if verdict in (ALIVE, SLOW):
                raise SwarmError(
                    f"worker {name!r} attempt {existing.attempt} is {verdict}; "
                    f"refusing to spawn it again (duplicate execution)",
                    code="DUPLICATE_WORKER",
                )
        # Charge the retry *before* the attempt so an exhausted budget stops
        # the retry instead of letting it start and then killing it.
        if existing is not None:
            attempt = existing.attempt + 1
            budget.record_retry(name)
        else:
            attempt = 1
        worker = registry.register(
            name, role=role, timeout_s=timeout_s, pid=pid, now=now
        )
        worker.attempt = attempt
        # Free the dead predecessor's slot before taking the new one. Without
        # this every retry dies on "already leased": the registry knows the old
        # attempt is gone, but the budget still counts its slot, so the retry
        # path could never actually run.
        if existing is not None:
            budget.leased.pop(name, None)
        lease = budget.lease(
            name, role=role, run_dir=None, epoch=worker.epoch,
            attempt=attempt, timeout_s=timeout_s,
        )
        registry.save(registry.load())
        budget.persist(run_dir)

    delay = retry_delay(attempt)
    return SpawnResult(worker=worker, lease=lease, retry_delay=delay)

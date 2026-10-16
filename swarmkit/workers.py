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

VALID_STATES = ("running",) + DONE

DEFAULT_TIMEOUT_S = 900.0

#: No beat for this long means the worker is presumed dead.
DEFAULT_STALE_SECONDS = 120.0

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


def reclaim(run_dir: Path, *, limits: Limits | None = None,
            stale_seconds: float = DEFAULT_STALE_SECONDS,
            names: list[str] | None = None,
            now: float | None = None) -> dict[str, Any]:
    """Free the slots of dead workers and mark them reclaimed.

    Registry and budget are updated inside one ``counters.lock`` critical
    section, so a reclaim can never interleave with a spawn and leave a worker
    holding a slot that no longer exists (or a slot freed twice).

    ``names`` restricts the sweep to specific workers. With no names, every
    worker currently classified ``stale`` or ``timeout`` is reclaimed. A worker
    that is merely ``slow`` is never reclaimed - beating means alive.
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
            if verdict in (STALE, TIMEOUT):
                targets.append(name)
            else:
                skipped.append({"name": name, "liveness": verdict})

        for name in sorted(targets):
            worker = workers[name]
            verdict = classify(worker, stale_seconds=stale_seconds, now=now)
            worker.state = "reclaimed" if verdict == STALE else "timeout"
            worker.note = (
                f"no heartbeat for {stale_seconds:.0f}s"
                if verdict == STALE
                else f"exceeded timeout_s={worker.timeout_s:.0f}s"
            )
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
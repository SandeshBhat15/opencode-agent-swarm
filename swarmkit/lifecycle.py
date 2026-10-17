"""Session-bound execution lifecycle.

Every other module in swarmkit treats a run as something the manager remembers
between CLI invocations. That is enough for a turn-keyed conversation, but it is
not enough once work is allowed to outlive the turn that started it, and this
module exists to make that outliving *safe* rather than accidental.

Three failures are being prevented, and each one is a real way this kind of
system hurts a user:

* **Orphaned work.** A task keeps running after the chat that asked for it is
  closed. The user closed the window; the process is still burning tokens and
  still writing files. So a task is bound to an owning session id and dies with
  it (:func:`session_close`), and the whole application dying takes every task
  it owned with it (:func:`shutdown`).
* **Work lost to a crash.** The kill was correct but the state was only ever
  held in memory. So every meaningful transition is written with
  :func:`store.atomic_write_json` and *also* appended to a durable journal
  (:meth:`Task.checkpoint`), and :func:`recover` can rebuild the task from the
  last checkpoint that actually reached disk.
* **Work restarted by accident.** The application reopens and a recovery
  routine helpfully resumes everything it finds. That is worse than not
  resuming, because the user did not ask. So recovery only ever *marks*
  interrupted work; turning it back into running work requires an explicit
  :meth:`Task.resume` (:func:`resume_plan` reports the candidate, it does not
  start it).

The states are deliberately more than a boolean. ``CANCELLING`` exists so that
"stop asked for, processes not yet dead" is a state a caller can observe and
wait on, and ``INTERRUPTED`` is distinct from ``PAUSED`` because the first means
*we did not choose to stop* and the second means *somebody deliberately did* --
which is the difference between a resume being routine and a resume needing a
verification pass.

**Ownership is never inferred from a pid.** ``record.processes`` is the only
source of pids this module will terminate, and each entry carries the process
birth time captured at registration (:func:`process_birth`). If the birth time
no longer matches, the pid was recycled by the OS and the number now belongs to
somebody else's program, so termination is refused rather than attempted. The
same reasoning is why cancellation never unlinks anything: saved state and
finished artifacts outlive the session that produced them until the retention
window expires.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import store
from .errors import SwarmError

#: Where task records live. Deliberately beside ``.opencode/swarm`` rather than
#: inside it: run directories are owned by ``store`` and are pruned by the
#: retention policy there, and a task record must not disappear when its run
#: directory is reconciled.
LIFECYCLE_ROOT = Path(".opencode") / "swarm-lifecycle"
TASKS_SUBDIR = "tasks"
LOCK_FILE = "lifecycle.lock"

RUNNING = "RUNNING"
CANCELLING = "CANCELLING"
PAUSED = "PAUSED"
INTERRUPTED = "INTERRUPTED"
RESUMING = "RESUMING"
BLOCKED = "BLOCKED"
FAILED = "FAILED"
COMPLETED = "COMPLETED"

STATES = (
    RUNNING, CANCELLING, PAUSED, INTERRUPTED, RESUMING, BLOCKED, FAILED, COMPLETED,
)

#: States in which the task still has work in flight and must be stopped.
LIVE_STATES = frozenset({RUNNING, CANCELLING, RESUMING})

#: States a user may explicitly ask to continue from.
RESUMABLE_STATES = frozenset({PAUSED, INTERRUPTED, BLOCKED, FAILED})

#: Nothing leaves ``COMPLETED``. It is the one state with no way back, because
#: a completed task is the only one whose artifacts may be trusted downstream.
TERMINAL_STATES = frozenset({COMPLETED})

#: Legal moves. Anything not listed is refused, which is what stops a stale
#: writer from dragging an interrupted task back to ``RUNNING`` after a resume
#: has already fenced it.
TRANSITIONS: dict[str, frozenset[str]] = {
    # RUNNING -> INTERRUPTED is the crash path: the owner died without getting
    # to stop anything. It is not optional. Without this edge a crash left the
    # task claiming to still be running, which is the state that gets "resumed"
    # later by something that assumes the work is live.
    RUNNING: frozenset({CANCELLING, PAUSED, INTERRUPTED, BLOCKED, FAILED, COMPLETED}),
    CANCELLING: frozenset({PAUSED, INTERRUPTED, FAILED}),
    PAUSED: frozenset({RESUMING, CANCELLING, INTERRUPTED, FAILED}),
    INTERRUPTED: frozenset({RESUMING, CANCELLING, FAILED}),
    RESUMING: frozenset({RUNNING, CANCELLING, INTERRUPTED, BLOCKED, FAILED}),
    BLOCKED: frozenset({RUNNING, CANCELLING, FAILED}),
    FAILED: frozenset({RESUMING, CANCELLING}),
    COMPLETED: frozenset(),
}

#: How long a process gets to exit after the polite signal before it is killed.
#: Bounded on purpose: "eventually" is not a duration, and a shutdown that can
#: hang forever is a shutdown the user cannot escape.
TERMINATE_GRACE_SECONDS = 5.0

#: Records older than this are eligible for :func:`prune`. Artifact files are
#: never touched by pruning -- only the bookkeeping record goes.
RETENTION_DAYS = 30

#: How many steps between automatic checkpoints. Checkpoints also happen on
#: every state change; this is the continuous part that covers a crash *during*
#: a long run, where no state change ever happens.
CHECKPOINT_EVERY = 5

MAX_GOAL_CHARS = 600


class LifecycleError(SwarmError):
    """Lifecycle refusal. Subclasses SwarmError so the CLI reports it as JSON."""


def lifecycle_root(workspace: Path) -> Path:
    return Path(workspace) / LIFECYCLE_ROOT


def tasks_root(workspace: Path) -> Path:
    return lifecycle_root(workspace) / TASKS_SUBDIR


def lock_path(workspace: Path) -> Path:
    return lifecycle_root(workspace) / LOCK_FILE


def _locked(workspace: Path) -> Any:
    """Session-bound writes take the bounded-wait tier, not the fail-fast one.

    Several OpenCode sessions can have tasks alive in one project at the same
    time, and each of them will be writing its own record on every checkpoint.
    ``store.mutation`` is deliberately fail-fast because its callers have
    already passed ``require_clean``; these writes are short append-style
    critical sections racing legitimately, which is exactly the case
    ``file_lock`` waits for.
    """
    return store.file_lock(lock_path(workspace), timeout=store.LOCK_WAIT_SECONDS)


# ---------------------------------------------------------------------------
# process identity
# ---------------------------------------------------------------------------


def process_birth(pid: int) -> float | None:
    """When this pid started, as epoch seconds. ``None`` when unknowable.

    This is the pid-reuse guard. A pid on its own identifies a *slot*, not a
    process; between registering a child and cancelling it, the child can exit
    and the OS can hand the same number to something unrelated. Comparing the
    birth time recorded at registration against the one read now is what makes
    "terminate the processes this task owns" true rather than approximately
    true.

    Windows uses ``GetProcessTimes`` (the same kernel-query family ``store``
    relies on). Linux exposes starttime in ``/proc/<pid>/stat`` field 22.
    Platforms that offer neither return ``None``, and the caller refuses to
    terminate rather than guessing.
    """
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes

            QUERY = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(QUERY, False, pid)
            if not handle:
                return None
            try:
                creation = ctypes.c_ulonglong()
                exit_time = ctypes.c_ulonglong()
                kernel_time = ctypes.c_ulonglong()
                user_time = ctypes.c_ulonglong()
                ok = kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel_time),
                    ctypes.byref(user_time),
                )
                if not ok:
                    return None
                # FILETIME counts 100ns intervals since 1601-01-01.
                return creation.value / 1e7 - 11644473600.0
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return None
    # Field 2 is comm and may contain spaces/parens, so split after the last ')'.
    tail = raw[raw.rfind(")") + 1:].split()
    if len(tail) < 20:
        return None
    try:
        ticks = int(tail[19])  # field 22 overall == index 19 after state
        hz = os.sysconf("SC_CLK_TCK")
        with open("/proc/uptime", encoding="utf-8") as fh:
            uptime = float(fh.read().split()[0])
        return time.time() - (uptime - ticks / hz)
    except (OSError, ValueError, IndexError, TypeError):
        return None


def _spawn_process(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    task_id: str,
    epoch: str,
    env_extra: dict[str, str] | None = None,
    capture: bool = False,
) -> tuple[subprocess.Popen[bytes], dict[str, Any]]:
    """Start a child that belongs to ``task_id`` and hand back its record.

    The child is put in its **own process group** so that the whole tree can be
    reached with one signal later. Signalling the group is the dependency-free
    way to do this: enumerating children portably needs a library this project
    does not have, but a child spawned in a new group is by definition the root
    of a tree we are allowed to signal.
    """
    env = dict(os.environ)
    # The child carries its ownership in its environment so that anything which
    # can read it (a watchdog, a log, a crash report) can tell which task it
    # belonged to. This is also what makes an unexplained survivor diagnosable
    # instead of mysterious.
    env["SWARM_TASK_ID"] = task_id
    env["SWARM_TASK_EPOCH"] = epoch
    if env_extra:
        env.update(env_extra)
    kwargs: dict[str, Any] = {"cwd": str(cwd) if cwd else None, "env": env}
    if capture:
        # Without pipes a caller has no way to read what the child produced, and
        # the process would have to be waited on for its exit status. Pipes keep
        # stdout/stderr off the inherited console so a worker cannot scribble
        # over the host application's own output.
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if os.name == "nt":
        kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
        )
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603 - cmd is caller-supplied
    try:
        pgid: int | None = os.getpgid(proc.pid)
    except (OSError, AttributeError):
        pgid = None
    record = {
        "pid": proc.pid,
        "pgid": pgid,
        "name": os.path.basename(cmd[0]) if cmd else "child",
        "cmd": list(cmd),
        "task_id": task_id,
        "epoch": epoch,
        "birth": process_birth(proc.pid),
        "registered_at": store.utcnow(),
        "state": "running",
    }
    return proc, record


def _signal_tree(entry: dict[str, Any], *, force: bool) -> bool:
    """Signal one registered process, and its tree. Never raises."""
    pid = int(entry.get("pid") or 0)
    if pid <= 0:
        return False
    if os.name == "nt":
        # /T takes the children with it; /F is the forced variant.
        args = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            args.append("/F")
        try:
            return subprocess.run(  # noqa: S603
                args, capture_output=True, timeout=30, check=False
            ).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    pgid = entry.get("pgid")
    if isinstance(pgid, int) and pgid > 0:
        try:
            os.killpg(pgid, signal.SIGKILL if force else signal.SIGTERM)
            return True
        except (OSError, AttributeError):
            pass
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        return True
    except (OSError, ProcessLookupError):
        return False


# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------


@dataclass
class TaskRecord:
    """Everything needed to stop, or to continue, one task."""

    task_id: str = ""
    session_id: str = ""
    parent_task_id: str | None = None
    project: str = ""
    goal: str = ""
    state: str = RUNNING
    epoch: str = ""
    phase: str = "INTAKE"
    plan: list[dict[str, Any]] = field(default_factory=list)
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    completed_work: list[str] = field(default_factory=list)
    worker_results: dict[str, Any] = field(default_factory=dict)
    retries: dict[str, int] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    processes: list[dict[str, Any]] = field(default_factory=list)
    total_steps: int = 0
    checkpoint_seq: int = 0
    last_checkpoint_at: str = ""
    owner_pid: int | None = None
    created_at: str = ""
    updated_at: str = ""
    finished_at: str = ""
    observed: dict[str, int] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TaskRecord":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in raw.items() if k in known}
        try:
            return cls(**clean)
        except (TypeError, ValueError) as exc:
            raise LifecycleError(
                f"malformed task record: {exc}", code="CORRUPT_TASK"
            ) from exc

    @property
    def completed_steps(self) -> int:
        """Counted from the plan, never guessed from an event count."""
        if self.plan:
            done = set(self.completed_work)
            return sum(1 for step in self.plan if step.get("id") in done)
        return len(self.completed_work)

    @property
    def running_workers(self) -> list[dict[str, Any]]:
        return [p for p in self.processes if p.get("state") == "running"]


def assert_transition(current: str, target: str) -> None:
    if target not in STATES:
        raise LifecycleError(
            f"{target!r} is not a lifecycle state; expected one of {', '.join(STATES)}",
            code="BAD_STATE",
        )
    if current == target:
        return
    allowed = TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise LifecycleError(
            f"cannot move task from {current} to {target}; "
            f"legal moves from {current}: "
            f"{', '.join(sorted(allowed)) or '(none, terminal)'}",
            code="BAD_TRANSITION",
        )


# ---------------------------------------------------------------------------
# the task
# ---------------------------------------------------------------------------


class Task:
    """One task bound to one OpenCode session, stored under ``workspace``."""

    def __init__(self, workspace: Path, record: TaskRecord) -> None:
        self.workspace = Path(workspace)
        self.record = record
        # Live Popen handles for children this process started. Kept beside the
        # record rather than inside it because a handle is meaningless across
        # processes, and the record has to stay JSON. Its only job is reaping:
        # signalling a pid stops the process, but only wait() clears the child
        # from the table, and an unreaped child is a zombie on POSIX and a
        # ResourceWarning on Windows.
        self._procs: dict[int, subprocess.Popen[bytes]] = {}

    # -- paths ----------------------------------------------------------
    @property
    def dir(self) -> Path:
        return tasks_root(self.workspace) / self.record.task_id

    @property
    def file(self) -> Path:
        return self.dir / "task.json"

    @property
    def journal_file(self) -> Path:
        return self.dir / "checkpoints.jsonl"

    @classmethod
    def path_for(cls, workspace: Path, task_id: str) -> Path:
        return tasks_root(workspace) / task_id / "task.json"

    @classmethod
    def load(cls, workspace: Path, task_id: str) -> "Task":
        path = cls.path_for(workspace, task_id)
        raw = store.read_json(path)
        if raw is None:
            raise LifecycleError(f"no task {task_id!r} on record", code="NO_SUCH_TASK")
        if not isinstance(raw, dict):
            raise LifecycleError(f"{path} is not a JSON object", code="CORRUPT_TASK")
        return cls(workspace, TaskRecord.from_dict(raw))

    @classmethod
    def exists(cls, workspace: Path, task_id: str) -> bool:
        return cls.path_for(workspace, task_id).exists()

    @classmethod
    def create(
        cls,
        workspace: Path,
        *,
        task_id: str,
        session_id: str,
        goal: str,
        project: str = "",
        parent_task_id: str | None = None,
        plan: list[dict[str, Any]] | None = None,
        total_steps: int | None = None,
    ) -> "Task":
        """Bind a new task to ``session_id``.

        An explicit ``task_id`` is honoured rather than generated so that a
        caller retrying after a crash re-attaches to the same record instead of
        orphaning the previous attempt.
        """
        workspace = Path(workspace)
        task_id = (task_id or "").strip() or f"t-{uuid.uuid4().hex[:12]}"
        if not session_id:
            raise LifecycleError(
                "a task must name the session that owns it", code="NO_SESSION"
            )
        path = cls.path_for(workspace, task_id)
        if path.exists():
            raise LifecycleError(
                f"task {task_id!r} already exists at {path}", code="TASK_EXISTS"
            )
        plan = list(plan or [])
        deps: dict[str, list[str]] = {}
        for step in plan:
            if step.get("id"):
                deps[str(step["id"])] = [str(d) for d in step.get("depends_on") or []]
        stamp = store.utcnow()
        record = TaskRecord(
            task_id=task_id,
            session_id=session_id,
            parent_task_id=parent_task_id or None,
            project=project or str(workspace),
            goal=(goal or "").strip()[:MAX_GOAL_CHARS],
            epoch=uuid.uuid4().hex,
            plan=plan,
            dependencies=deps,
            total_steps=int(total_steps if total_steps is not None else len(plan)),
            owner_pid=os.getpid(),
            created_at=stamp,
            updated_at=stamp,
        )
        task = cls(workspace, record)
        with _locked(workspace):
            task.save()
            task.journal(
                {"event": "created", "session_id": session_id, "state": RUNNING,
                 "epoch": record.epoch}
            )
        return task

    @classmethod
    def all(cls, workspace: Path) -> list["Task"]:
        """Every readable record. A single corrupt file must not hide the rest."""
        out: list[Task] = []
        root = tasks_root(workspace)
        if not root.is_dir():
            return out
        for entry in sorted(root.iterdir()):
            candidate = entry / "task.json"
            if not candidate.is_file():
                continue
            raw = store.read_json(candidate)
            if isinstance(raw, dict):
                try:
                    out.append(cls(workspace, TaskRecord.from_dict(raw)))
                except SwarmError:
                    # A record we cannot parse is a recovery problem for that
                    # one task. Skipping it keeps every other task resumable.
                    continue
        return out

    @classmethod
    def for_session(cls, workspace: Path, session_id: str) -> list["Task"]:
        return [t for t in cls.all(workspace) if t.record.session_id == session_id]

    # -- persistence ----------------------------------------------------
    def save(self) -> None:
        self.record.updated_at = store.utcnow()
        store.atomic_write_json(self.file, self.record.to_dict())

    def journal(self, entry: dict[str, Any]) -> None:
        """Append one durable line. Torn trailing lines are skipped on read."""
        store.append_jsonl(self.journal_file, entry)

    def checkpoints(self) -> list[dict[str, Any]]:
        return store.read_jsonl(self.journal_file)

    def checkpoint(self, **fields: Any) -> dict[str, Any]:
        """Write a durable checkpoint and record it in the journal.

        The record write is atomic, but atomicity is not enough: a crash
        *between* the write and the journal append would leave the journal
        behind the record, and recovery would then trust a stale record. So the
        journal append carries a monotonic ``seq`` and recovery replays
        anything newer than the record's own ``checkpoint_seq`` -- which makes
        the journal, not the record, the authority on what was last confirmed.
        """
        self.record.checkpoint_seq += 1
        self.record.last_checkpoint_at = store.utcnow()
        entry = {
            "seq": self.record.checkpoint_seq,
            "at": self.record.last_checkpoint_at,
            "epoch": self.record.epoch,
            "state": self.record.state,
            "phase": self.record.phase,
            "completed_work": list(self.record.completed_work),
            "worker_results": dict(self.record.worker_results),
            "retries": dict(self.record.retries),
            "budget": dict(self.record.budget),
            "artifacts": list(self.record.artifacts),
            "processes": [dict(p) for p in self.record.processes],
            "total_steps": self.record.total_steps,
        }
        entry.update(fields)
        with _locked(self.workspace):
            self.journal(entry)
            self.save()
        return entry

    def last_confirmed(self) -> dict[str, Any] | None:
        """Newest journal entry with the highest ``seq``."""
        best: dict[str, Any] | None = None
        for entry in self.checkpoints():
            if not isinstance(entry.get("seq"), int):
                continue
            if best is None or entry["seq"] > best["seq"]:
                best = entry
        return best

    # -- state ----------------------------------------------------------
    def set_state(self, target: str, *, reason: str = "", checkpoint: bool = True) -> "Task":
        """Move to ``target``, refusing moves that would misrepresent progress.

        Two guards are load-bearing. ``COMPLETED`` is unreachable while the
        task is cancelled or short of its step count -- a partially executed
        task must never be reported as done. And ``CANCELLING`` never leads to
        ``COMPLETED`` at all, which is why the transition table has no such
        edge even when every step happens to be checked off.
        """
        current = self.record.state
        assert_transition(current, target)
        if target == COMPLETED and not self.may_complete():
            raise LifecycleError(
                f"task {self.record.task_id!r} has "
                f"{self.record.completed_steps}/{self.record.total_steps} steps done "
                f"and state {current}; refusing to mark a partially executed task "
                f"complete. Cancel it and resume instead.",
                code="INCOMPLETE",
            )
        self.record.state = target
        if reason:
            self.record.reasons.append(f"{store.utcnow()} {current}->{target}: {reason}")
        if target in TERMINAL_STATES or target in {PAUSED, INTERRUPTED}:
            self.record.finished_at = store.utcnow()
        if checkpoint:
            self.checkpoint()
        else:
            self.save()
        return self

    def may_complete(self) -> bool:
        """Completion is earned, never declared."""
        if self.record.state in {CANCELLING, INTERRUPTED}:
            return False
        if self.record.completed_steps < self.record.total_steps:
            return False
        if any(p.get("state") == "running" for p in self.record.processes):
            return False
        return True

    def require_live_epoch(self, epoch: str) -> None:
        """Fence a writer that belongs to a superseded attempt.

        Cancellation and resume both mint a new epoch. A worker still running
        from the previous one will eventually try to write a result, and that
        write has to fail loudly: accepting it would let a cancelled task
        resurrect itself through the back door.
        """
        if epoch and self.record.epoch and epoch != self.record.epoch:
            raise LifecycleError(
                f"task {self.record.task_id!r} is at epoch {self.record.epoch}, "
                f"but a write arrived for epoch {epoch}. This attempt was "
                f"superseded; its result must not be recorded.",
                code="EPOCH_MISMATCH",
            )

    # -- ownership ------------------------------------------------------
    def adopt(self, session_id: str) -> "Task":
        """Re-own this task under a (possibly new) session.

        Used when a user resumes in a fresh session: the work keeps its task id,
        its completed steps and its artifacts, and only the owner changes.
        """
        if not session_id:
            raise LifecycleError(
                "a task must name the session that owns it", code="NO_SESSION"
            )
        self.record.session_id = session_id
        self.record.owner_pid = os.getpid()
        self.checkpoint(session_id=session_id, event="adopted")
        return self

    # -- progress -------------------------------------------------------
    def note(self, event: str) -> dict[str, Any]:
        """Record one real event from the host application.

        Only counters are touched. Nothing here invents a step, and an event
        name the host emits that we do not model is still counted rather than
        silently dropped -- an unrecognised event is evidence, not noise.
        """
        key = str(event or "unknown")
        self.record.observed[key] = self.record.observed.get(key, 0) + 1
        stamp = store.utcnow()
        self.journal({"at": stamp, "event": "note", "name": key, "epoch": self.record.epoch})
        self.save()
        return dict(self.record.observed)

    def complete_step(self, step_id: str, *, result: Any = None, epoch: str = "") -> "Task":
        """Mark one planned step done and record its result."""
        if not step_id:
            raise LifecycleError("a step needs an id", code="BAD_STEP")
        self.require_live_epoch(epoch)
        if self.record.state == CANCELLING:
            raise LifecycleError(
                f"task {self.record.task_id!r} is cancelling; refusing to record "
                f"progress for step {step_id!r}",
                code="TASK_CANCELLED",
            )
        # Progress may only be recorded by a task that is actually executing.
        # A PAUSED or INTERRUPTED task is stopped, so a step arriving for it
        # means either a survivor from a previous attempt or a retry loop that
        # has not noticed the stop. Both are exactly what cancellation has to
        # defeat, and letting them write would rebuild the progress count the
        # user was told was preserved *at the point they stopped*.
        if self.record.state not in {RUNNING, RESUMING}:
            raise LifecycleError(
                f"task {self.record.task_id!r} is {self.record.state}; refusing to "
                f"record progress for step {step_id!r}. A stopped task does not "
                f"execute, so a step cannot have completed in it. Resume first.",
                code="TASK_NOT_RUNNING",
            )
        unmet = self.unmet_dependencies(step_id)
        if unmet:
            raise LifecycleError(
                f"step {step_id!r} depends on unfinished step(s): "
                f"{', '.join(unmet)}",
                code="DEPENDENCY_UNMET",
            )
        if step_id not in self.record.completed_work:
            self.record.completed_work.append(step_id)
        if result is not None:
            self.record.worker_results[step_id] = result
        # Continuous checkpointing: the crash we care about is the one that
        # happens mid-step, with no state change to trigger anything.
        if self.record.completed_steps % CHECKPOINT_EVERY == 0:
            self.checkpoint()
        else:
            self.save()
        return self

    def unmet_dependencies(self, step_id: str) -> list[str]:
        done = set(self.record.completed_work)
        return [
            dep
            for dep in self.record.dependencies.get(step_id, [])
            if dep not in done
        ]

    def progress(self) -> dict[str, Any]:
        """Real progress only.

        Every number here is derived from the record: steps come from the plan
        and the completed set, workers come from the process registry. There is
        no estimate, no percentage and no wording that could be mistaken for
        work that did not happen.
        """
        rec = self.record
        return {
            "task_id": rec.task_id,
            "state": rec.state,
            "phase": rec.phase,
            "completed_steps": rec.completed_steps,
            "total_steps": rec.total_steps,
            "running_workers": [dict(p) for p in rec.running_workers],
            "epoch": rec.epoch,
            "session_id": rec.session_id,
            "observed": dict(rec.observed),
        }

    # -- subprocesses ---------------------------------------------------
    def spawn(
        self, cmd: list[str], *, cwd: Path | None = None, name: str = "",
        env_extra: dict[str, str] | None = None, capture: bool = False,
    ) -> tuple[subprocess.Popen[bytes], dict[str, Any]]:
        """Start a child that belongs to this task and to no other."""
        proc, entry = _spawn_process(
            cmd, cwd=cwd, task_id=self.record.task_id,
            epoch=self.record.epoch, env_extra=env_extra, capture=capture,
        )
        if name:
            entry["name"] = name
        self.record.processes.append(entry)
        self._procs[proc.pid] = proc
        self.checkpoint()
        return proc, entry

    def finish_process(self, pid: int, *, state: str = "exited") -> bool:
        """Retire one owned child. Idempotent, and only ever touches our own."""
        for entry in self.record.processes:
            if int(entry.get("pid") or -1) == int(pid):
                entry["state"] = state
                entry["finished_at"] = store.utcnow()
                self.save()
                return True
        return False

    def terminate_processes(
        self, *, grace_s: float = TERMINATE_GRACE_SECONDS
    ) -> dict[str, Any]:
        """Stop every child this task owns. Graceful first, then forced.

        Refusal to guess is the rule. A pid whose birth time no longer matches
        the one captured at registration has been recycled by the OS and is
        reported as ``refused`` without being signalled, because a task that
        cancels itself cleanly is worth much less than one that does not kill an
        unrelated program on the way out.
        """
        stopped: list[int] = []
        forced: list[int] = []
        refused: list[dict[str, Any]] = []
        for entry in self.record.processes:
            if entry.get("state") != "running":
                continue
            pid = int(entry.get("pid") or 0)
            if pid <= 0:
                continue
            if not store.pid_alive(pid):
                entry["state"] = "gone"
                continue
            birth = entry.get("birth")
            now_birth = process_birth(pid)
            if birth is None or now_birth is None:
                refused.append({
                    "pid": pid,
                    "why": "cannot verify process identity on this platform",
                })
                continue
            if abs(float(now_birth) - float(birth)) > 2.0:
                refused.append({
                    "pid": pid,
                    "why": "pid was recycled by another process",
                })
                continue
            _signal_tree(entry, force=False)
            deadline = time.monotonic() + max(0.0, grace_s)
            while time.monotonic() < deadline and store.pid_alive(pid):
                time.sleep(store.LOCK_POLL_SECONDS)
            if store.pid_alive(pid):
                _signal_tree(entry, force=True)
                forced.append(pid)
            stopped.append(pid)
            entry["state"] = "terminated"
        for entry in self.record.processes:
            if entry.get("state") == "running" and not store.pid_alive(
                int(entry.get("pid") or 0)
            ):
                entry["state"] = "gone"
        # Reap anything we hold a handle for. Signalling stopped the process;
        # this is what actually clears it, so a cancelled task leaves no zombie
        # and no dangling-child warning behind.
        for pid, proc in list(self._procs.items()):
            try:
                proc.wait(timeout=max(0.0, grace_s))
            except (subprocess.TimeoutExpired, OSError, ValueError):
                continue
            finally:
                self._procs.pop(pid, None)
        self.save()
        return {
            "stopped": stopped,
            "forced": forced,
            "refused": refused,
            "grace_s": grace_s,
        }

    # -- cancellation ---------------------------------------------------
    def cancel(self, *, reason: str = "session closed") -> dict[str, Any]:
        """Stop this task now. Idempotent, and destructive to nothing.

        A second call returns the same answer rather than failing: cancellation
        is requested from at least three independent places (the session event,
        the application shutdown hook, and the user) and the race between them
        is normal, not exceptional.
        """
        rec = self.record
        if rec.state == CANCELLING:
            # A cancellation that was interrupted before it finished is not a
            # cancellation that completed. Re-running the termination is what
            # makes the operation idempotent in the sense that matters: calling
            # it again always lands the task in a stopped state rather than
            # leaving it stranded in CANCELLING where nothing will move it.
            result = self.terminate_processes()
            rec.state = PAUSED
            rec.reasons.append(
                f"{store.utcnow()} {CANCELLING}->{PAUSED}: cancellation completed "
                f"by a repeat request"
            )
            self.checkpoint(event="cancelled", reason=reason)
            report = self.cancel_report(reason=reason, already=True)
            report.update(result)
            return report
        if rec.state in {PAUSED, INTERRUPTED} and not rec.running_workers:
            # Already stopped. Make sure nothing restarted underneath us.
            self.checkpoint(event="cancel-noop")
            return self.cancel_report(reason=reason, already=True)
        assert_transition(rec.state, CANCELLING)
        rec.state = CANCELLING
        # Cancellation fences the previous attempt exactly as a resume does.
        # Without a new epoch, a worker still alive under the old one could
        # write a result into the task we just decided was stopped -- and the
        # cancelled work would resurrect itself through that write.
        rec.epoch = uuid.uuid4().hex
        rec.finished_at = store.utcnow()
        if reason:
            rec.reasons.append(f"{store.utcnow()} cancel: {reason}")
        # The checkpoint goes down BEFORE the processes are signalled, so that a
        # hard kill halfway through still leaves a record saying cancellation
        # was requested. The alternative loses the reason for a stop that
        # happened and is exactly the state we must not resume into.
        self.checkpoint(event="cancelling", reason=reason)
        result = self.terminate_processes()
        # After the processes are gone the task is PAUSED: stopped on purpose,
        # by a user, with its completed work intact.
        self.record.state = PAUSED
        self.record.reasons.append(
            f"{store.utcnow()} {CANCELLING}->{PAUSED}: cancellation complete"
        )
        self.checkpoint(event="cancelled", reason=reason)
        report = self.cancel_report(reason=reason, already=False)
        report.update(result)
        return report

    def cancel_report(self, *, reason: str, already: bool) -> dict[str, Any]:
        return {
            "task_id": self.record.task_id,
            "session_id": self.record.session_id,
            "state": self.record.state,
            "already": already,
            "reason": reason,
            "completed_steps": self.record.completed_steps,
            "total_steps": self.record.total_steps,
            "artifacts": list(self.record.artifacts),
            "running_workers": [dict(p) for p in self.record.running_workers],
        }

    # -- resume ---------------------------------------------------------
    def resume(self, *, session_id: str = "", hint: str = "") -> "Task":
        """Continue this task under ``session_id``. Explicit by construction.

        There is no code path in this module that moves a task out of
        ``PAUSED``/``INTERRUPTED`` other than this method. Recovery routines,
        the startup hook and the prompt hook all stop at reporting, so an
        application that merely reopens cannot restart anyone's work.
        """
        rec = self.record
        if rec.state == RUNNING:
            return self
        assert_transition(rec.state, RESUMING)
        rec.state = RESUMING
        # A new epoch fences every worker from the previous attempt. Without it
        # a survivor could write a result into the resumed task.
        rec.epoch = uuid.uuid4().hex
        if session_id:
            rec.session_id = session_id
        rec.owner_pid = os.getpid()
        rec.finished_at = ""
        if hint:
            rec.reasons.append(f"{store.utcnow()} resume hint: {hint}")
        self.checkpoint(event="resuming", hint=hint)
        rec.state = RUNNING
        self.checkpoint(event="resumed")
        return self

    def brief(self) -> str:
        """What a resuming agent needs, assembled from the record only."""
        rec = self.record
        remaining = [
            str(step.get("id"))
            for step in rec.plan
            if step.get("id") and step.get("id") not in set(rec.completed_work)
        ]
        lines = [
            f"task {rec.task_id} ({rec.state}, phase {rec.phase})",
            f"goal: {rec.goal or '(not recorded)'}",
            f"progress: {rec.completed_steps}/{rec.total_steps} steps complete",
        ]
        if remaining:
            lines.append(f"still to do: {', '.join(remaining)}")
        if rec.artifacts:
            lines.append(f"artifacts already produced: {', '.join(rec.artifacts)}")
        if rec.retries:
            lines.append(
                "prior retries: "
                + ", ".join(f"{k}x{v}" for k, v in sorted(rec.retries.items()))
            )
        blocked = [s for s in remaining if self.unmet_dependencies(s)]
        if blocked:
            lines.append(f"unmet dependencies remain for: {', '.join(blocked)}")
        lines.append(
            "verify the above before trusting it, then continue through "
            "implementation, QC, repair and delivery."
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# session / application scope
# ---------------------------------------------------------------------------


def session_close(workspace: Path, session_id: str, *, reason: str = "session closed") -> dict[str, Any]:
    """Stop the work this session owns, and nothing else.

    Two OpenCode sessions can be mid-task in the same project at the same time.
    Closing one is not a reason to stop the other, so the filter is the owning
    session id and every other session's records are left exactly as they were.
    """
    if not session_id:
        raise LifecycleError("session id required", code="NO_SESSION")
    affected = Task.for_session(workspace, session_id)
    reports = [t.cancel(reason=reason) for t in affected]
    return {
        "session_id": session_id,
        "reason": reason,
        "cancelled": len(reports),
        "tasks": reports,
        "untouched": [
            t.record.task_id
            for t in Task.all(workspace)
            if t.record.session_id != session_id
        ],
    }


def shutdown(workspace: Path, *, reason: str = "application shutdown") -> dict[str, Any]:
    """The application is going away. Stop everything it owned.

    Distinct from :func:`session_close` on purpose: closing one chat does not
    close the application, and the user's own instruction was not to pretend it
    did. This is the branch that runs on real shutdown, when every session in
    the process is going away together.
    """
    affected = [t for t in Task.all(workspace) if t.record.state in LIVE_STATES]
    reports = [t.cancel(reason=reason) for t in affected]
    return {
        "reason": reason,
        "cancelled": len(reports),
        "tasks": reports,
        "preserved": [
            t.record.task_id for t in Task.all(workspace) if t.record.state not in LIVE_STATES
        ],
    }


def recover(workspace: Path, *, reason: str = "startup") -> dict[str, Any]:
    """Mark work that was in flight as INTERRUPTED. Never resumes it.

    The owner pid is the signal. A task whose owner process is gone was cut off
    by a crash or a shutdown that we never got to clean up after, and the
    honest state for it is ``INTERRUPTED`` -- distinct from ``PAUSED`` so that a
    later resume knows to re-verify rather than trusting the last run.

    Recovery reads the journal first. If the crash happened between the atomic
    record write and the journal append, the record is behind; replaying every
    journal entry newer than ``checkpoint_seq`` restores the last state that
    actually reached disk, which is the whole point of having both.
    """
    found: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    replayed_count = 0
    for task in Task.all(workspace):
        rec = task.record
        if rec.state not in LIVE_STATES:
            continue
        # Replay BEFORE deciding whether the owner is gone. A crash between the
        # journal append and the record write leaves the record behind while the
        # owner pid still looks alive, and that record is exactly the one a
        # resume would otherwise trust. Reconciling first means the pid check
        # below is made against the state that actually reached disk.
        replayed = _replay_journal(task)
        replayed_count += int(replayed.get("replayed", 0))
        owner_alive = store.pid_alive(rec.owner_pid) if rec.owner_pid else False
        if owner_alive and rec.state != CANCELLING:
            continue  # genuinely still running elsewhere
        try:
            task.set_state(INTERRUPTED, reason=f"recovered: {reason}")
        except SwarmError as exc:
            # Report the skip instead of continuing in silence: a recovery that quietly
            # fails to recover is indistinguishable from one that had nothing to do, and
            # the user is left believing their work is safely interrupted when nothing did.
            rec.reasons.append(f"{store.utcnow()} recover skipped: {exc}")
            skipped.append({"task_id": rec.task_id, "state": rec.state, "why": str(exc)})
            task.save()
            continue
        found.append({
            "task_id": rec.task_id,
            "session_id": rec.session_id,
            "was": replayed.get("state", rec.state),
            "now": task.record.state,
            "checkpoint_seq": rec.checkpoint_seq,
            "replayed": replayed.get("replayed", 0),
            "completed_steps": rec.completed_steps,
            "total_steps": rec.total_steps,
        })
    return {"reason": reason, "recovered": len(found), "replayed": replayed_count,
            "tasks": found, "skipped": skipped}


def _replay_journal(task: Task) -> dict[str, Any]:
    """Fold any checkpoint newer than the record back into the record."""
    rec = task.record
    applied = 0
    for entry in task.checkpoints():
        seq = entry.get("seq")
        if not isinstance(seq, int) or seq <= rec.checkpoint_seq:
            continue
        for key in (
            "phase", "completed_work", "worker_results", "retries",
            "budget", "artifacts", "processes", "total_steps",
        ):
            if key in entry:
                setattr(rec, key, entry[key])
        rec.checkpoint_seq = seq
        rec.last_checkpoint_at = str(entry.get("at") or rec.last_checkpoint_at)
        applied += 1
    if applied:
        task.save()
    return {"replayed": applied, "state": rec.state}


def resume_plan(
    workspace: Path, *, session_id: str = "", project: str = "", hint: str = ""
) -> dict[str, Any]:
    """Find the unfinished task a "continue" is probably about. Start nothing.

    Returns ``resumed`` for one unambiguous match with a ready-to-use brief,
    ``ambiguous`` with the candidates when the user must choose, and ``none``
    when there is nothing to continue. The state is always untouched: the
    caller has to invoke :meth:`Task.resume` afterwards, which is what keeps
    "reopened the app" from being enough.
    """
    pool = [t for t in Task.all(workspace) if t.record.state in RESUMABLE_STATES]
    if project:
        scoped = [t for t in pool if _same_project(t.record.project, project)]
        if scoped:
            pool = scoped
    if hint:
        tokens = [w for w in _tokens(hint) if len(w) > 2]
        if tokens:
            scored = sorted(
                pool,
                key=lambda t: -_overlap(tokens, _tokens(t.record.goal)),
            )
            best = _overlap(tokens, _tokens(scored[0].record.goal)) if scored else 0
            if best > 0:
                pool = [t for t in scored if _overlap(tokens, _tokens(t.record.goal)) == best]
    if not pool:
        return {"status": "none", "task_id": None, "brief": ""}
    if len(pool) > 1:
        return {
            "status": "ambiguous",
            "task_id": None,
            "candidates": [
                {
                    "task_id": t.record.task_id,
                    "title": t.record.goal or t.record.task_id,
                    "state": t.record.state,
                    "progress": f"{t.record.completed_steps}/{t.record.total_steps}",
                }
                for t in pool
            ],
        }
    task = pool[0]
    return {
        "status": "resumed",
        "task_id": task.record.task_id,
        "state": task.record.state,
        "progress": f"{task.record.completed_steps}/{task.record.total_steps}",
        "brief": task.brief(),
    }


def _same_project(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except (OSError, ValueError):
        return a == b


def _tokens(text: str) -> list[str]:
    return [w for w in "".join(
        c.lower() if c.isalnum() else " " for c in (text or "")
    ).split() if w]


def _overlap(tokens: list[str], other: list[str]) -> int:
    pool = set(other)
    return sum(1 for t in tokens if t in pool)


def prune(workspace: Path, *, retention_days: int = RETENTION_DAYS,
          now: float | None = None) -> dict[str, Any]:
    """Drop *records* past the retention window. Never artifacts, never live work.

    Not wired into anything automatic. A lifecycle file that deletes itself on a
    timer is how a user loses the ability to resume, so the only caller is an
    explicit command.
    """
    cutoff = (now if now is not None else time.time()) - retention_days * 86400
    removed: list[str] = []
    kept_live: list[str] = []
    root = tasks_root(workspace)
    if not root.is_dir():
        return {"removed": [], "kept": []}
    for entry in sorted(root.iterdir()):
        task_file = entry / "task.json"
        if not task_file.is_file():
            continue
        raw = store.read_json(task_file)
        if not isinstance(raw, dict):
            continue
        state = str(raw.get("state") or "")
        if state in LIVE_STATES:
            kept_live.append(str(raw.get("task_id") or entry.name))
            continue
        stamp = str(raw.get("finished_at") or raw.get("updated_at") or "")
        stamp_time = time.time()
        try:
            stamp_time = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            ).timestamp()
        except (ValueError, TypeError):
            pass  # unparseable stamp: treated as brand new, so never pruned
        # Keep anything stamped after the cutoff. Getting this comparison
        # backwards deletes the records the user just created, which is the one
        # outcome worse than never pruning at all.
        if stamp_time > cutoff:
            continue
        # Only the bookkeeping goes. The run directory and every artifact stay.
        try:
            task_file.unlink()
            (entry / "checkpoints.jsonl").unlink(missing_ok=True)
            try:
                entry.rmdir()
            except OSError:
                pass
            removed.append(str(raw.get("task_id") or entry.name))
        except OSError:
            continue
    return {"removed": removed, "kept": kept_live}
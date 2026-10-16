"""Crash-safe run state.

Two properties the previous prose-only design did not have:

* **Atomicity** - state files are written temp-then-``os.replace`` so an
  interrupt can never leave a half-written ``status.md`` that a resumed
  session reads as truth.
* **Interruption detection** - every mutation first drops a ``.pending``
  journal entry naming the file it is about to change. If a run is found with
  a journal entry still present, the previous process died mid-mutation and
  :func:`require_clean` refuses to continue until it is reconciled.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import phases
from .errors import LockError, RecoveryRequired, SwarmError

#: Run-directory layout. Every file here is recreated by ``swarm init``.
STATE_FILE = "state.json"
STATUS_FILE = "status.md"
HISTORY_FILE = "history.md"
REPORTS_FILE = "reports.md"
WORKSTREAMS_FILE = "workstreams.md"
PLAN_FILE = "plan.md"
#: The task graph the manager walks. Separate from ``plan.md`` on purpose:
#: ``plan.md`` is the human narrative, this is what code reads.
PLAN_TASKS_FILE = "plan.json"
RETRO_FILE = "retro.md"
CRITERIA_FILE = "criteria.json"
EVIDENCE_FILE = "evidence.jsonl"
JOURNAL_FILE = ".pending"
LOCK_FILE = ".lock"
LOCK_STALE_SECONDS = 900

RUNS_ROOT = Path(".opencode") / "swarm"


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically, durably, with LF newlines."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp creates 0600. Run state is not a secret, and silently
        # owner-only files break a shared checkout or a CI cache that runs as a
        # different user, so widen before the rename makes it visible.
        try:
            os.chmod(tmp, 0o644)
        except OSError:
            pass
        os.replace(tmp, path)
        # fsync the DIRECTORY too. Data without this does not make the rename
        # durable: on POSIX a crash can lose the directory entry and the write
        # silently reverts. Best effort — Windows has no equivalent to fsync a
        # directory handle, and the call is a no-op there.
        _fsync_dir(path.parent)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _fsync_dir(directory: Path) -> None:
    """Flush a directory entry so a completed rename survives a crash."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=False) + "\n")


def append_line(path: Path, line: str) -> None:
    """Append one durable line. Opens/closes so concurrent writers interleave
    whole lines rather than corrupting each other's buffers."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line.rstrip("\n") + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        raise SwarmError(
            f"{path} is corrupt (invalid JSON): {exc}", code="CORRUPT_STATE"
        ) from exc


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    append_line(path, json.dumps(record, sort_keys=True))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    # A torn final line is the signature of an interrupted
                    # append. Skip it rather than failing the whole run.
                    continue
    except FileNotFoundError:
        return []
    return out


@dataclass
class RunState:
    """Machine-readable truth for one run. ``status.md`` is a rendering of this."""

    slug: str
    phase: str = phases.INTAKE
    created_at: str = ""
    updated_at: str = ""
    fix_cycles: int = 0
    abort_reason: str | None = None
    workstreams: list[dict[str, Any]] = field(default_factory=list)
    budget: dict[str, Any] = field(default_factory=dict)
    version: int = 1

    @classmethod
    def load(cls, run_dir: Path) -> "RunState":
        raw = read_json(Path(run_dir) / STATE_FILE)
        if raw is None:
            raise SwarmError(
                f"no {STATE_FILE} in {run_dir}; run `swarm init` first",
                code="NO_SUCH_RUN",
            )
        if not isinstance(raw, dict):
            raise _corrupt_state(run_dir, f"expected a JSON object, got {type(raw).__name__}")
        missing = {"slug", "phase"} - set(raw)
        if missing:
            raise _corrupt_state(
                run_dir,
                f"missing required key(s): {', '.join(sorted(missing))}",
            )
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, run_dir: Path) -> None:
        self.updated_at = utcnow()
        atomic_write_json(Path(run_dir) / STATE_FILE, self.to_dict())

    # -- convenience ----------------------------------------------------
    @property
    def dir(self) -> Path:  # pragma: no cover - replaced by callers
        raise NotImplementedError

    def is_finished(self) -> bool:
        return self.phase in phases.TERMINAL


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def now_hhmm() -> str:
    return time.strftime("%H:%M", time.localtime())


def slugify(text: str, *, fallback: str = "run") -> str:
    cleaned = []
    prev_dash = False
    for ch in text.strip().lower():
        if ch.isalnum():
            cleaned.append(ch)
            prev_dash = False
        elif not prev_dash and cleaned:
            cleaned.append("-")
            prev_dash = True
    out = "".join(cleaned).strip("-")
    return out[:48] or fallback


def runs_root(workspace: Path) -> Path:
    return Path(workspace) / RUNS_ROOT


def init_run(workspace: Path, slug: str) -> Path:
    """Create a run directory with a complete, valid initial state."""
    root = runs_root(workspace)
    run_dir = root / slug
    if run_dir.exists() and (run_dir / STATE_FILE).exists():
        raise SwarmError(
            f"run {slug!r} already exists at {run_dir}", code="RUN_EXISTS"
        )
    run_dir.mkdir(parents=True, exist_ok=True)

    stamp = utcnow()
    state = RunState(slug=slug, created_at=stamp, updated_at=stamp)
    state.save(run_dir)

    atomic_write_text(
        run_dir / STATUS_FILE,
        f"# Swarm run: {slug}\n\n"
        f"- phase: {phases.INTAKE}\n"
        f"- created: {stamp}\n"
        f"- updated: {stamp}\n"
        f"- fix_cycles: 0\n\n"
        "## next action\n\n"
        "INTAKE: name the finished artifact and its evidence.\n\n"
        "## live processes\n\n"
        "(none)\n",
    )
    atomic_write_text(
        run_dir / HISTORY_FILE,
        f"# History for {slug} — append-only, one line per milestone\n"
        f"{now_hhmm()} init — run created\n",
    )
    atomic_write_text(
        run_dir / WORKSTREAMS_FILE,
        f"# Workstreams for {slug}\n\n"
        "| id | owner | status | scope | evidence |\n"
        "|---|---|---|---|---|\n",
    )
    for name, header in (
        (PLAN_FILE, f"# Plan for {slug}\n"),
        (REPORTS_FILE, f"# Worker reports for {slug} — verbatim, never summarised\n"),
        (RETRO_FILE, f"# Retro for {slug} — written by swarm/improve at RETRO\n"),
    ):
        if not (run_dir / name).exists():
            atomic_write_text(run_dir / name, header)
    if not (run_dir / PLAN_TASKS_FILE).exists():
        atomic_write_json(run_dir / PLAN_TASKS_FILE, {"goal": "", "tasks": []})
    # Keep the run dir out of version control. evidence.jsonl holds raw command
    # output verbatim, and that output can contain secrets; without this the
    # run dir sits inside the workspace where `git add -A` will happily stage
    # it. Same trick create_worktrees uses for its tree root.
    ignore = runs_root(workspace) / ".gitignore"
    if not ignore.exists():
        atomic_write_text(ignore, "*\n")
    atomic_write_json(run_dir / CRITERIA_FILE, [])
    atomic_write_text(run_dir / EVIDENCE_FILE, "")
    return run_dir


def find_latest_run(workspace: Path) -> Path | None:
    """Most-recently-modified run that is not finished, else most recent."""
    root = runs_root(workspace)
    if not root.is_dir():
        return None
    candidates = [p for p in root.iterdir() if p.is_dir() and (p / STATE_FILE).exists()]
    if not candidates:
        return None
    unfinished = []
    for cand in candidates:
        try:
            if not RunState.load(cand).is_finished():
                unfinished.append(cand)
        except SwarmError:
            continue
    pool = unfinished or candidates
    return max(pool, key=lambda p: p.stat().st_mtime)


def find_run(workspace: Path, slug: str) -> Path:
    """Locate a run by slug.

    ``cli.cmd_run_init`` date-prefixes new slugs (``<YYYY-MM-DD>-<slug>``, the
    convention ``agents/swarm.md`` documents), so the bare slug a caller typed
    at ``run init`` time will not be the directory name. Resolution order:

    1. an exact directory match;
    2. today's date prefix;
    3. any single run whose slug or directory name ends with ``-<slug>``.

    Rule 3 raises rather than guessing when several runs match, because
    silently picking one would resume the wrong run.

    A slug containing ``..`` resolves outside the run root. Rules 1 and 2 are
    the paths that make that reachable, so every return goes through
    :func:`_confined` - checking only the suffix match left the exact branch
    able to hand back (and then write state into) an outside directory.
    """
    root = runs_root(workspace)
    exact = root / slug
    if (exact / STATE_FILE).exists():
        return _confined(exact, root)

    dated = root / f"{utcnow()[:10]}-{slug}"
    if (dated / STATE_FILE).exists():
        return _confined(dated, root)

    matches = sorted(
        p
        for p in list_runs(workspace)
        if p.name.endswith(f"-{slug}") or _read_slug(p) == slug
    )
    if len(matches) == 1:
        return _confined(matches[0], root)
    if len(matches) > 1:
        names = ", ".join(p.name for p in matches)
        raise SwarmError(
            f"slug {slug!r} is ambiguous - it matches {names}. "
            "Pass the full run directory name instead.",
            code="AMBIGUOUS_RUN",
        )
    raise SwarmError(f"no run {slug!r} under {root}", code="NO_SUCH_RUN")


def _confined(run_dir: Path, root: Path) -> Path:
    """Assert a resolved run dir really is inside the run root.

    ``--slug`` containing `..` resolved to a directory outside
    ``.opencode/swarm`` when that directory happened to hold a state.json, and
    the next ``store.mutation`` then wrote a state file into it. Nothing in the
    resolution chain was checking containment, so it is checked here, once,
    after every resolution strategy has run.
    """
    resolved = Path(run_dir).resolve()
    anchor = Path(root).resolve()
    if resolved != anchor and anchor not in resolved.parents:
        raise SwarmError(
            f"run path {resolved} is outside {anchor}; refusing to touch it",
            code="RUN_OUTSIDE_ROOT",
        )
    return run_dir


def _read_slug(run_dir: Path) -> str:
    try:
        raw = read_json(run_dir / STATE_FILE, default={}) or {}
    except SwarmError:
        return ""
    value = raw.get("slug")
    return value if isinstance(value, str) else ""


def _corrupt_state(run_dir: Path, detail: str) -> SwarmError:
    """A malformed state file is a SwarmError, not a traceback.

    Raising here keeps the CLI's single `error [CODE]` reporting path: a
    corrupt run dir must tell the operator what to do instead of dumping an
    unpacking TypeError from deep inside a dataclass constructor.
    """
    return SwarmError(
        f"{STATE_FILE} in {run_dir} is unreadable ({detail}). "
        f"Move it aside and `swarm run recover --slug <slug>`, or delete the run "
        f"and start again: {STATE_FILE} is regenerated by `swarm run init`.",
        code="CORRUPT_STATE",
    )


def list_runs(workspace: Path) -> list[Path]:
    root = runs_root(workspace)
    if not root.is_dir():
        return []
    return sorted(
        (p for p in root.iterdir() if p.is_dir() and (p / STATE_FILE).exists()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


# -- journal / locking ---------------------------------------------------


def journal(rundir: Path) -> Path:
    return Path(rundir) / JOURNAL_FILE


def pending_journal(rundir: Path) -> dict[str, Any] | None:
    raw = read_json(journal(rundir))
    return raw if isinstance(raw, dict) else None


def require_clean(rundir: Path) -> None:
    """Raise if a previous mutation died before finishing."""
    entry = pending_journal(rundir)
    if entry is None:
        return
    raise RecoveryRequired(
        "run was interrupted mid-mutation; journal says "
        f"{entry.get('op')} on {entry.get('target')}. "
        f"Inspect the file, then run: swarm recover --slug {Path(rundir).name} --note \"what you did\"",
        code="RECOVERY_REQUIRED",
    )


@contextmanager
def mutation(rundir: Path, op: str, target: str) -> Iterator[None]:
    """Bracket a state change with a journal entry + an exclusive lock.

    Three outcomes are distinguished, because conflating them was a real bug:

    * **Success** — journal cleared.
    * **Refusal** (``SwarmError``: gate closed, illegal transition, budget
      exceeded, scope conflict) — journal cleared too. A refusal is a *known*
      outcome that committed nothing, so the run is not corrupt. Demanding
      ``swarm recover`` here would make every expected QC rejection force a
      manual recovery step before the FIX retry could even start.
    * **Anything else** — the journal survives and the next
      ``require_clean``/``mutation`` refuses with ``RecoveryRequired``. An
      unexpected exception may have left real damage behind (a half-created
      worktree, a side effect outside the atomic write), and a hard kill never
      unwinds at all. Neither may resume silently.
    """
    rundir = Path(rundir)
    with _exclusive_lock(rundir):
        require_clean(rundir)
        atomic_write_json(
            journal(rundir),
            {"op": op, "target": target, "pid": os.getpid(), "at": utcnow()},
        )
        try:
            yield
        except SwarmError:
            _clear_journal(rundir)
            raise
        # Reached only if the body completed. Clearing last is what makes an
        # interrupt detectable: no clear, no proof of completion.
        _clear_journal(rundir)


def _clear_journal(rundir: Path) -> None:
    try:
        journal(rundir).unlink()
    except FileNotFoundError:
        pass


#: How long a caller will wait for a contended lock before giving up. Worker
#: heartbeats and budget updates are short critical sections that legitimately
#: collide, so they wait rather than failing.
LOCK_WAIT_SECONDS = 5.0

#: How often a waiting caller re-checks for the lock.
LOCK_POLL_SECONDS = 0.02


@contextmanager
def file_lock(path: Path, *, timeout: float = LOCK_WAIT_SECONDS,
              stale_after: float = LOCK_STALE_SECONDS) -> Iterator[None]:
    """Exclusive advisory lock on ``path``, waiting up to ``timeout`` seconds.

    Used for short read-modify-write critical sections (budget counters, the
    worker registry) that several processes legitimately race on. A lock older
    than ``stale_after`` is assumed to belong to a dead process and is taken
    over, so a crash cannot wedge the run forever.

    Raises ``LockError`` (``LOCK_HELD``) if the lock is still held after the
    timeout.

    A stale lock is only taken over after its recorded pid is checked with
    ``os.kill(pid, 0)``: a live holder is never evicted, even if its critical
    section ran longer than ``stale_after``. The releasing process also
    unlinks **only the inode it created**, so a slow original holder cannot
    delete the lock a new holder is already using.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, timeout)
    mine: tuple[int, int] | None = None
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if _evict_if_dead(path, stale_after):
                continue
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                age = stale_after  # unreadable stat: treat as stale, not fresh
            if time.monotonic() >= deadline:
                raise LockError(
                    f"{path.name} is locked by another process (age {age:.0f}s) "
                    f"and was still held after waiting {timeout:.1f}s. "
                    f"If that process is gone, delete {path}.",
                    code="LOCK_HELD",
                ) from None
            time.sleep(LOCK_POLL_SECONDS)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"pid={os.getpid()} at={utcnow()}\n")
        try:
            st = path.stat()
            mine = (st.st_dev, st.st_ino)
        except OSError:
            mine = None
        break
    try:
        yield
    finally:
        # Unlink by identity, not by name. If a takeover replaced the lock while
        # this holder was running, a bare `path.unlink()` would free the NEW
        # holder's lock and let a third process enter concurrently (ABA).
        try:
            st = path.stat()
            if mine is None or (st.st_dev, st.st_ino) == mine:
                path.unlink()
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` still exists. Best effort and never raises.

    Platform split matters here. On POSIX ``os.kill(pid, 0)`` is the standard
    existence probe. **On Windows it is not**: ``os.kill`` maps any signal
    other than the two console events straight onto ``TerminateProcess``, so
    calling it with 0 would *kill* the process we are trying to inspect. This
    function is on the path that decides whether to steal another process's
    lock, so a wrong answer there means killing an unrelated process. Use the
    kernel query instead.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return True  # cannot tell: assume alive rather than evict
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return True  # cannot tell: assume alive rather than evict
    return True


def _evict_if_dead(path: Path, stale_after: float) -> bool:
    """Remove an abandoned lock. Returns True if the caller should retry.

    Two guards stop this from evicting a live holder:

    * age >= ``stale_after`` — the usual stale criterion; and
    * the recorded pid is gone — checked with ``os.kill(pid, 0)``.

    The pid check is what makes a takeover safe when a critical section simply
    ran longer than ``stale_after``: mtime alone would evict a healthy holder.
    """
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return True  # unreadable stat: treat as abandoned
    if age < stale_after:
        return False
    try:
        recorded = path.read_text(encoding="utf-8")
    except OSError:
        recorded = ""
    pid_text = ""
    for part in recorded.split():
        if part.startswith("pid="):
            pid_text = part[4:]
            break
    try:
        pid = int(pid_text)
    except ValueError:
        # No usable pid: fall back to the age criterion alone. The unlink must
        # happen here too — returning before it would re-check the same live
        # file forever and spin the acquisition loop until it was killed.
        try:
            path.unlink()
        except OSError:
            pass
        return True
    if _pid_alive(pid):
        return False  # holder is alive, however long it has held the lock
    try:
        path.unlink()
    except OSError:
        pass
    return True


@contextmanager
def _exclusive_lock(rundir: Path) -> Iterator[None]:
    """Lock a whole run directory for the duration of a ``mutation``.

    Delegates to :func:`file_lock` with ``timeout=0``: a state mutation must
    not queue behind anything, because the caller already had to pass
    ``require_clean``. Anything else is contention worth reporting.
    """
    with file_lock(Path(rundir) / LOCK_FILE, timeout=0.0):
        yield


def recover(rundir: Path, note: str) -> dict[str, Any]:
    """Clear an interrupted-mutation journal after human reconciliation.

    The directory lock is taken **before** anything is removed. Without that,
    an operator running `swarm run recover` while a `swarm run phase` is
    mid-mutation deleted the live holder's lock AND its journal; the holder
    then finished having written nothing, which is precisely the state
    `require_clean` exists to catch and which a later run would trust.
    """
    rundir = Path(rundir)
    entry = pending_journal(rundir) or {}
    with file_lock(rundir / LOCK_FILE, timeout=0.0):
        # Only unlink what is actually there; a file that vanishes between the
        # check and the unlink must not escape as an uncaught FileNotFoundError.
        for victim in (rundir / LOCK_FILE, journal(rundir)):
            try:
                victim.unlink()
            except FileNotFoundError:
                pass
    append_line(
        rundir / HISTORY_FILE,
        f"{now_hhmm()} recover — {note} (interrupted op: {entry.get('op', 'unknown')}"
        f" target: {entry.get('target', 'unknown')})",
    )
    return {"ok": True, "cleared": entry, "note": note}
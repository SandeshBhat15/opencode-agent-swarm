"""Safe parallel execution: write-scope analysis and git worktree isolation.

Two workers in the same working tree with overlapping scopes silently
clobber each other. A FILE BOUNDARY written in a contract is prose; this
module is the check.

Two failure modes are handled:

* **Scope overlap** is detected before any worker spawns. Because proving two
  globs disjoint is not decidable in general, overlap analysis reports two
  classes - ``definite`` (provable) and ``possible`` (not provably disjoint).
  Definite blocks the plan; possible warns, or blocks under ``--strict``.
* **Edit collision** is prevented at the filesystem level by giving each
  workstream its own git worktree. Integration goes through
  ``git apply --3way``, which refuses to clobber on conflict.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, Iterable

from .errors import ScopeConflict, SwarmError

#: Nothing in the swarm may write inside these, whatever a contract claims.
PROTECTED: tuple[str, ...] = (
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
)

#: A workstream may never rewrite the agent pages that define it.
SELF_PROTECTED: tuple[str, ...] = (
    ".opencode/swarm",
    ".config/opencode/agents/swarm.md",
    ".config/opencode/agents/swarm",
)

WILDCARD_CHARS = set("*?[")


def normalise(entry: str) -> str:
    """Posix-normalise one scope entry."""
    text = str(entry).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/") or text


def split_scope(raw: str | Iterable[str]) -> list[str]:
    """Accept ``"a, b"``/newline lists or an iterable; return clean entries."""
    if isinstance(raw, str):
        parts: list[str] = []
        for line in raw.splitlines():
            parts.extend(line.split(","))
    else:
        parts = list(raw)
    out: list[str] = []
    for part in parts:
        entry = normalise(part)
        if entry and entry not in out:
            out.append(entry)
    return out


def validate_entry(entry: str, *, allow_self_protected: bool = False) -> None:
    """Reject escape hatches and protected trees inside a FILE BOUNDARY."""
    raw = entry
    entry = normalise(entry)
    if entry in ("", "*", "**", "**/*"):
        raise ScopeConflict(
            f"scope entry {entry!r} is unbounded - a workstream must name real paths",
            code="UNBOUNDED_SCOPE",
        )
    if ".." in entry.split("/"):
        raise ScopeConflict(
            f"scope entry {entry!r} escapes the workspace via '..'",
            code="SCOPE_ESCAPE",
        )
    # An absolute path is a claim about somewhere outside the workspace. It was
    # previously accepted: `..` was checked but `/etc/passwd` and `C:/Windows`
    # were not, so the module whose whole job is vetting FILE BOUNDARY claims
    # waved them through.
    if raw.startswith(("/", "\\")) or PurePath(raw).is_absolute():
        raise ScopeConflict(
            f"scope entry {raw!r} is an absolute path; a FILE BOUNDARY names "
            "paths inside the workspace",
            code="SCOPE_ESCAPE",
        )
    head = entry.split("/")[0]
    if head in PROTECTED:
        raise ScopeConflict(
            f"scope entry {entry!r} targets protected path {head!r}", code="PROTECTED_SCOPE"
        )
    if not allow_self_protected:
        for guard in SELF_PROTECTED:
            if entry == guard or entry.startswith(guard + "/"):
                raise ScopeConflict(
                    f"scope entry {entry!r} targets the swarm's own definitions "
                    f"({guard!r}); these change only via an approved proposal",
                    code="SELF_PROTECTED_SCOPE",
                )


def is_glob(entry: str) -> bool:
    return bool(WILDCARD_CHARS & set(entry))


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Whole-value glob semantics: ``*`` spans '/', matching OpenCode's rules."""
    out: list[str] = ["^"]
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            out.append(".*")
        elif ch == "?":
            out.append(".")
        elif ch == "[":
            close = pattern.find("]", i)
            if close == -1:
                out.append(re.escape(ch))
            else:
                body = pattern[i + 1 : close]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = close + 1
                continue
        else:
            out.append(re.escape(ch))
        i += 1
    out.append("$")
    try:
        return re.compile("".join(out))
    except re.error as exc:
        # `[]`, `[z-a]` and friends are not valid globs. Left alone they raised
        # re.PatternError out of cmd_scope_plan as an uncaught traceback,
        # because main() only catches SwarmError. Refuse them like any other
        # bad scope.
        raise ScopeConflict(
            f"scope entry {pattern!r} has an unparseable character class: {exc}",
            code="BAD_PATTERN",
        ) from None


def _literal_prefix(entry: str) -> str:
    """Longest leading path that contains no wildcard."""
    parts: list[str] = []
    for seg in entry.split("/"):
        if WILDCARD_CHARS & set(seg):
            break
        parts.append(seg)
    return "/".join(parts)


def _segment_overlap(a: str, b: str) -> bool:
    """True when two literal paths are equal or one contains the other."""
    pa, pb = a.split("/"), b.split("/")
    short = min(len(pa), len(pb))
    return pa[:short] == pb[:short]


def _regex_match(prefix: str, pattern: str, samples: Iterable[str]) -> bool:
    rx = glob_to_regex(pattern)
    return any(rx.match(s) for s in samples)


def _candidates(a: str, b: str) -> set[str]:
    """Bounded sample set for testing whether two globs can intersect."""
    atoms: set[str] = set()
    for entry in (a, b):
        for seg in entry.split("/"):
            if WILDCARD_CHARS & set(seg):
                for ch in seg:
                    if ch not in WILDCARD_CHARS:
                        atoms.add(ch)
                atoms.update({"x", "y", "file", "index"})
            else:
                atoms.add(seg)
                atoms.add(seg + "/file")
                atoms.add(seg)
                if seg not in ("*",):
                    atoms.add(seg + "1")
    bases = [atoms] if atoms else [{"file"}]
    out: set[str] = set()
    for base in bases:
        out.add("/".join(sorted(base)))
        for atom in sorted(base):
            out.add(atom)
    # Concrete joins are what actually expose intersection.
    joined: set[str] = set()
    for left in list(out):
        for right in list(out):
            joined.add(f"{left}/{right}")
            joined.add(f"{left}{right}")
    out |= joined
    return out


def overlap_kind(a: str, b: str) -> str:
    """``definite`` | ``possible`` | ``none`` for two scope entries."""
    a, b = normalise(a), normalise(b)
    if a == b:
        return "definite"
    ga, gb = is_glob(a), is_glob(b)
    pa, pb = _literal_prefix(a), _literal_prefix(b)

    if not ga and not gb:
        return "definite" if _segment_overlap(a, b) else "none"

    # Different static prefixes in unrelated trees are cheap to clear.
    if pa and pb and not _segment_overlap(pa, pb):
        return "none" if pa != "/" else "possible"

    samples = _candidates(a, b)
    if ga and _regex_match(pa, b, samples):
        return "definite"
    if gb and _regex_match(pb, a, samples):
        return "definite"
    if ga and gb and fnmatch.fnmatch(pa, b) or (ga and gb and fnmatch.fnmatch(pb, a)):
        return "definite"
    return "possible"


def overlaps(a: str, b: str) -> bool:
    """Conservative predicate: True unless disjointness is provable."""
    return overlap_kind(a, b) != "none"


@dataclass
class ScopeConflictReport:
    kind: str
    left: str
    right: str
    owner_left: str
    owner_right: str

    def render(self) -> str:
        return (
            f"{self.kind.upper()} overlap: {self.owner_left} [{self.left}] "
            f"<-> {self.owner_right} [{self.right}]"
        )


def find_conflicts(scopes: dict[str, list[str]]) -> list[ScopeConflictReport]:
    """All scope collisions between named owners."""
    reports: list[ScopeConflictReport] = []
    names = sorted(scopes)
    for i, left in enumerate(names):
        for right in names[i + 1 :]:
            for entry_l in scopes[left]:
                for entry_r in scopes[right]:
                    kind = overlap_kind(entry_l, entry_r)
                    if kind != "none":
                        reports.append(ScopeConflictReport(kind, entry_l, entry_r, left, right))
    return reports


def plan_scopes(scopes: dict[str, list[str]], *, strict: bool = False) -> dict[str, Any]:
    """Validate every entry and cross-check for overlaps."""
    for owner, entries in scopes.items():
        for entry in entries:
            validate_entry(entry)
    reports = find_conflicts(scopes)
    definite = [r for r in reports if r.kind == "definite"]
    possible = [r for r in reports if r.kind == "possible"]
    if definite:
        raise ScopeConflict(
            "overlapping write scopes:\n"
            + "\n".join(r.render() for r in definite)
            + "\nSplit the workstreams or narrow the boundaries.",
            code="SCOPE_CONFLICT",
        )
    if possible and strict:
        raise ScopeConflict(
            "overlapping write scopes (strict mode, not provably disjoint):\n"
            + "\n".join(r.render() for r in possible),
            code="SCOPE_CONFLICT_POSSIBLE",
        )
    return {
        "ok": not reports,
        "definite_conflicts": [r.render() for r in definite],
        "possible_conflicts": [r.render() for r in possible],
    }


# -- git worktree isolation ---------------------------------------------


@dataclass
class Worktree:
    name: str
    path: Path
    branch: str
    created: bool = False

    def __post_init__(self) -> None:
        # Accept str from the caller but always hand back a Path, so callers
        # can do `wt.path / "x"` without a defensive wrap at every call site.
        self.path = Path(self.path)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "path": str(self.path), "branch": self.branch,
                "created": self.created}


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise SwarmError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}",
            code="GIT_FAILED",
        )
    return proc


def repo_root(start: Path) -> Path:
    proc = _git(Path(start), "rev-parse", "--show-toplevel")
    root = proc.stdout.strip()
    if not root:
        raise SwarmError("not inside a git repository", code="NOT_A_REPO")
    return Path(root)


def base_ref(repo: Path) -> str:
    """Branch the worktrees fork from, so integration diffs are meaningful."""
    head = _git(repo, "symbolic-ref", "--short", "HEAD", check=False)
    if head.returncode == 0 and head.stdout.strip():
        return head.stdout.strip()
    return "HEAD"


def create_worktrees(
    repo: Path,
    tree_root: Path,
    names: list[str],
    *,
    base: str | None = None,
) -> list[Worktree]:
    """One worktree + branch per workstream, created idempotently."""
    repo = repo_root(repo)
    tree_root = Path(tree_root)
    base = base or base_ref(repo)
    tree_root.mkdir(parents=True, exist_ok=True)
    # Keep worktrees out of the parent repo's own status output.
    ignore = tree_root / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")

    out: list[Worktree] = []
    root_resolved = tree_root.resolve()
    for name in names:
        # A workstream name becomes both a path segment and a git ref, so it
        # has to be a SINGLE segment. `..` and `/` are the traversal primitive:
        # `tree_root / "../precious-wt"` lands outside the root, and because the
        # "already exists" branch below skips `git worktree add`, the ref-name
        # check that would have rejected it never runs.
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise SwarmError(
                f"workstream name {name!r} must be a single path segment "
                "(no '/' , '\\\\', '.' or '..')",
                code="BAD_WORKSTREAM_NAME",
            )
        wt_path = tree_root / name
        # Defence in depth: even with a clean single-segment name, refuse to
        # register anything that does not resolve to a direct child of the root.
        if wt_path.resolve().parent != root_resolved:
            raise SwarmError(
                f"worktree for {name!r} would resolve to {wt_path.resolve()}, "
                f"which is outside {root_resolved}",
                code="WORKTREE_OUTSIDE_ROOT",
            )
        branch = f"swarm/{name}"
        created = False
        if not wt_path.exists():
            exists_branch = _git(repo, "rev-parse", "--verify", "--quiet", branch, check=False)
            if exists_branch.returncode == 0:
                _git(repo, "worktree", "add", str(wt_path), branch)
            else:
                _git(repo, "worktree", "add", "-b", branch, str(wt_path), base)
            created = True
        out.append(Worktree(name=name, path=str(wt_path), branch=branch, created=created))
    return out


def worktree_patch(repo: Path, wt_path: Path, *, base: str | None = None) -> str:
    """Unified diff of everything a workstream changed, for controlled merge."""
    repo = repo_root(repo)
    base = base or base_ref(repo)
    # Diff from inside the worktree, not the main checkout: we want this
    # workstream's HEAD, and `base` still resolves because worktrees share
    # the object database and refs.
    #
    # `--end-of-options` stops git parsing the revspec as an OPTION. Without
    # it, `--base '--output=/tmp/PWNED'` became the argv element
    # "--output=/tmp/PWNED...HEAD", git wrote a file at the attacker's path and
    # returned an empty diff, which this function reported as a successful
    # 0-byte patch. argv injection, not shell injection — but just as real.
    proc = _git(Path(wt_path), "diff", "--binary",
                "--end-of-options", f"{base}...HEAD", "--", check=False)
    if proc.returncode != 0:
        raise SwarmError(
            f"could not diff worktree {wt_path}: {(proc.stderr or proc.stdout).strip()}",
            code="GIT_FAILED",
        )
    return proc.stdout


def _patch_targets(patch: str) -> list[str]:
    """Destination paths a patch would write, read from its ``+++`` headers."""
    targets: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path == "/dev/null":
                continue
            # Strip a trailing timestamp column git sometimes appends.
            path = path.split("\t", 1)[0].strip()
            if path.startswith(("a/", "b/")):
                targets.append(path[2:])
    return targets


def _escapes_via_link(repo: Path, rel: str) -> str | None:
    """Return a reason if ``rel`` traverses a symlink/junction, else ``None``.

    git refuses ``../`` and absolute paths in a patch itself, but it happily
    follows a directory symlink that already exists inside the working tree: a
    patch targeting ``link/pwn.txt`` where ``link`` is a junction to
    ``../outside`` writes *outside* the repo, and git reports success. Windows
    junctions need no privilege (symbolic links do), so this is reachable by
    any worker that can create one inside its own scope.
    """
    current = repo
    for part in Path(rel).parts:
        if part in (".", "..", ""):
            return None
        current = current / part
        if current.is_symlink():
            return f"{current} is a symlink"
        # Windows directory junction: reparse point that is not a symlink.
        if os.name == "nt" and current.exists() and not current.is_dir():
            return None
        if os.name == "nt" and current.is_dir():
            try:
                st = os.lstat(current)
            except OSError:
                return None
            attrs = getattr(st, "st_file_attributes", 0)
            reparse = getattr(st, "st_reparse_tag", 0)
            # IO_REPARSE_TAG_MOUNT_POINT / SYMLINK
            if attrs & 0x400 or reparse in (0xA0000003, 0xA000000C):
                return f"{current} is a reparse point (junction/symlink)"
    return None


def integrate_patch(repo: Path, patch_path: Path, *, three_way: bool = True) -> dict[str, Any]:
    """Apply a workstream patch. Refuses to clobber on conflict.

    The probe and the real apply must be built separately. Reusing one arg
    list left ``--check`` on the applying invocation, so git only re-checked
    and returned success without touching the working tree -- a silent
    false-positive that reported integration done while nothing had landed.
    """
    repo = repo_root(repo)
    shared: list[str] = ["--3way"] if three_way else []
    target = str(Path(patch_path).resolve())

    # Refuse before git sees it: git will follow an in-tree junction and write
    # outside the repo while reporting success.
    try:
        body = Path(target).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"applied": False, "reason": "patch unreadable", "detail": str(exc)}
    for rel in _patch_targets(body):
        escaped = _escapes_via_link(repo, rel)
        if escaped:
            return {
                "applied": False,
                "reason": "patch target traverses a link out of the repo",
                "detail": f"{rel}: {escaped}",
            }

    probe = _git(repo, "apply", "--check", *shared, target, check=False)
    if probe.returncode != 0:
        return {
            "applied": False,
            "reason": "patch does not apply cleanly",
            "detail": (probe.stderr or probe.stdout).strip(),
        }

    applied = _git(repo, "apply", *shared, target, check=False)
    if applied.returncode != 0:
        return {
            "applied": False,
            "reason": "patch passed --check but failed to apply",
            "detail": (applied.stderr or applied.stdout).strip(),
        }
    return {"applied": True, "reason": "", "detail": ""}


def remove_worktree(repo: Path, wt_path: Path) -> dict[str, Any]:
    repo = repo_root(repo)
    proc = _git(repo, "worktree", "remove", "--force", str(wt_path), check=False)
    if proc.returncode != 0:
        return {"removed": False, "detail": (proc.stderr or proc.stdout).strip()}
    return {"removed": True, "detail": ""}
"""Contract linting for agent and command pages.

This is the module that proves the repo's guardrails are real. It parses the
frontmatter of every page under ``agents/`` and ``commands/`` and fails the
build on conditions that make a page silently inert or unsafe:

======================================  =====================================
``SW001`` missing/unterminated frontmatter
``SW002`` V1 field that V2 ignores
``SW003`` permission ``action`` not a V2 action  <- catches ``bash``
``SW004`` deny shadowed by a later broad allow
``SW005`` ``model:`` pin inside a subagent page
``SW006`` malformed ``provider/model`` reference
``SW007`` missing ``description``
``SW008`` missing or invalid ``mode``
``SW009`` command binds to an agent that does not exist
``SW010`` permission rule missing ``action``/``resource``/``effect``
======================================  =====================================

Why ``SW003`` matters: OpenCode's V2 action is ``shell``. An unmatched action
falls through to the base ``{"action": "*", "effect": "allow"}`` policy, so a
``{action: bash, resource: "git push*", effect: deny}`` rule blocks nothing at
all while looking like a hard guardrail on the page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

#: Built-in V2 permission actions (plugins may add more; unknown ones warn).
V2_ACTIONS = frozenset(
    {
        "read",
        "edit",
        "glob",
        "grep",
        "shell",
        "subagent",
        "skill",
        "question",
        "webfetch",
        "websearch",
        "external_directory",
        "execute",
        "doom_loop",
        "lsp",
    }
)

#: Fields V2 does not read. Presence means the author's intent is ignored.
LEGACY_FIELDS = frozenset(
    {"permission", "tools", "disable", "maxSteps", "max_steps", "prompt", "temperature", "top_p"}
)

VALID_MODES = frozenset({"primary", "subagent", "all"})
VALID_EFFECTS = frozenset({"allow", "ask", "deny"})

_SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str
    path: str
    message: str

    def render(self) -> str:
        return f"{self.severity.upper():5} {self.code}  {self.path}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "path": self.path,
            "message": self.message,
        }


@dataclass
class Page:
    path: Path
    rel: str
    frontmatter: dict[str, Any]
    body: str
    issues: list[Issue]

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    def add(self, code: str, severity: str, message: str) -> None:
        self.issues.append(Issue(code=code, severity=severity, path=self.rel, message=message))


# -- frontmatter parsing -------------------------------------------------


def _scalar(raw: str) -> Any:
    text = raw.strip()
    if not text:
        return ""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text in ("true", "false"):
        return text == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str, str | None]:
    """Minimal YAML reader for the shape agent pages actually use.

    Supports ``key: scalar``, ``key:`` followed by a list of scalars, and a
    list of flat maps. Deliberately dependency-free and deliberately strict:
    anything it cannot read it reports rather than guessing.
    """
    if not text.startswith("---"):
        return {}, text, "no frontmatter block"
    match = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.DOTALL)
    if not match:
        return {}, text, "frontmatter block is not terminated"
    block, body = match.group(1), text[match.end() :]
    lines = block.splitlines()

    data: dict[str, Any] = {}
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        idx += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1] in (" ", "\t"):
            raise ValueError(f"unexpected indentation at line {idx}: {line!r}")
        if ":" not in line:
            raise ValueError(f"line {idx} is not 'key: value': {line!r}")
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest:
            data[key] = _scalar(rest)
            continue
        items: list[Any] = []
        pending: dict[str, Any] | None = None
        while idx < len(lines):
            nxt = lines[idx]
            stripped = nxt.strip()
            if not stripped:
                idx += 1
                continue
            indent = len(nxt) - len(nxt.lstrip())
            if indent == 0:
                break
            if stripped.startswith("- "):
                if pending is not None:
                    items.append(pending)
                    pending = None
                scalar = stripped[2:].strip()
                if ":" in scalar and not scalar.startswith(("http://", "https://")):
                    k, _, v = scalar.partition(":")
                    pending = {k.strip(): _scalar(v)}
                else:
                    items.append(_scalar(scalar))
                idx += 1
                continue
            if pending is not None and ":" in stripped:
                k, _, v = stripped.partition(":")
                pending[k.strip()] = _scalar(v)
                idx += 1
                continue
            # An indented line that is neither a list item nor a key of the
            # pending item is a nested mapping, which this reader does not
            # support. Report it instead of dropping it on the floor: a silent
            # parse failure would let a V1 `permission:` block look clean.
            raise ValueError(
                f"unsupported nested mapping at line {idx + 1}: {stripped!r}. "
                "This reader supports scalars, lists of scalars, and lists of "
                "flat maps only."
            )
        if pending is not None:
            items.append(pending)
        data[key] = items
    return data, body, None


def load_page(path: Path, root: Path) -> Page:
    rel = str(Path(path).relative_to(root)).replace("\\", "/")
    text = Path(path).read_text(encoding="utf-8")
    issues: list[Issue] = []
    fm: dict[str, Any] = {}
    body = text
    try:
        fm, body, problem = parse_frontmatter(text)
        if problem:
            issues.append(Issue("SW001", "error", rel, f"unreadable frontmatter: {problem}"))
    except ValueError as exc:
        issues.append(Issue("SW001", "error", rel, f"unreadable frontmatter: {exc}"))
    # Backstop: flag V1 field names straight off the raw text, so a V1 block
    # that this reader cannot parse is still reported as V1 rather than
    # silently vanishing behind an SW001.
    for name in sorted(_raw_legacy_fields(text)):
        issues.append(
            Issue(
                "SW002",
                "error",
                rel,
                f"V1 field {name!r} is ignored in V2 (use 'permissions' with action 'shell')",
            )
        )
    page = Page(path=Path(path), rel=rel, frontmatter=fm, body=body, issues=issues)
    return page


def _raw_legacy_fields(text: str) -> set[str]:
    """Legacy V1 field names present anywhere in the frontmatter block."""
    match = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.DOTALL)
    block = match.group(1) if match else ""
    found: set[str] = set()
    for line in block.splitlines():
        stripped = line.strip().lstrip("-").strip()
        if ":" not in stripped:
            continue
        name = stripped.partition(":")[0].strip()
        if name in LEGACY_FIELDS:
            found.add(name)
    return found


# -- validation ----------------------------------------------------------


def _check_common(page: Page, *, is_command: bool = False) -> None:
    fm = page.frontmatter
    if not fm:
        return
    desc = fm.get("description")
    if not desc or not str(desc).strip():
        page.add("SW007", "error", "missing 'description'; OpenCode shows it when choosing an agent")

    # Command pages bind to an agent via `agent:`; they have no `mode:` of their own.
    if not is_command:
        mode = fm.get("mode")
        if mode is None:
            page.add("SW008", "error", "missing 'mode' (primary | subagent | all)")
        elif str(mode) not in VALID_MODES:
            page.add("SW008", "error", f"invalid mode {mode!r}; expected one of {sorted(VALID_MODES)}")

    # Legacy V1 fields are detected in `load_page` by scanning the raw block,
    # so an unreadable V1 mapping is still reported.

    perms = fm.get("permissions", [])
    if perms and not isinstance(perms, list):
        page.add("SW010", "error", "'permissions' must be a list of rules")
    elif isinstance(perms, list):
        for rule in perms:
            if not isinstance(rule, dict):
                page.add(
                    "SW010",
                    "error",
                    f"permission rule {rule!r} is not a mapping with "
                    "action/resource/effect",
                )
                continue
            missing = [k for k in ("action", "resource", "effect") if k not in rule]
            if missing:
                page.add("SW010", "error", f"permission rule missing {missing}")
                continue
            action = str(rule["action"])
            if action not in V2_ACTIONS:
                page.add(
                    "SW003",
                    "error",
                    f"action {action!r} is not a V2 action and matches nothing, so this "
                    f"rule is inert (V2 uses 'shell', not 'bash'; 'subagent', not 'task')",
                )
            effect = str(rule["effect"])
            if effect not in VALID_EFFECTS:
                page.add("SW010", "error", f"effect {effect!r} not in {sorted(VALID_EFFECTS)}")

        _check_ordering(page, perms)


def _check_ordering(page: Page, perms: list[Any]) -> None:
    """Last match wins, so a broad allow after a narrow deny kills the deny.

    Only compares rules for the *same* action: a deny on ``shell`` is not
    shadowed by a later ``webfetch`` allow, even though both use ``resource: *``.
    """
    for i, rule in enumerate(perms):
        if not isinstance(rule, dict):
            continue
        if str(rule.get("effect")) != "deny":
            continue
        target_action = str(rule.get("action", ""))
        target = str(rule.get("resource", ""))
        for later in perms[i + 1 :]:
            if not isinstance(later, dict):
                continue
            if str(later.get("effect")) != "allow":
                continue
            if str(later.get("action")) != target_action:
                continue
            if str(later.get("resource")) == "*":
                page.add(
                    "SW004",
                    "error",
                    f"deny on {target_action!r} {target!r} is shadowed by a later "
                    f"'{{action: {target_action}, resource: *}}' allow (last matching rule "
                    "wins) - move the allow above the denies",
                )
                break


def _check_model(page: Page) -> None:
    model = page.frontmatter.get("model")
    if not model:
        return
    mode = str(page.frontmatter.get("mode", ""))
    if mode == "subagent":
        page.add(
            "SW005",
            "error",
            f"subagent pins model {model!r}. Model pins go stale and break spawns; "
            "leave it unset so the worker inherits the session model.",
        )
    text = str(model)
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+(#[A-Za-z0-9_.\-]+)?", text):
        page.add(
            "SW006",
            "error",
            f"model {text!r} is not 'provider/model' with optional '#variant' "
            "(a ':free' suffix is not a V2 variant)",
        )


def _check_command(page: Page, known_agents: set[str]) -> None:
    agent = page.frontmatter.get("agent")
    if agent is None:
        page.add("SW010", "error", "command page has no 'agent:' binding")
        return
    name = str(agent)
    if name not in known_agents:
        page.add(
            "SW009",
            "error",
            f"command binds to agent {name!r}, which does not exist in this repo",
        )


# Markdown that lives under agents/ but is documentation, not an agent page.
NON_PAGE_NAMES = frozenset({"README.MD", "CHANGELOG.MD"})


def is_agent_page(path: Path) -> bool:
    """True when `path` is a loadable agent page rather than repo documentation."""
    return path.name.upper() not in NON_PAGE_NAMES


def collect_agent_ids(agents_dir: Path, root: Path) -> set[str]:
    """Agent IDs the way OpenCode derives them: nested path becomes ``a/b``."""
    ids: set[str] = set()
    if not agents_dir.is_dir():
        return ids
    for path in sorted(agents_dir.rglob("*.md")):
        if not is_agent_page(path):
            continue
        rel = path.relative_to(agents_dir)
        parts = list(rel.parts)
        parts[-1] = parts[-1][:-3]
        ids.add("/".join(parts))
    return ids


def validate_repo(root: Path) -> list[Page]:
    root = Path(root)
    agents_dir = root / "agents"
    commands_dir = root / "commands"
    pages: list[Page] = []
    agent_ids = collect_agent_ids(agents_dir, root)

    for path in sorted(agents_dir.rglob("*.md")) if agents_dir.is_dir() else []:
        if not is_agent_page(path):
            continue
        page = load_page(path, root)
        _check_common(page)
        _check_model(page)
        pages.append(page)

    for path in sorted(commands_dir.rglob("*.md")) if commands_dir.is_dir() else []:
        page = load_page(path, root)
        _check_common(page, is_command=True)
        _check_command(page, agent_ids)
        pages.append(page)

    return pages


def summarise(pages: Iterable[Page]) -> dict[str, Any]:
    pages = list(pages)
    issues = [i for p in pages for i in p.issues]
    return {
        "pages": len(pages),
        "errors": sum(1 for i in issues if i.severity == "error"),
        "warnings": sum(1 for i in issues if i.severity == "warn"),
        "issues": [i.to_dict() for i in sorted(issues, key=lambda i: _SEVERITY_ORDER[i.severity])],
    }


def render(pages: Iterable[Page]) -> str:
    summary = summarise(pages)
    lines = [f"contract lint: {summary['pages']} page(s), {summary['errors']} error(s)"]
    for issue in summary["issues"]:
        lines.append(
            f"  {issue['severity'].upper():5} {issue['code']}  {issue['path']}: {issue['message']}"
        )
    return "\n".join(lines)
"""Project memory and a deduplicating documentation cache.

:mod:`swarmkit.experience` answers "what happened, and how confident are we";
:mod:`swarmkit.lessons` holds the rules distilled from that. This module
answers the two questions neither of them can: *what do we already know about
this project*, and *have we already fetched this page*. It complements the
ledger rather than restating it - nothing here is a lesson, and nothing here
is promoted without evidence.

Three ideas carry the design:

**Facts, state and hypotheses are different types, not different labels.** A
single record with a ``kind`` string invites code that reads any record as
truth. :class:`Fact`, :class:`State` and :class:`Hypothesis` are separate
classes, so ``isinstance`` guards fail across them and a guess cannot be
consumed as an established fact by accident.

**Nothing is stored that is not true or cheap to lose.** Every value is
redacted on the way in, and a value longer than :data:`MAX_VALUE_CHARS` is
refused rather than trimmed: past that length the caller is trying to persist
a transcript, and a transcript log is precisely the store that grows forever
and retrieves nothing.

**Freshness is reported, never assumed.** A cached page carries the time it
was fetched and a TTL. :meth:`Doc.is_stale` and :meth:`Knowledge.freshness`
surface age instead of hiding it, because a silently stale cache is worse than
no cache: it is a wrong answer with a citation attached.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit

from .errors import SwarmError
from .store import atomic_write_text, read_jsonl, utcnow

# --- location -------------------------------------------------------------

#: Kept beside the experience ledger so one directory holds all project
#: memory. ``_ensure`` drops the same ``.gitignore`` guard the other stores
#: use: these files quote user text and fetched pages, and the workspace is
#: usually a git repo where ``git add -A`` would stage them.
DEFAULT_ROOT = Path(".opencode") / "swarm-knowledge"
ENTRIES_FILE = "knowledge.jsonl"
DOCS_FILE = "docs.jsonl"

# --- vocabularies ---------------------------------------------------------

KIND_FACT = "fact"
KIND_STATE = "state"
KIND_HYPOTHESIS = "hypothesis"

#: Exactly the three kinds the caller may name. Ordered most durable first.
KINDS = (KIND_FACT, KIND_STATE, KIND_HYPOTHESIS)

#: How long a fetched document is considered current unless told otherwise.
DEFAULT_TTL_SECONDS = 7 * 24 * 3600.0

#: How long a ``state`` entry survives before :meth:`Knowledge.prune` may drop
#: it. Temporary task state is the one kind whose loss is not a loss.
DEFAULT_STATE_TTL_SECONDS = 24 * 3600.0

# --- caps -----------------------------------------------------------------

#: Above this, a remembered value is a transcript, not a fact. Refused rather
#: than truncated: a silently shortened fact reads as a complete one.
MAX_VALUE_CHARS = 1000

MAX_KEY_CHARS = 200

#: Documents are bulk text and re-fetchable, so they are capped by trimming -
#: the head of a page carries its meaning and the tail is recoverable. The
#: record says ``truncated`` so nothing downstream mistakes it for the whole.
MAX_DOC_CHARS = 40_000

#: Tokens kept per document for retrieval. Storing the whole page as search
#: text would reintroduce the unbounded growth the content cap prevents.
MAX_SEARCH_TEXT_CHARS = 4_000

MAX_ENTRIES = 1000
MAX_DOCS = 200

#: Chars of a cached page echoed back by :meth:`Knowledge.lookup` callers that
#: only want to confirm a hit.
LOOKUP_EXCERPT_CHARS = 400

#: Token overlap below this is coincidence, not a cache hit. Without a floor
#: every query would match the longest document in the store.
LOOKUP_MIN_SCORE = 0.34

DEFAULT_RECALL_LIMIT = 5

# --- errors ---------------------------------------------------------------


class KnowledgeError(SwarmError):
    """A knowledge-store rule was violated.

    Defined here rather than in :mod:`swarmkit.errors` because that module's
    taxonomy belongs to the run machinery; the important part is that this is
    a :class:`~swarmkit.errors.SwarmError`, so the CLI keeps its single
    ``error [CODE]`` reporting path and a caller can catch one base class.
    """

    code = "KNOWLEDGE_INVALID"


# --- redaction ------------------------------------------------------------

#: ``<name> = <value>`` where ``<name>`` looks like it holds a credential.
#: This is the "env-var-looking" case: ``OPENAI_API_KEY=``,
#: ``AWS_SECRET_ACCESS_KEY=``, ``export DB_PASSWORD=``.
_CREDENTIAL_NAME = (
    r"[A-Za-z0-9_.\-]*"
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key"
    r"|private[_-]?key|client[_-]?secret|credential|auth)"
    r"[A-Za-z0-9_.\-]*"
)

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Provider-shaped credentials. Each alternative is a token format that has
    # exactly one meaning, so a false positive is close to impossible.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"), "[REDACTED]"),
    (re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{12,}"), "[REDACTED]"),
    (re.compile(r"(?i)\b(?:ghp|gho|ghs|ghu|ghr|github_pat)_[A-Za-z0-9_]{10,}"), "[REDACTED]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{8,}"), "[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{12,}"), "[REDACTED]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), "[REDACTED]"),
    (re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}"), "[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"), "[REDACTED]"),
    # A whole header line, because everything after the colon is the secret.
    (re.compile(r"(?i)\b(authorization|proxy-authorization|x-api-key)\s*:\s*[^\r\n]+"),
     r"\1: [REDACTED]"),
    # ``Bearer <token>`` / ``Basic <base64>``. Over-redacting prose that says
    # "token <word>" is the safe direction to fail.
    (re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._\-/+=]{8,}"), r"\1 [REDACTED]"),
    # Credentials embedded in a URL. The key/url normaliser additionally drops
    # userinfo, so these never reach a cache key either.
    (re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1[REDACTED]@"),
    # ``curl -u user:pass`` / ``--user user:pass``: a password with no
    # ``password=`` around it to key off.
    (re.compile(r"(?i)(--user|-u|--proxy-user)\s+[^\s:]+:\S+"), r"\1 [REDACTED]"),
    # Generic assignments, including quoted values and ``export`` forms.
    (re.compile(rf"(?i)\b({_CREDENTIAL_NAME})\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^\s,;]+)"),
     r"\1=[REDACTED]"),
    (re.compile(r"-----BEGIN[^-]*PRIVATE KEY-----[\s\S]*?-----END[^-]*PRIVATE KEY-----"),
     "[REDACTED]"),
)

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def redact_secrets(text: Any) -> str:
    """Replace credential-shaped substrings with ``[REDACTED]``.

    Applied to *everything* on the way in, so a caller cannot leak a key by
    forgetting to sanitise. Losing the tail of a key is acceptable; persisting
    it is not.
    """
    value = "" if text is None else str(text)
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


def redact_private(text: Any) -> str:
    """:func:`redact_secrets` plus e-mail addresses, for project memory.

    A cached document is a copy of public text and may legitimately quote a
    contact address. A memory value is our own note, and an address in it is
    private information we had no reason to keep, so the two paths differ.
    """
    return _EMAIL.sub("[REDACTED]", redact_secrets(text))


# --- time -----------------------------------------------------------------


def _to_epoch(when: Any) -> float:
    """Coerce a timestamp to epoch seconds.

    Accepts the ISO-8601 ``utcnow()`` form the rest of the engine writes, a
    :class:`datetime`, or a number, so callers can pass whichever clock they
    already hold. Raises rather than returning a sentinel: a caller that asked
    "is this stale at *this* moment" must not be answered with "now".
    """
    if when is None:
        return time.time()
    if isinstance(when, bool):
        raise KnowledgeError("a boolean is not a timestamp", code="BAD_TIME")
    if isinstance(when, (int, float)):
        return float(when)
    if isinstance(when, datetime):
        moment = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    if isinstance(when, str):
        parsed = _try_epoch(when)
        if parsed is not None:
            return parsed
    raise KnowledgeError(f"cannot read {when!r} as a timestamp", code="BAD_TIME")


def _try_epoch(when: Any) -> float | None:
    """Best-effort timestamp parse; ``None`` instead of raising.

    Used on values read back from disk. A record with an unreadable timestamp
    must still be *reportable* as stale rather than crashing the report, so
    this path returns ``None`` and the caller decides.
    """
    if not isinstance(when, str) or not when.strip():
        return None
    text = when.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                moment = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# --- url normalisation ----------------------------------------------------

#: Query parameters that identify *how you arrived*, not *what you asked for*.
_TRACKING_PARAMS = frozenset({
    "gclid", "fbclid", "msclkid", "dclid", "yclid", "igshid", "mc_cid", "mc_eid",
    "_hsenc", "_hsmi", "ref_src", "ref_url", "spm", "si", "feature",
})


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith("utm_") or lowered in _TRACKING_PARAMS


def looks_like_url(text: str) -> bool:
    """True when ``text`` is a URL rather than a search phrase."""
    low = (text or "").strip().lower()
    if not low:
        return False
    if "://" in low or low.startswith("//"):
        return True
    return bool(re.match(r"^[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}(?::\d+)?(?:[/?#].*)?$", low))


def normalise_url(url: str) -> str:
    """Collapse the several spellings of one page into one cache key.

    Lowercases the host, drops the fragment, drops tracking parameters, sorts
    what is left, drops default ports, and collapses trailing slashes. Two
    variants of the same page must not become two entries, or every re-fetch
    looks new and the cache delivers nothing.

    Userinfo (``user:pass@``) is dropped from the key rather than kept: a key
    is written to disk and printed in reports, and a credential has no place
    in either.
    """
    text = (url or "").strip()
    if not text:
        raise KnowledgeError("cannot normalise an empty url", code="BAD_URL")

    candidate = text if "://" in text else f"//{text}"
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
    except ValueError as exc:  # malformed IPv6 literal, bad port, ...
        raise KnowledgeError(f"cannot parse url {url!r}: {exc}", code="BAD_URL") from exc

    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    else:
        netloc = host

    path = parts.path
    while len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    if path == "/":
        path = ""
    # ``//a//b`` and ``/a/b`` are the same resource to every server that would
    # answer either, so leaving the doubled slashes in would let one page
    # occupy two cache keys.
    path = re.sub(r"/{2,}", "/", path)

    pairs = sorted(
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(name)
    )

    key = f"{scheme}://{netloc}{path}" if scheme else f"{netloc}{path}"
    if pairs:
        key += "?" + urlencode(pairs)
    return key


def content_hash(content: str) -> str:
    """Stable digest of what we actually persist.

    Hashed *after* redaction and trimming, because the question this answers is
    "is the stored copy still current" - comparing the raw input would make a
    page with a secret in it look changed on every fetch.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]


# --- relevance ------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+")

_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "at", "for", "with",
    "is", "are", "was", "be", "been", "do", "does", "did", "not", "that", "this",
    "it", "its", "by", "from", "as", "if", "then", "than", "so", "we", "you",
    "how", "what", "when", "why", "which", "there", "here", "but", "can", "will",
})


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower())
            if len(w) > 1 and w not in _STOPWORDS}


def relevance(query: str, *, key_text: str, value_text: str) -> float:
    """0.0-1.0 overlap between a query and one entry.

    A hit on the key counts double: the key is what the entry is *about*,
    where the value merely mentions it. Zero means no shared token at all, and
    the caller drops it - this is the mechanism that keeps :meth:`recall` from
    replaying history instead of answering the question asked.
    """
    want = _tokens(query)
    if not want:
        return 0.0
    key_hits = len(want & _tokens(key_text))
    value_hits = len(want & _tokens(value_text))
    if not (key_hits or value_hits):
        return 0.0
    return round(min(1.0, (2.0 * key_hits + value_hits) / (2.0 * len(want))), 4)


# --- memory entries -------------------------------------------------------


@dataclass
class Entry:
    """Shared shape of everything :meth:`Knowledge.remember` stores.

    The ``kind`` is a :class:`~typing.ClassVar`, not a field: it is a property
    of the *class*, which is what lets the store answer "is this a fact" with
    ``isinstance`` instead of trusting a string that any writer could set.
    """

    key: str
    value: str
    at: str = field(default_factory=utcnow)
    updated_at: str = ""
    id: str = ""
    evidence: str = ""

    kind: ClassVar[str] = ""

    @property
    def stamps_at(self) -> str:
        """Last time this entry changed, falling back to creation."""
        return self.updated_at or self.at

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {f.name: getattr(self, f.name) for f in fields(self)}
        data["kind"] = self.kind
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Entry":
        """Rebuild the concrete class named by ``kind``.

        An unknown kind is skipped by the caller rather than guessed at: a
        record we cannot classify must not default to ``fact``. ``fields()``
        rather than ``__dataclass_fields__`` is deliberate - the latter counts
        the ``kind`` ClassVar as a field, which would be passed to a
        constructor that has no such parameter.
        """
        klass = _ENTRY_TYPES.get(str(raw.get("kind", "")), cls)
        known = {f.name for f in fields(klass)}
        return klass(**{k: v for k, v in raw.items() if k in known})


@dataclass
class Fact(Entry):
    """Stable project truth: still true tomorrow without new evidence."""

    kind: ClassVar[str] = KIND_FACT
    promoted_from: str = ""


@dataclass
class State(Entry):
    """Temporary task state: true for this run, worthless afterwards."""

    kind: ClassVar[str] = KIND_STATE


@dataclass
class Hypothesis(Entry):
    """A guess. Never readable as a fact, only promotable with evidence."""

    kind: ClassVar[str] = KIND_HYPOTHESIS


_ENTRY_TYPES: dict[str, type[Entry]] = {
    Fact.kind: Fact,
    State.kind: State,
    Hypothesis.kind: Hypothesis,
}


def _entry_id(kind: str, key: str, value: str) -> str:
    """Deterministic id, so re-remembering the same thing is a no-op."""
    digest = hashlib.sha256(f"{kind}\x00{key}\x00{value}".encode("utf-8")).hexdigest()
    return digest[:16]


# --- cached documents -----------------------------------------------------


@dataclass
class Doc:
    """One fetched page, deduplicated by normalised URL and content hash."""

    url: str
    content: str
    hash: str = ""
    fetched_at: str = ""
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    etag: str = ""
    last_modified: str = ""
    title: str = ""
    first_seen: str = ""
    revisions: int = 1
    truncated: bool = False
    search_text: str = ""
    aliases: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.first_seen:
            self.first_seen = self.fetched_at or utcnow()
        if not self.hash:
            self.hash = content_hash(self.content)
        if not self.search_text:
            self.search_text = _search_text(self.title, self.content)

    # -- freshness -------------------------------------------------------

    def freshness(self, now: Any = None) -> dict[str, Any]:
        """Age, TTL and staleness for this entry.

        Reported, never hidden: a cache that quietly keeps serving an expired
        page is a wrong answer with a citation attached, which is harder to
        catch than no answer at all.
        """
        moment = _to_epoch(now)
        fetched = _try_epoch(self.fetched_at)
        if fetched is None:
            return {
                "url": self.url,
                "fetched_at": self.fetched_at,
                "age_seconds": None,
                "ttl_seconds": self.ttl_seconds,
                "expires_at": "",
                "stale": True,
                "reason": "fetched_at is unreadable; treating the entry as stale",
            }
        age = moment - fetched
        return {
            "url": self.url,
            "fetched_at": self.fetched_at,
            "age_seconds": round(age, 3),
            "ttl_seconds": self.ttl_seconds,
            "expires_at": _iso(fetched + self.ttl_seconds),
            "stale": age >= self.ttl_seconds,
            "reason": "",
        }

    def is_stale(self, now: Any = None) -> bool:
        return bool(self.freshness(now)["stale"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "content": self.content,
            "hash": self.hash,
            "fetched_at": self.fetched_at,
            "ttl_seconds": self.ttl_seconds,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "title": self.title,
            "first_seen": self.first_seen,
            "revisions": self.revisions,
            "truncated": self.truncated,
            "search_text": self.search_text,
            "aliases": list(self.aliases),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Doc":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in raw.items() if k in known}
        aliases = data.get("aliases")
        if not isinstance(aliases, list):
            data["aliases"] = []
        ttl = data.get("ttl_seconds")
        if not isinstance(ttl, (int, float)) or isinstance(ttl, bool):
            data["ttl_seconds"] = DEFAULT_TTL_SECONDS
        return cls(**data)


def _search_text(title: str, content: str) -> str:
    """Bounded token soup for retrieval, capped so the index cannot grow."""
    joined = f"{title}\n{content}"[: MAX_SEARCH_TEXT_CHARS * 8]
    return " ".join(sorted(_tokens(joined)))[:MAX_SEARCH_TEXT_CHARS]


# --- the store ------------------------------------------------------------


class Knowledge:
    """Project memory plus a deduplicating documentation cache.

    One object, two files, because the two have genuinely different rules:
    memory entries are few, typed and pruned by policy; documents are bulk,
    arbitrary and pruned by freshness. Sharing a store would force one set of
    rules onto both.
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        state_ttl_seconds: float = DEFAULT_STATE_TTL_SECONDS,
        max_entries: int = MAX_ENTRIES,
        max_docs: int = MAX_DOCS,
        value_cap: int = MAX_VALUE_CHARS,
        doc_cap: int = MAX_DOC_CHARS,
    ) -> None:
        self.root = Path(root) if root is not None else DEFAULT_ROOT
        self.ttl_seconds = float(ttl_seconds)
        self.state_ttl_seconds = float(state_ttl_seconds)
        self.max_entries = int(max_entries)
        self.max_docs = int(max_docs)
        self.value_cap = int(value_cap)
        self.doc_cap = int(doc_cap)

    # -- files -----------------------------------------------------------

    @property
    def entries_path(self) -> Path:
        return self.root / ENTRIES_FILE

    @property
    def docs_path(self) -> Path:
        return self.root / DOCS_FILE

    def _ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        guard = self.root / ".gitignore"
        if not guard.exists():
            guard.write_text("*\n", encoding="utf-8")

    def _write(self, path: Path, records: Iterable[dict[str, Any]]) -> None:
        """Rewrite a whole store file atomically.

        Unlike the append-only experience ledger, these files are rewritten in
        full: they are capped (so the rewrite is bounded), and ``prune`` has to
        remove records anyway. Append-plus-index would be the only way to make
        a removal and an addition race, and that race is not worth owning for
        a file this small.
        """
        self._ensure()
        body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)
        atomic_write_text(path, body)

    # -- memory ----------------------------------------------------------

    def entries(self) -> list[Entry]:
        out: list[Entry] = []
        for raw in read_jsonl(self.entries_path):
            if str(raw.get("kind", "")) not in _ENTRY_TYPES:
                continue  # unclassifiable: refuse to guess its kind
            out.append(Entry.from_dict(raw))
        return out

    def facts(self) -> list[Fact]:
        return [e for e in self.entries() if isinstance(e, Fact)]

    def states(self) -> list[State]:
        return [e for e in self.entries() if isinstance(e, State)]

    def hypotheses(self) -> list[Hypothesis]:
        return [e for e in self.entries() if isinstance(e, Hypothesis)]

    def remember(self, key: str, value: str, kind: str) -> Entry:
        """Store one memory entry, returning it typed by ``kind``.

        ``kind`` is validated rather than defaulted: a caller that mistypes
        ``hypothesis`` must be told, not handed a fact. Re-remembering the same
        key with the same value is a no-op (no rewrite, no churn); a new value
        under an existing key supersedes the old one, because for project
        truth and task state the newest statement is the true one.
        """
        if kind not in KINDS:
            raise KnowledgeError(
                f"kind must be one of {', '.join(KINDS)}; got {kind!r}. "
                f"A hypothesis is not a fact and must not be stored as one.",
                code="BAD_KIND",
            )
        clean_key = redact_private(key).strip()
        if not clean_key:
            raise KnowledgeError("a memory entry needs a key", code="EMPTY_KEY")
        if len(clean_key) > MAX_KEY_CHARS:
            raise KnowledgeError(
                f"key exceeds {MAX_KEY_CHARS} chars; keys are labels, not content",
                code="KEY_TOO_LARGE",
            )

        clean_value = redact_private(value).strip()
        if not clean_value:
            raise KnowledgeError("a memory entry needs a value", code="EMPTY_VALUE")
        if len(clean_value) > self.value_cap:
            raise KnowledgeError(
                f"value is {len(clean_value)} chars, over the {self.value_cap} cap. "
                "Refusing rather than truncating: past this length the caller is "
                "writing a transcript, and a transcript log retrieves nothing.",
                code="VALUE_TOO_LARGE",
            )

        existing = self.entries()
        same_key = next((e for e in existing if e.kind == kind and e.key == clean_key), None)
        if same_key is not None and same_key.value == clean_value:
            return same_key

        klass = _ENTRY_TYPES[kind]
        entry = klass(
            key=clean_key,
            value=clean_value,
            at=same_key.at if same_key else utcnow(),
            updated_at=utcnow(),
            id=_entry_id(kind, clean_key, clean_value),
        )
        if same_key is None and len(existing) >= self.max_entries:
            raise KnowledgeError(
                f"memory holds {len(existing)} entries (cap {self.max_entries}). "
                "Run prune() - dropping a fact silently to make room would lose "
                "project truth, so this refuses instead.",
                code="STORE_FULL",
            )

        kept = [e for e in existing if not (e.kind == kind and e.key == clean_key)]
        kept.append(entry)
        self._write(self.entries_path, (e.to_dict() for e in kept))
        return entry

    def recall(
        self,
        query: str,
        kind: str | None = None,
        limit: int = DEFAULT_RECALL_LIMIT,
    ) -> list[dict[str, Any]]:
        """Entries relevant to ``query``, scored, with their kind attached.

        Only entries sharing a token with the query come back: an empty or
        unrelated query returns nothing rather than the whole history, which is
        the difference between memory and a log. Each result names its kind, so
        a hypothesis is visibly a hypothesis at the point of use.
        """
        if kind is not None and kind not in KINDS:
            raise KnowledgeError(
                f"kind must be one of {', '.join(KINDS)}; got {kind!r}", code="BAD_KIND"
            )
        scored: list[dict[str, Any]] = []
        for entry in self.entries():
            if kind is not None and entry.kind != kind:
                continue
            score = relevance(query, key_text=entry.key, value_text=entry.value)
            if score <= 0.0:
                continue
            scored.append({
                "key": entry.key,
                "value": entry.value,
                "kind": entry.kind,
                "score": score,
                "at": entry.at,
                "updated_at": entry.stamps_at,
                "id": entry.id,
                "evidence": entry.evidence,
            })
        scored.sort(key=lambda r: (r["score"], r["updated_at"], r["id"]), reverse=True)
        return scored[: max(0, limit)]

    # -- promotion -------------------------------------------------------

    def promote(self, entry: Hypothesis | Entry | str, evidence: str) -> Fact:
        """Turn a hypothesis into a fact, and only with evidence.

        Evidence is the whole point of the distinction: a hypothesis plus a
        plausible-sounding reason is still a hypothesis, and letting it through
        is how a swarm teaches itself its own guesses. The hypothesis record is
        replaced, not kept alongside, so the store cannot be read as holding
        both the guess and the proof.
        """
        target = self._resolve_hypothesis(entry)
        clean = redact_private(evidence).strip()
        if not clean:
            raise KnowledgeError(
                "refusing to promote: evidence is required. A hypothesis without "
                "evidence is still a hypothesis.",
                code="NO_EVIDENCE",
            )
        fact = Fact(
            key=target.key,
            value=target.value,
            at=target.at,
            updated_at=utcnow(),
            id=_entry_id(KIND_FACT, target.key, target.value),
            evidence=clean,
            promoted_from=target.id,
        )
        kept = [e for e in self.entries() if e.id != target.id]
        kept.append(fact)
        self._write(self.entries_path, (e.to_dict() for e in kept))
        return fact

    def _resolve_hypothesis(self, entry: Hypothesis | Entry | str) -> Hypothesis:
        """Find the stored hypothesis the caller means, or say why not."""
        if isinstance(entry, Hypothesis):
            found = next((e for e in self.entries() if e.id and e.id == entry.id), None)
            if found is None:
                found = next(
                    (e for e in self.entries()
                     if e.kind == KIND_HYPOTHESIS and e.key == entry.key),
                    None,
                )
            if found is None:
                raise KnowledgeError(
                    f"no stored hypothesis for {entry.key!r}; remember it first",
                    code="NO_SUCH_ENTRY",
                )
            return found  # type: ignore[return-value]
        if isinstance(entry, Entry):
            raise KnowledgeError(
                f"{type(entry).__name__} is not a hypothesis; only hypotheses are "
                "promotable. To correct a fact, remember it again.",
                code="NOT_A_HYPOTHESIS",
            )
        if isinstance(entry, str):
            text = entry.strip()
            candidates = [e for e in self.entries() if e.id == text or e.key == text]
            if not candidates:
                raise KnowledgeError(f"no entry matching {text!r}", code="NO_SUCH_ENTRY")
            # A key can name several kinds at once; the hypothesis is the one
            # the caller is asking to promote, so it wins.
            found = next(
                (e for e in candidates if e.kind == KIND_HYPOTHESIS), candidates[0]
            )
            if found.kind != KIND_HYPOTHESIS:
                raise KnowledgeError(
                    f"{text!r} is a {found.kind}, not a hypothesis; only hypotheses "
                    "are promotable.",
                    code="NOT_A_HYPOTHESIS",
                )
            return found  # type: ignore[return-value]
        raise KnowledgeError(
            "promote needs a Hypothesis or its id/key", code="BAD_ENTRY"
        )

    # -- document cache --------------------------------------------------

    def docs(self) -> list[Doc]:
        return [Doc.from_dict(raw) for raw in read_jsonl(self.docs_path)]

    def _doc_index(self, docs: list[Doc]) -> dict[str, Doc]:
        """normalised url -> doc, including aliases joined by earlier dedupes."""
        index: dict[str, Doc] = {}
        for doc in docs:
            index[doc.url] = doc
            for alias in doc.aliases:
                index.setdefault(alias, doc)
        return index

    def cache_doc(
        self,
        url: str,
        content: str,
        fetched_at: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        *,
        ttl: float | None = None,
        title: str = "",
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Store a fetched page under its normalised URL.

        Three outcomes matter and are told apart in the returned ``reason``:

        * ``created`` / ``updated`` - the content for this URL changed.
        * ``unchanged`` - the same bytes came back. Nothing is written, so a
          polling caller cannot churn the store, and *freshness is left alone*:
          bumping ``fetched_at`` on an unchanged re-fetch would let a caller
          keep a TTL-expired page looking forever fresh, which defeats the TTL
          it just paid for. Content is the only thing that moves the clock.
        * ``alias`` - this URL's bytes are already cached under a different
          URL, so the new URL is recorded as an alias instead of duplicating
          the document. ``lookup`` resolves either spelling.
        """
        moment = _to_epoch(fetched_at)
        stamp = fetched_at or _iso(moment)
        try:
            ttl_seconds = float(ttl) if ttl is not None else self.ttl_seconds
        except (TypeError, ValueError) as exc:
            raise KnowledgeError(f"ttl must be a number, got {ttl!r}", code="BAD_TTL") from exc

        key = normalise_url(url)
        raw = "" if content is None else str(content)
        clean = redact_secrets(raw)
        stored_content = clean[: self.doc_cap]
        truncated = len(clean) > self.doc_cap
        digest = content_hash(stored_content)

        docs = self.docs()
        evicted: list[str] = []
        index = self._doc_index(docs)
        existing = index.get(key)

        if existing is not None and existing.hash == digest and not refresh:
            return {
                "url": key,
                "reason": "unchanged",
                "stored": False,
                "changed": False,
                "truncated": existing.truncated,
                "doc": existing,
                "evicted": evicted,
            }

        if existing is not None:
            # Update in place: the URL already has an identity here.
            same_content = existing.hash == digest
            existing.content = stored_content
            existing.hash = digest
            existing.fetched_at = stamp
            existing.ttl_seconds = ttl_seconds
            existing.etag = etag or ""
            existing.last_modified = last_modified or ""
            existing.title = redact_secrets(title)
            existing.revisions += 1
            existing.truncated = truncated
            existing.search_text = _search_text(existing.title, stored_content)
            self._write(self.docs_path, (d.to_dict() for d in docs))
            return {
                "url": key,
                # ``refreshed`` is only reachable with refresh=True on
                # identical bytes: the caller chose to move the clock without
                # the content moving. ``changed`` stays False so nobody
                # downstream reads it as new information.
                "reason": "refreshed" if same_content else "updated",
                "stored": True,
                "changed": not same_content,
                "truncated": truncated,
                "doc": existing,
                "evicted": evicted,
            }

        sibling = next((d for d in docs if d.hash == digest), None)
        if sibling is not None:
            # Same bytes, different URL: join them instead of paying for a
            # second copy of a page the store already answers for.
            if key not in sibling.aliases and sibling.url != key:
                sibling.aliases.append(key)
                self._write(self.docs_path, (d.to_dict() for d in docs))
            return {
                "url": key,
                "reason": "alias",
                "stored": False,
                "changed": False,
                "truncated": sibling.truncated,
                "doc": sibling,
                "evicted": evicted,
            }

        doc = Doc(
            url=key,
            content=stored_content,
            hash=digest,
            fetched_at=stamp,
            ttl_seconds=ttl_seconds,
            etag=etag or "",
            last_modified=last_modified or "",
            title=redact_secrets(title),
            first_seen=stamp,
            truncated=truncated,
        )
        if len(docs) >= self.max_docs:
            # A cache may evict; a memory store may not. The victim is the
            # stalest entry and it is named in the result, never dropped
            # silently.
            victim = min(docs, key=lambda d: _try_epoch(d.fetched_at) or 0.0)
            docs = [d for d in docs if d is not victim]
            evicted.append(victim.url)
        docs.append(doc)
        self._write(self.docs_path, (d.to_dict() for d in docs))
        return {
            "url": key,
            "reason": "created",
            "stored": True,
            "changed": True,
            "truncated": truncated,
            "doc": doc,
            "evicted": evicted,
        }

    def lookup(self, query_or_url: str, *, now: Any = None) -> dict[str, Any]:
        """Find the cached answer for a URL or a search phrase.

        This is the dedupe path: a caller asking for the same thing twice gets
        the stored entry and its freshness instead of triggering a new fetch.
        ``hit`` is always present so a caller never has to check for ``None``;
        a miss returns ``doc: None`` and ``freshness: None``.
        """
        text = (query_or_url or "").strip()
        if not text:
            return self._miss()

        docs = self.docs()
        if looks_like_url(text):
            try:
                key = normalise_url(text)
            except KnowledgeError:
                key = ""
            if key:
                found = self._doc_index(docs).get(key)
                if found is not None:
                    return self._hit(found, matched_on="url", score=1.0, now=now,
                                     requested=key)

        best: Doc | None = None
        best_score = 0.0
        for doc in docs:
            score = relevance(
                text,
                key_text=f"{doc.url} {doc.title}",
                value_text=doc.search_text,
            )
            if score > best_score:
                best, best_score = doc, score
        if best is not None and best_score >= LOOKUP_MIN_SCORE:
            return self._hit(best, matched_on="text", score=best_score, now=now,
                             requested=text)
        return self._miss()

    @staticmethod
    def _miss() -> dict[str, Any]:
        return {
            "hit": False,
            "matched_on": "",
            "score": 0.0,
            "requested": "",
            "url": "",
            "stale": False,
            "freshness": None,
            "excerpt": "",
            "doc": None,
        }

    def _hit(self, doc: Doc, *, matched_on: str, score: float, now: Any,
             requested: str) -> dict[str, Any]:
        report = doc.freshness(now)
        return {
            "hit": True,
            "matched_on": matched_on,
            "score": round(float(score), 4),
            "requested": requested,
            "url": doc.url,
            "stale": bool(report["stale"]),
            "freshness": report,
            "excerpt": doc.content[:LOOKUP_EXCERPT_CHARS],
            "doc": doc,
        }

    # -- freshness reports -----------------------------------------------

    def is_stale(self, url: str, now: Any = None) -> bool:
        """Whether the cached document for ``url`` is past its TTL."""
        doc = self._doc_index(self.docs()).get(normalise_url(url))
        if doc is None:
            raise KnowledgeError(
                f"nothing cached for {url!r}; freshness is unknown, not fresh",
                code="NO_SUCH_DOC",
            )
        return doc.is_stale(now)

    def freshness(self, now: Any = None) -> dict[str, Any]:
        """Store-wide freshness report.

        Stale documents are listed, not filtered: the caller deciding what to
        re-fetch needs to see them.
        """
        moment = _to_epoch(now)
        docs = self.docs()
        entries = self.entries()
        reports = [d.freshness(moment) for d in docs]
        return {
            "generated_at": _iso(moment),
            "ttl_seconds": self.ttl_seconds,
            "total": len(reports),
            "fresh": sum(1 for r in reports if not r["stale"]),
            "stale": sum(1 for r in reports if r["stale"]),
            "docs": reports,
            "memory": {
                "total": len(entries),
                "by_kind": {k: sum(1 for e in entries if e.kind == k) for k in KINDS},
            },
        }

    def stale_docs(self, now: Any = None) -> list[Doc]:
        return [d for d in self.docs() if d.is_stale(now)]

    # -- pruning ---------------------------------------------------------

    def prune(
        self,
        *,
        now: Any = None,
        max_entries: int | None = None,
        max_docs: int | None = None,
        older_than: float | None = None,
    ) -> dict[str, Any]:
        """Drop what is redundant, expired or oversized. Report every drop.

        Nothing leaves silently: each removal comes back with a reason, because
        "the store shrank and nobody knows why" is indistinguishable from
        corruption. Facts are evicted last, after every state entry, since
        dropping project truth to keep a scratch note is exactly backwards.
        """
        moment = _to_epoch(now)
        state_ttl = self.state_ttl_seconds if older_than is None else float(older_than)
        entry_cap = self.max_entries if max_entries is None else int(max_entries)
        doc_cap = self.max_docs if max_docs is None else int(max_docs)

        removed_entries: list[dict[str, Any]] = []
        removed_docs: list[dict[str, Any]] = []
        merged_docs: list[dict[str, Any]] = []

        # -- documents ---------------------------------------------------
        docs = self.docs()
        # Identical bytes under two URLs (usually written by an older build or
        # by hand - the write path aliases eagerly): keep one, remember the
        # other as an alias so lookup still resolves it.
        by_hash: dict[str, Doc] = {}
        deduped: list[Doc] = []
        for doc in sorted(docs, key=_epoch_of, reverse=True):
            keeper = by_hash.get(doc.hash)
            if keeper is None:
                by_hash[doc.hash] = doc
                deduped.append(doc)
                continue
            for alias in [doc.url, *doc.aliases]:
                if alias != keeper.url and alias not in keeper.aliases:
                    keeper.aliases.append(alias)
            removed_docs.append({"url": doc.url, "reason": "duplicate_content"})
            merged_docs.append({"url": doc.url, "into": keeper.url})
        docs = deduped
        if len(docs) > doc_cap:
            docs.sort(key=_epoch_of)
            for victim in docs[: len(docs) - doc_cap]:
                removed_docs.append({"url": victim.url, "reason": "over_doc_cap"})
            docs = docs[len(docs) - doc_cap:]

        # -- memory ------------------------------------------------------
        entries = self.entries()
        seen_keys: set[tuple[str, str]] = set()
        kept: list[Entry] = []
        for entry in sorted(entries, key=lambda e: e.stamps_at, reverse=True):
            identity = (entry.kind, entry.key)
            if identity in seen_keys:
                removed_entries.append({"kind": entry.kind, "key": entry.key,
                                        "reason": "duplicate_key"})
                continue
            if entry.kind == KIND_STATE and moment - _stamp_epoch(entry) >= state_ttl:
                removed_entries.append({"kind": entry.kind, "key": entry.key,
                                        "reason": "expired_state"})
                continue
            if len(entry.value) > self.value_cap:
                removed_entries.append({"kind": entry.kind, "key": entry.key,
                                        "reason": "oversized"})
                continue
            seen_keys.add(identity)
            kept.append(entry)

        if len(kept) > entry_cap:
            priority = {KIND_STATE: 0, KIND_HYPOTHESIS: 1, KIND_FACT: 2}
            ranked = sorted(kept, key=lambda e: (priority[e.kind], _stamp_epoch(e)))
            survivors = {id(e) for e in ranked[len(ranked) - entry_cap:]}
            fresh: list[Entry] = []
            for entry in kept:
                if id(entry) in survivors:
                    fresh.append(entry)
                else:
                    removed_entries.append({"kind": entry.kind, "key": entry.key,
                                            "reason": "over_entry_cap"})
            kept = fresh

        # -- write only what changed ------------------------------------
        if removed_entries:
            self._write(self.entries_path, (e.to_dict() for e in kept))
        remaining_entries = kept if removed_entries else entries
        if removed_docs:
            self._write(self.docs_path, (d.to_dict() for d in docs))
        remaining_docs = docs if removed_docs else self.docs()

        return {
            "at": _iso(moment),
            "entries_removed": len(removed_entries),
            "docs_removed": len(removed_docs),
            "removed_entries": removed_entries,
            "removed_docs": removed_docs,
            "merged_docs": merged_docs,
            "remaining": {
                "entries": len(remaining_entries),
                "docs": len(remaining_docs),
                "by_kind": {
                    k: sum(1 for e in remaining_entries if e.kind == k) for k in KINDS
                },
            },
        }

    def counts(self) -> dict[str, int]:
        entries = self.entries()
        return {
            "fact": sum(1 for e in entries if e.kind == KIND_FACT),
            "state": sum(1 for e in entries if e.kind == KIND_STATE),
            "hypothesis": sum(1 for e in entries if e.kind == KIND_HYPOTHESIS),
            "docs": len(self.docs()),
        }


def _epoch_of(doc: Doc) -> float:
    return _try_epoch(doc.fetched_at) or 0.0


def _stamp_epoch(entry: Entry) -> float:
    return _try_epoch(entry.stamps_at) or 0.0


# --- module-level convenience --------------------------------------------

_DEFAULT: Knowledge | None = None


def default_store() -> Knowledge:
    """The process-wide store the CLI uses.

    Agents should construct :class:`Knowledge` with an explicit root so their
    writes land in the workspace they are actually working on; this exists for
    the command layer, which has one workspace per invocation.
    """
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Knowledge()
    return _DEFAULT


def reset_default_store() -> None:
    """Drop the cached default store (test and CLI-lifecycle hook)."""
    global _DEFAULT
    _DEFAULT = None


def remember(key: str, value: str, kind: str) -> Entry:
    return default_store().remember(key, value, kind)


def recall(query: str, kind: str | None = None,
           limit: int = DEFAULT_RECALL_LIMIT) -> list[dict[str, Any]]:
    return default_store().recall(query, kind, limit)


def promote(entry: Hypothesis | Entry | str, evidence: str) -> Fact:
    return default_store().promote(entry, evidence)


def cache_doc(url: str, content: str, fetched_at: str | None = None,
              etag: str | None = None, last_modified: str | None = None,
              **kwargs: Any) -> dict[str, Any]:
    return default_store().cache_doc(url, content, fetched_at, etag,
                                     last_modified, **kwargs)


def lookup(query_or_url: str, **kwargs: Any) -> dict[str, Any]:
    return default_store().lookup(query_or_url, **kwargs)


def is_stale(url: str, now: Any = None) -> bool:
    return default_store().is_stale(url, now)


def freshness(now: Any = None) -> dict[str, Any]:
    return default_store().freshness(now)


def prune(**kwargs: Any) -> dict[str, Any]:
    return default_store().prune(**kwargs)


__all__ = [
    "DEFAULT_ROOT",
    "ENTRIES_FILE",
    "DOCS_FILE",
    "KIND_FACT",
    "KIND_STATE",
    "KIND_HYPOTHESIS",
    "KINDS",
    "DEFAULT_TTL_SECONDS",
    "DEFAULT_STATE_TTL_SECONDS",
    "MAX_VALUE_CHARS",
    "MAX_KEY_CHARS",
    "MAX_DOC_CHARS",
    "MAX_ENTRIES",
    "MAX_DOCS",
    "LOOKUP_MIN_SCORE",
    "DEFAULT_RECALL_LIMIT",
    "KnowledgeError",
    "Doc",
    "Entry",
    "Fact",
    "State",
    "Hypothesis",
    "Knowledge",
    "redact_secrets",
    "redact_private",
    "content_hash",
    "normalise_url",
    "looks_like_url",
    "relevance",
    "default_store",
    "reset_default_store",
    "remember",
    "recall",
    "promote",
    "cache_doc",
    "lookup",
    "is_stale",
    "freshness",
    "prune",
]

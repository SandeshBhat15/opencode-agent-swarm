"""Model routing over a *recorded* catalog and *recorded* outcomes.

This is a decision engine, not a client. It never calls a model, never opens a
socket, and never invents a price or a capability. WHY that restraint is the
whole point:

* A price the catalog did not report is stored as ``None`` and stays ``None``.
  Treating an unknown price as ``0`` would quietly turn "we do not know" into
  "free" and send paid traffic past a budget ceiling - so an unknown price
  *blocks* until the caller explicitly accepts the risk.
* A capability the host did not report is stored as ``None`` (unknown), never
  defaulted to ``True`` (which fabricates capability) or ``False`` (which
  fabricates incapability). An unknown capability downgrades a decision to
  ``assumed`` and names the exact gap.
* A model pin is an operator guarantee, so it is honoured absolutely. The only
  reasons to deviate are that the pinned model is gone, disabled, deprecated or
  provably incapable - and even then the router refuses loudly
  (``pinned-missing`` / ``pinned-incompatible``) instead of silently routing
  around it.
* Preference is driven by *recorded verified completions*, and that signal is
  OFF until at least :data:`MIN_COMPARABLE_ATTEMPTS` comparable attempts exist.
  Below that line the honest answer is "too few to prefer", so the router falls
  back to the host's configured default rather than guessing from noise.

Everything the router learns lives under a registry directory so a run survives
across processes: ``catalog.json`` (imported), ``outcomes.jsonl`` (append-only)
and ``router-budget.json`` (spent vs ceiling).
"""

from __future__ import annotations

import builtins
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import BudgetExceeded, ConfigError
from .store import append_jsonl, atomic_write_json, read_json, read_jsonl, utcnow

#: The fixed category vocabulary. A category is a *kind of work*, and each one
#: demands exactly one capability from the model (see CATEGORY_REQUIRES).
CATEGORIES: tuple[str, ...] = (
    "classify",
    "plan",
    "research",
    "code",
    "visual",
    "verify",
    "edit",
    "general",
)

#: Every status a decision may carry. Kept as a tuple so the CLI and tests can
#: assert membership instead of matching on ad-hoc strings.
STATUSES: tuple[str, ...] = (
    "ok",
    "assumed",
    "blocked",
    "pinned-missing",
    "pinned-incompatible",
)

#: Failure causes. ``unknown`` is deliberately last: it is *our* uncertainty and
#: must never be re-attributed to the model.
CAUSES: tuple[str, ...] = (
    "provider",
    "orchestration",
    "instructions",
    "tools",
    "budget",
    "cancelled",
    "model",
    "unknown",
)

#: The capability vocabulary the router reasons about. Hosts may report more or
#: fewer fields; anything not reported stays unknown.
CAPABILITY_KEYS: tuple[str, ...] = (
    "text",
    "tools",
    "vision",
    "reasoning",
    "code",
    "audio",
)

#: Which capability each category requires. A category is refused for a model
#: only when the capability is *reported False*; an unreported one downgrades
#: the decision to ``assumed`` rather than failing it.
CATEGORY_REQUIRES: dict[str, str] = {
    "classify": "text",
    "plan": "reasoning",
    "research": "tools",
    "code": "code",
    "visual": "vision",
    "verify": "reasoning",
    "edit": "tools",
    "general": "text",
}

#: The evidence floor. Below this many comparable attempts a record is noise,
#: and the router says so ("too few to prefer") instead of pretending otherwise.
MIN_COMPARABLE_ATTEMPTS = 5

#: A finite escalation budget. Escalating to a new model is expensive and
#: noisy, so it is capped; exhausting it returns ``blocked`` rather than
#: thrashing through the whole catalog.
MAX_ESCALATIONS = 3

#: ``provider/model`` with an optional ``#variant`` - the same grammar the page
#: linter enforces (contracts.SW006), so a pin that validates there is parseable
#: here.
PIN_RE = re.compile(r"^([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+)(?:#([A-Za-z0-9_.\-]+))?$")

#: Registry layout, deliberately beside the run state it belongs to.
DEFAULT_REGISTRY = Path(".opencode") / "swarm" / "models"
CATALOG_FILE = "catalog.json"
OUTCOMES_FILE = "outcomes.jsonl"
BUDGET_FILE = "router-budget.json"

#: Outcomes a caller may record. Only ``verified`` is evidence.
OUTCOMES: tuple[str, ...] = ("verified", "failed", "unverified", "rejected")

#: Substrings that mark a component as part of the machinery that JUDGES work.
#: A model that can edit one of these can weaken the check that would catch it,
#: so changes here never auto-promote.
VERIFICATION_TARGET_HINTS: tuple[str, ...] = (
    "test",
    "spec",
    "gate",
    "verify",
    "verification",
    "recover",
    "recovery",
    "qc",
    "evidence",
    "criteria",
    "judge",
    "assert",
)


# ---------------------------------------------------------------------------
# one-sentence reasons
# ---------------------------------------------------------------------------


def _one_sentence(*fragments: str) -> str:
    """Join fragments into a single sentence.

    WHY: the decision contract promises a reason that is *exactly one sentence*,
    so callers can render it in a status line without reflowing. Any full stop
    inside a fragment is collapsed to a comma clause, guaranteeing the result
    contains no internal sentence break.
    """
    parts: list[str] = []
    for frag in fragments:
        text = str(frag).strip()
        while text.endswith("."):
            text = text[:-1].rstrip()
        text = re.sub(r"\.\s+", ", ", text)
        if text:
            parts.append(text)
    sentence = ", ".join(parts).strip()
    if not sentence.endswith("."):
        sentence += "."
    return sentence


def is_one_sentence(text: str) -> bool:
    """True when ``text`` is a single terminal sentence (no internal breaks)."""
    stripped = text.strip()
    return (
        stripped.endswith(".")
        and ". " not in stripped
        and "? " not in stripped
        and "! " not in stripped
        and "\n" not in stripped
    )


# ---------------------------------------------------------------------------
# model entries + catalog import
# ---------------------------------------------------------------------------


def _normalise_capabilities(raw: Any) -> dict[str, bool | None]:
    """Copy only what the host reported; never default a capability.

    A dict may carry ``bool`` or ``None`` per key. A list is read as "these are
    supported", but every *other* capability stays unknown (``None``) rather
    than being defaulted to ``False`` - absence from a list is not proof of
    absence of the capability.
    """
    out: dict[str, bool | None] = {}
    if raw is None:
        return out
    if isinstance(raw, dict):
        for key, value in raw.items():
            name = str(key)
            if value is None:
                out[name] = None
            elif isinstance(value, bool):
                out[name] = value
            else:
                raise ConfigError(
                    f"capability {name!r} must be true, false or null, got "
                    f"{type(value).__name__}",
                    code="CONFIG_INVALID",
                )
        return out
    if isinstance(raw, (list, tuple, set)):
        for key in raw:
            out[str(key)] = True
        return out
    raise ConfigError(
        f"capabilities must be an object or a list, got {type(raw).__name__}",
        code="CONFIG_INVALID",
    )


def _opt_price(value: Any, field_name: str) -> float | None:
    """A host price, or ``None``. Never coerced to a default."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(
            f"{field_name} must be a number or null, got {type(value).__name__}",
            code="CONFIG_INVALID",
        )
    if value < 0:
        raise ConfigError(f"{field_name} must not be negative", code="CONFIG_INVALID")
    return float(value)


@dataclass
class ModelEntry:
    """One catalog model, exactly as the host reported it (plus ``None``s)."""

    provider: str
    model: str
    variant: str | None = None
    status: str | None = None
    capabilities: dict[str, bool | None] = field(default_factory=dict)
    price_in_per_mtok: float | None = None
    price_out_per_mtok: float | None = None

    @property
    def key(self) -> str:
        base = f"{self.provider}/{self.model}"
        return f"{base}#{self.variant}" if self.variant else base

    def capability(self, name: str) -> bool | None:
        """Reported capability value, or ``None`` when the host stayed silent."""
        return self.capabilities.get(name)

    @property
    def priced(self) -> bool:
        """True only when both prices were reported - an unknown price is not free."""
        return self.price_in_per_mtok is not None and self.price_out_per_mtok is not None

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any], *, provider: str = "", model: str = "") -> "ModelEntry":
        if not isinstance(raw, Mapping):
            raise ConfigError("each catalog model must be an object", code="CONFIG_INVALID")
        resolved_provider = str(raw.get("provider") or provider)
        resolved_model = str(raw.get("model") or model)
        if not resolved_provider or not resolved_model:
            raise ConfigError(
                "each catalog model needs a provider and a model", code="CONFIG_INVALID"
            )
        variant = raw.get("variant")
        status = raw.get("status")
        return cls(
            provider=resolved_provider,
            model=resolved_model,
            variant=str(variant) if variant else None,
            status=str(status) if status is not None else None,
            capabilities=_normalise_capabilities(raw.get("capabilities")),
            price_in_per_mtok=_opt_price(
                raw.get("price_in_per_mtok", raw.get("input_price")), "price_in_per_mtok"
            ),
            price_out_per_mtok=_opt_price(
                raw.get("price_out_per_mtok", raw.get("output_price")), "price_out_per_mtok"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iter_raw_models(payload: Any) -> list[tuple[str, str, Mapping[str, Any]]]:
    """Unpack the three catalog shapes hosts actually emit.

    Returns ``(provider, model, raw)`` triples; provider/model may be empty and
    then get filled from the record itself. Accepting a list, a ``{"models": []}``
    object and a ``{"provider/model": {...}}`` map keeps the importer usable
    without asking the host to reshape its data.
    """
    triples: list[tuple[str, str, Mapping[str, Any]]] = []
    if isinstance(payload, list):
        for item in payload:
            triples.append(("", "", item))
        return triples
    if isinstance(payload, dict):
        models = payload.get("models")
        if isinstance(models, list):
            for item in models:
                triples.append(("", "", item))
            return triples
        if models is None:
            # keyed map: "provider/model" -> record
            for key, item in payload.items():
                if isinstance(item, Mapping):
                    base = str(key)
                    provider, model = (base.split("/", 1) + [""])[:2] if "/" in base else ("", base)
                    triples.append((provider, model, item))
            return triples
    raise ConfigError(
        "catalog must be a list, a {'models': [...]} object, or a keyed object",
        code="CONFIG_INVALID",
    )


def catalog_path(registry: Path | str = DEFAULT_REGISTRY) -> Path:
    return Path(registry) / CATALOG_FILE


def transform_opencode_catalog(data: Any) -> dict[str, Any]:
    """Transform OpenCode's ``/api/model`` response into a router catalog.

    OpenCode reports capabilities as ``{"tools": bool, "input": [..], "output": [..]}``
    and prices per-token; the router expects ``{"text": bool, "tools": bool, ...}``
    and per-million-token prices. We only carry forward what OpenCode confirms;
    everything else stays unknown (None) rather than being defaulted.
    """
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
        records = data["data"]
    elif isinstance(data, list):
        records = data
    elif isinstance(data, dict) and "models" in data:
        records = data["models"]
    else:
        raise ConfigError("expected an OpenCode /api/model payload", code="CONFIG_INVALID")

    out: list[dict[str, Any]] = []
    for raw in records:
        if not isinstance(raw, Mapping):
            continue
        provider = str(raw.get("providerID") or raw.get("provider") or "")
        model = str(raw.get("modelID") or raw.get("id") or raw.get("model") or "")
        if not provider or not model:
            continue
        if raw.get("enabled") is False or raw.get("status") not in (None, "active"):
            continue
        caps_raw = raw.get("capabilities") or {}
        caps: dict[str, Any] = {}
        if isinstance(caps_raw, Mapping):
            if caps_raw.get("tools") is True:
                caps["tools"] = True
            inputs = caps_raw.get("input")
            if isinstance(inputs, (list, tuple)):
                if "text" in inputs:
                    caps["text"] = True
                if "image" in inputs or "video" in inputs:
                    caps["vision"] = True
        # price: OpenCode reports per-token; convert to per-million.
        price_in = None
        price_out = None
        cost = raw.get("cost")
        if isinstance(cost, list) and cost:
            c0 = cost[0]
            if isinstance(c0, Mapping):
                if "input" in c0 and c0["input"] is not None:
                    price_in = float(c0["input"]) * 1_000_000
                if "output" in c0 and c0["output"] is not None:
                    price_out = float(c0["output"]) * 1_000_000
        out.append({
            "provider": provider,
            "model": model,
            "status": str(raw.get("status") or "active"),
            "capabilities": caps,
            "price_in_per_mtok": price_in,
            "price_out_per_mtok": price_out,
        })
    if not out:
        raise ConfigError("no usable models in the payload", code="CONFIG_INVALID")
    return {"models": out}


def import_catalog(path: Path | str, source: str, *, registry: Path | str = DEFAULT_REGISTRY) -> dict[str, Any]:
    """Read a host catalog and persist it verbatim (unknowns preserved).

    ``source`` is recorded so a later audit knows *who* supplied the facts. The
    importer never fills a gap: a field the host omitted is persisted as ``None``.
    """
    raw_payload = read_json(Path(path))
    if raw_payload is None:
        raise ConfigError(f"no catalog at {path}", code="CONFIG_INVALID")
    entries: list[ModelEntry] = []
    seen: set[str] = set()
    for provider, model, raw in _iter_raw_models(raw_payload):
        entry = ModelEntry.from_raw(raw, provider=provider, model=model)
        if entry.key in seen:
            raise ConfigError(f"duplicate catalog entry {entry.key}", code="CONFIG_INVALID")
        seen.add(entry.key)
        entries.append(entry)
    if not entries:
        raise ConfigError("catalog contains no models", code="CONFIG_INVALID")
    payload = {
        "source": str(source),
        "imported_at": utcnow(),
        "models": [entry.to_dict() for entry in entries],
    }
    atomic_write_json(catalog_path(registry), payload)
    return {
        "source": str(source),
        "count": len(entries),
        "models": [entry.key for entry in entries],
    }


def load_catalog(registry: Path | str = DEFAULT_REGISTRY) -> list[ModelEntry]:
    """Load the imported catalog. A missing catalog is refused, not defaulted."""
    raw = read_json(catalog_path(registry))
    if raw is None:
        raise ConfigError(
            f"no catalog under {registry}; run import_catalog() first", code="CONFIG_INVALID"
        )
    entries: list[ModelEntry] = []
    for item in (raw or {}).get("models", []):
        entries.append(
            ModelEntry(
                provider=str(item.get("provider", "")),
                model=str(item.get("model", "")),
                variant=item.get("variant"),
                status=item.get("status"),
                capabilities=dict(item.get("capabilities") or {}),
                price_in_per_mtok=item.get("price_in_per_mtok"),
                price_out_per_mtok=item.get("price_out_per_mtok"),
            )
        )
    return entries


# ---------------------------------------------------------------------------
# pins
# ---------------------------------------------------------------------------


def _parse_pin(pin: str) -> tuple[str, str, str | None]:
    match = PIN_RE.match(str(pin).strip())
    if not match:
        raise ConfigError(
            f"{pin!r} is not 'provider/model' with optional '#variant'", code="CONFIG_INVALID"
        )
    return match.group(1), match.group(2), match.group(3)


def _find_entry(
    entries: Sequence[ModelEntry], provider: str, model: str, variant: str | None
) -> ModelEntry | None:
    for entry in entries:
        if entry.provider == provider and entry.model == model and entry.variant == variant:
            return entry
    return None


def _usable(entry: ModelEntry) -> bool:
    """Disabled and deprecated models are excluded; an unreported status is not."""
    return entry.status not in ("disabled", "deprecated")


def _capability_state(entry: ModelEntry, category: str) -> tuple[str, str]:
    """``(state, capability)`` where state is capable | unknown | incapable."""
    required = CATEGORY_REQUIRES[category]
    value = entry.capability(required)
    if value is False:
        return "incapable", required
    if value is None:
        return "unknown", required
    return "capable", required


def _candidate_keys(entries: Sequence[ModelEntry], category: str, limit: int = 6) -> list[str]:
    keys = [
        entry.key
        for entry in entries
        if _usable(entry) and _capability_state(entry, category)[0] != "incapable"
    ]
    return keys[:limit]


# ---------------------------------------------------------------------------
# budgets
# ---------------------------------------------------------------------------


def _budget_path(registry: Path | str = DEFAULT_REGISTRY) -> Path:
    return Path(registry) / BUDGET_FILE


def _remaining_cost(registry: Path | str = DEFAULT_REGISTRY) -> float | None:
    """Remaining cost ceiling from the registry, without creating the file."""
    raw = read_json(_budget_path(registry), default={}) or {}
    ceiling = raw.get("max_cost")
    if ceiling is None:
        return None
    spent = float(raw.get("spent_cost", 0.0) or 0.0)
    return float(ceiling) - spent


def budget(
    max_cost: float | None = None,
    max_tokens: int | None = None,
    *,
    registry: Path | str = DEFAULT_REGISTRY,
    charge_cost: float | None = None,
    charge_tokens: int | None = None,
) -> dict[str, Any]:
    """Report spent vs ceiling and refuse a charge that would not fit.

    Passing only ceilings re-declares them; passing ``charge_*`` adds spend and
    raises :class:`BudgetExceeded` if the charge would cross a ceiling, so the
    refusal happens *before* the money is spent rather than after.
    """
    state = {
        "max_cost": None,
        "max_tokens": None,
        "spent_cost": 0.0,
        "spent_tokens": 0,
    }
    raw = read_json(_budget_path(registry), default={}) or {}
    state["max_cost"] = raw.get("max_cost")
    state["max_tokens"] = raw.get("max_tokens")
    state["spent_cost"] = float(raw.get("spent_cost", 0.0) or 0.0)
    state["spent_tokens"] = int(raw.get("spent_tokens", 0) or 0)
    if max_cost is not None:
        state["max_cost"] = float(max_cost)
    if max_tokens is not None:
        state["max_tokens"] = int(max_tokens)
    if charge_cost:
        projected = state["spent_cost"] + float(charge_cost)
        if state["max_cost"] is not None and projected > state["max_cost"]:
            raise BudgetExceeded(
                f"charging {charge_cost} would spend {projected} against "
                f"max_cost={state['max_cost']}; refused",
                code="BUDGET_EXCEEDED",
            )
        state["spent_cost"] = projected
    if charge_tokens:
        projected = state["spent_tokens"] + int(charge_tokens)
        if state["max_tokens"] is not None and projected > state["max_tokens"]:
            raise BudgetExceeded(
                f"charging {charge_tokens} tokens would spend {projected} against "
                f"max_tokens={state['max_tokens']}; refused",
                code="BUDGET_EXCEEDED",
            )
        state["spent_tokens"] = projected
    state["remaining_cost"] = (
        None if state["max_cost"] is None else state["max_cost"] - state["spent_cost"]
    )
    state["remaining_tokens"] = (
        None if state["max_tokens"] is None else state["max_tokens"] - state["spent_tokens"]
    )
    atomic_write_json(_budget_path(registry), state)
    return state


def _cost_of(
    entry: ModelEntry,
    est_tokens_in: int,
    est_tokens_out: int,
    estimated_cost: float | None,
) -> tuple[float | None, bool]:
    """``(cost, priced)`` for one call. ``priced`` is False when price is unknown."""
    if estimated_cost is not None:
        return float(estimated_cost), True
    if not entry.priced:
        return None, False
    cost = (
        est_tokens_in / 1_000_000 * float(entry.price_in_per_mtok)
        + est_tokens_out / 1_000_000 * float(entry.price_out_per_mtok)
    )
    return cost, True


def _budget_block(
    entry: ModelEntry,
    cost: float | None,
    priced: bool,
    budget_remaining: float | None,
    accept_unknown_price: bool,
) -> str | None:
    """A clause explaining why the model cannot be afforded, or ``None``.

    An unknown price is *not* free: without an explicit acceptance it blocks.
    """
    if not priced:
        if accept_unknown_price:
            return None
        return (
            f"{entry.key} has no catalog price, so it is not treated as free, and "
            "accept_unknown_price was not set"
        )
    if budget_remaining is not None and cost is not None and cost > budget_remaining:
        return (
            f"{entry.key} would cost {cost:.4f} against the remaining budget of "
            f"{budget_remaining:.4f}, so it was dropped"
        )
    return None


# ---------------------------------------------------------------------------
# recorded outcomes
# ---------------------------------------------------------------------------


def outcomes_path(registry: Path | str = DEFAULT_REGISTRY) -> Path:
    return Path(registry) / OUTCOMES_FILE


def _read_outcomes(registry: Path | str = DEFAULT_REGISTRY) -> list[dict[str, Any]]:
    return read_jsonl(outcomes_path(registry))


def _same_model(left: str | None, right: str | None) -> bool:
    """Compare two model references, tolerating a missing/extra ``#variant``."""
    if not left or not right:
        return False
    return str(left).split("#", 1)[0] == str(right).split("#", 1)[0]


def record(
    provider: str,
    model: str,
    category: str,
    outcome: str,
    retries: int = 0,
    qc_rejected: bool = False,
    cause: str | None = None,
    *,
    registry: Path | str = DEFAULT_REGISTRY,
    variant: str | None = None,
    judge: str | None = None,
    target: str | None = None,
    authored_by: str | None = None,
) -> dict[str, Any]:
    """Append one outcome. Only a clean, independently-judged pass is evidence.

    WHY the evidence flag is derived and not trusted from the caller: a recorded
    "verified" that was actually self-judged, QC-rejected, or authored against
    the very component that judges it would poison the score. Those collapse to
    no-evidence here, at the single point of writing.
    """
    if category not in CATEGORIES:
        raise ConfigError(f"unknown category {category!r}", code="CONFIG_INVALID")
    if outcome not in OUTCOMES:
        raise ConfigError(f"unknown outcome {outcome!r}", code="CONFIG_INVALID")
    key = f"{provider}/{model}" + (f"#{variant}" if variant else "")
    evidence = outcome == "verified"
    if evidence and qc_rejected:
        evidence = False
    if evidence and _same_model(judge, key):
        # A model cannot verify itself: the judge would be grading its own work.
        evidence = False
    if evidence and authored_by and target and _same_model(authored_by, key) and is_verification_target(target):
        # The author could weaken the component that judges it; not evidence.
        evidence = False
    item = {
        "at": utcnow(),
        "provider": str(provider),
        "model": str(model),
        "variant": variant,
        "key": key,
        "category": category,
        "outcome": outcome,
        "retries": int(retries),
        "qc_rejected": bool(qc_rejected),
        "cause": cause,
        "judge": judge,
        "evidence": evidence,
    }
    append_jsonl(outcomes_path(registry), item)
    return item


def _stats(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarise records for one (model, category).

    ``attempts`` counts runs that produced a comparable *result* (verified,
    failed or rejected). An ``unverified`` run has no result to compare, so it is
    not an attempt - "a step that was never verified is not evidence".
    """
    attempts = 0
    verified = 0
    failed = 0
    for rec in records:
        outcome = str(rec.get("outcome", ""))
        if outcome == "unverified":
            continue
        attempts += 1
        if rec.get("evidence") and outcome == "verified":
            verified += 1
        elif outcome in ("failed", "rejected"):
            failed += 1
    enough = attempts >= MIN_COMPARABLE_ATTEMPTS
    return {
        "attempts": attempts,
        "verified": verified,
        "failed": failed,
        "enough_evidence": enough,
        "verdict": "proven" if (enough and verified > 0) else ("failing" if enough else "not-yet"),
    }


def _history_index(
    records: Sequence[Mapping[str, Any]], category: str | None = None
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for rec in records:
        if category is not None and rec.get("category") != category:
            continue
        grouped.setdefault(str(rec.get("key", "")), []).append(rec)
    index: dict[str, dict[str, Any]] = {}
    for key, recs in grouped.items():
        stats = _stats(recs)
        stats["key"] = key
        stats["provider"] = recs[0].get("provider")
        stats["model"] = recs[0].get("model")
        stats["category"] = category
        stats["min_comparable_attempts"] = MIN_COMPARABLE_ATTEMPTS
        index[key] = stats
    return index


def history(category: str | None = None, *, registry: Path | str = DEFAULT_REGISTRY) -> list[dict[str, Any]]:
    """What the router *believes*, with the evidence that supports it.

    Each row carries ``attempts`` and ``verified`` so the line between "proven"
    and "not yet" is visible: below :data:`MIN_COMPARABLE_ATTEMPTS` the verdict
    is ``not-yet`` and the row is not allowed to influence routing.
    """
    if category is not None and category not in CATEGORIES:
        raise ConfigError(f"unknown category {category!r}", code="CONFIG_INVALID")
    index = _history_index(_read_outcomes(registry), category)
    rows = sorted(index.values(), key=lambda row: (-int(row["verified"]), row["key"]))
    return rows


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    """A routing answer. ``reason`` is always exactly one sentence."""

    status: str
    reason: str
    category: str
    provider: str | None = None
    model: str | None = None
    variant: str | None = None
    assumed: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    cost: float | None = None

    @property
    def key(self) -> str | None:
        if not self.provider or not self.model:
            return None
        base = f"{self.provider}/{self.model}"
        return f"{base}#{self.variant}" if self.variant else base

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _decision_for_entry(
    entry: ModelEntry,
    category: str,
    *,
    status: str,
    reason: str,
    cost: float | None = None,
    assumed: Sequence[str] = (),
    alternatives: Sequence[str] = (),
) -> Decision:
    return Decision(
        status=status,
        reason=reason,
        category=category,
        provider=entry.provider,
        model=entry.model,
        variant=entry.variant,
        assumed=list(assumed),
        alternatives=list(alternatives),
        cost=cost,
    )


def route(
    category: str,
    *,
    registry: Path | str = DEFAULT_REGISTRY,
    pin: str | None = None,
    default: str | None = None,
    budget_remaining: float | None = None,
    est_tokens_in: int = 0,
    est_tokens_out: int = 0,
    estimated_cost: float | None = None,
    accept_unknown_price: bool = False,
) -> Decision:
    """Choose a model for ``category``, or refuse with a reason.

    Order of authority: an explicit pin, then recorded evidence, then the host's
    default. Budget and unknown-price checks can refuse a model that would
    otherwise win, and they say exactly what was dropped and why.
    """
    if category not in CATEGORIES:
        raise ConfigError(f"unknown category {category!r}", code="CONFIG_INVALID")
    entries = load_catalog(registry)
    if budget_remaining is None:
        budget_remaining = _remaining_cost(registry)

    if pin is not None:
        provider, model, variant = _parse_pin(pin)
        entry = _find_entry(entries, provider, model, variant)
        return _route_pin(
            entry,
            entries,
            category,
            pin,
            budget_remaining=budget_remaining,
            est_tokens_in=est_tokens_in,
            est_tokens_out=est_tokens_out,
            estimated_cost=estimated_cost,
            accept_unknown_price=accept_unknown_price,
        )

    return _route_open(
        entries,
        category,
        registry=registry,
        default=default,
        budget_remaining=budget_remaining,
        est_tokens_in=est_tokens_in,
        est_tokens_out=est_tokens_out,
        estimated_cost=estimated_cost,
        accept_unknown_price=accept_unknown_price,
    )


def _route_pin(
    entry: ModelEntry | None,
    entries: Sequence[ModelEntry],
    category: str,
    pin: str,
    *,
    budget_remaining: float | None,
    est_tokens_in: int,
    est_tokens_out: int,
    estimated_cost: float | None,
    accept_unknown_price: bool,
) -> Decision:
    alternatives = _candidate_keys(entries, category)
    if entry is None:
        return Decision(
            status="pinned-missing",
            reason=_one_sentence(
                f"pin {pin} is not in the recorded catalog",
                f"available instead: {', '.join(alternatives) or 'none'}",
            ),
            category=category,
            alternatives=alternatives,
        )
    if not _usable(entry):
        return _decision_for_entry(
            entry,
            category,
            status="pinned-missing",
            reason=_one_sentence(
                f"pin {pin} is recorded as {entry.status}",
                f"available instead: {', '.join(alternatives) or 'none'}",
            ),
            alternatives=alternatives,
        )
    state, capability = _capability_state(entry, category)
    if state == "incapable":
        capable = [k for k in alternatives if k != entry.key]
        return _decision_for_entry(
            entry,
            category,
            status="pinned-incompatible",
            reason=_one_sentence(
                f"pin {pin} is recorded as not supporting {capability}, which {category} requires",
                f"available instead: {', '.join(capable) or 'none'}",
            ),
            alternatives=capable,
        )
    cost, priced = _cost_of(entry, est_tokens_in, est_tokens_out, estimated_cost)
    block = _budget_block(entry, cost, priced, budget_remaining, accept_unknown_price)
    if block is not None:
        return _decision_for_entry(
            entry, category, status="blocked", reason=_one_sentence(block), cost=cost
        )
    if state == "unknown":
        return _decision_for_entry(
            entry,
            category,
            status="assumed",
            reason=_one_sentence(
                f"pin {pin} is honoured but the catalog did not report its {capability} capability",
                "proceeding on the assumption that it is capable",
            ),
            cost=cost,
            assumed=[capability],
        )
    return _decision_for_entry(
        entry,
        category,
        status="ok",
        reason=_one_sentence(f"pin {pin} is active and capable of {category}"),
        cost=cost,
    )


def _route_open(
    entries: Sequence[ModelEntry],
    category: str,
    *,
    registry: Path | str,
    default: str | None,
    budget_remaining: float | None,
    est_tokens_in: int,
    est_tokens_out: int,
    estimated_cost: float | None,
    accept_unknown_price: bool,
) -> Decision:
    index = _history_index(_read_outcomes(registry), category)

    affordable: list[tuple[ModelEntry, float | None, str | None]] = []
    dropped: list[str] = []
    for entry in entries:
        if not _usable(entry):
            continue
        state, _capability = _capability_state(entry, category)
        if state == "incapable":
            continue
        cost, priced = _cost_of(entry, est_tokens_in, est_tokens_out, estimated_cost)
        block = _budget_block(entry, cost, priced, budget_remaining, accept_unknown_price)
        if block is not None:
            dropped.append(block)
            continue
        affordable.append((entry, cost, index.get(entry.key, {}).get("verdict")))

    if not affordable:
        reason = _one_sentence(
            f"no catalog model fits {category}",
            *dropped[:3],
        ) if dropped else _one_sentence(
            f"the catalog has no active model capable of {category}"
        )
        return Decision(
            status="blocked",
            reason=reason,
            category=category,
            alternatives=_candidate_keys(entries, category),
        )

    proven = []
    for entry, cost, verdict in affordable:
        if verdict == "proven":
            proven.append((entry, cost, index.get(entry.key, {})))
    if proven:
        entry, cost, stats = max(
            proven,
            key=lambda item: (
                int(item[2].get("verified", 0)),
                -float(item[1] if item[1] is not None else float("inf")),
            ),
        )
        state, capability = _capability_state(entry, category)
        assumed = [capability] if state == "unknown" else []
        reason = _one_sentence(
            f"{entry.key} has {stats.get('verified', 0)} verified completions for {category} "
            f"from {stats.get('attempts', 0)} recorded attempts, the strongest recorded evidence"
        )
        return _decision_for_entry(
            entry,
            category,
            status="assumed" if assumed else "ok",
            reason=reason,
            cost=cost,
            assumed=assumed,
        )

    # No model has earned a verdict: defer to the host default, never guess.
    return _route_default(
        affordable,
        entries,
        category,
        default=default,
    )


def _route_default(
    affordable: Sequence[tuple[ModelEntry, float | None, str | None]],
    entries: Sequence[ModelEntry],
    category: str,
    *,
    default: str | None,
) -> Decision:
    alternatives = _candidate_keys(entries, category)
    if default is None:
        return Decision(
            status="blocked",
            reason=_one_sentence(
                f"no model has earned a verdict for {category} from recorded outcomes",
                "too few to prefer, and no default was configured",
            ),
            category=category,
            alternatives=alternatives,
        )
    provider, model, variant = _parse_pin(default)
    chosen = builtins.next(
        (item for item in affordable if item[0].provider == provider and item[0].model == model
         and item[0].variant == variant),
        None,
    )
    if chosen is None:
        in_catalog = any(
            e.provider == provider and e.model == model and e.variant == variant for e in entries
        )
        detail = (
            "it was dropped on budget or unknown price"
            if in_catalog
            else "it is not in the recorded catalog"
        )
        return Decision(
            status="blocked",
            reason=_one_sentence(
                f"the configured default {default} cannot be used because {detail}",
                "too few to prefer",
            ),
            category=category,
            alternatives=alternatives,
        )
    entry, cost, _verdict = chosen
    state, capability = _capability_state(entry, category)
    assumed = [capability] if state == "unknown" else []
    return _decision_for_entry(
        entry,
        category,
        status="assumed" if assumed else "ok",
        reason=_one_sentence(
            f"{entry.key} is the configured default for {category}",
            "too few to prefer, because no model has 5 comparable attempts yet",
        ),
        cost=cost,
        assumed=assumed,
    )


# ---------------------------------------------------------------------------
# diagnosis
# ---------------------------------------------------------------------------

#: Ordered cause patterns. Order is load-bearing: "unknown tool" must be caught
#: as a tool fault before any broad provider word can claim it, and budget words
#: must not be swallowed by a generic "limit".
_DIAGNOSIS_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cancelled", ("cancelled", "canceled", "aborted", "interrupted")),
    (
        "budget",
        (
            "budget exceeded",
            "exceeds budget",
            "over budget",
            "budget limit",
            "quota exceeded",
            "insufficient funds",
            "out of credits",
            "credit limit",
        ),
    ),
    (
        "tools",
        (
            "unknown tool",
            "no such tool",
            "tool not found",
            "missing tool",
            "invalid tool",
            "tool is not available",
            "tool call failed",
        ),
    ),
    (
        "orchestration",
        ("bad plan", "invalid plan", "plan invalid", "cannot proceed", "dependency failed", "orchestration"),
    ),
    (
        "instructions",
        ("vague", "ambiguous", "unclear", "underspecified", "contradictory", "missing context"),
    ),
    (
        "model",
        (
            "model error",
            "does not accept",
            "cannot process",
            "invalid model",
            "not capable",
            "hallucinat",
            "empty response",
            "context length",
            "context window",
            "malformed output",
        ),
    ),
    (
        "provider",
        (
            "rate limit",
            "429",
            "500",
            "502",
            "503",
            "504",
            "timeout",
            "timed out",
            "connection reset",
            "connection refused",
            "overloaded",
            "service unavailable",
            "network error",
            "econnreset",
            "gateway",
        ),
    ),
)


def diagnose(error_text: str, retries: int = 0) -> str:
    """Attribute an error to one cause, honestly.

    ``unknown`` is returned for anything unrecognised, and it means *our*
    classification failed - it is never folded into ``model``. ``retries`` is the
    number of times this failure has already happened; it is accepted so callers
    can pass context without the router pretending to know more than the text.
    """
    del retries  # pure text classification; never used to blame the model
    text = str(error_text or "").lower()
    if not text.strip():
        return "unknown"
    for cause, needles in _DIAGNOSIS_PATTERNS:
        if any(needle in text for needle in needles):
            return cause
    return "unknown"


# ---------------------------------------------------------------------------
# escalation
# ---------------------------------------------------------------------------

#: Non-model causes and how to recover. Model faults are handled separately
#: because only they may switch model, and only on a repeat.
_CAUSE_ACTIONS: dict[str, tuple[str, str]] = {
    "provider": ("retry", "retry the same model after a backoff"),
    "orchestration": ("replan", "rebuild the plan before spending another spawn"),
    "tools": ("fix-tool", "supply or repair the missing tool"),
    "instructions": ("repair-instructions", "tighten the instructions to remove the ambiguity"),
    "cancelled": ("stop", "stop: the run was cancelled"),
}


def _tried_keys(tried: Sequence[str]) -> list[str]:
    return [str(item) for item in tried if str(item)]


def _is_tried(entry: ModelEntry, tried: Sequence[str]) -> bool:
    return any(_same_model(entry.key, key) for key in tried)


def _split_ref(ref: str) -> tuple[str | None, str | None, str | None]:
    """Best-effort split of a ``provider/model#variant`` reference."""
    if not ref:
        return (None, None, None)
    try:
        return _parse_pin(ref)
    except ConfigError:
        return (None, None, None)


def next(  # noqa: A001 - the contract fixes this name
    cause: str,
    *,
    category: str = "general",
    tried: Sequence[str] = (),
    registry: Path | str = DEFAULT_REGISTRY,
    escalations_used: int = 0,
    max_escalations: int = MAX_ESCALATIONS,
    pin: str | None = None,
    default: str | None = None,
    retries: int = 0,
) -> dict[str, Any]:
    """Decide the next move after a failure, escalating only when warranted.

    A single provider hiccup retries the SAME model; only a *model* fault that
    has already happened before escalates to a different model, and a pin is
    never routed around. A finite escalation budget means exhaustion returns
    ``blocked`` instead of walking the whole catalog.
    """
    cause = cause if cause in CAUSES else "unknown"
    tried_keys = _tried_keys(tried)
    last = tried_keys[-1] if tried_keys else (pin or "")
    same_provider, same_model, same_variant = _split_ref(last)

    if cause in _CAUSE_ACTIONS:
        action, why = _CAUSE_ACTIONS[cause]
        backoff = min(60, 2 ** max(0, int(retries)))
        return {
            "status": "ok",
            "action": action,
            "cause": cause,
            "provider": same_provider,
            "model": same_model,
            "variant": same_variant,
            "backoff_s": backoff if cause == "provider" else None,
            "reason": _one_sentence(
                f"{cause} fault ({why})",
                f"not a model fault, so {last or 'the model'} is kept",
            ),
        }

    if cause == "budget":
        return {
            "status": "blocked",
            "action": "stop",
            "cause": cause,
            "provider": None,
            "model": None,
            "backoff_s": None,
            "reason": _one_sentence("the budget is exhausted, so there is nothing left to spend on a retry"),
        }

    if cause == "unknown":
        return {
            "status": "ok",
            "action": "observe",
            "cause": cause,
            "provider": None,
            "model": None,
            "backoff_s": None,
            "reason": _one_sentence(
                "the error could not be attributed, so it is our uncertainty and not the model's fault",
                "gather more evidence before switching models",
            ),
        }

    # cause == "model": the only cause allowed to switch model.
    if pin is not None:
        return {
            "status": "blocked",
            "action": "stop",
            "cause": cause,
            "provider": None,
            "model": None,
            "backoff_s": None,
            "reason": _one_sentence(
                f"pin {pin} is failing but a pin is honoured absolutely, so the router will not switch models"
            ),
        }
    if int(retries) < 1:
        provider, model, variant = (None, None, None)
        if last:
            try:
                provider, model, variant = _parse_pin(last)
            except ConfigError:
                provider = None
        return {
            "status": "ok",
            "action": "retry",
            "cause": cause,
            "provider": provider,
            "model": model,
            "backoff_s": None,
            "reason": _one_sentence(
                f"{last or 'the model'} failed once", "retry the same model before considering a switch"
            ),
        }
    if int(escalations_used) >= int(max_escalations):
        return {
            "status": "blocked",
            "action": "stop",
            "cause": cause,
            "provider": None,
            "model": None,
            "backoff_s": None,
            "reason": _one_sentence(
                f"the escalation budget of {int(max_escalations)} is exhausted, so a different model cannot be tried"
            ),
        }
    target = _escalation_target(
        registry,
        category,
        tried_keys,
        pin=pin,
        default=default,
    )
    if target is None:
        return {
            "status": "blocked",
            "action": "stop",
            "cause": cause,
            "provider": None,
            "model": None,
            "backoff_s": None,
            "reason": _one_sentence(
                f"no untried model is available for {category}, so escalation cannot proceed"
            ),
        }
    return {
        "status": "ok",
        "action": "escalate",
        "cause": cause,
        "provider": target.provider,
        "model": target.model,
        "variant": target.variant,
        "backoff_s": None,
        "reason": _one_sentence(
            f"{last or 'the model'} has failed repeatedly and the fault is attributed to the model",
            f"escalating to {target.key}",
        ),
    }


def _escalation_target(
    registry: Path | str,
    category: str,
    tried: Sequence[str],
    *,
    pin: str | None,
    default: str | None,
) -> ModelEntry | None:
    """Best untried model: proven evidence first, then the default, then cheapest."""
    entries = load_catalog(registry)
    index = _history_index(_read_outcomes(registry), category)
    candidates = [
        entry
        for entry in entries
        if _usable(entry)
        and _capability_state(entry, category)[0] != "incapable"
        and not _is_tried(entry, tried)
        and (pin is None or not _same_model(entry.key, pin))
    ]
    if not candidates:
        return None
    proven = [e for e in candidates if index.get(e.key, {}).get("verdict") == "proven"]
    if proven:
        return max(proven, key=lambda e: int(index.get(e.key, {}).get("verified", 0)))
    if default is not None:
        try:
            provider, model, variant = _parse_pin(default)
        except ConfigError:
            provider = model = None
            variant = None
        if provider is not None:
            match = builtins.next(
                (e for e in candidates if e.provider == provider and e.model == model and e.variant == variant),
                None,
            )
            if match is not None:
                return match
    return min(candidates, key=lambda e: float(e.price_in_per_mtok) if e.priced else float("inf"))


# ---------------------------------------------------------------------------
# judging / promotion
# ---------------------------------------------------------------------------


def is_verification_target(target: str | None) -> bool:
    """True when ``target`` is part of the machinery that judges work."""
    text = str(target or "").lower()
    return any(hint in text for hint in VERIFICATION_TARGET_HINTS)


def promotion_gate(change: Mapping[str, Any], *, registry: Path | str = DEFAULT_REGISTRY) -> dict[str, Any]:
    """Decide whether a change may auto-promote.

    WHY this exists: a model that can edit the component that judges it can
    weaken its own check. So a model-authored change to a verification or
    recovery target never auto-promotes, an unverified change is not evidence,
    and a model cannot judge its own change.
    """
    del registry  # the gate is a pure function of the change
    target = str(change.get("target", ""))
    authored_by = change.get("authored_by")
    judged_by = change.get("judged_by")
    verified = bool(change.get("verification", False))
    if not verified:
        return _gate(False, target, "a step that was never verified is not evidence")
    if authored_by and is_verification_target(target):
        return _gate(
            False,
            target,
            f"{authored_by} authored a change to the {target} verification component, and "
            "model-authored changes to verification or recovery targets never auto-promote",
        )
    if authored_by and judged_by and _same_model(str(authored_by), str(judged_by)):
        return _gate(False, target, f"{authored_by} cannot judge its own change")
    return _gate(
        True,
        target,
        f"{target} may auto-promote: it is verified and not a model-authored verification target",
    )


def _gate(allowed: bool, target: str, reason: str) -> dict[str, Any]:
    return {
        "status": "ok" if allowed else "refused",
        "allowed": allowed,
        "target": target,
        "reason": _one_sentence(reason),
    }

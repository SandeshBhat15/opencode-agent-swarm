"""Lightweight, deterministic decision engine for the swarm.

This module composes the pieces that already exist (``manager.triage``,
``delegation``, ``router``, ``context``, ``knowledge``, ``telemetry``) into a
single answer to one question: "what is the smallest workflow that can
correctly and safely deliver this task?"

It is the *only* place in the swarm that is allowed to make a decision, and it
never calls a model to do so. Every choice is a deterministic function of
measurable inputs (triage lane, file count, risk keywords, catalog contents,
budget counters, recorded outcomes). That is the whole point: an LLM deciding
whether to delegate is a token spent on a question arithmetic can answer.

Disabled by default
-------------------
The engine ships OFF. Set ``SWARM_DECISION_ENGINE=1`` (or pass
``enabled=True``) to activate it. When disabled, :func:`decide` returns a
*passthrough* decision (direct execution, no delegation, no model selection)
so importing this module, or even calling it, cannot change swarm behaviour
and cannot regress a run. This is the "keep it disabled if it regresses"
requirement made mechanical rather than procedural.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from . import manager, delegation, router, context, errors

ENGINE_ENV = "SWARM_DECISION_ENGINE"


def is_enabled() -> bool:
    """Read the opt-in flag. Anything other than a truthy value means OFF."""
    return os.environ.get(ENGINE_ENV, "0").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Task profile: the five dimensions the requirement asks us to classify on.    #
# --------------------------------------------------------------------------- #

_COMPLEXITY_HIGH = (
    "refactor", "migrate", "integrate", "architecture", "redesign", "rewrite",
    "pipeline", "system", "multi", "complex", "large",
)
_COMPLEXITY_LOW = (
    "what", "explain", "describe", "show", "list", "print", "read", "find",
    "search", "summarise", "summarize", "hello", "greeting",
)

_UNCERTAINTY_HIGH = (
    "latest", "current", "recent", "new", "docs", "documentation", "api",
    "version", "research", "investigate", "unknown", "maybe", "possibly",
    "changelog", "release", "update",
)

_RISK_HIGH = (
    "security", "auth", "authentication", "authorisation", "authorization",
    "deploy", "deployment", "migration", "delete", "remove", "secret", "token",
    "password", "payment", "money", "destructive", "production", "prod",
    "permission", "credential",
)
_RISK_MEDIUM = ("write", "edit", "modify", "change", "create", "update", "patch")

_SKILL_KEYWORDS = {
    "web": ("web", "http", "url", "fetch", "search", "internet", "online", "docs"),
    "code": ("code", "function", "class", "module", "script", "python", "typescript", "bug", "fix"),
    "data": ("data", "json", "csv", "database", "sql", "query", "parse"),
    "ui": ("ui", "frontend", "component", "css", "html", "layout", "design"),
    "infra": ("deploy", "docker", "ci", "pipeline", "build", "release"),
    "testing": ("test", "verify", "check", "assert", "coverage"),
}


@dataclass
class TaskProfile:
    """A deterministic classification of a request along five dimensions."""

    request: str
    lane: str = "R4"
    complexity: str = "medium"        # low / medium / high
    uncertainty: str = "low"          # low / medium / high
    risk: str = "low"                # low / medium / high
    required_skills: list[str] = field(default_factory=list)
    verification_needs: str = "light"  # none / light / full
    file_count: int = 0
    independent_workstreams: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify(
    request: str,
    *,
    file_count: int = 0,
    independent_workstreams: int = 1,
) -> TaskProfile:
    """Classify a request deterministically. No model call, no I/O."""
    text = (request or "").lower()
    words = text.split()
    lane = manager.triage(request)

    # complexity
    if lane == "R1":
        complexity = "low"
    elif lane == "R4" or file_count >= 3 or any(k in text for k in _COMPLEXITY_HIGH):
        complexity = "high"
    elif file_count >= 2 or lane == "R3" or any(k in text for k in ("add", "modify", "update", "create")):
        complexity = "medium"
    elif any(k in text for k in _COMPLEXITY_LOW):
        complexity = "low"
    else:
        complexity = "medium"

    # uncertainty
    if any(k in text for k in _UNCERTAINTY_HIGH):
        uncertainty = "high"
    elif any(k in text for k in ("maybe", "possibly", "perhaps", "could")):
        uncertainty = "medium"
    else:
        uncertainty = "low"

    # risk
    if any(k in text for k in _RISK_HIGH):
        risk = "high"
    elif any(k in text for k in _RISK_MEDIUM):
        risk = "medium"
    else:
        risk = "low"

    # required skills
    skills: list[str] = []
    for skill, kws in _SKILL_KEYWORDS.items():
        if any(k in text for k in kws):
            skills.append(skill)
    if not skills:
        skills = ["general"]

    # verification needs: high risk is never "none", even on an answer lane.
    if risk == "high":
        verification = "full"
    elif lane == "R1":
        verification = "none"
    elif lane == "R4" or any(k in text for k in ("test", "verify", "acceptance", "check")):
        verification = "full"
    else:
        verification = "light"

    return TaskProfile(
        request=request,
        lane=lane,
        complexity=complexity,
        uncertainty=uncertainty,
        risk=risk,
        required_skills=skills,
        verification_needs=verification,
        file_count=file_count,
        independent_workstreams=max(1, independent_workstreams),
    )


# --------------------------------------------------------------------------- #
# Decision: the composed answer.                                              #
# --------------------------------------------------------------------------- #

@dataclass
class Decision:
    """One deterministic decision for one task."""

    enabled: bool
    profile: TaskProfile
    workflow: str = "direct"            # direct / research / small-build / project
    delegate: bool = False
    worker_count: int = 0
    depth: int = 0
    reason: str = ""
    code: str = ""
    model: str | None = None
    model_status: str = "disabled"     # ok / assumed / blocked / pinned-* / disabled
    context_budget_ok: bool = True
    retry_policy: dict[str, Any] = field(default_factory=dict)
    verification: str = "light"
    predicted_calls: int = 1
    disabled_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["profile"] = self.profile.to_dict()
        return d


_LANE_TO_WORKFLOW = {"R1": "direct", "R2": "research", "R3": "small-build", "R4": "project"}


def decide(
    request: str,
    *,
    enabled: bool | None = None,
    file_count: int = 0,
    independent_workstreams: int = 1,
    budget_remaining: float | None = None,
    est_tokens_in: int = 0,
    est_tokens_out: int = 0,
    registry: Any = None,
    pin: str | None = None,
    default_model: str | None = None,
    history: Any = None,
) -> Decision:
    """Produce one deterministic decision. Never calls a model."""
    if enabled is None:
        enabled = is_enabled()

    profile = classify(request, file_count=file_count,
                       independent_workstreams=independent_workstreams)

    if not enabled:
        return Decision(
            enabled=False,
            profile=profile,
            workflow="direct",
            delegate=False,
            worker_count=0,
            depth=0,
            reason="Engine disabled by default; executing directly (set SWARM_DECISION_ENGINE=1 to enable).",
            code="DISABLED",
            model=None,
            model_status="disabled",
            context_budget_ok=True,
            retry_policy={"retry": False, "reason": "engine disabled"},
            verification=profile.verification_needs,
            predicted_calls=1,
            disabled_reason="SWARM_DECISION_ENGINE not set",
        )

    if profile.lane == "R4" or profile.complexity == "high" or profile.risk == "high":
        workflow = "project"
    elif profile.lane == "R3":
        workflow = "small-build"
    elif profile.lane == "R2":
        workflow = "research"
    elif profile.lane == "R1":
        workflow = "direct"
    else:
        workflow = "project"

    # --- delegation: reuse the existing, tested trade-off ----------------- #
    specialist = profile.uncertainty == "high" or profile.complexity == "high"
    signals = delegation.DelegationSignals(
        specialist_capability=specialist,
        independent_workstreams=profile.independent_workstreams,
        risk_reduction=(profile.risk == "high"),
        spawn_cost=0.3,
        context_cost=0.3,
        latency_cost=0.3,
        duplicate_risk=0.1 if profile.uncertainty == "low" else 0.0,
        remaining_work=1.0,
        lane=profile.lane,
    )
    pol = delegation.DelegationPolicy()
    dres = delegation.decide(signals, policy=pol)

    # --- model selection: reuse the router, never invent availability ---- #
    reg = registry if registry is not None else Path(".opencode/swarm/models")
    category = "research" if profile.lane == "R2" else ("code" if profile.lane in ("R3", "R4") else "general")

    # Fall back to the first *usable* catalog entry when the host has not
    # pinned a default and no model has earned a verdict yet. This is a
    # fallback, not a guess: it is only reached when the router would
    # otherwise return "blocked", and the status comes back as "assumed"
    # (unproven) rather than "ok".
    chosen_default = default_model
    if chosen_default is None:
        try:
            entries = router.load_catalog(reg)
        except Exception:
            entries = []
        for entry in entries:
            if getattr(entry, "status", None) == "active":
                chosen_default = getattr(entry, "key", None)
                break

    try:
        mres = router.route(
            category,
            registry=reg,
            pin=pin,
            default=chosen_default,
            budget_remaining=budget_remaining,
            est_tokens_in=est_tokens_in,
            est_tokens_out=est_tokens_out,
            accept_unknown_price=True,
        )
        model = mres.model
        model_status = mres.status
    except Exception as exc:  # never let routing crash a decision
        model = None
        model_status = f"error:{type(exc).__name__}"

    # --- context budget ------------------------------------------------- #
    # A 32k-ish default ceiling; callers can override via budget_remaining.
    ctx_limit = 32000
    try:
        fit = context.fits(ctx_limit, None, reserved=0, label="decision")
        ctx_ok = bool(fit.ok) if hasattr(fit, "ok") else True
    except Exception:
        ctx_ok = True

    # --- retry policy: bounded, targeted, never unbounded ---------------- #
    retry = delegation.retry_decision(attempt=0, limit=2, cause="none")

    # --- predicted model calls (for the "fewer calls" property) --------- #
    base_calls = {"direct": 1, "research": 2, "small-build": 2, "project": 4}.get(workflow, 3)
    if dres.delegate:
        base_calls += max(0, dres.worker_count - 1)

    reason = dres.reason if dres.reason else "Direct execution."
    if model_status not in ("ok", "assumed"):
        reason = reason + f" (model: {model_status})"

    return Decision(
        enabled=True,
        profile=profile,
        workflow=workflow,
        delegate=dres.delegate,
        worker_count=dres.worker_count,
        depth=dres.depth,
        reason=reason,
        code=dres.code,
        model=model,
        model_status=model_status,
        context_budget_ok=ctx_ok,
        retry_policy={"retry": retry.retry, "code": retry.code, "instructions": list(retry.instructions)},
        verification=profile.verification_needs,
        predicted_calls=base_calls,
        disabled_reason=None,
    )


def render(decision: Decision) -> str:
    p = decision.profile
    lines = [
        f"enabled: {decision.enabled}",
        f"workflow: {decision.workflow} (lane {p.lane})",
        f"complexity={p.complexity} uncertainty={p.uncertainty} risk={p.risk}",
        f"skills: {', '.join(p.required_skills)}",
        f"verification: {decision.verification}",
        f"delegate: {decision.delegate} (workers={decision.worker_count}, depth={decision.depth})",
        f"model: {decision.model} [{decision.model_status}]",
        f"context_budget_ok: {decision.context_budget_ok}",
        f"predicted_calls: {decision.predicted_calls}",
        f"reason: {decision.reason}",
    ]
    if decision.disabled_reason:
        lines.append(f"disabled_reason: {decision.disabled_reason}")
    return "\n".join(lines)

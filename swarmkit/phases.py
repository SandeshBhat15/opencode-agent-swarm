"""The swarm phase machine.

Prose describing a pipeline is not a pipeline. The phase order and the legal
edges between phases live here as data so that an interrupted, confused or
buggy orchestrator cannot skip INTEGRATE and jump straight from QC to
DELIVERED.
"""

from __future__ import annotations

from .errors import TransitionError

INTAKE = "INTAKE"
PLAN = "PLAN"
MESH = "MESH"
INTEGRATE = "INTEGRATE"
QC = "QC"
FIX = "FIX"
RETRO = "RETRO"
DELIVERED = "DELIVERED"
ABORTED = "ABORTED"

#: Ordered as a run walks them. ``FIX`` is the only phase the run re-enters.
PHASES: tuple[str, ...] = (INTAKE, PLAN, MESH, INTEGRATE, QC, FIX, RETRO, DELIVERED)

#: Phases a run may never leave.
TERMINAL: frozenset[str] = frozenset({DELIVERED, ABORTED})

#: Legal edges. ``QC -> FIX -> QC`` is the bounded fix loop.
TRANSITIONS: dict[str, frozenset[str]] = {
    INTAKE: frozenset({PLAN, ABORTED}),
    PLAN: frozenset({MESH, ABORTED}),
    MESH: frozenset({INTEGRATE, ABORTED}),
    INTEGRATE: frozenset({QC, ABORTED}),
    QC: frozenset({FIX, RETRO, ABORTED}),
    FIX: frozenset({QC, ABORTED}),
    RETRO: frozenset({DELIVERED, ABORTED}),
    DELIVERED: frozenset(),
    ABORTED: frozenset(),
}

#: Phases that count as "the run is still going".
UNFINISHED: frozenset[str] = frozenset(PHASES) - TERMINAL

#: Phases whose entry implies delivery obligations.
DELIVERY_GATED: frozenset[str] = frozenset({DELIVERED})


def is_phase(name: str) -> bool:
    return name in TRANSITIONS


def can_transition(current: str, nxt: str) -> bool:
    """Return whether ``current -> nxt`` is a legal edge."""
    if current not in TRANSITIONS or nxt not in TRANSITIONS:
        return False
    return nxt in TRANSITIONS[current]


def assert_transition(current: str, nxt: str) -> None:
    """Raise :class:`TransitionError` unless ``current -> nxt`` is legal."""
    if not is_phase(current):
        raise TransitionError(
            f"unknown current phase {current!r}", code="UNKNOWN_PHASE"
        )
    if not is_phase(nxt):
        raise TransitionError(f"unknown target phase {nxt!r}", code="UNKNOWN_PHASE")
    if current in TERMINAL:
        raise TransitionError(
            f"{current} is terminal; cannot transition to {nxt}",
            code="TERMINAL_PHASE",
        )
    if not can_transition(current, nxt):
        allowed = ", ".join(sorted(TRANSITIONS[current])) or "nothing"
        raise TransitionError(
            f"illegal transition {current} -> {nxt}; legal targets: {allowed}",
            code="ILLEGAL_TRANSITION",
        )


def describe() -> str:
    """Human-readable transition table for agent pages and docs."""
    lines = []
    for phase in PHASES:
        targets = sorted(TRANSITIONS[phase])
        lines.append(f"{phase:<10} -> {', '.join(targets) if targets else '(terminal)'}")
    return "\n".join(lines)
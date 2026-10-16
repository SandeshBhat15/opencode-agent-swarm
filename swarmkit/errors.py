"""Typed failures for swarmkit.

Every failure carries a stable ``code`` so the CLI, the test-suite and the
agent pages can react to a specific condition instead of matching on strings.
"""

from __future__ import annotations


class SwarmError(Exception):
    """Base class for every swarmkit failure."""

    code = "SWARM_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


class ConfigError(SwarmError):
    code = "CONFIG_INVALID"


class ContractError(SwarmError):
    """An agent page or command page violates the swarm contract."""

    code = "CONTRACT_INVALID"


class TransitionError(SwarmError):
    """An illegal phase transition was requested."""

    code = "ILLEGAL_TRANSITION"


class BudgetExceeded(SwarmError):
    code = "BUDGET_EXCEEDED"


class GateError(SwarmError):
    """The delivery gate refused to open."""

    code = "DELIVERY_GATE_CLOSED"


class ScopeConflict(SwarmError):
    """Two workstreams claim overlapping write scope."""

    code = "SCOPE_CONFLICT"


class LockError(SwarmError):
    code = "LOCK_UNAVAILABLE"


class RecoveryRequired(SwarmError):
    """A run was left in an inconsistent state by an interrupted mutation."""

    code = "RECOVERY_REQUIRED"


class LessonError(SwarmError):
    code = "LESSON_REJECTED"
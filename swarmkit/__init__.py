"""swarmkit - enforceable run control for the OpenCode agent swarm.

The agent pages in ``agents/`` describe *intent*; this package provides the
*mechanism* that makes the intent real:

* :mod:`swarmkit.phases`    - legal phase transitions (no skipping INTEGRATE)
* :mod:`swarmkit.store`     - atomic, journalled, lock-protected run state
* :mod:`swarmkit.gate`      - delivery gate driven by criteria + evidence
* :mod:`swarmkit.budget`    - hard ceilings on agents, spawns, retries, time, tokens
* :mod:`swarmkit.isolator`  - write-scope overlap detection + git worktree isolation
* :mod:`swarmkit.lessons`   - grounded, deduped, never-auto-promoted lessons
* :mod:`swarmkit.contracts` - lint that catches inert guardrails and stale model pins

Zero third-party dependencies. ``python -m swarmkit --help``.
"""

from __future__ import annotations

__version__ = "1.0.0"

from .errors import (
    BudgetExceeded,
    ConfigError,
    ContractError,
    GateError,
    LessonError,
    LockError,
    RecoveryRequired,
    ScopeConflict,
    SwarmError,
    TransitionError,
)

__all__ = [
    "__version__",
    "BudgetExceeded",
    "ConfigError",
    "ContractError",
    "GateError",
    "LessonError",
    "LockError",
    "RecoveryRequired",
    "ScopeConflict",
    "SwarmError",
    "TransitionError",
]
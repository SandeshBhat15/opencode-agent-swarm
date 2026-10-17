"""swarmkit - enforceable run control for the OpenCode agent swarm.

The agent pages in ``agents/`` describe *intent*; this package provides the
*mechanism* that makes the intent real:

* :mod:`swarmkit.phases`    - legal phase transitions (no skipping INTEGRATE)
* :mod:`swarmkit.store`     - atomic, journalled, lock-protected run state
* :mod:`swarmkit.gate`      - delivery gate driven by criteria + evidence
* :mod:`swarmkit.budget`    - hard ceilings on agents, spawns, retries, time, tokens
* :mod:`swarmkit.isolator`  - write-scope overlap detection + git worktree isolation
* :mod:`swarmkit.workers`   - worker liveness: heartbeats, stale/timeout detection, safe reclaim
* :mod:`swarmkit.lessons`   - grounded, deduped, never-auto-promoted lessons
* :mod:`swarmkit.contracts` - lint that catches inert guardrails and stale model pins

Zero third-party dependencies. ``python -m swarmkit --help``.
"""

from __future__ import annotations

__version__ = "1.1.0"

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

# The five engines, re-exported so they are reachable as ``swarmkit.<name>``
# and not only via a deep module path. The modules themselves are re-exported
# too, which is the only way to reach ``router.budget``: flattening it would
# shadow the ``swarmkit.budget`` module every caller already imports.
from . import context, decision, delegation, knowledge, router, telemetry
from .context import (
    build_packet,
    estimate_tokens,
    fits,
    read_artifact,
    sections_from_packet,
    store_artifact,
    summarise,
    token_estimate,
)
from .delegation import (
    benefit_score,
    cost_score,
    decide,
    dedupe_workers,
    mandatory_checks_required,
    retry_decision,
    reuse_verified,
    should_skip_qc,
    skip_redundant_summary,
)
from .knowledge import (
    cache_doc,
    content_hash,
    default_store,
    freshness,
    is_stale,
    looks_like_url,
    lookup,
    normalise_url,
    promote,
)
from .router import (
    catalog_path,
    diagnose,
    history,
    import_catalog,
    is_verification_target,
    load_catalog,
    next,
    outcomes_path,
    promotion_gate,
    record,
    route,
)
from .telemetry import (
    combine,
    compare,
    from_session_record,
    load_baselines,
    load_sessions,
    parse_run_log,
    pricing_available,
    render,
    rollup,
    write_baseline,
)
from .decision import (
    Decision,
    TaskProfile,
    classify,
    is_enabled,
    render as render_decision,
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
    # modules
    "context",
    "decide",
    "delegation",
    "knowledge",
    "router",
    "telemetry",
    # router
    "import_catalog",
    "load_catalog",
    "catalog_path",
    "outcomes_path",
    "route",
    "diagnose",
    "next",
    "record",
    "history",
    "promotion_gate",
    "is_verification_target",
    # context
    "build_packet",
    "sections_from_packet",
    "summarise",
    "fits",
    "estimate_tokens",
    "token_estimate",
    "store_artifact",
    "read_artifact",
    # delegation
    "decide",
    "benefit_score",
    "cost_score",
    "dedupe_workers",
    "reuse_verified",
    "should_skip_qc",
    "skip_redundant_summary",
    "mandatory_checks_required",
    "retry_decision",
    # knowledge
    "cache_doc",
    "lookup",
    "normalise_url",
    "content_hash",
    "is_stale",
    "freshness",
    "default_store",
    "looks_like_url",
    "promote",
    # telemetry
    "combine",
    "compare",
    "from_session_record",
    "load_sessions",
    "parse_run_log",
    "pricing_available",
    "rollup",
    "render",
    "write_baseline",
    "load_baselines",
    # decide
    "decide",
    "classify",
    "is_enabled",
    "Decision",
    "TaskProfile",
]
"""Bounded execution: concurrency, spawns, retries, fix cycles, wall clock, tokens.

Nothing here is advisory. Every limit raises :class:`BudgetExceeded`, which is
what stops the "spawn until it finishes" failure mode. Token cost is reported
**only** when the caller supplies pricing; otherwise it stays ``None`` rather
than inventing a number.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import BudgetExceeded, ConfigError
from .store import atomic_write_json, read_json, utcnow


#: A lease older than this belongs to a process that is gone. Matches the
#: store's LOCK_STALE_SECONDS so a crashed run self-heals instead of hanging.
LEASE_STALE_SECONDS = 900


def _lease_is_stale(since: Any) -> bool:
    """True when a lease is unreadable or older than LEASE_STALE_SECONDS."""
    if not isinstance(since, str) or not since:
        return True
    try:
        stamp = datetime.fromisoformat(since)
    except ValueError:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds() > LEASE_STALE_SECONDS


@dataclass
class Limits:
    """Configurable ceilings. Every one of these is a hard stop."""

    max_concurrent_agents: int = 4
    max_spawns_total: int = 40
    max_retries_per_worker: int = 2
    max_fix_cycles: int = 2
    max_wall_clock_seconds: int = 3600
    max_tokens: int | None = None
    price_in_per_mtok: float | None = None
    price_out_per_mtok: float | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "Limits":
        raw = raw or {}
        if not isinstance(raw, dict):
            raise ConfigError("limits must be an object", code="CONFIG_INVALID")
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(raw) - known
        if unknown:
            raise ConfigError(
                f"unknown limit(s): {', '.join(sorted(unknown))}", code="CONFIG_INVALID"
            )
        kwargs: dict[str, Any] = {}
        for key, value in raw.items():
            if value is None:
                kwargs[key] = None
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ConfigError(
                    f"limit {key!r} must be a number, got {type(value).__name__}",
                    code="CONFIG_INVALID",
                )
            if value <= 0:
                raise ConfigError(f"limit {key!r} must be positive", code="CONFIG_INVALID")
            kwargs[key] = value
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_concurrent_agents": self.max_concurrent_agents,
            "max_spawns_total": self.max_spawns_total,
            "max_retries_per_worker": self.max_retries_per_worker,
            "max_fix_cycles": self.max_fix_cycles,
            "max_wall_clock_seconds": self.max_wall_clock_seconds,
            "max_tokens": self.max_tokens,
        }


@dataclass
class Budget:
    """Mutable spend counter for one run."""

    limits: Limits = field(default_factory=Limits)
    started_at: float = field(default_factory=time.time)
    spawns: int = 0
    retries: dict[str, int] = field(default_factory=dict)
    leased: dict[str, dict[str, Any]] = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0

    # -- wall clock -----------------------------------------------------
    def elapsed(self) -> float:
        return time.time() - self.started_at

    def check_clock(self) -> None:
        limit = self.limits.max_wall_clock_seconds
        if self.elapsed() > limit:
            raise BudgetExceeded(
                f"run exceeded max_wall_clock_seconds={limit} "
                f"(elapsed {self.elapsed():.0f}s). Deliver, escalate, or split the run.",
                code="WALL_CLOCK_EXCEEDED",
            )

    # -- spawns ---------------------------------------------------------
    def record_spawn(self, *, name: str, role: str = "") -> None:
        self.check_clock()
        if self.spawns >= self.limits.max_spawns_total:
            raise BudgetExceeded(
                f"max_spawns_total={self.limits.max_spawns_total} reached; "
                "stop fanning out and integrate what you have.",
                code="SPAWN_LIMIT",
            )
        self.spawns += 1

    # -- concurrency ----------------------------------------------------
    def lease(self, name: str, *, role: str = "", run_dir: Path | None = None) -> None:
        """Take a concurrency slot for ``name``."""
        self.check_clock()
        if name in self.leased:
            raise BudgetExceeded(
                f"worker {name!r} is already leased; duplicate spawn blocked",
                code="DUPLICATE_WORKER",
            )
        if len(self.leased) >= self.limits.max_concurrent_agents:
            raise BudgetExceeded(
                f"max_concurrent_agents={self.limits.max_concurrent_agents} reached; "
                f"in flight: {', '.join(sorted(self.leased))}",
                code="CONCURRENCY_LIMIT",
            )
        self.leased[name] = {"role": role, "since": utcnow()}
        if run_dir is not None:
            self._persist(run_dir)

    def release(self, name: str, *, run_dir: Path | None = None) -> None:
        self.leased.pop(name, None)
        if run_dir is not None:
            self._persist(run_dir)

    @property
    def inflight(self) -> int:
        return len(self.leased)

    # -- retries --------------------------------------------------------
    def record_retry(self, name: str) -> int:
        count = self.retries.get(name, 0) + 1
        self.retries[name] = count
        if count > self.limits.max_retries_per_worker:
            raise BudgetExceeded(
                f"worker {name!r} exceeded max_retries_per_worker="
                f"{self.limits.max_retries_per_worker}. Stop retrying; "
                "reroute to a general executor or escalate.",
                code="RETRY_LIMIT",
            )
        return count

    def check_fix_cycles(self, used: int) -> None:
        if used > self.limits.max_fix_cycles:
            raise BudgetExceeded(
                f"max_fix_cycles={self.limits.max_fix_cycles} exhausted "
                f"(attempting cycle {used}). Escalate to the user with evidence.",
                code="FIX_CYCLE_LIMIT",
            )

    # -- tokens / cost --------------------------------------------------
    def record_tokens(self, tokens_in: int = 0, tokens_out: int = 0) -> None:
        if tokens_in < 0 or tokens_out < 0:
            raise ConfigError("token counts must be non-negative", code="CONFIG_INVALID")
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        if (
            self.limits.max_tokens is not None
            and self.tokens_in + self.tokens_out > self.limits.max_tokens
        ):
            raise BudgetExceeded(
                f"max_tokens={self.limits.max_tokens} exceeded "
                f"({self.tokens_in + self.tokens_out} used)",
                code="TOKEN_LIMIT",
            )

    def estimated_cost_usd(self) -> float | None:
        """Cost only when pricing is known. ``None`` means 'unreliable'."""
        if self.limits.price_in_per_mtok is None or self.limits.price_out_per_mtok is None:
            return None
        return (
            self.tokens_in / 1_000_000 * self.limits.price_in_per_mtok
            + self.tokens_out / 1_000_000 * self.limits.price_out_per_mtok
        )

    # -- persistence ----------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "spawns": self.spawns,
            "inflight": sorted(self.leased),
            "leases": {k: dict(v) for k, v in self.leased.items()},
            "retries": dict(self.retries),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "started_at": self.started_at,
            "elapsed_s": round(self.elapsed(), 1),
            "limits": self.limits.to_dict(),
            "estimated_cost_usd": self.estimated_cost_usd(),
        }

    def _persist(self, run_dir: Path) -> None:
        atomic_write_json(Path(run_dir) / "budget.json", self.snapshot())

    @classmethod
    def load(cls, run_dir: Path, limits: Limits | None = None) -> "Budget":
        """Restore a budget from disk so caps hold ACROSS CLI invocations.

        The orchestrator spawns workers with one `swarmkit budget spawn` call
        per worker, each in its own short-lived process. Restoring nothing here
        would reset every cap on every command and make them pure decoration.

        Leases are restored so concurrent workers really are counted, but a
        lease older than LEASE_STALE_SECONDS is dropped: the process that held
        it is gone, and keeping it would deadlock the resumed run.
        """
        raw = read_json(Path(run_dir) / "budget.json", default={}) or {}
        budget = cls(limits=limits or Limits.from_dict(raw.get("limits")))
        budget.spawns = int(raw.get("spawns", 0) or 0)
        budget.tokens_in = int(raw.get("tokens_in", 0) or 0)
        budget.tokens_out = int(raw.get("tokens_out", 0) or 0)
        retries = raw.get("retries")
        budget.retries = {str(k): int(v) for k, v in retries.items()} if isinstance(retries, dict) else {}
        try:
            budget.started_at = float(raw.get("started_at") or 0) or time.time()
        except (TypeError, ValueError):
            budget.started_at = time.time()
        for name, entry in (raw.get("leases") or {}).items():
            if isinstance(entry, dict) and not _lease_is_stale(entry.get("since")):
                budget.leased[str(name)] = entry
        return budget

    def persist(self, run_dir: Path) -> None:
        self._persist(Path(run_dir))
"""Connect real OpenCode usage to swarmkit budgets.

OpenCode exposes usage in two places: session records (aggregate, authoritative)
and run logs (per-step, lower bound). This module is the single funnel between
those records and :class:`swarmkit.budget.Budget`: it deduplicates sessions,
refuses to treat missing usage as zero, and answers the one question the
orchestrator actually needs before spawning -- "can we still afford this?"

It never invents a number. A field the provider did not report stays ``None``
and is reported as unmeasured rather than being coerced to zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from . import budget, telemetry
from .budget import Budget, Limits
from .errors import SwarmError
from .telemetry import Usage, from_session_record, load_sessions


class UsageError(SwarmError):
    pass


@dataclass
class RecordResult:
    """Outcome of feeding one session record into a budget."""

    session_id: str | None
    status: str          # recorded | duplicate | missing | exhausted
    usage: Usage | None
    tokens_added: int    # 0 when nothing was added (duplicate/missing/exhausted)
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "tokens_added": self.tokens_added,
            "reason": self.reason,
            "usage": self.usage.as_dict() if self.usage else None,
        }


@dataclass
class UsageLedger:
    """Idempotent funnel from session records to a :class:`Budget`.

    ``seen`` holds session ids already fed, so a retry or a resumed session
    cannot double-count. ``budget`` is the single budget being charged.
    """

    budget: Budget
    seen: set[str] = field(default_factory=set)
    results: list[RecordResult] = field(default_factory=list)

    def record_session(self, record: Any) -> RecordResult:
        usage = from_session_record(record if not isinstance(record, str) else _parse(record))
        sid = usage.session_id

        # Missing telemetry: never coerce to zero.
        if usage.total_tokens is None and usage.model_calls is None and usage.elapsed_s is None:
            res = RecordResult(sid, "missing", usage, 0,
                               "no usage reported for this session; treated as unmeasured, not zero")
            self.results.append(res)
            return res

        # Duplicate: idempotent.
        if sid is not None and sid in self.seen:
            res = RecordResult(sid, "duplicate", usage, 0,
                               f"session {sid} already recorded; skipped to avoid double counting")
            self.results.append(res)
            return res

        # Budget check before charging.
        added = usage.total_tokens or 0
        if added and not self.budget.can_record_tokens(added):
            res = RecordResult(sid, "exhausted", usage, 0,
                               f"recording {added} tokens would exceed the budget; not charged")
            self.results.append(res)
            return res

        # Charge.
        if usage.input_tokens is not None or usage.output_tokens is not None:
            self.budget.record_tokens(
                tokens_in=usage.input_tokens or 0,
                tokens_out=(usage.output_tokens or 0) + (usage.reasoning_tokens or 0),
            )
        if sid is not None:
            self.seen.add(sid)
        res = RecordResult(sid, "recorded", usage, added, "usage recorded")
        self.results.append(res)
        return res

    def record_many(self, records: Iterable[Any]) -> list[RecordResult]:
        return [self.record_session(r) for r in records]

    def can_afford(self, estimated_tokens: int) -> tuple[bool, str]:
        """Whether ``estimated_tokens`` more tokens fit in the remaining budget."""
        if estimated_tokens < 0:
            raise UsageError("estimated_tokens must be non-negative", code="USAGE_INVALID")
        if self.budget.limits.max_tokens is None:
            return True, "no token ceiling configured"
        used = self.budget.tokens_in + self.budget.tokens_out
        remaining = self.budget.limits.max_tokens - used
        if estimated_tokens <= remaining:
            return True, f"{remaining} tokens remaining"
        return False, f"only {remaining} tokens remaining, need {estimated_tokens}"


def _parse(raw: str) -> Any:
    import json
    return json.loads(raw)


def record_session(budget_obj: Budget, record: Any, *, seen: set[str] | None = None) -> RecordResult:
    """Convenience wrapper for a single record without a long-lived ledger."""
    ledger = UsageLedger(budget=budget_obj, seen=seen if seen is not None else set())
    return ledger.record_session(record)


def can_afford(budget_obj: Budget, estimated_tokens: int) -> tuple[bool, str]:
    ledger = UsageLedger(budget=budget_obj)
    return ledger.can_afford(estimated_tokens)


# Extend Budget with a non-raising affordability check if missing.
def _ensure_budget_helpers() -> None:
    if not hasattr(Budget, "can_record_tokens"):
        def can_record_tokens(self, tokens: int) -> bool:  # type: ignore[misc]
            if self.limits.max_tokens is None:
                return True
            return (self.tokens_in + self.tokens_out + tokens) <= self.limits.max_tokens
        Budget.can_record_tokens = can_record_tokens  # type: ignore[attr-defined]


_ensure_budget_helpers()

# Efficiency

Efficiency here is measured, not asserted. `src/core/metrics.js` summarises each run from the
journal and the budget, and `delivery.metrics` reports it.

## Rules the implementation enforces

1. **Deterministic before expensive.** Classification, workflow selection, criteria derivation
   and memory recall are pure functions. They run before any model call and produce the plan.
2. **Do the minimum.** The workflow catalog starts at a single in-session step. A trivial or
   answer-shaped request never creates a child session.
3. **Delegate only when routed.** Only steps the plan assigned a specialist role are
   delegated, and the plan is derived from signals rather than guessed.
4. **No duplicate work.** A previously verified similar task becomes a `preset`, and the
   corresponding step is marked `reused` instead of being executed again.
5. **No polling.** Worker completion is awaited through `ctx.session.wait()` raced against a
   single timeout; there is no sleep-and-check loop.
6. **Diagnose before repair.** A failed step is diagnosed (cause, retryable, strategy) before
   any retry. Non-retryable causes fail once and mark only their dependents as skipped.
7. **Bounded retries and timeouts.** Retries are capped per step, backoff is bounded, and a
   step that overruns its timeout stops the run rather than hanging.
8. **Finite budgets.** Time, tokens, cost, tool calls, steps and concurrency all have limits.
   On exhaustion the run stops with an honest `budget_exceeded` status.
9. **Stop when verified.** Delivery is reported as `delivered` only when verification passed.

## Missing telemetry

A measurement that was not reported is *unknown*, never zero. `src/core/budget.js` keeps an
`unknown` counter alongside `used`, and `src/plugin/telemetry.js` records the source of every
usage figure (`session.aggregate`, `session.messages`, `session.messages.unreported` or
`unavailable`). A missing cost is reported as unreported; it is not estimated and not
silently shown as `0`.

`undefined` means "not reported at all" and is ignored; `null` means "reported as
unavailable" and increments `unknown`. Both are asserted in the test suite.

## Reported metrics

`metrics.efficiency` contains ratios that are `null` when there is no data behind them:

| Field | Meaning |
|---|---|
| `deterministicShare` | share of decisions resolved without a model |
| `directShare` | share of steps executed in-session rather than delegated |
| `retryRate` | retries per attempt |
| `repairRate` | repairs per failure |
| `verificationPassRate` | verifications that passed |
| `costPerVerifiedStep`, `tokensPerVerifiedStep` | spend per verified step |
| `durationP50Ms` | median step duration |

`metrics.usage` carries the actual observed totals, `metrics.unknown` carries what could not
be observed, `metrics.budget` carries the budget snapshot, and `metrics.models` ranks the
models by steps with their tokens and cost.

## Measuring a change

`src/improve/` only acts on evidence: proposals need a minimum sample count, a minimum
measured effect, and a bounded, reversible target. Each applied change opens an experiment
with a sample floor and a deadline; the experiment is confirmed or reverted from the
observed statistics afterwards.

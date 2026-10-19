# Architecture

## Shape

```
                 ┌──────────────────────────────────────────────┐
   user request  │  OpenCode V2 session                         │
  ──────────────▶│  primary agent: swarm (manager)              │
                 └───────────────┬──────────────────────────────┘
                                 │  plugin runtime (src/plugin)
                 ┌───────────────▼──────────────────────────────┐
                 │  session.hook('prompt')   -> activate?       │
                 │  session.hook('context')  -> manager contract│
                 │  tool.transform           -> swarm_* tools   │
                 │  tool.hook('execute.after')-> real usage     │
                 └───────────────┬──────────────────────────────┘
                                 │
                 ┌───────────────▼──────────────────────────────┐
                 │  manager (src/engine.js)                     │
                 │  classify → recall → route → plan → execute  │
                 │  → verify → deliver → learn → (improve)      │
                 └───┬─────────┬──────────┬──────────┬──────────┘
                     │         │          │          │
              decide │   route │    exec  │  verify  │  memory / improve
        (deterministic)  (models)  (steps)  (judge)    (store, learning, tuner)
```

Every layer is plain ESM with no dependencies, so each can be exercised in isolation with
`node --test`.

## Pipeline

`createManager({runtime, store, clock, logger, policy}).submit(task, options)` runs:

1. **Classify** — `src/decide/signals.js` extracts deterministic signals from the text
   (domain hits, risk, ambiguity, paths, sub-task estimate) by whole-word matching; no model.
2. **Recall** — `src/memory/tasks.js` looks for a previously *verified* run of a similar
   task within the reuse window. A hit becomes a `preset`, so the expensive research step is
   marked `reused` and never re-run.
3. **Route** — `src/route/` normalises the live model catalog and scores candidates for the
   requirement. Dimensions with no observation are dropped from the score and their weight is
   redistributed; they are never treated as zero.
4. **Plan** — `src/decide/workflows.js` selects the minimum sufficient workflow from a fixed
   catalog; `src/decide/planner.js` materialises steps and derives acceptance criteria.
5. **Execute** — `src/exec/` topologically schedules steps, runs independent ones
   concurrently under a semaphore, diagnoses failures before retrying, applies bounded
   retries and step timeouts, and charges every attempt to the budget. In `interactive` mode
   the role-less step is returned to the manager's own session as a deferred instruction; in
   `headless` mode it is delegated to the `swarm` agent.
6. **Verify** — `src/verify/` evaluates deterministic criteria against the collected evidence
   and, when a verdict criterion exists, asks a QC agent bound to a *different* model to
   judge. A deterministic failure short-circuits before any model call.
7. **Deliver** — one delivery object with status, verified result, outputs, deferred steps,
   next actions and measured metrics.
8. **Learn** — one outcome per step plus a manager outcome is appended to
   `src/memory/learning.js`; the router reads these statistics on the next run.
9. **Improve** — optionally, `src/improve/loop.js` reviews open experiments, derives at most
   one bounded, reversible tuning change from evidence, and opens an experiment to measure it.

## Design rules

- **Deterministic before expensive.** Classification, workflow choice, criteria and reuse
  checks are pure functions. Models are consulted only where judgement is genuinely required.
- **Minimum sufficient workflow.** The cheapest workflow that can satisfy the request wins;
  a trivial question is answered in-session with no child sessions at all.
- **Missing telemetry is unknown.** A field that was not reported increments an `unknown`
  counter; it is never coerced to zero and never silently omitted from reporting.
- **Bounded everything.** Time, tokens, cost, tool calls, steps, retries and concurrency all
  have finite limits, and the run stops honestly when one is reached.
- **Verification is independent.** The judge is a different agent, and where the catalog
  allows, a different model from the producer. Independence is reported, never assumed.
- **Reversible improvement only.** The tuner changes policy parameters it can undo, records
  history, and opens a measurement window; it never edits its own source or agent pages.

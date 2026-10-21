# Testing

```sh
npm test            # everything, no network, no provider access
node --test         # equivalent, from the repository root
```

Node's default discovery treats files matching its test patterns under `test/` as tests, so a
bare `node --test` finds the whole suite (147 tests). Directory arguments such as
`node --test test/unit test/integration` do **not** work — Node treats the directory names as
failing test files.

## Layout

```
test/support/fake-context.js      a fake OpenCode plugin context (ctx) built on the real shapes
test/unit/core.test.js            result, errors, clock, budget, journal, metrics, logger
test/unit/decide.test.js          signals, classifier, workflows, criteria, plan
test/unit/route.test.js           catalog normalisation and routing behaviour
test/unit/exec.test.js            scheduling, parallelism, retries, timeouts, budget stops
test/unit/verify.test.js          deterministic criteria, verdict judging, independence
test/unit/memory.test.js          store adapters, learning statistics, task recall
test/unit/improve.test.js         tuner, proposals, gates, experiments, improvement loop
test/integration/plugin.test.js   activation, telemetry, tool registration, hooks, cleanup
test/integration/manager.test.js  end-to-end submit() through a fake runtime
```

## What the tests pin down

- **Budget honesty.** Unreported usage is counted as unknown; an explicit measured `0` is
  real data and is not treated as missing.
- **No delegation when it is not needed.** A trivial request produces a `direct` plan and
  creates zero child sessions.
- **Evidence-gated routing.** A model only wins on success after enough samples, and
  unobserved dimensions are dropped rather than scored zero.
- **Deterministic verification first.** A failing deterministic criterion short-circuits
  before any judge model is called.
- **Bounded work.** Retries, timeouts and budgets stop runs instead of looping.
- **Reuse.** A repeated, previously verified task reuses its research and creates no new
  sessions.
- **Resilience.** An unreachable model catalog is recorded as `source: "error"` and the run
  still proceeds; it is never reported as "zero models".

## Live check

`npm run live` runs a bounded check against a running OpenCode server (it uses the local
`opencode` CLI / server and will spend provider credit). It is deliberately kept out of
`test/` so it can never run as part of `npm test`. It is skipped unless `SWARM_LIVE=1` is
set, and it fails loudly if no provider is reachable rather than reporting success.

```sh
SWARM_LIVE=1 npm run live
```

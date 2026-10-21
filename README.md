# opencode-swarm

An autonomous, manager-led agent swarm for **OpenCode V2**. Submit a task in ordinary
natural language; the manager plans the smallest sufficient workflow, executes it, delegates
to specialists only when that pays off, verifies the acceptance criteria independently, and
delivers one result.

Zero dependencies. Pure ESM. No build step. Runs inside OpenCode's own Node runtime as a
plugin, and advertises a `swarm` primary agent so ordinary requests reach the manager without
any special command.

```
you: "Compare the current vector database options and write a short recommendation"
manager: swarm_plan -> researcher (child session) -> derive criteria -> swarm_verify
        -> delivery: recommendation + sources, verification: not_required, usage: measured
```

## What it does

- **Manager** — plans, orchestrates, tracks progress, verifies and delivers. Registered as
  the `swarm` primary agent.
- **Decision engine** — deterministic classification, workflow selection and acceptance
  criteria. Free, before any model call.
- **Specialists** — researcher, builder, visual, QC and improvement agents, spawned only
  when the plan routes a step to them.
- **Model router** — discovers the live catalog and chooses by capability, context window,
  latency, cost and *measured* success. Unobserved dimensions are dropped, never scored zero.
- **Execution engine** — dependency-aware scheduling, safe parallelism, bounded retries,
  timeouts and worker lifecycle.
- **Verification engine** — deterministic criteria first; a model judge only when needed, and
  only from a different model where one is available.
- **Memory and learning** — persistent task memory for reuse, and evidence-based outcome
  statistics that feed the router.
- **Controlled self-improvement** — bounded, reversible tuning proposals gated on sample size
  and effect, each opened as a measured experiment. Inspired by Harness²; no runtime
  dependency on it.

## Install

```sh
node scripts/install.mjs            # inspect first
node scripts/install.mjs --write    # apply
```

The installer is additive and idempotent: it copies the agent pages into
`~/.config/opencode/agents/`, adds this directory to the global `plugins` list, sets
`default_agent` to `swarm`, and removes the previous swarm's files. It preserves every
unrelated setting and never deletes anything it did not create.

Two rules keep re-runs safe. Pages that accumulate local history (such as
`agents/swarm/CHANGELOG.md`) are never overwritten once they exist, and a plain-JSON config
is validated before it is written — the installer aborts rather than leaving OpenCode with
a config it cannot parse. Run it without `--write` to see every action it would take.

## Test

```sh
npm test          # 151 unit + integration tests, no network, no provider access
npm run live      # opt-in bounded live check against the running OpenCode server
```

## Layout

```
src/core       budgets, journal, metrics, clock, errors, logger, result
src/decide     signals, classifier, workflow catalog, planner   (deterministic)
src/route      model catalog and capability-based router
src/exec       scheduler, executor, specialist definitions
src/verify     deterministic criteria and the verdict judge
src/memory     store adapters, learning statistics, task memory
src/improve    tuner, proposals, gates, experiments, loop
src/plugin     OpenCode adapter, activation, telemetry, tools, plugin runtime
src/engine.js  the manager
agents/        swarm manager + specialists
docs/          architecture, agents, configuration, efficiency, testing
```

## Documentation

- [Architecture](docs/architecture.md)
- [Agents](docs/agents.md)
- [Configuration](docs/configuration.md)
- [Efficiency](docs/efficiency.md)
- [Testing](docs/testing.md)

## License

MIT.

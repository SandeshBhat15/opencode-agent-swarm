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
npm test          # 173 unit + integration tests, no network, no provider access
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
src/dashboard  local read-only mission-control view (events, state, SSE server, page)
src/engine.js  the manager
agents/        swarm manager + specialists
docs/          architecture, agents, configuration, dashboard, efficiency, testing
```

## Mission control

A local dashboard shows the six agents as live nodes, driven only by events the engine
actually emitted. It starts with the plugin and opens in your default browser the first time
the swarm engages — an accepted prompt, a `swarm_*` tool call or a run starting — so no tab
appears before there is something in it to watch. It never slows or blocks startup: the
server comes up off the critical path and the browser launch happens in an unref'd
`setImmediate`.

Nothing to configure — but each switch turns part of it off:

```jsonc
{
  "plugins": [
    { "package": "file:///C:/path/to/opencode-agent-swarm/src/index.js",
      "options": { "dashboard": { "autoOpen": false } } }    // serve, but do not open a tab
      // "options": { "dashboard": { "autoOpen": "startup" } } // open the moment the plugin loads
      // "options": { "dashboard": { "enabled": false } }     // no dashboard at all
  ]
}
```

It binds `127.0.0.1` on an ephemeral port and requires a per-process capability token. The
browser is launched with a single-use launch ticket rather than the token, so the token never
reaches the address bar, a command line or a log line; a direct URL (token included) stays
available from `swarm_status` under `dashboard.url`. Opening is claimed once per process, so
extra sessions and plugin reloads do not stack tabs. If the browser cannot be launched the
server keeps running and one warning is logged.

`npm run demo` replays a full run against the real engine with a stubbed model, which is the
fastest way to see what the panels do. See [Dashboard](docs/dashboard.md) for what is shown,
what is deliberately not shown, and the limits of the security model.

## Documentation

- [Architecture](docs/architecture.md)
- [Agents](docs/agents.md)
- [Configuration](docs/configuration.md)
- [Dashboard](docs/dashboard.md)
- [Efficiency](docs/efficiency.md)
- [Testing](docs/testing.md)

## License

MIT.

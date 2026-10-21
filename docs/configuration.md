# Configuration

## Plugin options

Options are passed through the `plugins` entry as an object:

```jsonc
{
  "plugins": [
    { "package": "file:///C:/path/to/opencode-agent-swarm/src/index.js",
      "options": { "logLevel": "warn" } }
  ]
}
```

| Option | Default | Effect |
|---|---|---|
| `logLevel` | `"warn"` | `silent` \| `error` \| `warn` \| `info` \| `debug`. Logs go to stderr. |
| `namespace` | `"swarm"` | Storage namespace for memory, learning and tuner state. |
| `dataDir` | — | Use a filesystem store in this directory instead of the runtime store. |
| `store` | — | Inject a store adapter directly (used by tests). |
| `pluginId` | `"opencode-swarm"` | Plugin id reported to OpenCode. |
| `maxRetries` | unset | If set, installs a `session.hook('retry')` bound so a request cannot retry forever. Left unset, no retry hook is registered and OpenCode's own behaviour is untouched. |
| `dashboard.enabled` | `true` | Serve the local read-only mission-control page. On by default; only an explicit `false` turns it off. Starting it never delays plugin setup. |
| `dashboard.autoOpen` | `true` | Open the page in the machine's default browser once the server is listening — once per OpenCode process, never per prompt, task or reload. `false` keeps the server and leaves opening to you. |
| `dashboard.port` | `0` | Port for the dashboard. `0` = ephemeral. The bind host is always `127.0.0.1` and is not configurable. |

By default the dashboard starts with the plugin and opens itself in the default browser once
the server is listening; the URL, including its per-process capability token, is also
reported by `swarm_status` under `dashboard.url`, and `dashboard.autoOpen` there records
whether the tab was actually launched. See [Dashboard](dashboard.md).

## Policy

`src/engine.js` exports `DEFAULT_POLICY`, merged with anything passed to `createManager`:

| Key | Default | Effect |
|---|---|---|
| `budget.maxMs` | 900000 | Wall-clock ceiling for a task. |
| `budget.maxTokens` | 400000 | Token ceiling. |
| `budget.maxCostUSD` | 2 | Cost ceiling. |
| `budget.maxToolCalls` | 200 | Tool-call ceiling. |
| `budget.maxSteps` | 24 | Model-step ceiling. |
| `budget.maxRetries` | 2 | Retries per step. |
| `budget.maxConcurrency` | 3 | Concurrent steps. |
| `stepTimeoutMs` | 300000 | Per-step timeout. |
| `verify` | `true` | Run verification before delivering. |
| `reuseMemoryOverlap` | 0.6 | Keyword overlap needed to reuse a prior verified run. |
| `reuseMemoryMaxAgeMs` | 604800000 | Maximum age of a reusable result (7 days). |
| `mode` | `"interactive"` | `interactive` defers in-session steps to the manager; `headless` delegates them. |
| `autoImprove` | `false` | Run the gated improvement loop after each task. |

## Storage

State is keyed under a namespace (`swarm/...` by default) and lives in one of three adapters,
chosen in this order: an injected `store`, then OpenCode's plugin `ctx.storage`, then
`dataDir`, then an in-process memory store that logs a warning that it is volatile.

Keys written:

| Key | Contents |
|---|---|
| `<ns>/memory/tasks` | recent tasks with keywords, outputs and verification status |
| `<ns>/learning/outcomes` | per-step outcomes used for routing statistics |
| `<ns>/improve/state` | tuner weights, exclusions, retry bound, workflow preferences, history, experiments |

## Permissions

The manager needs to launch the `swarm/*` subagents. If you restrict `subagent` permissions
globally, grant them for the swarm agents. The installer does not modify your permission
rules beyond the swarm entries it creates.

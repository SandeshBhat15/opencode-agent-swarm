# Dashboard

A local, read-only mission-control view of the swarm. It renders the Manager, Researcher,
Builder, Visual, QC and Improve agents as connected nodes and shows what the engine
actually did: assignments, handoffs, step outcomes, verification, elapsed time, model
calls, measured tokens and cost, and the remaining budget.

It is a **view**, never a source of work. Nothing on the page can start, cancel or change a
task.

## Enabling it

The dashboard is off unless the plugin options say otherwise:

```jsonc
{
  "plugins": [
    { "package": "file:///C:/path/to/opencode-agent-swarm/src/index.js",
      "options": { "dashboard": { "enabled": true } } }
  ]
}
```

| Option | Default | Effect |
|---|---|---|
| `dashboard.enabled` | `false` | Explicit opt-in. Anything other than `true` leaves it off. |
| `dashboard.port` | `0` | `0` means an ephemeral port chosen by the OS. A fixed port is allowed but not recommended. |

`host` is not configurable: the server refuses to bind anything except `127.0.0.1`, and
re-checks the bound address after `listen()` in case a future refactor widens it.

Starting is deliberately off the critical path. The plugin returns from `setup()` first and
only then schedules `start()`, so a slow or failing dashboard can never delay or break
plugin initialisation. A failure is logged at `warn` and the swarm runs exactly as if the
dashboard were disabled.

## Opening it

Call `swarm_status`. The result contains:

```jsonc
"dashboard": {
  "enabled": true,
  "running": true,
  "host": "127.0.0.1",
  "port": 51234,
  "url": "http://127.0.0.1:51234/?token=…",
  "clients": 1,
  "events": { "published": 42, "retained": 42, "limit": 200, "subscribers": 1 }
}
```

Open `url` in a browser on the same machine. The token is generated per process, held in
memory only, never written to disk and never logged. It is returned only through the
authorised `swarm_status` tool call, which is why the URL is the one way to reach the page.

## What is shown — and what is not

Shown, and only when it was measured:

- run phase (`idle`, `running`, `completed`, `failed`, `cancelled`, `unknown`), task kind,
  workflow and elapsed time;
- each agent's state, current step id, attempt, resolved model and step duration;
- handoffs, derived from the plan's real dependency edges as steps start;
- step outcomes, retries and failures;
- verification result and the independence of the judge (`different_model`,
  `same_model_check`, `not_required`, `unavailable`);
- model calls (delegations), tokens, cost, tool calls and budget usage.

Never shown:

- prompt or task text, model output, tool output, file contents, source code, credentials,
  the capability token of the session, or any absolute path;
- free-text failure and verification reasons. Those quote the criterion, and a criterion is
  derived from the user's prompt, so the wire carries an error `code`, the judge's
  `independence`, and a failed-criterion count instead of prose.

That rule is enforced twice. At the source, `src/dashboard/events.js` drops `prompt`,
`output`, `text`, `task`, `content`, `messages`, `result`, `reason`, `message`, `detail`,
`error`, `token`, `url`, `file` and `path` keys - matched case-insensitively and by suffix,
so `userPrompt` and `toolOutput` go too - and truncates every string to 96 characters. At
the boundary, `src/dashboard/server.js` runs the whole derived state through the same
sanitizer before writing `/state` or an SSE frame, so a producer that forgets the rule
still cannot serve prose.

An unmeasured metric is `null` on the wire and renders as **Unavailable**, never `0`. That
is the same rule the rest of the swarm follows, applied to the UI.

## Transport

`GET /events` is Server-Sent Events:

- the first frame is `event: snapshot` with the whole derived state plus the last 40 events;
- every subsequent frame is `event: swarm` carrying one event and the state it produced;
- a `: ping` comment every 15 s keeps proxies from dropping an idle stream (no polling);
- a client that falls more than 1 MB behind is dropped rather than buffered;
- disconnects unsubscribe immediately and are counted in `clients()`.

The page reconnects at most five times with exponential backoff (0.5 s → 8 s), then stops
and says so. A hidden tab closes its stream and reopens it when visible again.

## Security model

- **Loopback only.** `127.0.0.1`, never `0.0.0.0`.
- **Capability token.** 32 random bytes, base64url, compared in constant time over SHA-256
  digests. Missing or wrong → `401`.
- **DNS rebinding.** The `Host` header must be `127.0.0.1:<port>` or `localhost:<port>`,
  otherwise `403`. A page served from an attacker's domain cannot read the endpoint by
  pointing a hostname at loopback.
- **Cross-origin.** A present `Origin` must be the dashboard's own origin, otherwise `403`.
  No CORS headers are ever sent, so a browser will not hand the response to another origin
  anyway.
- **Read-only.** `GET` and `HEAD` only; anything else is `405` with `Allow: GET, HEAD`.
  `/healthz` answers `{"ok":true}` without a token and without data.
- **Headers.** `cache-control: no-store`, `referrer-policy: no-referrer`,
  `x-content-type-options: nosniff`, and a CSP of `default-src 'none'` with inline
  style/script only — the page loads nothing from anywhere.
- **Rendering.** The page writes every server value with `textContent`. There is no
  `innerHTML` for data, so a malformed or hostile event cannot inject markup.

## Bounds

| Bound | Value | Where |
|---|---|---|
| Retained events | 200 | `src/dashboard/bus.js` |
| Per-subscriber queue | 64 (oldest dropped, counted) | `src/dashboard/bus.js` |
| Concurrent streams | 8 | `src/dashboard/bus.js` + `src/dashboard/server.js` |
| Pending bytes per stream | 1 MB | `src/dashboard/server.js` |
| Client log entries | 60 | `src/dashboard/page.js` |
| Reconnect attempts | 5 | `src/dashboard/page.js` |

A subscriber that throws three times loses its subscription and can never fail the
publisher. The publisher is the engine, so this is the mechanism that keeps a broken
dashboard from breaking a task.

## Relationship to the journal

The journal is the durable, authoritative record and is not modified by the dashboard. The
dashboard derives its state in memory from the same lifecycle points the journal records,
so the two agree by construction: there is no second measurement path that could disagree
with the first.

## Known limitations

- It is a **live view**, not history. State is in memory, so a plugin reload or an OpenCode
  restart clears it; use the journal for durable evidence.
- One process, one dashboard. Two OpenCode instances each get their own port and token.
- Node/agent placement is a fixed layout, not a force-directed graph; the edges animate
  only for handoffs that actually occurred.
- Live verification covers the engine, the HTTP surface and SSE via the real plugin
  bootstrap in the test suite. It has not been exercised end-to-end against a running
  OpenCode server with a real provider call — treat browser output as a view to be trusted
  only as far as the underlying events are real.

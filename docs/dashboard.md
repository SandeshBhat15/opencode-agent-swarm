# Dashboard

A local, read-only mission-control view of the swarm. It renders the Manager, Researcher,
Builder, Visual, QC and Improve agents as connected nodes and shows what the engine
actually did: assignments, handoffs, step outcomes, verification, elapsed time, model
calls, measured tokens and cost, and the remaining budget.

It is a **view**, never a source of work. Nothing on the page can start, cancel or change a
task.

## Enabling it

It is **on by default**. Nothing has to be configured: the server starts with the plugin, and
the first time the swarm engages with real work — an activated prompt, the first `swarm_*`
tool call, or the first engine event of a run — the plugin opens the page in the machine's
default browser. Loading the plugin on its own opens nothing, so a tab never appears before
there is something in it to watch.

```jsonc
// turn the server off entirely — no port, no page, no browser
{ "options": { "dashboard": { "enabled": false } } }

// keep the server and the swarm_status URL, but never open a tab
{ "options": { "dashboard": { "autoOpen": false } } }

// open as soon as the plugin loads, without waiting for work
{ "options": { "dashboard": { "autoOpen": "startup" } } }
```

| Option | Default | Effect |
|---|---|---|
| `dashboard.enabled` | `true` | Serves the page at all. Only an explicit `false` turns it off. |
| `dashboard.autoOpen` | `true` | `true` opens the page on first engagement; `"startup"` opens it as soon as the server is listening; `false` keeps the server and leaves opening to you. At most one tab per process either way. |
| `dashboard.port` | `0` | `0` means an ephemeral port chosen by the OS. A fixed port is allowed but not recommended. |

`host` is not configurable: the server refuses to bind anything except `127.0.0.1`, and
re-checks the bound address after `listen()` in case a future refactor widens it.

Everything about starting stays off the critical path. The plugin returns from `setup()`
first and only then schedules `start()`, and `start()` resolves as soon as the socket is
listening — the browser is opened on a later tick and never awaited. A slow, missing or
broken browser cannot delay plugin initialisation, fail it, or keep the process alive: the
launcher is started detached and unreferenced. A failure is logged once at `warn` and the
swarm runs exactly as if the dashboard were disabled.

## Opening it

The plugin does this for you, at most once per OpenCode process, and by default at the moment
the swarm engages: the first prompt it accepts, the first `swarm_*` tool call, or the first
engine event. The rule is deliberately process-wide rather than per-instance: a reload, a
second session, an agent spawn or two setups racing in the same tick all see the same claim,
so the user never collects duplicate tabs. The claim is never released — after a reload the
dashboard is a new server on a new port, and reopening would produce exactly the duplicate
tab this prevents.

The opener is platform-native and never involves a shell: `cmd /d /s /c start "" <url>` on
Windows, `open <url>` on macOS, `xdg-open <url>` on Linux — an argv array with
`shell: false`, so no part of the URL can be interpreted.

If you would rather open it yourself, call `swarm_status`. The result contains:

```jsonc
"dashboard": {
  "enabled": true,
  "running": true,
  "host": "127.0.0.1",
  "port": 51234,
  "url": "http://127.0.0.1:51234/?token=…",
  "clients": 1,
  "autoOpen": {
    "enabled": true, "when": "engaged", "claimed": true, "attempts": 1,
    "opened": true, "failed": false, "trigger": "activation",
    "origin": "http://127.0.0.1:51234", "error": null
  },
  "events": { "published": 42, "retained": 42, "limit": 200, "subscribers": 1 }
}
```

`autoOpen` reports what was **observed**, never what was intended: `opened: true` means the
platform launcher process actually started — this side cannot see inside the browser, so it
does not claim the page rendered. `when` is the configured moment (`engaged`, `startup` or
`never`) and `trigger` is what actually asked for the tab. `failed: true` with an `error`
means the server is fine and only the convenience failed.

Open `url` in a browser on the same machine. The token is generated per process, held in
memory only, never written to disk and never logged. It is returned only through the
authorised `swarm_status` tool call.

### What the browser is actually given

The capability token is never placed on a command line. `start()` mints a **launch ticket**
instead: 24 random bytes, valid for two minutes, redeemable once, and only for the page
document. Redeeming it makes the server serve the page with the token embedded in the body,
so the address bar, the browser history and every process that can read a command line hold
nothing durable. A ticket that leaks buys one page load inside its window — not the standing
capability, and not a single data route.

## Watching the swarm work

A plain chat message with the manager does not start a run: you see `activation` and `tool`
events, and the agent nodes stay idle. The full picture — plan steps, agents moving through
`running` → `completed`, handoff lines, counters, verification — appears when the engine
actually runs a task (`swarm_run`, or `swarm_delegate` past the activation gate).

To see it happen on demand, without spending model quota:

```bash
npm run demo                       # opens the tab and runs a three-step research → build → verify task
npm run demo -- --delay=4000       # slower steps, easier to follow
npm run demo -- --no-open          # serve only; prints the origin
npm run demo -- --keep-alive=600   # leave the dashboard up after the run
npm run demo -- --repeats=3        # run it three times; each run resets the view and animates again
```

A run starts only once a browser is actually watching: the demo polls `dashboard.info().clients`
and waits up to 90 s for the tab to subscribe before beginning, because a background tab closes
its event stream on `visibilitychange` and would otherwise let the whole animation play where
nobody sees it. If nothing connects it runs anyway and says so. With `--no-open` it starts
immediately, and `--port=N` pins the listening port instead of taking an ephemeral one.

The demo is the real dashboard, real event bus, real derived state, real classifier, planner,
step runner and verifier. Only `runtime.delegate()` is stubbed: it sleeps and returns a canned
answer (plus a parseable verdict for the QC agent) instead of calling a provider. So the run
ends `delivered` with `independence: different_model`, and every frame you see came from an
event the engine genuinely emitted — nothing is pre-scripted into the page.

Exit codes: `0` when the run finished and the dashboard shut down, `1` when the server could
not start. `SWARM_DEMO_STATE=<path>` writes `{pid, origin, startedAt, task}` so a script can
read the port back; the capability token is never included.

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
- **Launch tickets.** The browser is opened with a single-use ticket, never the token:
  24 random bytes, two-minute TTL, one redemption, page document only, at most 8
  outstanding. Host and `Origin` are validated *before* a ticket is looked at, so a page on
  another origin cannot spend one either. The token reaches the page in its body, so it is
  never in the address bar, the history or an argv.
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
| Outstanding launch tickets | 8 (oldest dropped) | `src/dashboard/handshake.js` |
| Launch ticket lifetime | 120 s, one use | `src/dashboard/handshake.js` |

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
- The tab is opened **once per process**, so a plugin reload does not open a second one: the
  page already open keeps running against its original port. Refresh it with the new
  `swarm_status` URL after a reload if you want the new server's state.
- `autoOpen.opened` means the platform launcher process started; it is not a claim that a
  window became visible (a window manager may focus another desktop, or a policy may block
  it). The `swarm_status` URL is always the fallback.
- Node/agent placement is a fixed layout, not a force-directed graph; the edges animate
  only for handoffs that actually occurred.
- Live verification covers the engine, the HTTP surface and SSE via the real plugin
  bootstrap in the test suite. It has not been exercised end-to-end against a running
  OpenCode server with a real provider call — treat browser output as a view to be trusted
  only as far as the underlying events are real.

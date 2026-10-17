# `swarm-lifecycle` — the OpenCode plugin

Session-bound lifecycle control for the OpenCode Agent Swarm. It wires OpenCode's real
session events to the stdlib-only `swarmkit` lifecycle engine: it stops the work a session
owns when that session ends, stops everything when the *application* ends, keeps a durable
checkpoint of real progress, and resumes a task only after the user asks.

It owns no durable state. Every number it renders comes from a `swarmkit` task record on
disk, which is why a restart cannot invent progress.

- Engine: [`swarmkit/lifecycle.py`](../swarmkit/lifecycle.py) · CLI: `python -m swarmkit lifecycle --help`
- Zero dependencies. Node built-ins only.

---

## Install it in exactly ONE place

Two locations are supported. Pick one.

**A. Global — every project on this machine**

```sh
cp plugin/swarm-lifecycle.js ~/.config/opencode/plugins/swarm-lifecycle.js
```

**B. Per-project — this repository only**

The file is already in place at `.opencode/plugins/swarm-lifecycle.js`. It contains no logic:
it re-exports the one real implementation.

```js
export { default, __testing } from "../../plugin/swarm-lifecycle.js"
```

**Do not install it in both.** Two files are two module instances with two independent
install guards, so both install and every lifecycle action fires twice — a session billed
twice, and progress lines the user cannot tell apart. That duplication is the exact failure
the re-export shim exists to prevent.

A per-project install needs no configuration: with no options set, the plugin resolves
`swarmkit/` from its own directory before walking up from the project directory, so it finds
the engine sitting next to it. Full order: `ctx.options.repoRoot` → `SWARMKIT_ROOT` → the
plugin's own directory → a walk up from the project directory. If Python is not on `PATH` as
`python`, set `SWARMKIT_PYTHON` (or the `pythonExe` option).

---

## What it uses from OpenCode

OpenCode **V2**. Every interface below was verified against a real **OpenCode 2.0.32 server**,
not read off a changelog. The measurements are recorded in the source next to the code that
depends on them.

| Interface | Why |
| --- | --- |
| default export `{ id, setup(ctx) }` | plugin entry point |
| `setup()` returning a cleanup function | the **only** supported application-shutdown signal |
| `ctx.event.subscribe({ signal })` | async stream of server events |
| `ctx.session.hook("prompt", fn)` | user-prompt admission, i.e. the "continue" path |
| `ctx.session.synthetic({ sessionID, text })` | plugin-authored messages that do **not** re-trigger the prompt hook |
| `ctx.tool.transform(editor)` → `editor.add(...)` | in-session `swarm_lifecycle` tool |
| `ctx.options`, `ctx.paths`, `ctx.location` | optional configuration, temp dir, project directory |

Optional keys read from `ctx.options`: `enabled`, `diagnostics`, `pythonExe`, `timeoutMs`,
`progressThrottleMs`, `postProgress`, `verbose`, `repoRoot`.

### The measured fact that shaped the code

**OpenCode instantiates a fresh module per `setup()` call.** A live 2.0.32 server called
`setup()` four times in one process during plugin hot-reload, and every event was handled four
times over. That is not cosmetic: four event pumps, four `session-close` calls on the same
close, four `note`s per step.

Idempotency is therefore the plugin's job, not the host's — and the guard **must** live on
`globalThis`, not in module scope. A module-level `let installed` compiled, passed every unit
test, and still never fired: each `setup()` gets a re-initialised module scope, so the flag
could never become true. `globalThis` is the only scope shared by every module instance in the
process.

Two other things measured rather than assumed, both recorded in the source:

- The session id lives at `event.data.sessionID`, with `event.durable.aggregateID` as a
  fallback. Reading only the older `properties` spelling resolved no session at all, which
  would have left every event inert.
- `durable.aggregateID` is **not** always a session id — a `shell.created` event carries `sh_…`
  there. Returning it blindly would aim `session-close` at a session that does not exist, and
  the cancel would silently do nothing. The plugin prefix-checks it.

---

## Closing one chat is not closing the application

These are two different events and they are handled by two different paths, on purpose.

- **A session ends** (`session.deleted` / `session.closed` / `session.destroyed`) →
  `lifecycle session-close --session <id>`. Stops only what *that* session owns and leaves
  every other session's tasks running. Two chats can be mid-task in one project; closing one
  is not a reason to stop the other.
- **The application goes away** (the cleanup function `setup()` returns) →
  `lifecycle shutdown`. Every session in the process is ending together, so everything this
  application owned is stopped.

Neither deletes anything. Records, completed steps and artifacts outlive the session that
produced them until `lifecycle prune` is run explicitly.

---

## Progress is read, never estimated

`formatProgress()` renders one line, and it returns `null` unless the engine returned both a
task id and a state — there is no default that could stand in for missing information and read
as work. Steps come from the plan and the completed set, workers from the process registry.
The last rendered line is fingerprinted and only re-posted when it actually changes, so an
idle session stays quiet instead of posting the same line forever.

`session.idle` and tool-activity events trigger a read. Only `session.step.ended` is counted as
progress, and it goes to the engine, which derives the step count from the plan — so nothing
in the plugin can invent a step that did not end.

## In-session tool

`swarm_lifecycle`, in the `swarm` namespace. The plugin forwards **only** the flags each action
accepts, because forwarding everything turned `lifecycle checkpoint --reason x` into an
argparse usage error — the subcommand has no such flag, so the call died before touching state.

```
swarm_lifecycle action=status session=<session-id>
swarm_lifecycle action=start session=<session-id> goal="..."
swarm_lifecycle action=checkpoint task=<id> step=<id> epoch=<epoch> artifact=<path>
swarm_lifecycle action=resume task=<id>
swarm_lifecycle action=cancel task=<id> reason="..."
swarm_lifecycle action=list
```

`session-close`, `shutdown`, `recover`, `note` and `resume-plan` are deliberately absent: they
are host-driven, and an agent that could fire them could cancel another session's work or
force a resume the user never asked for.

---

## Tests

```sh
node --test plugin/swarm-lifecycle.test.js
```

Last run on this machine: **24 passed, 0 failed**.

**These tests are SIMULATED, and that is a precise word.** `ctx` is a fake, and the
`swarmkit` the plugin shells out to is a stub that records the argv it was handed and prints a
canned payload. Nothing in that file runs inside a real OpenCode server or against the real
state store. What it proves is the plugin's half of the contract: which command it builds for
a given host event, which flags it forwards, and that it never claims progress it did not read.

That half is where four silent failures lived — a `--session` flag the parser did not accept, a
subcommand that did not exist, flags forwarded to a subcommand that did not accept them, and a
JSON document parsed one line at a time (the engine pretty-prints, so that yields a bare `}`).
None of them looked wrong until something was executed.

Two things make the simulation honest rather than convenient:

- `ACTION_FLAGS only offers flags the real lifecycle CLI accepts` shells out to the **real**
  `python -m swarmkit lifecycle <action> --help` and asserts every flag the plugin is willing
  to send exists. That one test would have caught all three flag/subcommand breaks.
- The other half — the plugin loaded by a live server, cancelling real sessions — is the
  verification described above. It is not simulated and it is not reproducible from CI.

So: simulation guards the seam, the live-server run is the real check. Neither substitutes for
the other, and this file reports both rather than claiming the stronger one.
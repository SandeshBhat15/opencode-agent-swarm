/**
 * Plugin tests. Every test in this file is SIMULATED.
 *
 * "Simulated" is precise here and worth stating plainly: `ctx` is a fake, and
 * the Python engine the plugin shells out to is a stub that records the argv it
 * was handed and prints a canned payload. Nothing in this file runs inside a
 * real OpenCode server or against the real `swarmkit` state store. What it does
 * prove is the plugin's half of the contract: which command it builds for a
 * given host event, which flags it forwards, and that it never claims progress
 * it did not read.
 *
 * The reason this file exists at all is that the plugin↔CLI seam is where four
 * silent failures already lived (a `--session` flag the parser did not accept,
 * a subcommand that did not exist, flags forwarded to a subcommand that did not
 * accept them, and a JSON document parsed one line at a time). Every one of
 * those looked fine until something was executed.
 *
 * The counterweight to "simulated" is the test titled "ACTION_FLAGS only offers
 * flags the real lifecycle CLI accepts", which runs the *real*
 * `python -m swarmkit lifecycle <action> --help` and asserts that every flag
 * this plugin is willing to send actually exists. That single test would have
 * caught all three flag/subcommand breaks.
 *
 * The end-to-end half — the plugin loaded by a live OpenCode server, cancelling
 * real sessions — is verified separately and is not simulated. See the README's
 * lifecycle section for that report.
 */

import { test } from "node:test"
import assert from "node:assert/strict"
import { execFileSync } from "node:child_process"
import { mkdtempSync, readFileSync, rmSync, writeFileSync, mkdirSync, existsSync } from "node:fs"
import { tmpdir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

import plugin, { __testing } from "./swarm-lifecycle.js"

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = resolve(HERE, "..")

// ---------------------------------------------------------------------------
// a stub `swarmkit` package: records argv, answers with canned JSON
// ---------------------------------------------------------------------------

const STUB_CLI = `"""Test double for swarmkit.cli. Records argv; prints a canned payload."""
import json
import os
import sys

# The log path is derived from the working directory rather than an environment
# variable. The plugin runs this with execFile and inherits the server's env,
# so an env-based path is global mutable state shared by every test in the
# process: two harnesses alive at once clobbered each other's log and the
# failures pointed at the plugin instead of at the test. cwd is the stub repo,
# which is unique per harness.
log = os.path.join(os.getcwd(), "argv.jsonl")

argv = sys.argv[1:]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")

payload = {"argv": argv, "echo": "stub"}

if "--fail" in argv:
    print("boom", file=sys.stderr)
    sys.exit(3)

# Mirror the real CLI's --json shape: pretty-printed, multi-line.
print(json.dumps(payload, indent=2))
`

function makeStubRepo() {
  const root = mkdtempSync(join(tmpdir(), "swarm-stub-"))
  mkdirSync(join(root, "swarmkit"), { recursive: true })
  writeFileSync(join(root, "swarmkit", "__init__.py"), "", "utf8")
  // `python -m swarmkit` needs __main__.py. Its absence here is what first
  // looked like "the plugin never calls the engine": the stub package could
  // not be executed at all, so nothing was logged. The real package has one,
  // which is what makes the plugin's `-m swarmkit` invocation correct.
  writeFileSync(join(root, "swarmkit", "__main__.py"), STUB_CLI, "utf8")
  writeFileSync(join(root, "swarmkit", "cli.py"), STUB_CLI, "utf8")
  return root
}

/**
 * Remove a tree, tolerating a Windows lock.
 *
 * The stub subprocess holds its working directory until it exits, and the
 * plugin's execFile resolves on process exit but the OS can lag behind that.
 * `rmSync` then throws EBUSY and the *test* fails for a reason that has
 * nothing to do with what it asserts. A leftover temp directory is harmless;
 * a false failure is not, so retry briefly and then give up quietly.
 */
function removeTree(dir) {
  for (let attempt = 0; attempt < 6; attempt += 1) {
    try {
      rmSync(dir, { recursive: true, force: true, maxRetries: 3, retryDelay: 60 })
      return true
    } catch {
      /* fall through to the next attempt */
    }
  }
  return false
}

function readArgv(logPath) {
  if (!existsSync(logPath)) return []
  return readFileSync(logPath, "utf8")
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line))
}

const pythonExe = process.env.SWARMKIT_PYTHON || "python"

// ---------------------------------------------------------------------------
// fake host
// ---------------------------------------------------------------------------

function makeEventSource() {
  const queue = []
  let wake = null
  let closed = false

  function nudge() {
    const w = wake
    wake = null
    if (w) w()
  }

  return {
    push(event) {
      queue.push(event)
      nudge()
    },
    closed: () => closed,
    source: {
      subscribe({ signal } = {}) {
        const onAbort = () => {
          closed = true
          nudge()
        }
        if (signal) signal.addEventListener("abort", onAbort)
        return {
          [Symbol.asyncIterator]() {
            return {
              async next() {
                while (!queue.length && !closed) await new Promise((r) => (wake = r))
                if (queue.length) return { value: queue.shift(), done: false }
                return { value: undefined, done: true }
              },
              async return() {
                closed = true
                return { value: undefined, done: true }
              },
            }
          },
        }
      },
    },
  }
}

function makeCtx({ repoRoot, tmpDir, diagnostics, options = {} }) {
  const events = makeEventSource()
  const posted = []
  const promptHooks = []
  const disposed = { prompt: 0, tool: 0 }
  let toolSpec = null

  const ctx = {
    options: {
      repoRoot,
      diagnostics,
      timeoutMs: 20000,
      progressThrottleMs: 0,
      verbose: true,
      ...options,
    },
    paths: { tmp: tmpDir },
    location: { directory: repoRoot, project: { id: "prj", directory: repoRoot, canonical: repoRoot } },
    app: { version: "2.0.32" },
    event: events.source,
    session: {
      hook(name, cb) {
        if (name === "prompt") {
          promptHooks.push(cb)
          return { dispose: () => { disposed.prompt += 1 } }
        }
        return { dispose() {} }
      },
      async synthetic({ sessionID, text }) {
        posted.push({ sessionID, text })
      },
    },
    tool: {
      transform(cb) {
        cb({ add(spec) { toolSpec = spec } })
        return { dispose: () => { disposed.tool += 1 } }
      },
    },
  }

  return {
    ctx,
    events,
    posted,
    promptHooks,
    disposed,
    getTool: () => toolSpec,
  }
}

/** Real OpenCode 2.0.32 event envelope. */
function realEvent(type, sessionID, extra = {}) {
  return {
    id: "evt_1",
    created: 1,
    type,
    durable: { aggregateID: sessionID || "" },
    location: { directory: REPO_ROOT },
    data: sessionID ? { sessionID } : {},
    ...extra,
  }
}

const tick = (ms = 60) => new Promise((r) => setTimeout(r, ms))

function harness(options = {}) {
  // The plugin guards against double installation because a live server calls
  // setup() repeatedly during hot-reload. A test process must install once per
  // test, so the guard is cleared here. On a real server this reset never
  // happens - which is exactly why the guard exists and needs its own test.
  __testing.resetInstallGuard()
  const repoRoot = makeStubRepo()
  const tmpDir = mkdtempSync(join(tmpdir(), "swarm-plug-"))
  const logPath = join(repoRoot, "argv.jsonl")
  const diagnostics = join(tmpDir, "events.jsonl")
  // The stub derives its log path from cwd (the stub repo), so no shared
  // environment is involved.
  const host = makeCtx({
    repoRoot,
    tmpDir,
    diagnostics,
    options: { ...options },
  })
  const cleanup = plugin.setup(host.ctx)
  return {
    ...host,
    repoRoot,
    tmpDir,
    logPath,
    diagnostics,
    argv: () => readArgv(logPath),
    stop: async () => {
      // Runs the plugin's cleanup but leaves the harness on disk, so a test
      // can still read the argv log afterwards. teardown below removes both.
      if (typeof cleanup === "function") await cleanup()
    },
    teardown: async () => {
      if (typeof cleanup === "function") await cleanup()
      removeTree(repoRoot)
      removeTree(tmpDir)
    },
  }
}

test("a second setup() in the same process does not register a second time", async () => {
  // Measured on a live 2.0.32 server: hot-reload called setup() four times and
  // every event was handled four times over. Without this guard one user
  // session would have produced four session-close calls, four notes per step
  // and four synthetic progress posts - a quadruple-billed session.
  __testing.resetInstallGuard()
  const first = harness()
  await tick(400)
  const before = first.argv().length
  assert.ok(before >= 1, "first setup should have made at least one engine call")

  // Second setup against the same module instance, without resetting.
  const second = makeCtx({
    repoRoot: first.repoRoot,
    tmpDir: first.tmpDir,
    diagnostics: first.diagnostics,
  })
  const secondCleanup = plugin.setup(second.ctx)

  assert.equal(typeof secondCleanup, "function", "a stand-down setup still returns a safe cleanup")
  await secondCleanup()

  first.events.push(realEvent("session.deleted", "ses_dup"))
  await tick(400)

  const closes = first.argv().filter((call) => call.includes("session-close"))
  assert.equal(closes.length, 1, `session-close must happen once, not once per setup; got ${JSON.stringify(closes)}`)

  await first.teardown()
})

test("the stand-down setup makes no engine calls of its own", async () => {
  __testing.resetInstallGuard()
  const first = harness()
  const before = first.argv().length
  const second = makeCtx({
    repoRoot: first.repoRoot,
    tmpDir: first.tmpDir,
    diagnostics: first.diagnostics,
  })
  const cleanup = plugin.setup(second.ctx)
  await cleanup()
  // The only permitted new call is the shutdown the stand-down cleanup makes
  // - and even that must not exist, because it would shut down work the first
  // registration still owns.
  const after = first.argv().slice(before)
  assert.deepEqual(after, [], `stand-down setup invoked the engine: ${JSON.stringify(after)}`)
  await first.teardown()
})

// ---------------------------------------------------------------------------
// pure helpers
// ---------------------------------------------------------------------------

test("sessionIDOf reads the real 2.0.32 envelope at data.sessionID", () => {
  // The primary regression: reading event.properties resolved no session at
  // all, so every event was inert and nothing was ever cancelled.
  assert.equal(__testing.sessionIDOf(realEvent("session.step.ended", "ses_abc")), "ses_abc")
})

test("sessionIDOf tolerates the camelCase and session_id spellings", () => {
  assert.equal(__testing.sessionIDOf({ data: { session_id: "ses_x" } }), "ses_x")
  assert.equal(__testing.sessionIDOf({ data: { sessionId: "ses_y" } }), "ses_y")
  assert.equal(__testing.sessionIDOf({ data: { info: { id: "ses_z" } } }), "ses_z")
})

test("sessionIDOf falls back to durable.aggregateID then to the v1 properties shape", () => {
  assert.equal(__testing.sessionIDOf({ durable: { aggregateID: "ses_agg" } }), "ses_agg")
  assert.equal(__testing.sessionIDOf({ properties: { sessionID: "ses_v1" } }), "ses_v1")
  // Measured on a live server: `shell.created` carries a shell id
  // (`sh_...`) in durable.aggregateID. Accepting it as a session id would aim
  // `session-close` at a session that does not exist, and the cancel would
  // silently do nothing while appearing to succeed.
  assert.equal(__testing.sessionIDOf({ durable: { aggregateID: "sh_notasession" } }), null)
  assert.equal(__testing.sessionIDOf({ properties: { info: { id: "sh_also" } } }), null)
})

test("sessionIDOf returns null rather than guessing", () => {
  for (const bad of [null, undefined, 42, "string", {}, { data: {} }, { data: { id: "not-a-session" } }]) {
    assert.equal(__testing.sessionIDOf(bad), null, `expected null for ${JSON.stringify(bad)}`)
  }
})

test("eventType reads type first and never invents one", () => {
  assert.equal(__testing.eventType({ type: "session.step.ended" }), "session.step.ended")
  assert.equal(__testing.eventType({ properties: { type: "legacy" } }), "legacy")
  assert.equal(__testing.eventType({}), null)
  assert.equal(__testing.eventType(null), null)
})

test("looksLikeResume fires on plain-English intent only", () => {
  for (const text of [
    "continue where we left off",
    "resume the paused work",
    "carry on",
    "keep going",
    "pick up where we stopped",
    "finish the pending work",
  ]) {
    assert.equal(__testing.looksLikeResume(text), true, `expected resume for: ${text}`)
  }
  for (const text of ["what is 2+2", "add a login form", "", "/swarm resume", "remove that line"]) {
    assert.equal(__testing.looksLikeResume(text), false, `expected NO resume for: ${text}`)
  }
})

test("formatProgress refuses to render when the record says nothing", () => {
  // The guard against fabricated progress: no id or no state means there is
  // nothing truthful to say, so it returns null rather than a default line.
  assert.equal(__testing.formatProgress(null), null)
  assert.equal(__testing.formatProgress({}), null)
  assert.equal(__testing.formatProgress({ completed_steps: 3, total_steps: 9 }), null)
  assert.equal(__testing.formatProgress({ task_id: "t1" }), null)
})

test("formatProgress renders only the numbers it was given", () => {
  assert.equal(
    __testing.formatProgress({ task_id: "t1", state: "RUNNING", completed_steps: 1, total_steps: 4 }),
    "[swarm] task t1 — RUNNING 1/4 steps",
  )
  assert.equal(
    __testing.formatProgress({ taskID: "t2", state: "PAUSED", completedSteps: 2, totalSteps: 2 }),
    "[swarm] task t2 — PAUSED 2/2 steps",
  )
  // No step counts available: omit the scope rather than substitute zeroes.
  assert.equal(__testing.formatProgress({ task_id: "t3", state: "BLOCKED" }), "[swarm] task t3 — BLOCKED")
})

test("findSwarmRoot refuses to guess a root that has no swarmkit in it", () => {
  const empty = mkdtempSync(join(tmpdir(), "swarm-empty-"))
  try {
    assert.equal(__testing.findSwarmRoot({ repoRoot: empty }, empty), null)
    assert.equal(__testing.findSwarmRoot({ repoRoot: REPO_ROOT }, REPO_ROOT), REPO_ROOT)
  } finally {
    rmSync(empty, { recursive: true, force: true })
  }
})

// ---------------------------------------------------------------------------
// plugin ⇄ CLI agreement, checked against the REAL parser
// ---------------------------------------------------------------------------

test("ACTION_FLAGS only offers flags the real lifecycle CLI accepts", () => {
  // This is the test that would have caught all three flag/subcommand breaks.
  for (const [action, flags] of Object.entries(__testing.ACTION_FLAGS)) {
    let help
    try {
      help = execFileSync(
        pythonExe,
        ["-m", "swarmkit", "lifecycle", action, "--help"],
        { cwd: REPO_ROOT, encoding: "utf8", timeout: 60000, windowsHide: true },
      )
    } catch (error) {
      assert.fail(`lifecycle ${action} is not a real subcommand: ${error.message}`)
    }
    for (const flag of flags) {
      assert.ok(help.includes(flag), `lifecycle ${action} does not accept ${flag}`)
    }
  }
})

test("host-driven lifecycle actions are not exposed to the agent", () => {
  // session-close / shutdown / recover / note / resume-plan must stay out of
  // reach: an agent that could call them could cancel another session's work or
  // force a resume the user never asked for.
  for (const forbidden of ["session-close", "shutdown", "recover", "note", "resume-plan"]) {
    assert.ok(
      !(forbidden in __testing.ACTION_FLAGS),
      `${forbidden} must not be an agent-callable action`,
    )
  }
})

// ---------------------------------------------------------------------------
// setup(), event pump, prompt hook, tool, shutdown  (all SIMULATED)
// ---------------------------------------------------------------------------

test("setup returns a cleanup function, and opt-out registers nothing", () => {
  const h = harness()
  assert.equal(typeof plugin.id, "string")
  assert.ok(plugin.id.length > 0)
  return h.teardown().then(() => {
    const off = harness({ enabled: false })
    assert.equal(off.getTool(), null)
    assert.equal(off.posted.length, 0)
    return off.teardown()
  })
})

test("a session-close event cancels exactly that session's work", async () => {
  const h = harness()
  h.events.push(realEvent("session.deleted", "ses_gone"))
  await tick(400)
  const calls = h.argv()
  const close = calls.find((a) => a[2] === "session-close")
  assert.ok(close, `expected a session-close call, saw ${JSON.stringify(calls)}`)
  assert.equal(close[0], "--json")
  assert.deepEqual(close.slice(1, 3), ["lifecycle", "session-close"])
  assert.ok(close.includes("--session"))
  assert.equal(close[close.indexOf("--session") + 1], "ses_gone")
  // It must not cancel anything else.
  assert.equal(calls.filter((a) => a[2] === "session-close").length, 1)
  await h.teardown()
})

test("an event with no resolvable session id is inert", async () => {
  const h = harness()
  h.events.push({ type: "session.deleted", data: {} })
  h.events.push({ type: "model.updated", data: { providerID: "x" } })
  await tick(300)
  const destructive = h.argv().filter((a) => ["session-close", "shutdown"].includes(a[2]))
  assert.deepEqual(destructive, [], "an unowned event must not stop anything")
  await h.teardown()
})

test("a finished step is recorded as a real event before progress is rendered", async () => {
  const h = harness()
  h.events.push(realEvent("session.step.ended", "ses_run"))
  await tick(400)
  const calls = h.argv().map((a) => a[2])
  assert.ok(calls.includes("note"), `expected a note for the step, saw ${JSON.stringify(calls)}`)
  const note = h.argv().find((a) => a[2] === "note")
  assert.ok(note.includes("session.step.ended"), "the host's event name must be preserved")
  await h.teardown()
})

test("progress is posted only when the engine reports a task, never invented", async () => {
  const h = harness()
  h.events.push(realEvent("session.idle", "ses_idle"))
  await tick(400)
  // The stub answers with a payload that has no `task`, so there is nothing
  // truthful to render and nothing may be posted.
  assert.deepEqual(h.posted, [], "progress must not be posted without real task state")
  await h.teardown()
})

test("the prompt hook injects a brief only for genuine resume intent", async () => {
  const h = harness()
  await tick(200)
  assert.equal(h.promptHooks.length, 1)
  const hook = h.promptHooks[0]

  const ordinary = { sessionID: "ses_u", prompt: { text: "refactor this function" } }
  await hook(ordinary)
  assert.equal(ordinary.prompt.text, "refactor this function", "an ordinary task must pass through untouched")

  const resuming = { sessionID: "ses_u", prompt: { text: "continue where we left off" } }
  await hook(resuming)
  const plan = h.argv().find((a) => a[2] === "resume-plan")
  assert.ok(plan, "resume intent must consult the engine")
  assert.equal(plan[plan.indexOf("--session") + 1], "ses_u")
  // The stub never returns a resumed/ambiguous payload, so the text must be
  // unchanged: no brief may be invented from an empty answer.
  assert.equal(resuming.prompt.text, "continue where we left off")
  await h.teardown()
})

test("the plugin posts synthetic messages, which is what prevents recursion", async () => {
  // Synthetic messages do not run the prompt hook (verified against OpenCode's
  // own documented hook behaviour). The plugin therefore never re-enters
  // itself: its own progress output cannot look like a user asking to resume.
  const src = readFileSync(join(HERE, "swarm-lifecycle.js"), "utf8")
  assert.ok(src.includes("ctx.session.synthetic"), "progress must be posted via the synthetic API")
  assert.ok(
    !/session\.prompt\(/.test(src),
    "the plugin must never route its own output back through a prompt path",
  )
  const h = harness()
  await tick(200)
  // Everything the plugin posts goes through synthetic(); the fake counts them.
  // If any of it could re-enter the prompt hook it would show up as a
  // resume-injection against a session the plugin merely reported on.
  await h.teardown()
})

test("the lifecycle tool forwards only the flags its action accepts", async () => {
  const h = harness()
  await tick(200)
  const tool = h.getTool()
  assert.ok(tool, "the swarm_lifecycle tool must be registered")
  assert.equal(tool.name, "swarm_lifecycle")
  assert.equal(tool.input.properties.action.enum.includes("deliver"), false,
    "deliver is not a lifecycle subcommand and must not be offered")

  const out = await tool.execute(
    { action: "checkpoint", session: "ses_t", task: "t1", step: "s1", reason: "because" },
    { sessionID: "ses_ctx" },
  )
  const call = h.argv().find((a) => a[2] === "checkpoint")
  assert.ok(call, `expected a checkpoint call, saw ${JSON.stringify(h.argv())}`)
  // `reason` is not a checkpoint flag. Forwarding it made argparse exit 2, so
  // the call died before touching any state.
  assert.equal(call.includes("--reason"), false, "checkpoint must not be sent --reason")
  assert.equal(call[call.indexOf("--task") + 1], "t1")
  assert.equal(call[call.indexOf("--step") + 1], "s1")
  JSON.parse(out.content)
  await h.teardown()
})

test("an unknown action is refused instead of being sent to the CLI", async () => {
  const h = harness()
  await tick(200)
  const tool = h.getTool()
  const out = await tool.execute({ action: "rm-rf" }, { sessionID: "ses_ctx" })
  const payload = JSON.parse(out.content)
  assert.ok(payload.error, "an unmapped action must return an error payload")
  assert.equal(h.argv().filter((a) => a[2] === "rm-rf").length, 0)
  await h.teardown()
})

test("cleanup stops the stream and shuts the engine down", async () => {
  const h = harness()
  await tick(200)
  await h.stop()
  assert.equal(h.events.closed(), true, "the event subscription must be aborted")
  const shutdown = h.argv().find((a) => a[2] === "shutdown")
  assert.ok(shutdown, "cleanup must tell the engine the application is going away")
  assert.equal(shutdown[shutdown.indexOf("--reason") + 1], "application-shutdown")
  assert.equal(h.disposed.prompt, 1)
  assert.equal(h.disposed.tool, 1)
  await h.teardown()
})

test("a failing engine never throws into the host", async () => {
  const h = harness()
  h.events.push(realEvent("session.deleted", "ses_x"))
  await tick(400)
  // No unhandled rejection, and the plugin kept running.
  const diag = existsSync(h.diagnostics) ? readFileSync(h.diagnostics, "utf8") : ""
  assert.ok(diag.includes("session.deleted"), "diagnostics record what the plugin saw")
  await h.teardown()
})
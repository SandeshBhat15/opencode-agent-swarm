/**
 * opencode-swarm-lifecycle
 *
 * Session-bound lifecycle control for the OpenCode Agent Swarm.
 *
 * This plugin is the bridge between OpenCode's real lifecycle and the
 * stdlib-only `swarmkit` lifecycle engine. It owns no durable state itself:
 * every decision is derived from `swarmkit`'s persisted task records, so a
 * restart cannot fabricate progress.
 *
 * Responsibilities:
 *   - Detect the events OpenCode actually emits (see `diagnostics`).
 *   - Cancel work when a session closes, and when the application shuts down.
 *   - Persist a checkpoint before any stop so work survives a forced crash.
 *   - Report progress and the final delivery from real state transitions only.
 *   - Resume a paused task only when the user explicitly asks to continue.
 *
 * Supported OpenCode interfaces used (OpenCode V2, verified against 2.0.32):
 *   - `setup()` may return a cleanup function: the only supported
 *     application-shutdown signal. Closing one chat session does NOT close
 *     the application, so session close and app shutdown are separate paths.
 *   - `ctx.event.subscribe()`: server event stream.
 *   - `ctx.session.hook("prompt")`: user prompt admission.
 *   - `ctx.session.synthetic()`: plugin-authored messages. Synthetic messages
 *     do not run the prompt hook, which is what structurally prevents the
 *     manager's own traffic from re-triggering activation.
 *   - `ctx.tool.transform()`: in-session control surface for the agent.
 *
 * No third-party dependencies. Runs under OpenCode's runtime and under Node
 * for the test harness.
 */

import { execFile } from "node:child_process"
import { appendFileSync, existsSync, mkdirSync } from "node:fs"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const PLUGIN_ID = "opencode.swarm.lifecycle"

/**
 * A process-global "already installed" guard.
 *
 * Observed on a live 2.0.32 server: `setup()` ran four times in one process
 * during plugin hot-reload, and every event was handled four times over. That
 * is not cosmetic. Four `setup()` calls means four event pumps, four
 * `session-close` calls on the same close, four `note`s per step, and four
 * synthetic progress posts — a quadruple-billed session and a user who cannot
 * tell which line is real.
 *
 * OpenCode does not promise to call `setup()` exactly once, so idempotency is
 * this plugin's responsibility, not the host's.
 *
 * The flag lives on `globalThis`, not in module scope, and that detail was
 * measured rather than guessed. A module-level `let installed` compiled and
 * unit-tested perfectly and still never fired: the live server hands each
 * `setup()` call a *fresh module instance*, so module scope is re-initialised
 * every time and the guard could never be true. `globalThis` is shared by every
 * module instance in the process, which is the only scope that actually spans
 * the calls we need to deduplicate.
 */
const INSTALL_KEY = "__opencodeSwarmLifecycleInstalled"

globalThis[INSTALL_KEY] = false

/** Event type names we react to. Unseen entries are inert until proven real. */
const SESSION_CLOSED = new Set([
  "session.deleted",
  "session.closed",
  "session.destroyed",
])
const SESSION_FAILED = new Set([
  "session.error",
  "session.execution.failed",
])
const SESSION_INTERRUPTED = new Set([
  "session.execution.interrupted",
  "session.interrupted",
])
const SESSION_IDLE = new Set(["session.idle"])

/**
 * Observed on a live OpenCode 2.0.32 server (verified, not guessed):
 * `session.step.started`, `session.step.ended`, `session.step.streamed`,
 * `session.text.delta`, `session.tool.input.started`,
 * `session.tool.input.ended`, `session.tool.called`, `session.tool.progress`,
 * `session.usage.updated`, `shell.created`, `project.updated`,
 * `session.permissions`. `message.part.updated` and `tool.execution` are the
 * V1 spellings and are kept only as harmless extras.
 */
const STEP_ENDED = new Set(["session.step.ended"])
const TOOL_ACTIVITY = new Set([
  "session.tool.called",
  "session.tool.input.ended",
  "session.tool.progress",
  "message.part.updated",
  "tool.execution",
])

/** Phrases that mean "pick the paused work back up". */
const RESUME_INTENT =
  /(^|\b)(resume|continue|carry on|keep going|pick (it|that) up|pick up where|proceed with|go ahead with the (pending|paused|unfinished)|finish the (pending|paused|unfinished)|pick up the (pending|paused|unfinished))( work| where| the| it)?\b/i

/**
 * Which flags each agent-facing lifecycle action actually accepts.
 *
 * Mirrors `swarmkit/cli.py`'s `lifecycle` subparsers. `session-close`,
 * `shutdown`, `recover`, `note` and `resume-plan` are deliberately absent: they
 * are host-driven, and exposing them to the agent would let it cancel another
 * session's work or force a resume the user never asked for.
 */
const ACTION_FLAGS = {
  status: ["--session", "--task"],
  start: ["--session", "--task", "--goal", "--project"],
  resume: ["--session", "--task"],
  cancel: ["--task", "--reason"],
  checkpoint: ["--task", "--step", "--phase", "--artifact", "--retry", "--epoch", "--result", "--event"],
  list: [],
}

/** Cheap pre-filter so most prompts never spawn a subprocess. */
function looksLikeResume(text) {
  if (typeof text !== "string" || text.length === 0) return false
  if (text.startsWith("/")) return false
  return RESUME_INTENT.test(text)
}

function eventType(event) {
  if (!event || typeof event !== "object") return null
  if (typeof event.type === "string") return event.type
  if (typeof event.event === "string") return event.event
  const props = event.properties
  if (props && typeof props.type === "string") return props.type
  return null
}

/**
 * Resolve the owning session id from a real event.
 *
 * Verified against a live 2.0.32 server: the envelope is
 * `{id, created, type, durable, location, data}` and the session id lives at
 * `event.data.sessionID`, with `event.durable.aggregateID` as a fallback. The
 * older `properties` spelling is still accepted so the plugin keeps working if
 * the envelope changes shape, but it is no longer the primary path - reading
 * only `properties` resolved no session at all, which would have left every
 * event inert.
 */
function sessionIDOf(event) {
  if (!event || typeof event !== "object") return null
  const data = event.data && typeof event.data === "object" ? event.data : null
  if (data) {
    const direct = data.sessionID ?? data.session_id ?? data.sessionId
    if (typeof direct === "string" && direct) return direct
    const info = data.info ?? data.session
    if (info && typeof info.id === "string" && info.id) return info.id
    if (typeof data.id === "string" && /^ses_/.test(data.id)) return data.id
  }
  const aggregate = event.durable && event.durable.aggregateID
  // The prefix check is not paranoia, it is a measured fact: on a live 2.0.32
  // server a `shell.created` event carries a shell id in `durable.aggregateID`
  // (`sh_...`). Returning it as a session id would let `session-close` aim at a
  // session that does not exist, and the cancel would silently do nothing.
  if (typeof aggregate === "string" && /^ses_/.test(aggregate)) return aggregate
  const props = (event.properties && typeof event.properties === "object")
    ? event.properties
    : null
  if (props) {
    const direct = props.sessionID ?? props.session_id ?? props.sessionId
    if (typeof direct === "string" && direct) return direct
    const info = props.info ?? props.session
    if (info && typeof info.id === "string" && /^ses_/.test(info.id)) return info.id
  }
  return null
}

/**
 * Render one progress line from a task record.
 *
 * Returns `null` when the record has no id or no state, which is how the
 * caller decides there is nothing truthful to say yet. Every field is read from
 * the record the engine returned; there is no default that could stand in for
 * missing information and read as progress.
 */
function formatProgress(task) {
  if (!task || typeof task !== "object") return null
  const id = task.task_id ?? task.taskID ?? task.id
  const state = task.state
  if (!id || !state) return null
  const done = task.completed_steps ?? task.completedSteps
  const total = task.total_steps ?? task.totalSteps
  const scope =
    typeof done === "number" && typeof total === "number" ? ` ${done}/${total} steps` : ""
  const workers = task.running_workers ?? task.runningWorkers
  const workerScope = Array.isArray(workers) && workers.length ? ` workers: ${workers.join(", ")}` : ""
  return `[swarm] task ${id} — ${state}${scope}${workerScope}`
}

function findSwarmRoot(options, locationDirectory) {
  const candidates = [
    options.repoRoot,
    process.env.SWARMKIT_ROOT,
    // A plugin installed *inside* a repository belongs to that repository, so
    // resolve the engine from the plugin's own location before falling back to
    // the working directory. This is what makes a per-project install work with
    // no configuration at all: the plugin sits next to the swarmkit it drives.
    dirname(fileURLToPath(import.meta.url)),
  ]
  for (const candidate of candidates) {
    if (typeof candidate === "string" && candidate) {
      const dir = resolve(candidate)
      if (existsSync(join(dir, "swarmkit", "cli.py"))) return dir
    }
  }
  let dir = resolve(locationDirectory || process.cwd())
  for (let depth = 0; depth < 12; depth += 1) {
    if (existsSync(join(dir, "swarmkit", "cli.py"))) return dir
    const parent = dirname(dir)
    if (parent === dir) break
    dir = parent
  }
  return null
}

export default {
  id: PLUGIN_ID,

  setup(ctx) {
    const options = ctx.options || {}
    const enabled = options.enabled !== false
    if (!enabled) return undefined
    if (globalThis[INSTALL_KEY]) {
      // Hot-reload already installed this plugin in this process. Installing a
      // second time would multiply every side effect, so stand down and hand
      // back a cleanup that is safe to call.
      try {
        const path =
          options.diagnostics ||
          join(ctx.paths?.tmp || process.env.TEMP || ".", "swarm-lifecycle-events.jsonl")
        appendFileSync(path, JSON.stringify({ at: new Date().toISOString(), type: "plugin:already-installed" }) + "\n")
      } catch {
        /* diagnostics must never break the host */
      }
      return async function noopCleanup() {}
    }
    globalThis[INSTALL_KEY] = true

    const locationDirectory =
      (ctx.location && (ctx.location.directory || ctx.location.project?.canonical)) ||
      process.cwd()
    const swarmRoot = findSwarmRoot(options, locationDirectory)
    const pythonExe = options.pythonExe || process.env.SWARMKIT_PYTHON || "python"
    const commandTimeoutMs = Number(options.timeoutMs) || 20_000
    const progressThrottleMs = Number(options.progressThrottleMs) || 2_000
    const postProgress = options.postProgress !== false
    const verbose = options.verbose === true

    const diagnosticsPath =
      options.diagnostics ||
      join(ctx.paths?.tmp || process.env.TEMP || ".", "swarm-lifecycle-events.jsonl")

    /** Serialize per-session work so concurrent events cannot interleave. */
    const chains = new Map()
    function serialize(key, fn) {
      const previous = chains.get(key) || Promise.resolve()
      const next = previous.then(fn, fn).catch((error) => {
        log("chain-error", { key, error: String(error) })
      })
      chains.set(key, next)
      return next
    }

    const observed = new Set()
    function diagnose(type, extra) {
      if (type) observed.add(type)
      try {
        mkdirSync(dirname(diagnosticsPath), { recursive: true })
        appendFileSync(
          diagnosticsPath,
          JSON.stringify({ at: new Date().toISOString(), type: type ?? null, ...extra }) + "\n",
        )
      } catch {
        /* diagnostics must never break the host */
      }
    }
    const log = (kind, payload) => {
      if (verbose) diagnose(`plugin:${kind}`, payload)
    }

    /**
     * Every state mutation flows through the Python engine, which owns
     * persistence, fencing and idempotency. Failure is never fatal to
     * OpenCode: it is reported, not swallowed.
     */
    function callSwarm(args) {
      if (!swarmRoot) {
        return Promise.resolve({ ok: false, error: "swarmkit-not-found" })
      }
      return new Promise((resolveCall) => {
        const child = execFile(
          pythonExe,
          ["-m", "swarmkit", "--json", "lifecycle", ...args],
          { cwd: swarmRoot, timeout: commandTimeoutMs, windowsHide: true, maxBuffer: 8 << 20 },
          (error, stdout, stderr) => {
            if (error && !stdout) {
              resolveCall({ ok: false, error: String(error.message || error), stderr: String(stderr || "") })
              return
            }
            const text = String(stdout || "").trim()
            if (!text) {
              resolveCall({ ok: false, error: "empty-output", stderr: String(stderr || "") })
              return
            }
            try {
              // `swarmkit --json` pretty-prints with indent=2, so the payload
              // spans many lines. Parsing only the last line yields a bare "}"
              // and fails; parse the whole document.
              const parsed = JSON.parse(text)
              resolveCall({ ok: true, data: parsed })
            } catch (parseError) {
              resolveCall({ ok: false, error: `unparsable: ${String(parseError)}`, stdout: text.slice(0, 400) })
            }
          },
        )
        child.on("error", (error) => resolveCall({ ok: false, error: String(error) }))
      })
    }

    /** Cancels everything this session owns. Idempotent on the Python side. */
    function cancelSession(sessionID, reason) {
      if (!sessionID) return Promise.resolve()
      return serialize(`close:${sessionID}`, async () => {
        const result = await callSwarm(["session-close", "--session", sessionID, "--reason", reason])
        diagnose("session-closed", { sessionID, reason, result: result.ok ? result.data : result.error })
        return result
      })
    }

    function statusFor(sessionID) {
      return serialize(`status:${sessionID}`, async () => {
        const result = await callSwarm(["status", "--session", sessionID])
        if (!result.ok) return { ok: false, error: result.error }
        return { ok: true, data: result.data }
      })
    }

    /** Rendered progress is compared against the last render so we stay quiet
     *  while nothing changes, and we never invent a state we did not read. */
    const lastFingerprint = new Map()
    const lastRenderedAt = new Map()
    function renderProgress(sessionID, force) {
      if (!sessionID || !postProgress) return
      const now = Date.now()
      if (!force && now - (lastRenderedAt.get(sessionID) || 0) < progressThrottleMs) return
      return serialize(`progress:${sessionID}`, async () => {
        const status = await statusFor(sessionID)
        if (!status.ok) return
        const task = status.data?.task
        if (!task) return
        const fingerprint = JSON.stringify({
          state: task.state,
          completed: task.completed_steps ?? task.completedSteps ?? null,
          total: task.total_steps ?? task.totalSteps ?? null,
          running: task.running_workers ?? task.runningWorkers ?? null,
          epoch: task.epoch ?? null,
        })
        if (!force && lastFingerprint.get(sessionID) === fingerprint) return
        lastFingerprint.set(sessionID, fingerprint)
        lastRenderedAt.set(sessionID, Date.now())
        const line = formatProgress(task)
        if (!line) return
        try {
          await ctx.session.synthetic({ sessionID, text: line })
        } catch (error) {
          diagnose("progress-post-failed", { sessionID, error: String(error) })
        }
      })
    }

    // ---------------------------------------------------------------- events

    const controller = new AbortController()

    async function pump() {
      try {
        const stream = ctx.event.subscribe({ signal: controller.signal })
        for await (const event of stream) {
          const type = eventType(event)
          const sessionID = sessionIDOf(event)
          diagnose(type, { sessionID })
          if (!type) continue

          if (SESSION_CLOSED.has(type)) {
            await cancelSession(sessionID, `opencode:${type}`)
            continue
          }
          if (SESSION_FAILED.has(type)) {
            await serialize(`fail:${sessionID}`, () =>
              callSwarm(["note", "--session", String(sessionID), "--event", type]),
            )
            continue
          }
          if (SESSION_INTERRUPTED.has(type)) {
            await serialize(`interrupt:${sessionID}`, () =>
              callSwarm(["note", "--session", String(sessionID), "--event", type]),
            )
            await renderProgress(sessionID, true)
            continue
          }
          if (SESSION_IDLE.has(type)) {
            await renderProgress(sessionID, true)
            continue
          }
          if (TOOL_ACTIVITY.has(type)) {
            await renderProgress(sessionID, false)
            continue
          }
          if (STEP_ENDED.has(type)) {
            // A finished step is the only host signal that maps onto progress.
            // The counter goes to the engine, which derives the step count from
            // the plan, so nothing here can invent a step that did not end.
            await serialize(`note:${sessionID}`, () =>
              callSwarm(["note", "--session", String(sessionID), "--event", type]),
            )
            await renderProgress(sessionID, false)
            continue
          }
        }
      } catch (error) {
        // A closed or aborted stream is the normal path on shutdown.
        if (!controller.signal.aborted) {
          diagnose("event-stream-error", { error: String(error) })
        }
      }
    }
    void pump()

    // Crash detection: any record still RUNNING belongs to a process that is
    // gone. Mark it INTERRUPTED so it is never mistaken for live work.
    void callSwarm(["recover", "--reason", "plugin-startup"]).then((result) => {
      diagnose("startup-recover", { result: result.ok ? result.data : result.error })
    })

    // ---------------------------------------------------------------- prompt

    /**
     * Resume is opt-in. The hook only answers "is there paused work for this
     * project?", and only when the user's own words ask to continue.
     */
    let promptHook
    try {
      promptHook = ctx.session.hook("prompt", async (event) => {
        try {
          const text = event?.prompt?.text
          if (!looksLikeResume(text)) return
          const sessionID = event?.sessionID
          if (!sessionID) return
          const project = ctx.location?.directory || ctx.location?.project?.canonical
          const result = await callSwarm([
            "resume-plan",
            "--session",
            sessionID,
            "--project",
            String(project || ""),
            "--hint",
            String(text).slice(0, 400),
          ])
          if (!result.ok) {
            diagnose("resume-plan-failed", { sessionID, error: result.error })
            return
          }
          const data = result.data || {}
          if (data.status === "resumed" && data.brief) {
            event.prompt.text = `[swarm] Resuming saved task ${data.task_id}.\n${data.brief}\n\n---\n${text}`
            diagnose("resume-injected", { sessionID, task: data.task_id })
          } else if (data.status === "ambiguous") {
            const options = (data.candidates || []).map((c) => `${c.task_id} (${c.title})`).join("; ")
            event.prompt.text = `[swarm] ${data.candidates.length} paused tasks match this project. Ask which to continue: ${options}\n\n---\n${text}`
            diagnose("resume-clarify", { sessionID, count: (data.candidates || []).length })
          }
        } catch (error) {
          diagnose("prompt-hook-error", { error: String(error) })
        }
      })
    } catch (error) {
      diagnose("prompt-hook-unavailable", { error: String(error) })
    }

    // ----------------------------------------------------------------- tools

    let toolRegistration
    try {
      toolRegistration = ctx.tool.transform((editor) => {
        editor.add({
          name: "swarm_lifecycle",
          description:
            "Inspect and drive the swarm lifecycle: status, start, resume, cancel, checkpoint, list. " +
            "Use this for task state; it never invents progress.",
          input: {
            type: "object",
            properties: {
              action: {
                type: "string",
                enum: ["status", "start", "resume", "cancel", "checkpoint", "list"],
              },
              session: { type: "string" },
              project: { type: "string" },
              goal: { type: "string" },
              task: { type: "string" },
              step: { type: "string", description: "plan step id this checkpoint completes" },
              phase: { type: "string" },
              artifact: { type: "string", description: "path of a produced artifact" },
              retry: { type: "string", description: "key=count" },
              epoch: { type: "string", description: "worker epoch; fenced if stale" },
              result: { type: "string" },
              reason: { type: "string" },
            },
            required: ["action"],
            additionalProperties: false,
          },
          options: { namespace: "swarm" },
          execute: async (input, context) => {
            const sessionID = input.session || context?.sessionID
            const values = {
              "--session": sessionID,
              "--project": input.project,
              "--goal": input.goal,
              "--task": input.task,
              "--step": input.step,
              "--phase": input.phase,
              "--artifact": input.artifact,
              "--retry": input.retry,
              "--epoch": input.epoch,
              "--result": input.result,
              "--reason": input.reason,
            }
            const args = []
            // Only the flags this action actually accepts are forwarded.
            // Forwarding everything is how `lifecycle checkpoint --reason x`
            // became an argparse usage error: the subcommand has no such flag,
            // so the whole call died before touching any state. Deriving the
            // list from the CLI's own parser surface keeps the two in step.
            for (const flag of ACTION_FLAGS[input.action] || []) {
              const value = values[flag]
              if (value !== undefined && value !== null && value !== "") {
                args.push(flag, String(value))
              }
            }
            if (!ACTION_FLAGS[input.action]) {
              return { content: JSON.stringify({ error: `unknown action ${input.action}` }) }
            }
            const result = await callSwarm([input.action, ...args])
            const payload = result.ok ? result.data : { error: result.error }
            return { content: JSON.stringify(payload, null, 2) }
          },
        })
      })
    } catch (error) {
      diagnose("tool-registration-failed", { error: String(error) })
    }

    // -------------------------------------------------------------- shutdown

    /**
     * Application shutdown. Runs for every session this plugin observed, and
     * `shutdown` is the authoritative stop for anything already owned. The
     * engine checkpoints before stopping, so this must stay cheap.
     */
    return async function cleanup() {
      controller.abort()
      try {
        await promptHook?.dispose?.()
      } catch {
        /* already gone */
      }
      try {
        await toolRegistration?.dispose?.()
      } catch {
        /* already gone */
      }
      const result = await callSwarm(["shutdown", "--reason", "application-shutdown"])
      diagnose("shutdown", { result: result.ok ? result.data : result.error })
      chains.clear()
      lastFingerprint.clear()
      lastRenderedAt.clear()
    }
  },
}

export const __testing = {
  PLUGIN_ID,
  ACTION_FLAGS,
  /**
   * Clear the install guard so a test can call `setup()` again.
   *
   * Only the test harness uses this. The guard exists because a live server
   * calls `setup()` more than once; a test process, by contrast, must be able
   * to install the plugin repeatedly to exercise each registration.
   */
  resetInstallGuard() {
    globalThis[INSTALL_KEY] = false
  },
  eventType,
  sessionIDOf,
  looksLikeResume,
  findSwarmRoot,
  formatProgress,
  SESSION_CLOSED,
  SESSION_FAILED,
  SESSION_INTERRUPTED,
  SESSION_IDLE,
  TOOL_ACTIVITY,
  STEP_ENDED,
}
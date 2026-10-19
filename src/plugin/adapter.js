/**
 * Runtime adapter: the only place that talks to OpenCode.
 *
 * Every capability is probed, not assumed. `capabilities` records what was
 * actually found on the live plugin context, so the manager can report an
 * unavailable feature honestly instead of failing later or pretending success.
 */

import { CancelledError, CapabilityError, TimeoutError } from "../core/errors.js";
import { systemClock } from "../core/clock.js";
import { silentLogger } from "../core/logger.js";
import { parseRef } from "../route/catalog.js";
import { readUsage, textOfMessage } from "./telemetry.js";

export function createRuntime(ctx, { logger = silentLogger, clock = systemClock() } = {}) {
  const session = ctx?.session ?? null;
  const has = (target, key) => typeof target?.[key] === "function";

  const capabilities = Object.freeze({
    version: ctx?.app?.version ?? null,
    models: has(ctx?.model, "list"),
    agents: has(ctx?.agent, "list"),
    storage: has(ctx?.storage, "get") && has(ctx?.storage, "set"),
    scan: has(ctx?.storage, "scan"),
    tools: has(ctx?.tool, "transform"),
    toolHooks: has(ctx?.tool, "hook"),
    sessionHooks: has(session, "hook"),
    createSession: has(session, "create"),
    prompt: has(session, "prompt"),
    wait: has(session, "wait"),
    interrupt: has(session, "interrupt"),
    context: has(session, "context"),
    sessionGet: has(session, "get"),
    generate: has(ctx?.generate, "text"),
  });

  const missing = Object.entries(capabilities)
    .filter(([, value]) => value === false)
    .map(([key]) => key);

  const runtime = {
    capabilities,
    missing,
    canDelegate: capabilities.createSession && capabilities.prompt && capabilities.wait && capabilities.context,
    raw: ctx,
    logger,

    /** Live catalog of usable models. Throws only if the domain is absent. */
    async models() {
      if (!capabilities.models) throw new CapabilityError("model.list");
      const list = await ctx.model.list();
      return Array.isArray(list) ? list : [];
    },

    async agents() {
      if (!capabilities.agents) return [];
      const list = await ctx.agent.list();
      return Array.isArray(list) ? list : [];
    },

    async createSession(input = {}) {
      if (!capabilities.createSession) throw new CapabilityError("session.create");
      const payload = {};
      if (input.title) payload.title = input.title;
      if (input.agent) payload.agent = input.agent;
      if (input.model) payload.model = typeof input.model === "string" ? parseRef(input.model) : input.model;
      if (input.parentID) payload.parentID = input.parentID;
      if (input.metadata) payload.metadata = input.metadata;
      if (input.location) payload.location = input.location;
      const created = await session.create(payload);
      const sessionID = created?.id ?? created?.sessionID ?? null;
      if (!sessionID) throw new CapabilityError("session.create (no session id returned)");
      return { ...created, sessionID };
    },

    async prompt(sessionID, text, extra = {}) {
      if (!capabilities.prompt) throw new CapabilityError("session.prompt");
      return session.prompt({ sessionID, text, ...extra });
    },

    async wait(sessionID) {
      if (!capabilities.wait) throw new CapabilityError("session.wait");
      return session.wait({ sessionID });
    },

    async interrupt(sessionID) {
      if (!capabilities.interrupt) return { interrupted: false };
      try {
        return await session.interrupt({ sessionID });
      } catch (error) {
        logger.debug("interrupt failed", { sessionID, error: String(error?.message ?? error) });
        return { interrupted: false };
      }
    },

    async messages(sessionID) {
      if (!capabilities.context) return [];
      const list = await session.context({ sessionID });
      return Array.isArray(list) ? list : [];
    },

    async usage(sessionID, messages = null) {
      return readUsage({ session, sessionID, messages });
    },

    async removeSession(sessionID) {
      if (!has(session, "remove")) return false;
      try {
        await session.remove({ sessionID });
        return true;
      } catch {
        return false;
      }
    },

    /**
     * Runs one worker to completion and returns its final text plus measured
     * usage. A timeout interrupts the session and raises `TimeoutError`; the
     * caller (executor) decides whether that is retryable.
     */
    async delegate({
      agent = "swarm",
      prompt,
      model = null,
      signal = null,
      timeoutMs = 300000,
      label = "step",
      parentSessionID = null,
      keepSession = true,
      metadata = null,
    } = {}) {
      if (!runtime.canDelegate) throw new CapabilityError("session delegation");
      const step = { agent, prompt, model, timeoutMs, label, parentSessionID, keepSession, metadata };
      const startedAt = clock.now();
      let created = null;

      try {
        created = await runtime.createSession({
          title: `swarm:${label}`.slice(0, 120),
          agent,
          model,
          parentID: parentSessionID,
          metadata: { swarm: { internal: true, label, ...(metadata ?? {}) } },
        });
      } catch (error) {
        throw error instanceof CapabilityError ? error : new CapabilityError("session.create", { cause: error });
      }

      const sessionID = created.sessionID;
      const controller = new AbortController();
      const relay = () => controller.abort(signal?.reason);
      if (signal) {
        if (signal.aborted) controller.abort(signal.reason);
        else signal.addEventListener("abort", relay, { once: true });
      }

      try {
        await runtime.prompt(sessionID, prompt);
        await raceWait({ runtime, sessionID, timeoutMs, signal: controller.signal, clock, label });

        const messages = await runtime.messages(sessionID);
        const text = finalText(messages);
        const usage = await runtime.usage(sessionID, messages);
        const info = capabilities.sessionGet ? await safeGet(session, sessionID) : null;
        const error = firstError(messages);

        return {
          sessionID,
          text,
          usage,
          error,
          model: info?.model ?? created.model ?? (model ? parseRef(model) : null),
          agent,
          label,
          durationMs: clock.now() - startedAt,
          outcome: error ? "failed" : info?.outcome ?? "succeeded",
        };
      } catch (error) {
        await runtime.interrupt(sessionID);
        if (error instanceof TimeoutError || error instanceof CapabilityError) {
          if (!keepSession) await runtime.removeSession(sessionID);
          throw error;
        }
        throw Object.assign(new Error(error?.message ?? "delegation failed"), {
          code: error?.code ?? "delegation_failed",
          sessionID,
        });
      } finally {
        if (signal) signal.removeEventListener("abort", relay);
        if (!keepSession) await runtime.removeSession(sessionID);
      }
    },

    async storage() {
      return capabilities.storage ? ctx.storage : null;
    },
  };

  return runtime;
}

/** Waits for the session, bounded by `timeoutMs` and the abort signal. */
async function raceWait({ runtime, sessionID, timeoutMs, signal, clock, label }) {
  // Own the timeout timer so it is released the moment the race settles, even
  // when the worker finishes first (otherwise every delegation leaves a live
  // timer behind for the full timeout window).
  const timer = new AbortController();
  const waiters = [runtime.wait(sessionID).then(() => ({ kind: "done" }))];

  if (Number.isFinite(timeoutMs) && timeoutMs > 0) {
    waiters.push(
      clock
        .delay(timeoutMs, { signal: timer.signal })
        .then(() => ({ kind: "timeout" }))
        // Cancelled timer: never settles and never surfaces as an unhandled
        // rejection, because the race has already been decided.
        .catch(() => new Promise(() => {})),
    );
  }

  const onAbort = new Promise((resolve) => {
    if (!signal) return;
    if (signal.aborted) return resolve({ kind: "aborted" });
    signal.addEventListener("abort", () => resolve({ kind: "aborted" }), { once: true });
  });
  waiters.push(onAbort);

  const result = await Promise.race(waiters);
  timer.abort();
  if (result.kind === "timeout") {
    throw new TimeoutError(`worker "${label}" exceeded ${timeoutMs}ms`, { timeoutMs, label });
  }
  if (result.kind === "aborted") {
    throw new CancelledError(`worker "${label}" was cancelled`);
  }
  return result;
}

async function safeGet(session, sessionID) {
  try {
    return await session.get({ sessionID });
  } catch {
    return null;
  }
}

/** The final assistant text, or the last non-empty assistant text. */
export function finalText(messages = []) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.type !== "assistant") continue;
    const text = textOfMessage(message).trim();
    if (text) return text;
  }
  return "";
}

function firstError(messages = []) {
  for (const message of messages) {
    if (message?.type === "assistant" && message.error) return message.error;
  }
  return null;
}

export default createRuntime;

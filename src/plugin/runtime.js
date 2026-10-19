/**
 * Plugin entry point.
 *
 * `setup(ctx)` wires the swarm into a live OpenCode server and returns a
 * cleanup function. Everything it registers is disposed on cleanup, so a
 * hot-reload re-entrant `setup()` replaces the previous instance instead of
 * stacking hooks — the previous registrations are tracked on `globalThis`,
 * because each reload gets a fresh module instance.
 */

import { systemClock } from "../core/clock.js";
import { createLogger, silentLogger } from "../core/logger.js";
import { createManager } from "../engine.js";
import { createFsAdapter, createMemoryAdapter, createRuntimeAdapter, createStore } from "../memory/store.js";
import { shouldActivate, managerDirective, DIRECTIVE_MARKER, MANAGER_AGENT_IDS } from "./activation.js";
import { createRuntime } from "./adapter.js";
import { createToolState, registerTools } from "./tools.js";

export const PLUGIN_ID = "opencode-swarm";

/** Survives module reloads, so a re-entrant `setup()` can undo the last one. */
const GUARD = Symbol.for("opencode-swarm.guard");

function guardState() {
  if (!globalThis[GUARD] || typeof globalThis[GUARD] !== "object") {
    globalThis[GUARD] = { instances: 0, cleanups: [], active: null };
  }
  return globalThis[GUARD];
}

export function createPlugin({ id = PLUGIN_ID, config = {} } = {}) {
  return {
    id,
    async setup(ctx) {
      return bootstrap(ctx, { id, config });
    },
  };
}

export async function bootstrap(ctx, { id = PLUGIN_ID, config = {} } = {}) {
  const guard = guardState();
  guard.instances += 1;

  // A re-entrant setup (hot reload) must replace, not duplicate.
  const previous = guard.cleanups.splice(0);
  for (const dispose of previous) {
    try {
      await dispose();
    } catch {
      // a stale registration is already gone
    }
  }

  const clock = systemClock();
  const logger = createLogger({
    level: config.logLevel ?? "warn",
    scope: id,
  });

  const runtime = createRuntime(ctx, { logger: logger.child("runtime"), clock });
  const { store, persistence } = createStore_(ctx, { logger, config });

  const manager = createManager({
    runtime,
    store,
    clock,
    logger: logger.child("manager"),
    policy: config.policy ?? {},
  });

  const state = createToolState();
  const cleanups = [];
  const registry = {
    id,
    instance: guard.instances,
    version: ctx?.app?.version ?? null,
    registrations: { tools: [], hooks: [] },
  };
  guard.active = registry;

  /** What the prompt hook learned, waiting for the matching context call. */
  const pending = new Map();

  // ---- tools ---------------------------------------------------------------
  if (typeof ctx?.tool?.transform === "function") {
    try {
      const registration = await ctx.tool.transform((editor) => {
        registerTools(editor, { manager, runtime, logger: logger.child("tools"), state });
      });
      cleanups.push(() => registration?.dispose?.());
      registry.registrations.tools = [
        "swarm_plan",
        "swarm_delegate",
        "swarm_verify",
        "swarm_status",
        "swarm_memory",
        "swarm_improve",
        "swarm_run",
      ];
    } catch (error) {
      logger.error("tool registration failed", { error: message(error) });
    }
  } else {
    logger.warn("tool.transform is unavailable; the manager has no swarm tools");
  }

  // ---- tool usage: real telemetry, observed not assumed --------------------
  if (typeof ctx?.tool?.hook === "function") {
    try {
      const registration = await ctx.tool.hook("execute.after", (event) => {
        state.toolCallsObserved += 1;
        if (event?.status === "error") state.toolFailuresObserved += 1;
        if (typeof event?.tool === "string" && event.tool.startsWith("swarm_")) {
          logger.debug("swarm tool finished", { tool: event.tool, status: event.status });
        }
      });
      cleanups.push(() => registration?.dispose?.());
      registry.registrations.hooks.push("tool.execute.after");
    } catch (error) {
      logger.warn("tool hook unavailable", { error: message(error) });
    }
  }

  // ---- prompt: decide, deterministically, whether the manager owns this ----
  if (typeof ctx?.session?.hook === "function") {
    try {
      const registration = await ctx.session.hook("prompt", (event) => {
        const text = event?.prompt?.text ?? "";
        const decision = shouldActivate({ text, metadata: event?.metadata ?? null });
        pending.set(event.sessionID, {
          text,
          decision,
          at: clock.now(),
          injected: false,
        });

        if (decision.activate) {
          event.metadata = {
            ...(event.metadata ?? {}),
            swarm: { managed: true, reason: decision.reason, task: text.slice(0, 400) },
          };
          logger.debug("manager activated", { sessionID: event.sessionID, reason: decision.reason });
        }
      });
      cleanups.push(() => registration?.dispose?.());
      registry.registrations.hooks.push("session.prompt");
    } catch (error) {
      logger.warn("session prompt hook unavailable", { error: message(error) });
    }

    // ---- context: hand the manager its contract, once per user turn --------
    try {
      const registration = await ctx.session.hook("context", (event) => {
        injectDirection({ event, pending, logger });
      });
      cleanups.push(() => registration?.dispose?.());
      registry.registrations.hooks.push("session.context");
    } catch (error) {
      logger.warn("session context hook unavailable", { error: message(error) });
    }

    // ---- retry: only override when a bound was explicitly configured -------
    const retryBound = retryBoundFor(manager, config);
    if (Number.isFinite(retryBound)) {
      try {
        const registration = await ctx.session.hook("retry", (event) => {
          if (event.attempt > retryBound) event.decision = { retry: false };
        });
        cleanups.push(() => registration?.dispose?.());
        registry.registrations.hooks.push("session.retry");
      } catch (error) {
        logger.warn("session retry hook unavailable", { error: message(error) });
      }
    }
  } else {
    logger.warn("session.hook is unavailable; prompts cannot be routed to the manager");
  }

  await manager.bootstrap();

  logger.info("swarm ready", {
    instance: registry.instance,
    persistence,
    missing: runtime.missing,
  });

  const cleanup = async () => {
    for (const dispose of cleanups.splice(0)) {
      try {
        await dispose();
      } catch {
        // already disposed
      }
    }
    pending.clear();
    if (guard.active === registry) guard.active = null;
    const index = guard.cleanups.indexOf(cleanup);
    if (index >= 0) guard.cleanups.splice(index, 1);
  };

  guard.cleanups.push(cleanup);
  return cleanup;
}

/* ------------------------------------------------------------------ helpers */

function createStore_(ctx, { logger, config }) {
  const adapterOption = config.store;
  if (adapterOption && typeof adapterOption.get === "function") {
    return { store: createStore({ adapter: adapterOption, logger }), persistence: "injected" };
  }
  if (typeof ctx?.storage?.get === "function" && typeof ctx.storage.set === "function") {
    return {
      store: createStore({ adapter: createRuntimeAdapter(ctx.storage), logger }),
      persistence: "opencode.storage",
    };
  }
  if (config.dataDir) {
    return { store: createStore({ adapter: createFsAdapter({ dir: config.dataDir }), logger }), persistence: "filesystem" };
  }
  logger.warn("no durable storage available; task memory is volatile for this process");
  return { store: createStore({ adapter: createMemoryAdapter(), logger }), persistence: "volatile" };
}

/**
 * Appends the manager contract to the system prompt of a manager session.
 *
 * Guarded on every axis: only for a manager session, only once per user turn,
 * only when the marker is absent, and never at the cost of the user's request —
 * any failure is logged and the request continues unchanged.
 */
function injectDirection({ event, pending, logger }) {
  try {
    if (!event || !Array.isArray(event.system)) return;
    if (!MANAGER_AGENT_IDS.includes(event.agent)) return;

    const entry = pending.get(event.sessionID);
    if (!entry || !entry.decision?.activate) return;
    if (entry.injected) return;
    if (containsMarker(event.system)) {
      entry.injected = true;
      return;
    }

    const directive = managerDirective({ task: entry.text });
    const shape = event.system.length ? { ...event.system[0], text: directive } : { type: "text", text: directive };
    event.system.push(shape);
    entry.injected = true;
    logger.debug("manager contract injected", { sessionID: event.sessionID });
  } catch (error) {
    logger.warn("could not inject the manager contract", { error: message(error) });
  }
}

function containsMarker(system) {
  for (const part of system) {
    if (typeof part?.text === "string" && part.text.includes(DIRECTIVE_MARKER)) return true;
  }
  return false;
}

function retryBoundFor(manager, config) {
  if (Number.isFinite(config.maxRetries)) return config.maxRetries;
  const override = manager.tuner?.overrides?.()?.retry?.maxRetries;
  return Number.isFinite(override) ? override : null;
}

function message(error) {
  return String(error?.message ?? error);
}

export { silentLogger };
export default createPlugin;

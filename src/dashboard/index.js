/**
 * Dashboard façade: event bus + derived state + local server.
 *
 * It exists so the rest of the plugin can treat the dashboard as one optional
 * observer with a single rule: reporting must never affect the work. Every
 * entry point is wrapped, nothing is awaited by the caller, and a failure here
 * leaves the swarm running exactly as it would without a dashboard.
 */

import { createEventBus } from "./bus.js";
import { createDashboardState } from "./state.js";
import { EVENT } from "./events.js";
import { BIND_HOST, createToken, startDashboardServer } from "./server.js";

const DEFAULT_CONFIG = Object.freeze({ enabled: false, port: 0 });

/**
 * Explicit opt-in only: anything other than `enabled: true` leaves the
 * dashboard off. `host` is intentionally not configurable.
 */
export function normalizeDashboardConfig(raw = {}) {
  const source = raw && typeof raw === "object" ? raw : {};
  const port = Number.isFinite(source.port) && source.port >= 0 && source.port <= 65535 ? Math.floor(source.port) : DEFAULT_CONFIG.port;
  return {
    enabled: source.enabled === true,
    port,
    ignored: Object.keys(source).filter((key) => !["enabled", "port"].includes(key)),
  };
}

export function createDashboard({ config = {}, logger = null, clock = null, token = null } = {}) {
  const settings = normalizeDashboardConfig(config);
  const now = () => (typeof clock?.now === "function" ? clock.now() : Date.now());
  const bus = createEventBus({ now });
  const state = createDashboardState({ now });

  const capabilityToken = token ?? createToken();
  let server = null;
  let starting = null;
  let stopped = false;
  let error = null;
  let startedAt = null;

  function publish(event) {
    try {
      const type = typeof event?.type === "string" ? event.type : "note";
      const { type: _drop, ...data } = event ?? {};
      state.apply({ ...data, type });
      bus.publish(type, data);
    } catch (cause) {
      // A rendering problem is not a task problem.
      logger?.debug?.("dashboard event dropped", { error: String(cause?.message ?? cause) });
    }
  }

  async function start() {
    if (!settings.enabled || stopped) return info();
    if (server) return info();
    if (starting) return starting;

    starting = (async () => {
      try {
        server = await startDashboardServer({
          bus,
          snapshot: () => state.snapshot(),
          token: capabilityToken,
          port: settings.port,
          logger,
        });
        startedAt = now();
        error = null;
        publish({ type: EVENT.DASHBOARD, state: "started", port: server.port });
        logger?.info?.("dashboard listening", { host: BIND_HOST, port: server.port });
      } catch (cause) {
        error = String(cause?.message ?? cause);
        logger?.warn?.(`dashboard unavailable: ${error}`);
      } finally {
        starting = null;
      }
      return info();
    })();

    return starting;
  }

  async function stop() {
    stopped = true;
    if (starting) await starting.catch(() => {});
    if (server) {
      const closing = server;
      server = null;
      try {
        publish({ type: EVENT.DASHBOARD, state: "stopped" });
      } catch {
        // ignore
      }
      await closing.close().catch(() => {});
    }
    bus.clear();
  }

  function info() {
    if (!settings.enabled) {
      return {
        enabled: false,
        running: false,
        host: BIND_HOST,
        hint: 'disabled by default; opt in with { "dashboard": { "enabled": true } } in the plugin configuration',
      };
    }
    return {
      enabled: true,
      running: Boolean(server),
      host: BIND_HOST,
      port: server?.port ?? null,
      url: server?.url ?? null,
      clients: server?.clients?.() ?? 0,
      events: bus.stats(),
      since: startedAt,
      error,
      config: { port: settings.port, ignored: settings.ignored },
    };
  }

  return {
    get enabled() {
      return settings.enabled;
    },
    settings,
    bus,
    state,
    start,
    stop,
    info,
    snapshot() {
      return { ...state.snapshot(), events: bus.tail(40), dashboard: { enabled: settings.enabled, running: Boolean(server) } };
    },
    /** Engine lifecycle events (`createManager({ onEvent })`). */
    onEvent(event) {
      publish(event);
    },
    /** Plugin-level observations: real events, never inferred ones. */
    onPrompt({ activate = false, reason = null } = {}) {
      publish({ type: EVENT.ACTIVATION, activate: activate === true, reason });
    },
    onTool({ tool = null, status = null } = {}) {
      if (typeof tool !== "string" || !tool.startsWith("swarm_")) return;
      publish({ type: EVENT.TOOL, tool, status });
    },
  };
}

export { EVENT } from "./events.js";
export { createEventBus } from "./bus.js";
export { createDashboardState, agentForRole } from "./state.js";
export { dashboardPage } from "./page.js";
export { startDashboardServer, createToken, tokenMatches, BIND_HOST } from "./server.js";

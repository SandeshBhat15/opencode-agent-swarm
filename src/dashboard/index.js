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
import { autoOpenState, openDashboardOnce } from "./autoopen.js";
import { openInBrowser } from "./open.js";

const DEFAULT_CONFIG = Object.freeze({ enabled: true, autoOpen: true, port: 0 });

/**
 * Engine events that mean real work is under way. Notice-only events
 * (`dashboard`, `activation`, `tool`) are handled by the plugin-level hooks,
 * which call `engage()` themselves.
 */
const ENGAGEMENT_EVENTS = Object.freeze(new Set([
  "task.start",
  "classified",
  "planned",
  "step.start",
  "delegate",
  "verify",
  "deliver",
]));

/** Documented keys. `open` exists so tests can inject an opener instead of a browser. */
export const DASHBOARD_KEYS = Object.freeze(["enabled", "autoOpen", "port", "open"]);

/**
 * The dashboard is on unless it is explicitly turned off. `enabled: false`
 * stops the server entirely.
 *
 * `autoOpen` is a three-way switch:
 * - `true` (default) opens the tab once the swarm actually engages — the first
 *   activated prompt, the first `swarm_*` tool call or the first engine event.
 * - `"startup"` opens it as soon as the server is listening.
 * - `false` keeps the server but never opens a browser.
 *
 * `host` is intentionally not configurable.
 */
export function normalizeDashboardConfig(raw = {}) {
  const source = raw && typeof raw === "object" ? raw : {};
  const port = Number.isFinite(source.port) && source.port >= 0 && source.port <= 65535 ? Math.floor(source.port) : DEFAULT_CONFIG.port;
  const autoOpen = source.autoOpen === false ? false : source.autoOpen === "startup" ? "startup" : true;
  return {
    enabled: source.enabled !== false,
    autoOpen,
    port,
    ignored: Object.keys(source).filter((key) => !DASHBOARD_KEYS.includes(key)),
  };
}

/** Human-readable form of the three-way switch, for `swarm_status`. */
function whenOf(autoOpen) {
  if (autoOpen === "startup") return "startup";
  return autoOpen ? "engaged" : "never";
}

export function createDashboard({ config = {}, logger = null, clock = null, token = null, open = null } = {}) {
  const settings = normalizeDashboardConfig(config);
  const source = config && typeof config === "object" ? config : {};
  // An injected opener is a test seam, exactly like an injected store.
  const opener = typeof open === "function" ? open : typeof source.open === "function" ? source.open : openInBrowser;
  const now = () => (typeof clock?.now === "function" ? clock.now() : Date.now());
  const bus = createEventBus({ now });
  const state = createDashboardState({ now });

  const capabilityToken = token ?? createToken();
  let server = null;
  let starting = null;
  let opening = null;
  let stopped = false;
  let error = null;
  let startedAt = null;
  let pendingTrigger = null;

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

  /**
   * Opening the browser is scheduled, never awaited. `start()` resolves as soon
   * as the socket is listening, so a missing or slow browser can neither delay
   * plugin initialisation nor fail it. The work itself is guarded by the
   * process-wide claim in `autoopen.js`, which is what makes a second session,
   * a reload or a concurrent setup a no-op instead of a second tab.
   */
  function scheduleOpen(trigger = "startup") {
    if (!settings.autoOpen || stopped) return null;
    if (opening) return opening;

    opening = new Promise((resolve) => {
      const begin = () => {
        // Shutdown may have landed between scheduling and running.
        if (stopped || !server) {
          resolve({ ...autoOpenState(), skipped: "stopped" });
          return;
        }
        const target = typeof server.launchUrl === "function" ? server.launchUrl() : null;
        if (!target) {
          resolve({ ...autoOpenState(), skipped: "no_launch_url" });
          return;
        }
        openDashboardOnce({ url: target, logger, open: opener, now, trigger }).then(resolve, (cause) => {
          logger?.debug?.("dashboard auto-open failed", { error: String(cause?.message ?? cause) });
          resolve({ ...autoOpenState(), skipped: "error" });
        });
      };

      if (typeof setImmediate === "function") {
        const handle = setImmediate(begin);
        handle.unref?.();
      } else {
        const handle = setTimeout(begin, 0);
        handle.unref?.();
      }
    });

    return opening;
  }

  /**
   * The swarm engaged with real work. This is the default moment to open the
   * tab: opening at startup means a tab appears even when the dashboard has
   * nothing to show. `startup` mode ignores this and opens earlier.
   */
  function engage(trigger) {
    if (settings.autoOpen !== true || stopped) return null;
    if (!server) {
      // Engagement can arrive while the socket is still binding; remember it.
      pendingTrigger = pendingTrigger ?? String(trigger);
      return null;
    }
    return scheduleOpen(trigger);
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
        if (settings.autoOpen === "startup") scheduleOpen("startup");
        else if (pendingTrigger) scheduleOpen(pendingTrigger);
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
        hint: 'disabled by configuration; re-enable it with { "dashboard": { "enabled": true } }',
        autoOpen: { enabled: false, when: "never", ...autoOpenState() },
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
      // What actually happened to the browser, as observed — never assumed.
      autoOpen: { enabled: settings.autoOpen, when: whenOf(settings.autoOpen), ...autoOpenState() },
      config: { port: settings.port, autoOpen: settings.autoOpen, ignored: settings.ignored },
    };
  }

  return {
    get enabled() {
      return settings.enabled;
    },
    get autoOpen() {
      return settings.autoOpen;
    },
    settings,
    bus,
    state,
    start,
    stop,
    info,
    /** Resolves once the one-time browser attempt has finished (or was skipped). */
    opened() {
      return opening ?? Promise.resolve(autoOpenState());
    },
    snapshot() {
      return { ...state.snapshot(), events: bus.tail(40), dashboard: { enabled: settings.enabled, running: Boolean(server) } };
    },
    /** Engine lifecycle events (`createManager({ onEvent })`). */
    onEvent(event) {
      publish(event);
      const type = typeof event?.type === "string" ? event.type : null;
      if (type && ENGAGEMENT_EVENTS.has(type)) engage(type);
    },
    /** Plugin-level observations: real events, never inferred ones. */
    onPrompt({ activate = false, reason = null } = {}) {
      publish({ type: EVENT.ACTIVATION, activate: activate === true, reason });
      if (activate === true) engage("activation");
    },
    onTool({ tool = null, status = null } = {}) {
      if (typeof tool !== "string" || !tool.startsWith("swarm_")) return;
      publish({ type: EVENT.TOOL, tool, status });
      engage("tool");
    },
    /** The swarm engaged with real work; opens the tab in the default mode. */
    engage,
  };
}

export { EVENT } from "./events.js";
export { createEventBus } from "./bus.js";
export { createDashboardState, agentForRole } from "./state.js";
export { dashboardPage } from "./page.js";
export { startDashboardServer, createToken, tokenMatches, BIND_HOST } from "./server.js";
export { createHandshake } from "./handshake.js";
export { openInBrowser, isOpenableUrl, browserCommand } from "./open.js";
export { autoOpenState, openDashboardOnce, resetAutoOpenState } from "./autoopen.js";

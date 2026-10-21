/**
 * Local, read-only dashboard server.
 *
 * Deliberate constraints, each enforced here:
 *  - binds to 127.0.0.1 only; there is no option to bind elsewhere;
 *  - ephemeral port by default; the caller may pin one explicitly;
 *  - every data route needs the per-process capability token, compared in
 *    constant time and never written to disk or a log;
 *  - a single-use, short-lived launch ticket can be exchanged for the page
 *    document only, which is how the browser is opened without the token ever
 *    reaching a command line;
 *  - `Host` and `Origin` are validated, so a DNS-rebinding page cannot read the
 *    endpoint through the user's browser;
 *  - GET only, no CORS headers, `no-store`, and a CSP that forbids everything
 *    except the inline page itself;
 *  - bounded subscribers, bounded queue, and a heartbeat instead of polling.
 */

import http from "node:http";
import crypto from "node:crypto";
import { dashboardPage } from "./page.js";
import { sanitizeEvent } from "./events.js";
import { createHandshake } from "./handshake.js";

export const BIND_HOST = "127.0.0.1";
export const DEFAULT_PORT = 0;
export const MAX_CLIENTS = 8;
export const HEARTBEAT_MS = 15000;
export const MAX_BUFFERED_BYTES = 1024 * 1024;

const CSP = [
  "default-src 'none'",
  "style-src 'unsafe-inline'",
  "script-src 'unsafe-inline'",
  "connect-src 'self'",
  "img-src data:",
  "base-uri 'none'",
  "form-action 'none'",
  "frame-ancestors 'none'",
].join("; ");

export function createToken() {
  return crypto.randomBytes(32).toString("base64url");
}

function digest(value) {
  return crypto.createHash("sha256").update(String(value), "utf8").digest();
}

/** Constant-time token comparison that never throws on length mismatch. */
export function tokenMatches(expected, presented) {
  if (typeof expected !== "string" || expected.length === 0) return false;
  if (typeof presented !== "string" || presented.length === 0) return false;
  return crypto.timingSafeEqual(digest(expected), digest(presented));
}

/**
 * @param {object} options
 * @param {import("./bus.js").ReturnType<typeof import("./bus.js").createEventBus>} options.bus
 * @param {() => object} options.snapshot  current derived state
 * @param {string} options.token
 */
export async function startDashboardServer({
  bus,
  snapshot,
  token,
  port = DEFAULT_PORT,
  maxClients = MAX_CLIENTS,
  heartbeatMs = HEARTBEAT_MS,
  logger = null,
}) {
  if (!bus) throw new TypeError("startDashboardServer requires a bus");
  if (typeof snapshot !== "function") throw new TypeError("startDashboardServer requires a snapshot function");
  if (typeof token !== "string" || token.length < 16) throw new TypeError("startDashboardServer requires a capability token");

  const clients = new Set();
  const handshake = createHandshake();
  let boundPort = null;

  /**
   * The derived state is redacted again at the wire, not only where it is
   * built: proving that no field can carry prompt text must not depend on a
   * future edit to `state.js` remembering the rule.
   */
  function wireState() {
    return sanitizeEvent(snapshot());
  }

  const server = http.createServer((req, res) => {
    try {
      handle(req, res);
    } catch (error) {
      // A dashboard fault is contained: answer, never crash the host process.
      logger?.warn?.("dashboard request failed", { error: String(error?.message ?? error) });
      if (!res.headersSent) send(res, 500, { error: "internal" });
      else res.destroy();
    }
  });

  server.on("clientError", (_error, socket) => {
    socket.destroy();
  });

  function allowedHosts() {
    return new Set([`127.0.0.1:${boundPort}`, `localhost:${boundPort}`]);
  }

  function hostAllowed(req) {
    const host = req.headers.host;
    if (typeof host !== "string" || host.length === 0) return false;
    return allowedHosts().has(host.toLowerCase());
  }

  function originAllowed(req) {
    const origin = req.headers.origin;
    if (origin === undefined) return true; // same-origin navigation and curl send none
    if (typeof origin !== "string") return false;
    const allowed = new Set([`http://127.0.0.1:${boundPort}`, `http://localhost:${boundPort}`]);
    return allowed.has(origin.toLowerCase());
  }

  function presentedToken(req, url) {
    const header = req.headers.authorization;
    if (typeof header === "string" && /^Bearer\s+/i.test(header)) return header.replace(/^Bearer\s+/i, "").trim();
    return url.searchParams.get("token") ?? "";
  }

  function handle(req, res) {
    const url = new URL(req.url ?? "/", `http://127.0.0.1:${boundPort}`);

    // Reject anything a rebinding attacker could drive: wrong Host or a
    // cross-origin browser context.
    if (!hostAllowed(req)) return send(res, 403, { error: "forbidden_host" });
    if (!originAllowed(req)) return send(res, 403, { error: "forbidden_origin" });
    if (req.method !== "GET" && req.method !== "HEAD") {
      res.setHeader("Allow", "GET, HEAD");
      return send(res, 405, { error: "method_not_allowed" });
    }

    if (url.pathname === "/healthz") {
      // Liveness only: no state, no token. Useful for the tests and for `curl`.
      return send(res, 200, { ok: true });
    }

    if (!tokenMatches(token, presentedToken(req, url))) {
      // A launch ticket is the only other credential, it is single-use and
      // short-lived, and it is accepted for the page document alone. It is
      // exchanged here for the token, which is embedded in the body rather
      // than in the address bar.
      if (url.pathname === "/" && handshake.redeem(url.searchParams.get("ticket"))) {
        return sendPage(req, res, dashboardPage(token));
      }
      res.setHeader("WWW-Authenticate", 'Bearer realm="swarm-dashboard"');
      return send(res, 401, { error: "unauthorized" });
    }

    if (url.pathname === "/") {
      return sendPage(req, res, dashboardPage());
    }

    if (url.pathname === "/state") {
      return send(res, 200, wireState());
    }

    if (url.pathname === "/events") {
      return stream(req, res);
    }

    return send(res, 404, { error: "not_found" });
  }

  function sendPage(req, res, body) {
    res.writeHead(200, {
      "content-type": "text/html; charset=utf-8",
      "content-length": Buffer.byteLength(body),
      "content-security-policy": CSP,
      "x-content-type-options": "nosniff",
      "referrer-policy": "no-referrer",
      "cache-control": "no-store",
    });
    return res.end(req.method === "HEAD" ? undefined : body);
  }

  function stream(req, res) {
    if (clients.size >= maxClients) return send(res, 503, { error: "too_many_clients" });

    res.writeHead(200, {
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-store",
      "connection": "keep-alive",
      "x-accel-buffering": "no",
      "x-content-type-options": "nosniff",
      "referrer-policy": "no-referrer",
      "content-security-policy": CSP,
    });

    const write = (chunk) => {
      try {
        if (res.writableLength > MAX_BUFFERED_BYTES) {
          // A client that cannot keep up is dropped instead of buffered.
          cleanup();
          res.destroy();
          return false;
        }
        res.write(chunk);
        return true;
      } catch {
        cleanup();
        return false;
      }
    };

    const subscription = bus.subscribe((event) => {
      write(`event: swarm\ndata: ${JSON.stringify({ event, state: wireState() })}\n\n`);
    });

    if (!subscription) {
      // The bus independently caps subscribers; headers are already sent, so
      // close the stream rather than trying to answer with a status.
      res.end();
      return undefined;
    }

    clients.add(subscription);

    const heartbeat = setInterval(() => write(": ping\n\n"), heartbeatMs);
    heartbeat.unref?.();

    function cleanup() {
      clearInterval(heartbeat);
      subscription.unsubscribe();
      clients.delete(subscription);
    }

    res.on("close", cleanup);
    res.on("error", cleanup);
    req.on("aborted", cleanup);

    write(`retry: 3000\nevent: snapshot\ndata: ${JSON.stringify({ ...wireState(), events: bus.tail(40) })}\n\n`);
    return undefined;
  }

  await new Promise((resolve, reject) => {
    const onError = (error) => reject(error);
    server.once("error", onError);
    server.listen(port, BIND_HOST, () => {
      server.removeListener("error", onError);
      const address = server.address();
      boundPort = typeof address === "object" && address ? address.port : port;
      resolve();
    });
  });

  // Belt and braces: refuse to serve if a future refactor binds wider.
  const address = server.address();
  if (!address || typeof address !== "object" || address.address !== BIND_HOST) {
    await new Promise((resolve) => server.close(resolve));
    throw new Error(`dashboard refused to bind: ${address?.address ?? "unknown"}`);
  }

  return {
    host: BIND_HOST,
    port: boundPort,
    url: `http://127.0.0.1:${boundPort}/?token=${encodeURIComponent(token)}`,
    origin: `http://127.0.0.1:${boundPort}`,
    /**
     * A short-lived, single-use URL for opening the page in a browser. It
     * carries a ticket rather than the token, so it is safe to hand to a child
     * process: after one redemption (or the ticket's TTL) it authorises
     * nothing.
     */
    launchUrl() {
      return `http://127.0.0.1:${boundPort}/?ticket=${encodeURIComponent(handshake.issue())}`;
    },
    clients() {
      return clients.size;
    },
    async close() {
      for (const subscription of clients) subscription.unsubscribe();
      clients.clear();
      server.closeAllConnections?.();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

function send(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
  });
  res.end(body);
}

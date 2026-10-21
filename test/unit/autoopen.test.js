/**
 * Automatic opening of the dashboard.
 *
 * These tests are about the browser side of the dashboard: that it starts by
 * itself, that it opens exactly one tab per process, that it can be turned off,
 * that a broken browser cannot break the swarm, and that shutdown leaves no
 * opener behind. The opener is always injected — a test run must never launch a
 * real browser — except in the ticket tests, which talk to the real server over
 * real HTTP.
 */

import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";

import { createFakeContext } from "../support/fake-context.js";
import { createDashboard, normalizeDashboardConfig } from "../../src/dashboard/index.js";
import { startDashboardServer } from "../../src/dashboard/server.js";
import { createEventBus } from "../../src/dashboard/bus.js";
import { isOpenableUrl, openInBrowser } from "../../src/dashboard/open.js";
import { autoOpenState, resetAutoOpenState } from "../../src/dashboard/autoopen.js";
import { bootstrap } from "../../src/plugin/runtime.js";

const TOKEN = "test-token-0123456789abcdef0123456789abcdef";

function request({ port, path, method = "GET", headers = {} }) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: "127.0.0.1", port, path, method, headers }, (res) => {
      const chunks = [];
      res.on("data", (chunk) => chunks.push(chunk));
      res.on("end", () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString("utf8") }));
    });
    req.on("error", reject);
    req.end();
  });
}

async function waitFor(predicate, { timeoutMs = 2000, stepMs = 15 } = {}) {
  const started = Date.now();
  for (;;) {
    if (await predicate()) return true;
    if (Date.now() - started > timeoutMs) return false;
    await new Promise((resolve) => setTimeout(resolve, stepMs));
  }
}

function extractToken(url) {
  return new URL(url).searchParams.get("token");
}

function recorder() {
  const calls = [];
  const open = async (url) => {
    calls.push(url);
    return { opened: true };
  };
  return { calls, open };
}

function statusOf(ctx) {
  return ctx.__swarm.callTool("swarm_status", {}).then((result) => JSON.parse(result.content[0].text));
}

/* ---------------------------------------------------------- configuration */

test("the installed defaults are on: the dashboard starts and opens unless configured otherwise", () => {
  assert.deepEqual(normalizeDashboardConfig({}), { enabled: true, autoOpen: true, port: 0, ignored: [] });
  assert.equal(normalizeDashboardConfig({ enabled: false }).enabled, false, "explicit false opts out");
  assert.equal(normalizeDashboardConfig({ autoOpen: false }).autoOpen, false, "autoOpen can be turned off alone");
  assert.equal(normalizeDashboardConfig({ enabled: "no" }).enabled, true, "only an explicit false disables");
  assert.deepEqual(normalizeDashboardConfig({ host: "0.0.0.0" }).ignored, ["host"], "the bind host stays unconfigurable");
});

/* ------------------------------------------------------------- the opener */

test("the opener is a shell-free argv invocation on every platform", async () => {
  const seen = [];
  const fakeSpawn = (command, args, options) => {
    seen.push({ command, args, options });
    const listeners = {};
    const child = {
      once(event, fn) {
        listeners[event] = fn;
        return child;
      },
      unref() {},
      removeListener() {},
    };
    setImmediate(() => listeners.spawn?.());
    return child;
  };

  const url = "http://127.0.0.1:5555/?ticket=abc_DEF-123";
  const result = await openInBrowser(url, { platform: "win32", spawn: fakeSpawn });
  assert.equal(result.opened, true, "a launch is reported only after the launcher actually spawned");
  assert.equal(seen[0].options.shell, false, "no shell means the URL is never interpreted");
  assert.deepEqual(seen[0].args.slice(0, 4), ["/d", "/s", "/c", "start"]);
  assert.equal(seen[0].args.at(-1), url, "the URL is one argv entry");

  seen.length = 0;
  await openInBrowser(url, { platform: "darwin", spawn: fakeSpawn });
  assert.deepEqual([seen[0].command, ...seen[0].args], ["open", url]);

  seen.length = 0;
  await openInBrowser(url, { platform: "linux", spawn: fakeSpawn });
  assert.deepEqual([seen[0].command, ...seen[0].args], ["xdg-open", url]);
});

test("the opener refuses anything that is not this process's loopback dashboard", async () => {
  const refused = [
    "file:///C:/Windows/System32/calc.exe",
    "http://example.com/?ticket=x",
    "https://127.0.0.1:9000/?ticket=x",
    "http://127.0.0.1:9000/?ticket=x&calc=1",
    "javascript:alert(1)",
    "cmd.exe",
    "",
  ];

  for (const url of refused) {
    assert.equal(isOpenableUrl(url), false, `should refuse ${url}`);
    let spawned = false;
    const result = await openInBrowser(url, {
      spawn: () => {
        spawned = true;
        throw new Error("must not spawn");
      },
    });
    assert.equal(result.opened, false, url);
    assert.equal(spawned, false, `nothing is spawned for ${url}`);
  }

  assert.equal(isOpenableUrl("http://127.0.0.1:9/?ticket=abc"), true, "the real shape is accepted");
});

test("a launcher that does not exist is reported, not thrown", async () => {
  const url = "http://127.0.0.1:5555/?ticket=abc";
  const result = await openInBrowser(url, {
    spawn: () => {
      const listeners = {};
      const child = {
        once(event, fn) {
          listeners[event] = fn;
          return child;
        },
        unref() {},
        removeListener() {},
      };
      setImmediate(() => listeners.error?.(Object.assign(new Error("spawn cmd.exe ENOENT"), { code: "ENOENT" })));
      return child;
    },
  });
  assert.equal(result.opened, false);
  assert.equal(result.error, "ENOENT");
});

/* ------------------------------------------------------- launch tickets */

test("a launch ticket buys one page load and no data route", async () => {
  const server = await startDashboardServer({
    bus: createEventBus(),
    snapshot: () => ({ phase: "idle" }),
    token: TOKEN,
    port: 0,
  });

  try {
    const launch = new URL(server.launchUrl());
    assert.equal(launch.origin, `http://127.0.0.1:${server.port}`);
    assert.equal(launch.searchParams.get("token"), null, "the token is never in a launch URL");
    assert.equal(launch.searchParams.get("ticket").length >= 32, true);

    const first = await request({ port: server.port, path: `${launch.pathname}${launch.search}` });
    assert.equal(first.status, 200);
    assert.ok(first.body.includes(TOKEN), "the page is handed the token in its body instead of the address bar");

    const replay = await request({ port: server.port, path: `${launch.pathname}${launch.search}` });
    assert.equal(replay.status, 401, "the ticket is already spent");

    const viaTicketOnState = await request({ port: server.port, path: `/state?ticket=${launch.searchParams.get("ticket")}` });
    assert.equal(viaTicketOnState.status, 401, "a ticket never authorises a data route");
  } finally {
    await server.close();
  }
});

test("Host and Origin are still enforced before a ticket is looked at", async () => {
  const server = await startDashboardServer({
    bus: createEventBus(),
    snapshot: () => ({ phase: "idle" }),
    token: TOKEN,
    port: 0,
  });

  try {
    const launch = new URL(server.launchUrl());
    const path = `${launch.pathname}${launch.search}`;

    const rebound = await request({ port: server.port, path, headers: { host: "evil.example" } });
    assert.notEqual(rebound.status, 200, "a foreign Host cannot spend a ticket");

    const crossOrigin = await request({ port: server.port, path, headers: { origin: "http://evil.example" } });
    assert.notEqual(crossOrigin.status, 200, "a foreign Origin cannot spend a ticket");
  } finally {
    await server.close();
  }
});

/* ----------------------------------------------------------- automatic */

test("the plugin starts the dashboard and opens it once, after it is listening", async () => {
  resetAutoOpenState();
  const { calls, open } = recorder();
  const ctx = createFakeContext();
  const cleanup = await bootstrap(ctx, { config: { logLevel: "silent", dashboard: { open } } });

  try {
    assert.ok(
      await waitFor(async () => (await statusOf(ctx)).dashboard?.running === true),
      "the dashboard started without being asked",
    );
    assert.ok(await waitFor(() => calls.length === 1), "exactly one browser was launched");

    const status = await statusOf(ctx);
    assert.equal(status.dashboard.autoOpen.enabled, true);
    assert.equal(status.dashboard.autoOpen.opened, true, "the status reports what was observed");
    assert.equal(status.dashboard.autoOpen.attempts, 1);
    assert.equal(status.dashboard.autoOpen.origin, `http://127.0.0.1:${status.dashboard.port}`);

    const launched = new URL(calls[0]);
    assert.equal(launched.port, String(status.dashboard.port), "the real ephemeral port, not a guess");
    assert.equal(launched.searchParams.get("token"), null, "no capability token on the command line");
    assert.ok(launched.searchParams.get("ticket"), "a single-use ticket travels instead");

    // The URL the browser was given really serves the dashboard.
    const page = await request({ port: status.dashboard.port, path: `${launched.pathname}${launched.search}` });
    assert.equal(page.status, 200);
    assert.ok(page.body.includes(extractToken(status.dashboard.url)), "the served page carries the live token");
  } finally {
    await cleanup();
  }
});

test("a second dashboard in the same process does not open a second tab", async () => {
  resetAutoOpenState();
  const { calls, open } = recorder();
  const first = createDashboard({ config: { open }, token: TOKEN });
  const second = createDashboard({ config: { open }, token: TOKEN });

  try {
    await first.start();
    await first.opened();
    await second.start();
    await second.opened();

    assert.equal(calls.length, 1, "one tab per process, not one per dashboard");
    assert.equal(second.info().running, true, "the later dashboard still serves");
    assert.equal(second.info().autoOpen.opened, true, "and reports the tab that already exists");
    assert.notEqual(second.info().url, first.info().url, "on its own ephemeral port");
  } finally {
    await second.stop();
    await first.stop();
  }
});

test("concurrent initialisation opens exactly one tab", async () => {
  resetAutoOpenState();
  const { calls, open } = recorder();
  const dashboards = [
    createDashboard({ config: { open }, token: TOKEN }),
    createDashboard({ config: { open }, token: TOKEN }),
    createDashboard({ config: { open }, token: TOKEN }),
  ];

  try {
    await Promise.all(dashboards.map((dashboard) => dashboard.start()));
    await Promise.all(dashboards.map((dashboard) => dashboard.opened()));
    assert.equal(calls.length, 1, "a race between setups still produces one tab");
    assert.equal(dashboards.filter((dashboard) => dashboard.info().running).length, 3);
  } finally {
    await Promise.all(dashboards.map((dashboard) => dashboard.stop()));
  }
});

/* ------------------------------------------------------------- opt-out */

test("dashboard.autoOpen: false keeps the server and opens nothing", async () => {
  resetAutoOpenState();
  const { calls, open } = recorder();
  const dashboard = createDashboard({ config: { autoOpen: false, open }, token: TOKEN });

  try {
    const info = await dashboard.start();
    const state = await dashboard.opened();

    assert.equal(info.running, true, "the dashboard is still available");
    assert.equal(calls.length, 0, "no browser");
    assert.equal(state.claimed, false, "nothing was even attempted");
    assert.equal(info.autoOpen.enabled, false);
    assert.equal(info.config.autoOpen, false);

    const served = await request({ port: info.port, path: `/state?token=${TOKEN}` });
    assert.equal(served.status, 200, "the manual URL still works");
  } finally {
    await dashboard.stop();
  }
});

test("dashboard.enabled: false starts no server and opens no browser", async () => {
  resetAutoOpenState();
  const { calls, open } = recorder();
  const dashboard = createDashboard({ config: { enabled: false, open }, token: TOKEN });

  const info = await dashboard.start();
  const state = await dashboard.opened();

  assert.equal(info.running, false);
  assert.equal(calls.length, 0);
  assert.equal(state.claimed, false);
  assert.equal(info.autoOpen.enabled, false, "a dashboard that is not running cannot be opening");
});

/* ------------------------------------------------------------- failures */

test("a browser that cannot open leaves the dashboard and the swarm working", async () => {
  resetAutoOpenState();
  const warnings = [];
  const logger = {
    warn: (...args) => warnings.push(args),
    debug: () => {},
    info: () => {},
    error: () => {},
  };
  const dashboard = createDashboard({
    config: {
      open: async () => {
        throw new Error("ENOENT: no default browser");
      },
    },
    token: TOKEN,
    logger,
  });

  try {
    const info = await dashboard.start();
    const state = await dashboard.opened();

    assert.equal(info.running, true, "the server keeps running");
    assert.equal(state.opened, false);
    assert.equal(state.failed, true);
    assert.match(state.error, /ENOENT/);
    assert.equal(warnings.length, 1, "one concise warning, not a stream");
    assert.match(JSON.stringify(warnings[0]), /could not open the dashboard/);
    assert.ok(!JSON.stringify(warnings[0]).includes(TOKEN), "the token is never logged");

    const served = await request({ port: info.port, path: `/state?token=${TOKEN}` });
    assert.equal(served.status, 200);
  } finally {
    await dashboard.stop();
  }
});

test("a failed browser launch does not disturb the plugin's tools", async () => {
  resetAutoOpenState();
  const ctx = createFakeContext();
  const cleanup = await bootstrap(ctx, {
    config: {
      logLevel: "silent",
      dashboard: {
        open: async () => ({ opened: false, error: "no browser here" }),
      },
    },
  });

  try {
    assert.ok(await waitFor(async () => (await statusOf(ctx)).dashboard?.autoOpen?.failed === true));
    const status = await statusOf(ctx);
    assert.equal(status.dashboard.running, true, "the dashboard survived");
    assert.equal(status.dashboard.autoOpen.error, "no browser here");
    assert.ok(ctx.__swarm.toolNames().includes("swarm_plan"), "tools are untouched");
  } finally {
    await cleanup();
  }
});

/* ------------------------------------------------------------ shutdown */

test("shutting down before the scheduled open means no browser is ever launched", async () => {
  resetAutoOpenState();
  const { calls, open } = recorder();
  const dashboard = createDashboard({ config: { open }, token: TOKEN });

  await dashboard.start();
  await dashboard.stop();

  const state = await dashboard.opened();
  assert.equal(calls.length, 0, "shutdown won the race");
  assert.equal(state.opened, false);
  assert.equal(state.claimed, false, "the claim was never taken, so a later process start can still open");
});

test("cleanup stops the dashboard and leaves no opening work behind", async () => {
  resetAutoOpenState();
  const { calls, open } = recorder();
  const ctx = createFakeContext();
  const cleanup = await bootstrap(ctx, { config: { logLevel: "silent", dashboard: { open } } });

  assert.ok(await waitFor(async () => (await statusOf(ctx)).dashboard?.running === true));
  assert.ok(await waitFor(() => calls.length === 1), "the tab was opened while the plugin was alive");
  const port = (await statusOf(ctx)).dashboard.port;
  await cleanup();

  assert.equal(calls.length, 1, "exactly one tab, never reopened");
  await assert.rejects(request({ port, path: "/healthz" }), "and the server is gone afterwards");
  assert.equal(autoOpenState().opened, true, "the claim is history, not a live handle");
});

/* ---------------------------------------------------------- non-blocking */

test("a browser that never answers cannot block plugin startup", async () => {
  resetAutoOpenState();
  let release = null;
  const hang = () => new Promise((resolve) => {
    release = resolve;
  });

  const ctx = createFakeContext();
  const cleanup = await bootstrap(ctx, { config: { logLevel: "silent", dashboard: { open: hang } } });

  try {
    assert.ok(ctx.__swarm.toolNames().includes("swarm_plan"), "startup completed while the opener was pending");
    assert.ok(await waitFor(async () => (await statusOf(ctx)).dashboard?.running === true), "the server came up anyway");
    const status = await statusOf(ctx);
    assert.equal(status.dashboard.running, true);
    assert.equal(status.dashboard.autoOpen.opened, false, "a pending open is never reported as opened");
    release?.({ opened: false, error: "test finished" });
  } finally {
    release?.({ opened: false, error: "test finished" });
    await cleanup();
  }
});

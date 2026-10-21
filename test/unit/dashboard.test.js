/**
 * Dashboard tests.
 *
 * Everything here runs against the real modules: the real engine emits the
 * events, the real HTTP server answers, and the real bus bounds its queues.
 * Nothing is stubbed at the dashboard boundary, because the boundary is exactly
 * what these tests are about.
 */

import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";

import { createFakeContext } from "../support/fake-context.js";
import { createRuntime } from "../../src/plugin/adapter.js";
import { createManager } from "../../src/engine.js";
import { createMemoryAdapter, createStore } from "../../src/memory/store.js";
import { createEventBus } from "../../src/dashboard/bus.js";
import { createDashboardState, agentForRole } from "../../src/dashboard/state.js";
import { sanitizeEvent, modelLabel } from "../../src/dashboard/events.js";
import { createDashboard } from "../../src/dashboard/index.js";
import { startDashboardServer, tokenMatches, BIND_HOST } from "../../src/dashboard/server.js";
import { dashboardPage } from "../../src/dashboard/page.js";
import { bootstrap, createPlugin } from "../../src/plugin/runtime.js";

const TOKEN = "test-token-0123456789abcdef0123456789abcdef";

/**
 * The opener seam. Tests that start an enabled dashboard would otherwise open a
 * real browser on the machine running them, so they inject this instead and
 * assert on its calls where the opening itself is under test.
 */
const noBrowser = () => Promise.resolve({ opened: false, error: "test: browser disabled" });

function managerWith({ onEvent = null, worker = null } = {}) {
  const ctx = createFakeContext(worker ? { worker } : {});
  const runtime = createRuntime(ctx);
  const store = createStore({ adapter: createMemoryAdapter() });
  const manager = createManager({ runtime, store, onEvent });
  return { ctx, runtime, store, manager };
}

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

/* ------------------------------------------------------------------ bus */

test("the bus keeps a bounded history and counts what it dropped", () => {
  const bus = createEventBus({ limit: 5 });
  for (let index = 0; index < 12; index += 1) bus.publish("probe", { index });

  const tail = bus.tail(100);
  assert.equal(tail.length, 5, "history is capped at the limit");
  assert.deepEqual(tail.map((event) => event.index), [7, 8, 9, 10, 11], "the newest events survive");
  assert.equal(bus.stats().published, 12);
});

test("a subscriber queue is bounded and reports lag instead of growing", () => {
  const bus = createEventBus({ limit: 50, queueLimit: 4 });
  const seen = [];
  const subscription = bus.subscribe((event) => seen.push(event.index));
  for (let index = 0; index < 20; index += 1) bus.publish("probe", { index });

  assert.equal(subscription.dropped, 16, "overflow is counted, not hidden");
  return new Promise((resolve) => {
    setTimeout(() => {
      assert.deepEqual(seen, [16, 17, 18, 19], "the consumer still gets the newest events");
      resolve();
    }, 10);
  });
});

test("a throwing subscriber never breaks the publisher and is eventually dropped", () => {
  const bus = createEventBus();
  bus.subscribe(() => {
    throw new Error("subscriber exploded");
  });

  for (let index = 0; index < 3; index += 1) bus.publish("probe", { index });
  assert.doesNotThrow(() => bus.publish("probe", { index: 9 }));
  return new Promise((resolve) => {
    setTimeout(() => {
      assert.equal(bus.subscriberCount, 0, "a repeatedly failing subscriber loses its subscription");
      resolve();
    }, 10);
  });
});

/* ------------------------------------------------------------- redaction */

test("sanitization drops prompts, output, text and the capability token, and bounds strings", () => {
  const safe = sanitizeEvent({
    prompt: "secret",
    output: "secret",
    text: "secret",
    task: "secret",
    token: "secret",
    stepId: "build",
    tokens: 12,
    stepId: "x".repeat(400),
  });

  assert.deepEqual(Object.keys(safe).sort(), ["stepId", "tokens"]);
  assert.ok(safe.stepId.length <= 97, "long strings are truncated");
});

/* ------------------------------------------------------------- engine */

test("real engine events reach an observer, in order, with no prompt text", async () => {
  const events = [];
  const { manager } = managerWith({ onEvent: (event) => events.push(event) });
  const task = "Research the router and summarise how the catalog is scored";

  const result = await manager.submit(task, { workflow: "research_only", sessionID: "ses_parent" });
  assert.equal(result.ok, true, "the run itself succeeded");

  const types = events.map((event) => event.type);
  for (const expected of ["task.start", "classified", "planned", "step.start", "delegate", "step.end", "verify", "deliver", "run.end"]) {
    assert.ok(types.includes(expected), `missing ${expected} in ${types.join(",")}`);
  }
  assert.deepEqual(types.slice(0, 3), ["task.start", "classified", "planned"], "lifecycle order is preserved");

  // Genuine data, not placeholders.
  const planned = events.find((event) => event.type === "planned");
  assert.equal(planned.workflow, "research_only");
  assert.deepEqual(planned.steps.map((step) => step.role), ["researcher", "none"]);

  const delegate = events.find((event) => event.type === "delegate");
  assert.equal(delegate.role, "researcher");
  assert.equal(delegate.agent, "swarm/researcher");

  const deliver = events.find((event) => event.type === "deliver");
  assert.equal(deliver.verified, true);
  assert.ok(deliver.budget && Number.isFinite(deliver.budget.limits.maxTokens), "budget limits travel with delivery");

  // The whole point of the redaction layer: no user text anywhere.
  const serialized = JSON.stringify(events);
  assert.ok(!serialized.includes("Research the router"), "prompt text never reaches the dashboard");
});

test("an observer that throws cannot fail the task", async () => {
  const { manager } = managerWith({
    onEvent: () => {
      throw new Error("dashboard exploded");
    },
  });

  const result = await manager.submit("Research the installer", { workflow: "research_only" });
  assert.equal(result.ok, true, JSON.stringify(result.delivery ?? result.error));
  assert.equal(result.verification.passed, true);
});

/* -------------------------------------------------------------- state */

test("state distinguishes idle, running, completed, failed and unknown from real events only", () => {
  const state = createDashboardState({ now: () => 1000 });
  const idle = state.snapshot();
  assert.equal(idle.phase, "idle");
  assert.equal(idle.agents.manager.state, "idle");
  assert.equal(idle.counters.tokens, null, "an unmeasured metric is null, never zero");

  state.apply({ type: "task.start", taskId: "task_1", at: 1000 });
  state.apply({ type: "planned", at: 1001, workflow: "research_build_verify", steps: [
    { id: "research", role: "researcher", needs: [] },
    { id: "build", role: "none", needs: ["research"] },
    { id: "verify", role: "qc", needs: ["build"] },
  ] });

  state.apply({ type: "step.start", stepId: "research", role: "researcher", at: 1002 });
  assert.equal(state.snapshot().agents.researcher.state, "running");

  state.apply({ type: "step.end", stepId: "research", role: "researcher", status: "ok", durationMs: 40, at: 1042 });
  state.apply({ type: "step.start", stepId: "build", role: "none", at: 1043 });
  const snap = state.snapshot();
  assert.equal(snap.agents.researcher.state, "completed");
  assert.equal(snap.agents.manager.state, "running");
  assert.deepEqual(snap.handoffs.at(-1), { from: "researcher", to: "manager", stepId: "build", at: 1043 }, "handoffs come from the plan");

  state.apply({ type: "step.end", stepId: "build", role: "none", status: "failed", at: 1050 });
  assert.equal(state.snapshot().agents.manager.state, "failed");

  state.apply({ type: "verify", passed: false, independence: "different_model", reason: "criterion failed", at: 1051 });
  state.apply({ type: "deliver", status: "unverified", verified: false, durationMs: 60, at: 1060, budget: { limits: { maxMs: 1 }, used: { ms: 60 }, unknown: { tokens: 1 } } });
  const done = state.snapshot();
  assert.equal(done.phase, "failed");
  assert.equal(done.verification.passed, false);
  assert.equal(done.budget.limits.maxMs, 1);

  const live = createDashboardState({ now: () => 2000 });
  live.apply({ type: "task.start", taskId: "task_2", at: 2000 });
  live.apply({ type: "step.start", stepId: "build", role: "builder", at: 2001 });
  live.apply({ type: "dashboard", state: "stopped", at: 2002 });
  assert.equal(live.snapshot().agents.builder.state, "unknown", "lost observation is unknown, not completed");
});

test("roles map to the six documented agents", () => {
  assert.equal(agentForRole("none"), "manager");
  assert.equal(agentForRole("qc"), "qc");
  assert.equal(agentForRole("researcher"), "researcher");
  assert.equal(agentForRole("something-new"), "unknown");
});

/* ------------------------------------------------------------- server */

test("the server binds loopback only, on an ephemeral port by default", async () => {
  const bus = createEventBus();
  const server = await startDashboardServer({
    bus,
    snapshot: () => ({ phase: "idle" }),
    token: TOKEN,
    // A caller cannot ask for a different host; the parameter is not accepted.
    host: "0.0.0.0",
  });

  try {
    assert.equal(server.host, BIND_HOST);
    assert.equal(server.url.startsWith("http://127.0.0.1:"), true);
    assert.ok(server.port > 0, "an ephemeral port was assigned");
    const health = await request({ port: server.port, path: "/healthz" });
    assert.equal(health.status, 200);
  } finally {
    await server.close();
  }
});

test("access control: token, Host and Origin are all enforced, and only GET is allowed", async () => {
  const bus = createEventBus();
  const server = await startDashboardServer({ bus, snapshot: () => ({ phase: "idle" }), token: TOKEN });
  const port = server.port;

  try {
    assert.equal((await request({ port, path: "/" })).status, 401, "no token");
    assert.equal((await request({ port, path: `/?token=${"x".repeat(43)}` })).status, 401, "wrong token");
    assert.equal((await request({ port, path: "/state" })).status, 401, "state needs the token too");

    const good = await request({ port, path: `/?token=${TOKEN}` });
    assert.equal(good.status, 200);
    assert.match(good.headers["content-security-policy"] ?? "", /default-src 'none'/);
    assert.equal(good.headers["cache-control"], "no-store");
    assert.equal(good.headers["referrer-policy"], "no-referrer");

    const rebinding = await request({ port, path: `/state?token=${TOKEN}`, headers: { host: `evil.example:${port}` } });
    assert.equal(rebinding.status, 403, "a foreign Host is refused (DNS rebinding)");

    const crossOrigin = await request({ port, path: `/state?token=${TOKEN}`, headers: { origin: "http://evil.example" } });
    assert.equal(crossOrigin.status, 403, "a cross-origin browser context is refused");

    const sameOrigin = await request({ port, path: `/state?token=${TOKEN}`, headers: { origin: `http://127.0.0.1:${port}` } });
    assert.equal(sameOrigin.status, 200);
    assert.deepEqual(JSON.parse(sameOrigin.body), { phase: "idle" });

    const post = await request({ port, path: `/?token=${TOKEN}`, method: "POST" });
    assert.equal(post.status, 405);
    assert.equal(post.headers.allow, "GET, HEAD");
  } finally {
    await server.close();
  }
});

test("SSE streams a real snapshot, forwards published events, and cleans up on disconnect", async () => {
  const bus = createEventBus();
  const server = await startDashboardServer({ bus, snapshot: () => ({ phase: "idle" }), token: TOKEN });
  const chunks = [];

  const client = await new Promise((resolve, reject) => {
    const req = http.request(
      {
        host: "127.0.0.1",
        port: server.port,
        path: `/events?token=${TOKEN}`,
        headers: { accept: "text/event-stream" },
      },
      (res) => {
        assert.equal(res.statusCode, 200);
        assert.match(res.headers["content-type"], /text\/event-stream/);
        res.on("data", (chunk) => chunks.push(chunk.toString("utf8")));
        resolve({ res, req });
      },
    );
    req.on("error", reject);
    req.end();
  });

  try {
    assert.ok(await waitFor(() => chunks.join("").includes("event: snapshot")), "the first frame is a snapshot");

    bus.publish("step.start", { stepId: "research", role: "researcher" });
    assert.ok(await waitFor(() => chunks.join("").includes("event: swarm")), "a published event reaches the stream");
    assert.ok(chunks.join("").includes("research"));

    assert.equal(server.clients(), 1);
    client.req.destroy();
    assert.ok(await waitFor(() => server.clients() === 0), "a disconnected client is removed");
  } finally {
    client.res.destroy();
    await server.close();
  }
});

test("token comparison is constant-time and refuses empty values", () => {
  assert.equal(tokenMatches(TOKEN, TOKEN), true);
  assert.equal(tokenMatches(TOKEN, `${TOKEN}x`), false);
  assert.equal(tokenMatches(TOKEN, ""), false);
  assert.equal(tokenMatches("", ""), false);
});

/* ---------------------------------------------------------- page facts */

test("the page renders unavailable metrics honestly and supports reduced motion", () => {
  const html = dashboardPage();
  assert.ok(html.includes("Unavailable"), "missing measurements have a label");
  assert.ok(html.includes("prefers-reduced-motion"), "reduced motion is respected");
  assert.ok(html.includes("EventSource"), "the page uses SSE");
  assert.ok(html.includes("data-agent"), "agents are rendered as nodes");
  for (const agent of ["manager", "researcher", "builder", "visual", "qc", "improve"]) {
    assert.ok(html.includes(agent), `missing node ${agent}`);
  }
});

/* ------------------------------------------------------- dashboard life */

test("a disabled dashboard starts nothing and says why", async () => {
  const dashboard = createDashboard({ config: { enabled: false }, token: TOKEN });
  assert.equal(dashboard.enabled, false);
  const info = await dashboard.start();
  assert.equal(info.running, false);
  assert.match(info.hint, /disabled by configuration/);
  assert.equal(info.url, undefined);
  assert.equal(info.autoOpen.enabled, false, "a disabled dashboard never opens a browser either");
  await dashboard.stop();
});

test("an enabled dashboard serves state and stops cleanly", async () => {
  const dashboard = createDashboard({ config: { enabled: true }, token: TOKEN, open: noBrowser });
  const info = await dashboard.start();
  assert.equal(info.running, true);

  try {
    const state = await request({ port: info.port, path: `/state?token=${TOKEN}` });
    assert.equal(state.status, 200);
    assert.equal(JSON.parse(state.body).phase, "idle");

    dashboard.onEvent({ type: "task.start", taskId: "task_1" });
    const after = JSON.parse((await request({ port: info.port, path: `/state?token=${TOKEN}` })).body);
    assert.equal(after.phase, "running", "state reflects the real event");
  } finally {
    await dashboard.stop();
  }

  await assert.rejects(request({ port: info.port, path: "/healthz" }), "the port is closed after stop");
});

test("starting twice is idempotent and never delays the caller", async () => {
  const dashboard = createDashboard({ config: { enabled: true }, token: TOKEN, open: noBrowser });
  const [first, second] = await Promise.all([dashboard.start(), dashboard.start()]);
  try {
    assert.equal(first.port, second.port, "one server, not two");
    assert.equal(dashboard.bus.subscriberCount, 0);
  } finally {
    await dashboard.stop();
  }
});

/* ------------------------------------------------- plugin integration */

test("the plugin wires the dashboard, exposes it in swarm_status, and keeps unrelated tool flow intact", async () => {
  const ctx = createFakeContext();
  const cleanup = await bootstrap(ctx, { config: { dashboard: { enabled: true, open: noBrowser } } });

  try {
    assert.ok(
      await waitFor(async () => {
        const result = await ctx.__swarm.callTool("swarm_status", {});
        const payload = JSON.parse(result.content?.[0]?.text ?? "{}");
        return payload.dashboard?.running === true;
      }),
      "the plugin started the dashboard in its own time",
    );

    const status = JSON.parse((await ctx.__swarm.callTool("swarm_status", {})).content[0].text);
    assert.equal(status.dashboard.host, "127.0.0.1");
    assert.ok(status.dashboard.port > 0);
    assert.ok(status.dashboard.url.includes("token="));
    assert.equal(status.dashboard.events.limit >= 1, true);

    const page = await request({ port: status.dashboard.port, path: `/state?token=${extractToken(status.dashboard.url)}` });
    assert.equal(page.status, 200);
  } finally {
    await cleanup();
  }

  const closed = await ctx.__swarm.callTool("swarm_status", {}).catch(() => null);
  assert.equal(closed, null, "cleanup removes the tools as before");
});

test("the plugin stays fully functional when the dashboard is disabled", async () => {
  const ctx = createFakeContext();
  const cleanup = await bootstrap(ctx, { config: { dashboard: { enabled: false } } });
  try {
    const status = JSON.parse((await ctx.__swarm.callTool("swarm_status", {})).content[0].text);
    assert.equal(status.dashboard.enabled, false);
    assert.equal(status.dashboard.url, undefined);
    assert.ok(ctx.__swarm.toolNames().includes("swarm_run"), "tools are unaffected");
  } finally {
    await cleanup();
  }
});

test("plugin-entry options arrive in setup and override creation-time config", async () => {
  const ctx = createFakeContext();
  const plugin = createPlugin({ id: "opencode-swarm", config: { logLevel: "warn", dashboard: { enabled: false } } });
  // This is how OpenCode calls a plugin configured as `[path, options]`.
  const cleanup = await plugin.setup(ctx, { dashboard: { enabled: true, port: 0, open: noBrowser } });

  try {
    const started = await waitFor(async () => {
      const payload = JSON.parse((await ctx.__swarm.callTool("swarm_status", {})).content[0].text);
      return payload.dashboard?.running === true;
    });
    assert.equal(started, true, "the options from the config entry reached setup and enabled the dashboard");

    const status = JSON.parse((await ctx.__swarm.callTool("swarm_status", {})).content[0].text);
    assert.equal(status.dashboard.enabled, true);
    assert.equal(status.dashboard.host, "127.0.0.1");
    assert.equal(status.dashboard.config.port, 0, "an ephemeral port stays the default");
    assert.deepEqual(status.dashboard.config.ignored, [], "only documented options were given");
  } finally {
    await cleanup();
  }
});

test("sanitization blocks prompt-shaped keys in any casing", () => {
  const safe = sanitizeEvent({ Prompt: "secret", PROMPT: "secret", userPrompt: "secret", toolOutput: "secret", stepId: "build" });
  assert.deepEqual(safe, { stepId: "build" });
});

test("the wire drops free text even when a producer forgets to", async () => {
  const dashboard = createDashboard({ config: { enabled: true }, token: TOKEN, open: noBrowser });
  const info = await dashboard.start();
  try {
    // A snapshot shaped like the one the verifier used to hand over.
    const raw = () => ({
      phase: "failed",
      verification: { passed: false, independence: "different_model", reason: "c_scope: none of: ZEBRAUNICORN" },
      lastError: { stepId: "build", reason: "ZEBRAUNICORN exploded" },
    });
    const server = await startDashboardServer({ bus: createEventBus(), snapshot: raw, token: TOKEN, port: 0 });
    try {
      const state = await request({ port: server.port, path: `/state?token=${TOKEN}` });
      assert.equal(state.status, 200);
      assert.ok(!state.body.includes("ZEBRAUNICORN"), "criterion text never reaches the wire");
      assert.ok(!state.body.includes('"reason"'), "free-text reason fields are removed");
    } finally {
      await server.close();
    }
  } finally {
    await dashboard.stop();
    void info;
  }
});

test("a real run's served state contains no prompt text anywhere", async () => {
  const dashboard = createDashboard({ config: { enabled: true }, token: TOKEN, open: noBrowser });
  const info = await dashboard.start();
  const { manager } = managerWith({ onEvent: (event) => dashboard.onEvent(event) });
  try {
    const task = "Research the ZEBRAUNICORN router and summarise how the catalog is scored";
    await manager.submit(task, { workflow: "research_only", sessionID: "ses_parent" });

    const state = await request({ port: info.port, path: `/state?token=${TOKEN}` });
    assert.equal(state.status, 200);
    assert.ok(!state.body.includes("ZEBRAUNICORN"), "the prompt word is nowhere in the served state");
    assert.ok(!state.body.includes("Research the"), "not even a prefix of the prompt");
  } finally {
    await dashboard.stop();
  }
});

function extractToken(url) {
  return new URL(url).searchParams.get("token");
}

/* ------------------------------------------------- model label (regression) */

test("a model reported as an object becomes its ref, never [object Object]", () => {
  // A ref string passes through untouched; the object shapes different runtimes
  // report collapse to the same label; anything else is simply absent.
  assert.equal(modelLabel("opencode/deepseek-v4.1-flash-free"), "opencode/deepseek-v4.1-flash-free");
  assert.equal(modelLabel({ providerID: "opencode", id: "x" }), "opencode/x");
  assert.equal(modelLabel({ providerID: "opencode", modelID: "x" }), "opencode/x");
  assert.equal(modelLabel({ canonical: "opencode/x" }), "opencode/x");
  assert.equal(modelLabel({ id: "x" }), "x");
  assert.equal(modelLabel({}), null);
  assert.equal(modelLabel(42), null);
  assert.equal(modelLabel(""), null);

  const state = createDashboardState();
  state.apply({ type: "step.start", stepId: "research", role: "researcher" });
  state.apply({ type: "delegate", stepId: "research", role: "researcher", model: { providerID: "opencode", id: "x" } });
  assert.equal(state.snapshot().agents.researcher.model, "opencode/x");

  assert.deepEqual(sanitizeEvent({ type: "delegate", model: { providerID: "opencode", id: "x" } }), {
    type: "delegate",
    model: "opencode/x",
  });
});

test("a real run never serves [object Object] for the model", async () => {
  // The runtime's session reports the model as an object, which is exactly the
  // shape that used to reach the page verbatim.
  const dashboard = createDashboard({ config: { enabled: true }, token: TOKEN, open: noBrowser });
  const info = await dashboard.start();
  const { manager } = managerWith({ onEvent: (event) => dashboard.onEvent(event) });
  try {
    await manager.submit("Audit the catalog router and summarise scoring", {
      workflow: "research_only",
      sessionID: "ses_parent",
    });

    const served = await request({ port: info.port, path: `/state?token=${TOKEN}` });
    assert.equal(served.status, 200);
    assert.ok(!served.body.includes("[object Object]"), "an object model is normalised before the wire");

    const models = Object.values(JSON.parse(served.body).agents)
      .map((agent) => agent.model)
      .filter((model) => model !== null && model !== undefined);
    assert.ok(models.length > 0, "the runtime did report a model, so this is the path that broke");
    for (const model of models) {
      assert.equal(typeof model, "string");
      assert.ok(model.includes("/"), `expected a provider/model ref, got ${String(model)}`);
    }
  } finally {
    await dashboard.stop();
  }
});

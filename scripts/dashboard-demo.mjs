#!/usr/bin/env node
/**
 * Watch the swarm work, live.
 *
 * Everything that decides what the dashboard shows is the real thing: the real
 * dashboard server, the real event bus, the real derived state, the real
 * classification, planner, step runner and verification. The only stub is the
 * model runtime — `delegate()` sleeps and returns a canned answer instead of
 * calling a provider, so this spends no quota and needs no network.
 *
 * That distinction matters: the dashboard is a view of work that actually
 * happened, and this script exists to make that view observable on demand. It
 * is a demo of the machine, not fake data inside the product.
 *
 * Usage:
 *   node scripts/dashboard-demo.mjs                 open the tab and run
 *   node scripts/dashboard-demo.mjs --no-open       serve only, print the origin
 *   node scripts/dashboard-demo.mjs --port=7777     fixed port instead of ephemeral
 *   node scripts/dashboard-demo.mjs --delay=4000    slower steps, easier to watch
 *   node scripts/dashboard-demo.mjs --repeats=3     run the task three times so the tab animates again
 *   node scripts/dashboard-demo.mjs --keep-alive=60 stay up N seconds after the run
 *
 * Set SWARM_DEMO_STATE to a file path to publish {pid, origin, startedAt} there
 * (handy for scripts and for reading the port back). The capability token is
 * never written to that file, stdout or the log.
 */

import { writeFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";

import { createDashboard } from "../src/dashboard/index.js";
import { createManager } from "../src/engine.js";
import { createLogger } from "../src/core/logger.js";
import { createMemoryAdapter, createStore } from "../src/memory/store.js";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const argv = new Map(
  process.argv.slice(2).map((arg) => {
    if (!arg.startsWith("--")) return [arg, "true"];
    const body = arg.slice(2);
    const eq = body.indexOf("=");
    return eq === -1 ? [body, "true"] : [body.slice(0, eq), body.slice(eq + 1)];
  }),
);

const noOpen = argv.has("no-open");
const port = Number(argv.get("port") ?? 0) || 0;
const delayMs = Math.max(0, Number(argv.get("delay") ?? 2200) || 0);
const keepAliveSec = Math.max(0, Number(argv.get("keep-alive") ?? 120) || 0);
const repeats = Math.max(1, Math.min(10, Number(argv.get("repeats") ?? 1) || 1));

const TASK =
  "Research how the dashboard event pipeline currently works, implement a live progress " +
  "indicator in the page, verify it renders correctly, and report the changed files.";

// Shapes match Model.Info, so the real router scores a real catalog.
const STUB_MODELS = [
  { id: "stub-manager", modelID: "stub-manager", providerID: "stub", canonical: "stub/stub-manager", name: "stub-manager", status: "active", enabled: true, capabilities: { tools: true, input: ["text"], output: ["text"], temperature: true }, limit: { context: 200000, output: 8192 } },
  { id: "stub-worker", modelID: "stub-worker", providerID: "stub", canonical: "stub/stub-worker", name: "stub-worker", status: "active", enabled: true, capabilities: { tools: true, input: ["text"], output: ["text"], temperature: true }, limit: { context: 128000, output: 8192 } },
  { id: "stub-checker", modelID: "stub-checker", providerID: "stub", canonical: "stub/stub-checker", name: "stub-checker", status: "active", enabled: true, capabilities: { tools: true, input: ["text"], output: ["text"], temperature: true }, limit: { context: 128000, output: 8192 } },
];

const logger = createLogger({ level: "info", scope: "demo" });

const dashboard = createDashboard({
  config: { enabled: true, autoOpen: !noOpen, port },
  logger,
});

const runtime = {
  calls: [],
  async models() {
    return STUB_MODELS;
  },
  async delegate(input = {}) {
    const agent = input.agent ?? "swarm";
    const call = { agent, label: input.label ?? null, model: input.model ?? null };
    runtime.calls.push(call);
    process.stderr.write(`  delegate ${agent} ${call.label ?? ""}\n`);
    // Long enough to watch an agent sit in `running`, short enough to finish.
    await sleep(delayMs + Math.floor(Math.random() * Math.max(1, delayMs * 0.4)));

    // The QC agent is asked for a verdict, so answer in the shape the real
    // verifier parses. Everything else returns prose.
    const isVerifier = agent.includes("qc") || String(input.label ?? "").startsWith("verify");
    const text = isVerifier
      ? `${JSON.stringify({ passed: true, reason: "stub verdict: the deliverable is present" })} — the dashboard page renders and the changed files are reported.`
      : `stub result for ${input.label ?? agent}`;

    return {
      text,
      usage: { tokens: 900 + Math.floor(Math.random() * 400), costUSD: 0.0007, toolCalls: 1 },
      // A distinct ref per agent keeps the verifier's independence check honest.
      model: input.model ?? `stub/${String(agent).replace(/[^a-z0-9]+/gi, "-")}`,
      agent,
    };
  },
};

const manager = createManager({
  runtime,
  store: createStore({ adapter: createMemoryAdapter() }),
  logger,
  onEvent: (event) => dashboard.onEvent(event),
});

const statePath = process.env.SWARM_DEMO_STATE || join(tmpdir(), "opencode-dashboard-demo.json");

let stopping = false;
async function shutdown(code = 0) {
  if (stopping) return;
  stopping = true;
  await dashboard.stop().catch(() => {});
  await rm(statePath, { force: true }).catch(() => {});
  process.exit(code);
}

process.on("SIGINT", () => void shutdown(0));
process.on("SIGTERM", () => void shutdown(0));

const started = await dashboard.start();
const info = dashboard.info();
if (!info.running) {
  process.stderr.write(`dashboard did not start: ${info.error ?? "unknown reason"}\n`);
  process.exit(1);
}

await writeFile(
  statePath,
  `${JSON.stringify({ pid: process.pid, origin: `http://${info.host}:${info.port}`, startedAt: Date.now(), task: TASK }, null, 2)}\n`,
  "utf8",
);

process.stdout.write(`dashboard origin http://${info.host}:${info.port}${noOpen ? "" : " — opening the tab in your default browser"}\n`);
process.stdout.write(`state file ${statePath}\n`);
process.stdout.write(`watch: agents, handoffs, counters and the log update on every engine event (no prompt text, ever)\n`);

// The page suspends its stream while hidden, and a background tab would mean
// the run plays out where nobody is looking. So wait for a viewer first.
if (!noOpen) {
  process.stdout.write(`waiting for the tab to connect (click it if it opened in the background)…\n`);
  const waitUntil = Date.now() + 90000;
  while (Date.now() < waitUntil && dashboard.info().clients === 0 && !stopping) {
    await sleep(400);
  }
  if (dashboard.info().clients === 0) {
    process.stdout.write(`no browser connected after 90s — running anyway\n`);
  } else {
    process.stdout.write(`browser connected (${dashboard.info().clients} stream) — starting\n`);
    await sleep(600);
  }
}

await manager.bootstrap();

for (let index = 1; index <= repeats; index += 1) {
  if (index > 1) {
    // Each run publishes `task.start`, which resets the derived state, so the
    // tab animates again from a clean slate rather than appending to the last.
    process.stdout.write(`\nrun ${index}/${repeats} starting\n`);
    await sleep(1500);
  }

  const result = await manager.submit(TASK, { mode: "headless" });

  const delivery = result?.delivery ?? {};
  const criteria = Array.isArray(delivery.criteria) ? delivery.criteria : [];
  const planSteps = Array.isArray(result?.plan?.steps) ? result.plan.steps : [];
  const summary = {
    status: delivery.status ?? null,
    kind: delivery.taskKind ?? null,
    workflow: delivery.workflow ?? null,
    plan: planSteps.map((step) => `${step.id}:${step.role}`),
    verified: delivery.verified ?? null,
    criteria: criteria.length ? `${criteria.filter((entry) => entry?.passed === true).length}/${criteria.length}` : null,
    unpassed: criteria.filter((entry) => entry?.passed !== true).map((entry) => entry?.id ?? "?"),
    independence: delivery?.verification?.independence ?? null,
    failures: Array.isArray(delivery.failures) ? delivery.failures.length : null,
    durationMs: delivery.durationMs ?? null,
    delegates: runtime.calls.length,
    clients: dashboard.info().clients,
  };
  process.stdout.write(`run ${index}/${repeats} finished: ${JSON.stringify(summary)}\n`);
}

if (keepAliveSec <= 0) {
  await shutdown(0);
}

process.stdout.write(`dashboard stays up for ${keepAliveSec}s — Ctrl+C to stop after the tab shows the final state\n`);

let lastClients = dashboard.info().clients;
const until = Date.now() + keepAliveSec * 1000;
while (Date.now() < until && !stopping) {
  await sleep(1000);
  const clients = dashboard.info().clients;
  if (clients !== lastClients) {
    process.stdout.write(`browser clients: ${clients}\n`);
    lastClients = clients;
  }
}

await shutdown(0);

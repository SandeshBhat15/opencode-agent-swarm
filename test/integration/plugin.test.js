/**
 * Integration: the plugin layer against a fake OpenCode context.
 *
 * Everything here goes through the real `bootstrap()` path — tools registered
 * on the editor, hooks registered on the session, runtime built from `ctx` —
 * so the wiring is exercised, not just the modules underneath it.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { bootstrap, PLUGIN_ID } from "../../src/plugin/runtime.js";
import { shouldActivate, managerDirective, isAcknowledgement, DIRECTIVE_MARKER, MANAGER_AGENT_IDS } from "../../src/plugin/activation.js";
import { normalizeTokens, textOfMessage, toolCallsOfMessages, readUsage, toLimitUsage } from "../../src/plugin/telemetry.js";
import { createFakeContext, createFakeStorage, toolContext, FAKE_MODELS } from "../support/fake-context.js";

/**
 * The dashboard is on by default, so a bare config would start one HTTP server
 * — and one browser attempt — per bootstrap in this file. These tests are about
 * activation and the tool surface, so they turn it off; the dashboard has its
 * own suite, including the default-on path.
 */
const CONFIG = { logLevel: "silent", dashboard: { enabled: false } };

/* -------------------------------------------------------------- activation */

test("an ordinary prompt activates the manager by default", () => {
  const decision = shouldActivate({ text: "Build a REST API for a todo list" });
  assert.equal(decision.activate, true);
  assert.equal(decision.reason, "default_manager");
  assert.equal(decision.internal, false);
});

test("an ordinary prompt on a manager agent activates the manager", () => {
  const decision = shouldActivate({ text: "Summarize this design doc", agent: "swarm" });
  assert.equal(decision.activate, true);
  assert.equal(decision.reason, "manager_agent");
});

test("a prompt pinned to another agent is left alone", () => {
  const decision = shouldActivate({ text: "Build a REST API", agent: "build" });
  assert.equal(decision.activate, false);
  assert.equal(decision.reason, "explicit_agent");
});

test("internal delegations never re-activate the manager", () => {
  const decision = shouldActivate({ text: "Research vector databases", metadata: { swarm: { internal: true } } });
  assert.equal(decision.activate, false);
  assert.equal(decision.reason, "internal_delegation");
  assert.equal(decision.internal, true);
});

test("commands, empty prompts and acknowledgements are not tasks", () => {
  assert.equal(shouldActivate({ text: "/swarm-status" }).reason, "explicit_command");
  assert.equal(shouldActivate({ text: "!ls" }).reason, "explicit_command");
  assert.equal(shouldActivate({ text: "   " }).reason, "empty");
  assert.equal(shouldActivate({ text: "thanks" }).reason, "acknowledgement");
  assert.equal(shouldActivate({ text: "ok cool" }).reason, "acknowledgement");
});

test("acknowledgement detection is narrow: real work is not an acknowledgement", () => {
  assert.equal(isAcknowledgement("thanks"), true);
  assert.equal(isAcknowledgement("good job"), true);
  assert.equal(isAcknowledgement("go ahead"), true);
  assert.equal(isAcknowledgement("write the tests"), false);
  assert.equal(isAcknowledgement("yes please build the landing page with a hero"), false);
});

test("the manager contract is compact, marked, and names the real tools", () => {
  const directive = managerDirective({ task: "Build a CLI", workflow: "build_verify" });
  assert.ok(directive.includes(DIRECTIVE_MARKER));
  assert.ok(directive.includes("swarm_plan"));
  assert.ok(directive.includes("swarm_delegate"));
  assert.ok(directive.includes("swarm_verify"));
  assert.ok(directive.includes("build_verify"));
  assert.ok(directive.includes("Build a CLI"));
  assert.ok(directive.length < 3000, "must stay cheap to inject on every step");
  assert.ok(MANAGER_AGENT_IDS.includes("swarm"));
});

/* --------------------------------------------------------------- telemetry */

test("token normalization keeps the measured cache breakdown", () => {
  const normalized = normalizeTokens({ input: 10, output: 5, reasoning: 1, cache: { read: 2, write: 3 } });
  assert.deepEqual(normalized, { input: 10, output: 5, reasoning: 1, total: 16, cacheRead: 2, cacheWrite: 3 });
});

test("a token field that was never reported normalizes to null, not to zero", () => {
  assert.equal(normalizeTokens(undefined), null);
  assert.equal(normalizeTokens({}), null);
  assert.deepEqual(normalizeTokens({ input: 0, output: 0, reasoning: 0 }), {
    input: 0,
    output: 0,
    reasoning: 0,
    total: 0,
    cacheRead: 0,
    cacheWrite: 0,
  });
});

test("assistant text and tool calls are read from message content", () => {
  const messages = [
    { type: "user", content: [{ type: "text", text: "hi" }] },
    { type: "assistant", content: [{ type: "text", text: "one" }, { type: "tool", id: "t1" }, { type: "text", text: "two" }] },
  ];
  assert.equal(textOfMessage(messages[1]), "one\ntwo");
  assert.equal(toolCallsOfMessages(messages), 1);
});

test("usage prefers the session aggregate when the runtime reports one", async () => {
  const session = {
    async get() {
      return { tokens: { input: 100, output: 40, reasoning: 0, cache: { read: 5, write: 1 } }, cost: 0.02 };
    },
    async context() {
      return [{ type: "assistant", tokens: { input: 1, output: 1, reasoning: 0, cache: { read: 0, write: 0 } }, cost: 9 }];
    },
  };
  const report = await readUsage({ session, sessionID: "ses_1" });
  assert.equal(report.source, "session.aggregate");
  assert.equal(report.tokens.total, 140);
  assert.equal(report.costUSD, 0.02);
  assert.equal(report.missing, 0);
});

test("usage falls back to per-message tokens and reports unreported messages honestly", async () => {
  const session = {
    async get() {
      return { tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } }, cost: 0 };
    },
    async context() {
      return [
        { type: "assistant", tokens: { input: 10, output: 5, reasoning: 0, cache: { read: 0, write: 0 } }, cost: 0.01 },
        { type: "assistant" },
      ];
    },
  };
  const report = await readUsage({ session, sessionID: "ses_2" });
  assert.equal(report.source, "session.aggregate", "a real aggregate still wins");
  assert.equal(report.tokens.total, 0, "the runtime said zero, and zero is respected as measured");

  const noAggregate = {
    async get() {
      return {};
    },
    async context() {
      return [{ type: "assistant", tokens: { input: 10, output: 5, reasoning: 0, cache: { read: 0, write: 0 } }, cost: 0.01 }, { type: "assistant" }];
    },
  };
  const fallback = await readUsage({ session: noAggregate, sessionID: "ses_3" });
  assert.equal(fallback.source, "session.messages");
  assert.equal(fallback.tokens.total, 15);
  assert.equal(fallback.missing, 1, "one message reported nothing");
});

test("a session that reports nothing is unavailable, never zero", async () => {
  const session = {
    async get() {
      return {};
    },
    async context() {
      return [{ type: "assistant" }];
    },
  };
  const report = await readUsage({ session, sessionID: "ses_4" });
  assert.equal(report.source, "session.messages.unreported");
  assert.equal(report.tokens, null);
  assert.equal(report.costUSD, null);
  assert.equal(report.missing, 1);
  assert.deepEqual(toLimitUsage(report), { tokens: null, costUSD: null, toolCalls: 0 });
});

test("an unreachable runtime yields an unavailable report rather than an exception", async () => {
  const throwing = {
    async get() {
      throw new Error("server gone");
    },
    async context() {
      throw new Error("server gone");
    },
  };
  const report = await readUsage({ session: throwing, sessionID: "ses_5" });
  assert.equal(report.source, "unavailable");
  assert.equal(report.tokens, null);
});

/* ---------------------------------------------------------------- bootstrap */

test("bootstrap registers the swarm tools and the manager hooks", async () => {
  const ctx = createFakeContext();
  const cleanup = await bootstrap(ctx, { config: CONFIG });

  assert.deepEqual(ctx.__swarm.toolNames().sort(), [
    "swarm_delegate",
    "swarm_improve",
    "swarm_memory",
    "swarm_plan",
    "swarm_run",
    "swarm_status",
    "swarm_verify",
  ]);
  assert.ok(ctx.__swarm.calls.hookNames.includes("prompt"));
  assert.ok(ctx.__swarm.calls.hookNames.includes("context"));

  await cleanup();
  assert.equal(ctx.__swarm.toolNames().length, 0, "cleanup removes every registered tool");
});

test("every swarm tool opts into the namespace the manager contract names", async () => {
  const ctx = createFakeContext();
  const cleanup = await bootstrap(ctx, { config: CONFIG });

  // Declaring a namespace is not enough on the real server: a tool joins it
  // only through `options.namespace`. Without that the tools would register as
  // bare `plan` / `status`, and every `swarm_*` call in agents/swarm.md would miss.
  for (const tool of ctx.__swarm.tools.values()) {
    assert.equal(tool.options?.namespace, "swarm", `${tool.name} must declare its namespace`);
  }
  assert.deepEqual(ctx.__swarm.toolNames().sort(), [
    "swarm_delegate",
    "swarm_improve",
    "swarm_memory",
    "swarm_plan",
    "swarm_run",
    "swarm_status",
    "swarm_verify",
  ]);

  await cleanup();
});

test("the prompt hook tags an admitted task without rewriting the user's text", async () => {
  const ctx = createFakeContext();
  const cleanup = await bootstrap(ctx, { config: CONFIG });

  const event = { sessionID: "ses_parent", messageID: "msg_1", prompt: { text: "Build a todo app with tests" }, metadata: {} };
  await ctx.__swarm.fire("prompt", event);

  assert.equal(event.prompt.text, "Build a todo app with tests", "user text is left alone");
  assert.equal(event.metadata.swarm?.managed, true);
  assert.equal(event.metadata.swarm?.task, "Build a todo app with tests");
  await cleanup();
});

test("the context hook injects the manager contract once per turn, for manager agents only", async () => {
  const ctx = createFakeContext();
  const cleanup = await bootstrap(ctx, { config: CONFIG });
  await ctx.__swarm.fire("prompt", { sessionID: "ses_parent", prompt: { text: "Build a todo app with tests" }, metadata: {} });

  const event = {
    sessionID: "ses_parent",
    agent: "swarm",
    model: { id: "m", providerID: "opencode" },
    system: [{ type: "text", text: "base" }],
    messages: [],
    options: {},
    tools: {},
  };
  await ctx.__swarm.fire("context", event);
  assert.equal(event.system.length, 2);
  assert.ok(String(event.system[1].text).includes(DIRECTIVE_MARKER));

  await ctx.__swarm.fire("context", event);
  assert.equal(event.system.length, 2, "not injected twice into the same turn");

  const other = { ...event, agent: "build", system: [{ type: "text", text: "base" }] };
  await ctx.__swarm.fire("context", other);
  assert.equal(other.system.length, 1, "a non-manager agent is untouched");
  await cleanup();
});

test("a broken system part never breaks the user's request", async () => {
  const ctx = createFakeContext();
  const cleanup = await bootstrap(ctx, { config: CONFIG });
  await ctx.__swarm.fire("prompt", { sessionID: "ses_parent", prompt: { text: "Build a thing" }, metadata: {} });

  const event = { sessionID: "ses_parent", agent: "swarm", system: null, messages: [], options: {}, tools: {} };
  await ctx.__swarm.fire("context", event);
  assert.equal(event.system, null, "left untouched when the shape is unexpected");
  await cleanup();
});

/* -------------------------------------------------------------------- tools */

test("swarm_plan is deterministic and spends no model call", async () => {
  const ctx = createFakeContext();
  const cleanup = await bootstrap(ctx, { config: CONFIG });

  const result = await ctx.__swarm.callTool("swarm_plan", { task: "Build a REST API in Node with tests" }, toolContext());
  const plan = JSON.parse(result.content[0].text);

  assert.equal(plan.deterministic, true);
  assert.equal(plan.kind, "build");
  assert.equal(plan.workflow, "build_verify");
  assert.equal(plan.catalog.source, "live");
  assert.equal(plan.catalog.count, FAKE_MODELS.length);
  assert.ok(plan.steps.length >= 2);
  assert.ok(plan.criteria.length >= 1);
  assert.equal(ctx.__swarm.calls.created.length, 0, "planning delegates nothing");
  assert.equal(ctx.__swarm.calls.prompt.length, 0, "planning spends no tokens");
  await cleanup();
});

test("swarm_delegate runs one specialist in its own session with measured usage", async () => {
  const ctx = createFakeContext();
  const cleanup = await bootstrap(ctx, { config: CONFIG });

  const result = await ctx.__swarm.callTool(
    "swarm_delegate",
    { role: "researcher", prompt: "Find the current best practice", label: "research" },
    toolContext(),
  );
  const payload = JSON.parse(result.content[0].text);

  assert.equal(payload.ok, true);
  assert.equal(payload.agent, "swarm/researcher");
  assert.match(payload.sessionID, /^ses_/);
  assert.ok(payload.usage.tokens.total > 0);
  assert.equal(ctx.__swarm.calls.created.length, 1);
  assert.equal(ctx.__swarm.calls.created[0].agent, "swarm/researcher");
  assert.equal(ctx.__swarm.calls.created[0].parentID, "ses_parent");
  assert.equal(ctx.__swarm.calls.created[0].metadata.swarm.internal, true);
  await cleanup();
});

test("swarm_delegate refuses an unknown role without spending anything", async () => {
  const ctx = createFakeContext();
  const cleanup = await bootstrap(ctx, { config: CONFIG });
  const result = await ctx.__swarm.callTool("swarm_delegate", { role: "wizard", prompt: "do magic" }, toolContext());
  const payload = JSON.parse(result.content[0].text);
  assert.equal(payload.ok, false);
  assert.match(payload.error, /unknown_role/);
  assert.equal(ctx.__swarm.calls.created.length, 0);
  await cleanup();
});

test("swarm_verify passes a deterministic criterion without a model judgement when none is needed", async () => {
  const ctx = createFakeContext();
  const cleanup = await bootstrap(ctx, { config: CONFIG });

  const result = await ctx.__swarm.callTool(
    "swarm_verify",
    {
      task: "produce a summary",
      output: "A summary about the vector database landscape and its trade offs.",
      criteria: [{ id: "c1", check: "mentionsAny", values: ["vector database"], text: "mentions the subject" }],
    },
    toolContext(),
  );
  const payload = JSON.parse(result.content[0].text);
  assert.equal(payload.passed, true);
  assert.equal(payload.independence, "not_required");
  assert.equal(ctx.__swarm.calls.created.length, 0);
  await cleanup();
});

test("swarm_verify fails a deterministic criterion without spending a model call", async () => {
  const ctx = createFakeContext();
  const cleanup = await bootstrap(ctx, { config: CONFIG });

  const result = await ctx.__swarm.callTool(
    "swarm_verify",
    { task: "produce a summary", output: "nothing useful", criteria: [{ id: "c1", check: "mentionsAll", values: ["vector database"], text: "mentions it" }] },
    toolContext(),
  );
  const payload = JSON.parse(result.content[0].text);
  assert.equal(payload.passed, false);
  assert.deepEqual(payload.failed, ["c1"]);
  assert.equal(ctx.__swarm.calls.created.length, 0, "a deterministic failure short-circuits the judge");
  await cleanup();
});

test("swarm_status reports the real interfaces, catalog and instrumentation", async () => {
  const ctx = createFakeContext();
  const cleanup = await bootstrap(ctx, { config: CONFIG });

  const result = await ctx.__swarm.callTool("swarm_status", {}, toolContext());
  const payload = JSON.parse(result.content[0].text);

  assert.equal(payload.opencode.version, "2.0.32");
  assert.deepEqual(payload.opencode.missingInterfaces, []);
  assert.equal(payload.catalog.count, FAKE_MODELS.length);
  assert.equal(payload.catalog.source, "live");
  assert.equal(payload.instrumentation.plans, 0);
  assert.equal(payload.lastRun, null);
  await cleanup();
});

test("swarm_status reports an unreachable catalog as an error source, not as zero models", async () => {
  const ctx = createFakeContext({ modelListFails: true });
  const cleanup = await bootstrap(ctx, { config: CONFIG });
  const result = await ctx.__swarm.callTool("swarm_status", {}, toolContext());
  const payload = JSON.parse(result.content[0].text);
  assert.equal(payload.catalog.source, "error");
  assert.equal(payload.catalog.count, 0);
  await cleanup();
});

test("swarm_memory reads the task memory it was asked for", async () => {
  const ctx = createFakeContext();
  const cleanup = await bootstrap(ctx, { config: CONFIG });

  const recent = JSON.parse((await ctx.__swarm.callTool("swarm_memory", { action: "recent" }, toolContext())).content[0].text);
  assert.equal(recent.action, "recent");
  assert.deepEqual(recent.entries, []);

  const recall = JSON.parse((await ctx.__swarm.callTool("swarm_memory", { action: "recall", task: "something new" }, toolContext())).content[0].text);
  assert.equal(recall.action, "recall");
  assert.equal(recall.similar, null, "no evidence, no match invented");

  const lessons = JSON.parse((await ctx.__swarm.callTool("swarm_memory", { action: "lessons" }, toolContext())).content[0].text);
  assert.equal(lessons.action, "lessons");
  assert.deepEqual(lessons.models, []);
  await cleanup();
});

test("swarm_improve defaults to a dry run and never edits anything", async () => {
  const ctx = createFakeContext();
  const cleanup = await bootstrap(ctx, { config: CONFIG });
  const payload = JSON.parse((await ctx.__swarm.callTool("swarm_improve", {}, toolContext())).content[0].text);
  assert.equal(payload.dryRun, true);
  assert.equal(payload.applied.length, 0);
  await cleanup();
});

test("swarm_run carries a task end to end and reports real measured usage", async () => {
  const ctx = createFakeContext({ worker: workerWithVerdict });
  const cleanup = await bootstrap(ctx, { config: CONFIG });

  const payload = JSON.parse(
    (await ctx.__swarm.callTool("swarm_run", { task: "Build a REST API in Node with tests" }, toolContext())).content[0].text,
  );

  assert.equal(payload.workflow, "build_verify");
  assert.equal(payload.status, "delivered");
  assert.equal(payload.verified, true);
  assert.equal(payload.verification.independence, "different_model");
  assert.equal(payload.delivery.deferredSteps.length, 1, "the build step runs in the manager's session");
  assert.ok(payload.metrics.usage.tokens > 0, "usage came from the runtime's own report");
  assert.equal(payload.metrics.usage.tokensObserved, true);
  assert.equal(ctx.__swarm.calls.created.length >= 2, true, "at least the verifier step and the judge delegated");
  await cleanup();
});

test("swarm_run still delivers when the model catalog is unreachable", async () => {
  const ctx = createFakeContext({ modelListFails: true, worker: workerWithVerdict });
  const cleanup = await bootstrap(ctx, { config: CONFIG });

  const payload = JSON.parse(
    (await ctx.__swarm.callTool("swarm_run", { task: "Build a REST API in Node with tests" }, toolContext())).content[0].text,
  );
  assert.equal(payload.status, "delivered");
  assert.equal(payload.verification.independence, "same_model_check", "no second model was available to be independent");
  await cleanup();
});

test("two runs of the same verified research task reuse the first run's findings", async () => {
  const ctx = createFakeContext();
  const cleanup = await bootstrap(ctx, { config: CONFIG });
  const task = "Research the latest state of the art in vector databases and summarize what you find";

  const first = JSON.parse((await ctx.__swarm.callTool("swarm_run", { task }, toolContext())).content[0].text);
  assert.equal(first.workflow, "research_only");
  assert.equal(first.run.steps.find((step) => step.stepId === "research").status, "ok");

  const delegationsAfterFirst = ctx.__swarm.calls.created.length;
  const second = JSON.parse((await ctx.__swarm.callTool("swarm_run", { task }, toolContext())).content[0].text);

  const research = second.run.steps.find((step) => step.stepId === "research");
  assert.equal(research.status, "reused", "the second run reuses the remembered findings");
  assert.equal(ctx.__swarm.calls.created.length, delegationsAfterFirst, "no duplicate research was delegated");
  await cleanup();
});

test("state persists through the runtime storage adapter, not just in memory", async () => {
  const storage = createFakeStorage();
  const ctx = createFakeContext({ storage });
  const cleanup = await bootstrap(ctx, { config: CONFIG });
  await ctx.__swarm.callTool("swarm_run", { task: "Build a small utility with tests" }, toolContext());
  await cleanup();

  const keys = [...storage.__data.keys()];
  assert.ok(keys.some((key) => key.includes("memory/tasks")), `expected task memory to persist, saw ${keys.join(", ")}`);
  assert.ok(keys.some((key) => key.includes("learning/outcomes")), `expected learning to persist, saw ${keys.join(", ")}`);
});

function workerWithVerdict(text, { agent } = {}) {
  const body = String(text ?? "");
  const tokens = { input: 120, output: 60, reasoning: 0, cache: { read: 0, write: 0 } };
  if (agent === "swarm/qc" || body.includes('"passed"')) {
    // A verifier sees the whole brief, so it echoes it: this keeps the derived
    // scope criterion satisfiable while the final JSON line stays parseable.
    return { text: `${body.slice(0, 300)}\n{"passed":true,"reason":"the deliverable is present and coherent","failed":[]}`, tokens, cost: 0.002 };
  }
  return { text: `[${agent ?? "worker"}] ${body.slice(0, 200)}`, tokens, cost: 0.001 };
}

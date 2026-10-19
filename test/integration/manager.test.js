import test from "node:test";
import assert from "node:assert/strict";

import { createManager } from "../../src/engine.js";
import { createRuntime } from "../../src/plugin/adapter.js";
import { createStore, createMemoryAdapter } from "../../src/memory/store.js";
import { createLogger } from "../../src/core/logger.js";
import { createFakeContext } from "../support/fake-context.js";

const logger = createLogger({ level: "silent", scope: "test" });

/**
 * A worker that echoes the brief back so the derived scope criterion stays
 * satisfiable, and answers the QC judge with a final-line verdict object.
 */
function worker(text, { agent } = {}) {
  if (agent === "swarm/qc" || /\bpassed\b/i.test(text)) {
    const body = String(text).slice(0, 300);
    return { text: `${body}\n{"passed":true,"reason":"independent check ok","failed":[]}` };
  }
  return { text: `Completed: ${String(text).slice(0, 400)}` };
}

function harness({ worker: w = worker, models, modelListFails, store } = {}) {
  const ctx = createFakeContext({ worker: w, models, modelListFails });
  const runtime = createRuntime(ctx, { logger });
  const manager = createManager({
    runtime,
    store: store ?? createStore({ adapter: createMemoryAdapter() }),
    logger,
    policy: { mode: "interactive", verify: true },
  });
  return { ctx, runtime, manager };
}

const RESEARCH =
  "Research the current state of vector database indexing and summarise the tradeoffs";

test("research task delivers with a delegated researcher and a deferred delivery step", async () => {
  const { ctx, manager } = harness();
  const result = await manager.submit(RESEARCH);

  assert.equal(result.ok, true);
  assert.equal(result.classification.kind, "research");
  assert.equal(result.plan.workflow, "research_only");
  assert.equal(result.delivery.status, "delivered");
  assert.equal(result.delivery.verified, true);

  const research = result.run.steps.find((s) => s.stepId === "research");
  assert.equal(research.status, "ok");
  assert.equal(research.mode, "delegated");
  assert.equal(research.model.providerID, "opencode");

  const deliver = result.run.steps.find((s) => s.stepId === "deliver");
  assert.equal(deliver.status, "deferred");
  assert.equal(result.delivery.deferredSteps.length, 1);
  assert.equal(result.delivery.deferredSteps[0].stepId, "deliver");

  // one child session for the researcher, never a second for the deferred step
  assert.equal(ctx.__swarm.calls.created.length, 1);
  assert.equal(ctx.__swarm.calls.created[0].agent, "swarm/researcher");
  assert.equal(result.verification.independence, "not_required");

  // real telemetry is captured, never invented
  assert.ok(result.metrics.usage.tokens > 0);
  assert.ok(result.metrics.usage.tokensObserved > 0);
  assert.equal(result.metrics.unknown.tokens, 0);
  assert.ok(result.budget.limits.maxTokens > 0);
});

test("headless mode delegates the manager step instead of deferring it", async () => {
  const { manager } = harness();
  const result = await manager.submit("Build a small utility that formats currency and test it", {
    mode: "headless",
  });

  const build = result.run.steps.find((s) => s.stepId === "build");
  assert.equal(build.mode, "delegated_manager");
  assert.equal(build.status, "ok");
  assert.equal(result.delivery.deferredSteps.length, 0);
  assert.equal(result.delivery.status, "delivered");
});

test("an unreachable model catalog is reported as an error, not as zero models", async () => {
  const { manager } = harness({ modelListFails: true });
  const result = await manager.submit("Research the tradeoffs of local vector search");

  assert.equal(result.delivery.status, "delivered");
  // no usable candidates, so the judge could not run on an independent model
  assert.notEqual(result.verification.independence, "different_model");
  assert.ok(result.metrics.usage.tokens > 0);
});

test("a repeated verified task reuses the remembered research step", async () => {
  const store = createStore({ adapter: createMemoryAdapter() });
  const { manager, ctx } = harness({ store });

  const first = await manager.submit(RESEARCH);
  assert.equal(first.delivery.verified, true);
  assert.equal(first.delivery.reuse, null);
  const createdAfterFirst = ctx.__swarm.calls.created.length;

  const second = await manager.submit(RESEARCH);
  const research = second.run.steps.find((s) => s.stepId === "research");
  assert.equal(research.status, "reused");
  assert.ok(second.delivery.reuse);
  assert.equal(second.delivery.reuse.taskId, first.taskId);
  // no new delegation for the reused step
  assert.equal(ctx.__swarm.calls.created.length, createdAfterFirst);
});

test("submit reports the full result shape and records efficiency metrics", async () => {
  const { manager } = harness();
  const result = await manager.submit("Build a CLI flag parser and verify it");

  for (const key of [
    "ok",
    "taskId",
    "classification",
    "plan",
    "run",
    "verification",
    "delivery",
    "metrics",
    "budget",
  ]) {
    assert.ok(key in result, `missing ${key}`);
  }
  assert.equal(typeof result.metrics.efficiency.directShare, "number");
  assert.equal(typeof result.metrics.efficiency.retryRate, "number");
  assert.ok(result.plan.criteria.length >= 1);
  assert.equal(result.plan.criteria.some((c) => c.id === "c_deliverable"), true);
});

test("the manager exposes status and refuses to run without a runtime", async () => {
  const { manager } = harness();
  await manager.submit("Research something specific about storage engines");
  const status = await manager.status();
  assert.equal(typeof status.models, "number");
  assert.ok(status.learning);
  assert.ok(status.tuner);

  assert.throws(() => createManager({}), /runtime/i);
});

/**
 * Persistent memory: store adapters, evidence-based learning, task recall.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { createFsAdapter, createMemoryAdapter, createRuntimeAdapter, createStore } from "../../src/memory/store.js";
import { createLearning } from "../../src/memory/learning.js";
import { createTaskMemory, keywordsOf } from "../../src/memory/tasks.js";
import { createFakeStorage } from "../support/fake-context.js";

test("store: get/set/remove with namespacing", async () => {
  const adapter = createMemoryAdapter();
  const store = createStore({ adapter, namespace: "swarm" });
  assert.equal(await store.get("missing", "fallback"), "fallback");
  await store.set("a", { n: 1 });
  assert.deepEqual(await store.get("a"), { n: 1 });
  assert.deepEqual([...(await adapter.keys())], ["swarm/a"]);
  assert.deepEqual(await store.keys(), ["a"]);
  await store.remove("a");
  assert.equal(await store.get("a", null), null);
});

test("store: append caps the list and update is read-modify-write", async () => {
  const store = createStore({ adapter: createMemoryAdapter() });
  for (let i = 0; i < 5; i += 1) await store.append("log", { i }, { max: 3 });
  const log = await store.get("log");
  assert.equal(log.length, 3);
  assert.equal(log[0].i, 2);
  const next = await store.update("counter", (current) => (current ?? 0) + 1, 0);
  assert.equal(next, 1);
  assert.equal(await store.get("counter"), 1);
});

test("store: the filesystem adapter round-trips", async () => {
  const dir = await mkdtemp(join(tmpdir(), "swarm-store-"));
  try {
    const store = createStore({ adapter: createFsAdapter({ dir }) });
    await store.set("state", { ok: true });
    assert.deepEqual(await store.get("state"), { ok: true });
    assert.deepEqual(await store.keys(), ["state"]);
    await store.remove("state");
    assert.equal(await store.get("state", null), null);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test("store: the runtime adapter reads the real scan shape", async () => {
  const storage = createFakeStorage({ "swarm/x": { v: 1 }, "other/y": { v: 2 } });
  const store = createStore({ adapter: createRuntimeAdapter(storage) });
  assert.deepEqual(await store.get("x"), { v: 1 });
  assert.deepEqual(await store.keys(), ["x"]);
});

test("store: a failing adapter is contained, never thrown at the caller", async () => {
  const adapter = {
    kind: "broken",
    async get() {
      throw new Error("disk on fire");
    },
    async set() {
      throw new Error("disk on fire");
    },
    async remove() {},
    async keys() {
      throw new Error("disk on fire");
    },
  };
  const store = createStore({ adapter });
  assert.equal(await store.get("x", "safe"), "safe");
  assert.equal(await store.set("x", 1), undefined);
  assert.deepEqual(await store.keys(), []);
});

test("learning: metrics require observed samples and never invent a value", () => {
  const learning = createLearning({});
  const empty = learning.overall();
  assert.equal(empty.samples, 0);
  assert.equal(empty.successRate, null);
  assert.equal(empty.costPerStep, null);
  assert.equal(empty.tokensPerStep, null);

  learning.record({ model: "opencode/a", role: "builder", success: true, durationMs: 100, costUSD: 0.01, tokens: 50, retries: 0 });
  const stats = learning.overall();
  assert.equal(stats.samples, 1);
  assert.equal(stats.successRate, 1);
  assert.equal(stats.latencyP50Ms, 100);
  assert.equal(stats.costPerStep, 0.01);
  assert.deepEqual(stats.unknown, { outcome: 0, cost: 0, tokens: 0 });
});

test("learning: unreported outcomes are counted as unknown", () => {
  const learning = createLearning({});
  learning.record({ model: "opencode/a", role: "builder", success: null, durationMs: null });
  const stats = learning.overall();
  assert.equal(stats.samples, 1);
  assert.equal(stats.successRate, null, "no success samples yet");
  assert.equal(stats.unknown.outcome, 1);
  assert.equal(stats.unknown.cost, 1);
});

test("learning: role-scoped stats need a minimum sample before they apply", () => {
  const learning = createLearning({});
  for (let i = 0; i < 4; i += 1) {
    learning.record({ model: "opencode/a", role: "builder", success: true, durationMs: 10 });
  }
  const scoped = learning.modelStats("opencode/a", { role: "builder" });
  assert.equal(scoped.scope, "role");
  assert.equal(scoped.samples, 4);

  const fallback = learning.modelStats("opencode/a", { role: "visual" });
  assert.equal(fallback.scope, "model", "one unmatched sample is not representative");
});

test("learning: persistence is fire-and-forget and reloads", async () => {
  const store = createStore({ adapter: createMemoryAdapter() });
  const learning = createLearning({ store });
  await learning.load();
  learning.record({ model: "opencode/a", role: "builder", success: true, durationMs: 5 });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(learning.knownModels(), ["opencode/a"]);

  const reloaded = createLearning({ store });
  await reloaded.load();
  assert.equal(reloaded.size, 1);
  assert.equal(reloaded.overall().samples, 1);
});

test("task memory: save, recall by keyword overlap, and reuse", async () => {
  const store = createStore({ adapter: createMemoryAdapter() });
  const memory = createTaskMemory({ store });
  assert.equal(await memory.similar("anything"), null);
  await memory.save({
    id: "task_1",
    at: 1000,
    task: "Research the latest state of the art in vector databases",
    kind: "research",
    workflow: "research_only",
    verified: true,
    outputs: { findings: "HNSW dominates" },
  });
  const recent = await memory.recent();
  assert.equal(recent.length, 1);
  assert.equal(recent[0].verified, true);

  const similar = await memory.similar("Research the latest state of the art in vector databases and summarise");
  assert.ok(similar);
  assert.ok(similar.overlap > 0.6);

  const unrelated = await memory.similar("Bake sourdough bread with a rye starter");
  assert.ok(!unrelated || unrelated.overlap < 0.6);
});

test("task memory: keywords are deterministic and stopword-free", () => {
  const keywords = keywordsOf("Build the widget and then build the widget tests");
  assert.ok(keywords.includes("build"));
  assert.ok(keywords.includes("widget"));
  assert.equal(keywords.includes("the"), false);
});

/**
 * Model catalog + router.
 *
 * The important behaviour to lock in: an unobserved dimension is dropped, not
 * scored as zero; and measured success only moves the route once there is
 * enough evidence.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { buildCatalog, normalizeModel, parseRef, ref } from "../../src/route/catalog.js";
import { createRouter, DEFAULT_WEIGHTS, ROLE_REQUIREMENTS } from "../../src/route/router.js";
import { createLearning } from "../../src/memory/learning.js";
import { FAKE_MODELS } from "../support/fake-context.js";

function clone(model, overrides = {}) {
  return { ...structuredClone(model), ...overrides };
}

test("catalog: parses and formats model references", () => {
  assert.deepEqual(parseRef("opencode/big-pickle"), { providerID: "opencode", id: "big-pickle" });
  assert.deepEqual(parseRef("opencode/glm#thinking"), { providerID: "opencode", id: "glm", variant: "thinking" });
  assert.equal(parseRef("no-slash"), null);
  assert.equal(parseRef(null), null);
  assert.equal(ref({ providerID: "opencode", id: "x" }), "opencode/x");
  assert.equal(ref({}), null);
});

test("catalog: normalises unknown fields to null rather than a favourable default", () => {
  const normalised = normalizeModel({ id: "m", providerID: "p", name: "M" });
  assert.equal(normalised.limit.context, null);
  assert.equal(normalised.cost, null);
  assert.equal(normalised.capabilities.tools, false);
});

test("catalog: usable() filters deprecated and tool-less models", () => {
  const models = [
    ...FAKE_MODELS,
    clone(FAKE_MODELS[0], { id: "old", status: "deprecated" }),
    clone(FAKE_MODELS[0], { id: "blind", capabilities: { tools: false, input: ["text"], output: ["text"] } }),
  ];
  const catalog = buildCatalog(models);
  const usable = catalog.usable({ requireTools: true }).map((model) => model.id);
  assert.equal(usable.includes("old"), false);
  assert.equal(usable.includes("blind"), false);
  assert.equal(catalog.size, 12);
  assert.equal(catalog.get("opencode/big-pickle").id, "big-pickle");
});

test("router: composes the five weighted dimensions with finite defaults", () => {
  assert.equal(Object.values(DEFAULT_WEIGHTS).reduce((a, b) => a + b, 0), 1);
  for (const role of Object.keys(ROLE_REQUIREMENTS)) {
    assert.equal(ROLE_REQUIREMENTS[role].needsTools, true);
  }
});

test("router: an unobserved dimension is dropped, not scored zero", () => {
  const router = createRouter({ models: FAKE_MODELS, learning: createLearning({}) });
  const route = router.route({ needsTools: true, minContext: 0 });
  assert.ok(route);
  assert.ok(route.ref.startsWith("opencode/"));
  assert.ok(route.dropped.includes("latency"));
  assert.ok(route.dropped.includes("success"));
  assert.equal(route.observed.samples, 0);
  assert.equal(route.components.cost, 1, "genuinely free models score 1 on cost");
});

test("router: describe reports availability without throwing", () => {
  const router = createRouter({ models: FAKE_MODELS, learning: createLearning({}) });
  const described = router.describe({ needsTools: true, minContext: 64000 });
  assert.equal(described.available, true);
  assert.ok(described.candidateCount >= 1);
  assert.ok(described.chosen);

  const impossible = createRouter({ models: [], learning: createLearning({}) }).describe({ needsTools: true });
  assert.equal(impossible.available, false);
  assert.ok(impossible.reason);
});

test("router: a model with unreported context is not assumed infinite", () => {
  const router = createRouter({ models: FAKE_MODELS, learning: createLearning({}) });
  const ranked = router.rankCandidates({ needsTools: true, minContext: 200000 });
  assert.ok(ranked.length >= 1);
  for (const entry of ranked) {
    assert.ok(entry.model.limit.context >= 200000);
  }
});

test("router: measured success only wins once there is enough evidence", () => {
  const learning = createLearning({});
  const good = clone(FAKE_MODELS[0], { id: "good", modelID: "good", canonical: "opencode/good" });
  const bad = clone(FAKE_MODELS[0], { id: "bad", modelID: "bad", canonical: "opencode/bad" });

  // One lucky success is not enough: both models still route on capability.
  learning.record({ model: "opencode/good", role: "builder", success: true, durationMs: 100 });
  let router = createRouter({ models: [good, bad], learning });
  const early = router.routeForRole("builder");
  assert.ok(early.dropped.includes("success"), "success is dropped with one sample");

  // Six clean runs for `good`, six failures for `bad`.
  for (let i = 0; i < 6; i += 1) {
    learning.record({ model: "opencode/good", role: "builder", success: true, durationMs: 100, costUSD: 0.01, retries: 0 });
    learning.record({ model: "opencode/bad", role: "builder", success: false, durationMs: 100, costUSD: 0.01, retries: 0 });
  }
  router = createRouter({ models: [good, bad], learning });
  const late = router.routeForRole("builder");
  assert.equal(late.ref, "opencode/good");
  assert.equal(late.dropped.includes("success"), false);
});

test("router: exclusions are honoured", () => {
  const router = createRouter({ models: FAKE_MODELS, learning: createLearning({}), excluded: ["opencode/glm-5.3-flash-free"] });
  const refs = router.candidates({ needsTools: true }).map((model) => ref(model));
  assert.equal(refs.includes("opencode/glm-5.3-flash-free"), false);
});

test("router: fallback chain is bounded and ordered", () => {
  const router = createRouter({ models: FAKE_MODELS, learning: createLearning({}) });
  const chain = router.fallbackChain({ needsTools: true, minContext: 0 }, 3);
  assert.equal(chain.length, 3);
  assert.equal(new Set(chain).size, 3);
});

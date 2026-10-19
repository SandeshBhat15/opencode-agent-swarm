import test from "node:test";
import assert from "node:assert/strict";

import { manualClock } from "../../src/core/clock.js";
import { createJournal } from "../../src/core/journal.js";
import { createMemoryAdapter, createStore } from "../../src/memory/store.js";
import { createLearning } from "../../src/memory/learning.js";
import { createTuner, INITIAL_STATE } from "../../src/improve/tuner.js";
import { propose, PROPOSAL_KINDS } from "../../src/improve/proposals.js";
import { evaluateGates, DEFAULT_POLICY } from "../../src/improve/gates.js";
import { openExperiment, reviewExperiments, judge } from "../../src/improve/experiment.js";
import { runImprovement } from "../../src/improve/loop.js";

function newStore() {
  return createStore({ adapter: createMemoryAdapter() });
}

function seed(records) {
  const learning = createLearning({ store: newStore() });
  for (const record of records) learning.record(record);
  return learning;
}

function outcomes({ model, role = "builder", count, success, retries = 0, costUSD = null, workflow = "build_verify", kind = "build" }) {
  return Array.from({ length: count }, (_, index) => ({
    at: 1_000 + index,
    kind,
    workflow,
    role,
    model,
    success,
    durationMs: 100,
    tokens: 50,
    costUSD,
    retries,
  }));
}

test("tuner applies and reverts a model exclusion", async () => {
  const tuner = createTuner({ store: newStore() });
  await tuner.load();
  assert.deepEqual(tuner.overrides().excluded, []);

  await tuner.apply({ id: "exclude:builder:bad", kind: PROPOSAL_KINDS.MODEL_EXCLUDE, action: { model: "opencode/bad", role: "builder" } });
  assert.deepEqual(tuner.overrides().excluded, ["opencode/bad"]);
  assert.equal(tuner.get().version, 1);

  const result = await tuner.revert("exclude:builder:bad", { reason: "test" });
  assert.equal(result.reverted, true);
  assert.deepEqual(tuner.overrides().excluded, []);
  assert.equal(tuner.history().at(-1).kind, "revert");
});

test("tuner revert restores a previous preferred model instead of clobbering it", async () => {
  const tuner = createTuner({ store: newStore() });
  await tuner.apply({ id: "prefer:builder:good", kind: PROPOSAL_KINDS.MODEL_PREFER, action: { model: "opencode/good", role: "builder" } });
  assert.equal(tuner.overrides().preferred, "opencode/good");

  const reverted = await tuner.revert("prefer:builder:good");
  assert.equal(reverted.reverted, true);
  assert.equal(tuner.overrides().preferred, null);
});

test("tuner retry policy is reflected in overrides, and absent when unset", async () => {
  const tuner = createTuner({ store: newStore() });
  assert.deepEqual(tuner.overrides().retry, {});

  await tuner.apply({ id: "retry:reduce", kind: PROPOSAL_KINDS.RETRY_POLICY, action: { maxRetries: 1, previous: 2 } });
  assert.deepEqual(tuner.overrides().retry, { maxRetries: 1 });

  // Reverting restores the recorded previous value (2 is the engine default).
  await tuner.revert("retry:reduce");
  assert.deepEqual(tuner.overrides().retry, { maxRetries: 2 });
});

test("tuner state is seeded from the frozen initial state", () => {
  const tuner = createTuner({ store: newStore() });
  assert.deepEqual(tuner.get(), INITIAL_STATE);
});

test("tuner workflow preference and avoidance key off the task kind", async () => {
  const tuner = createTuner({ store: newStore() });
  await tuner.apply({ id: "workflow:prefer:build:direct", kind: PROPOSAL_KINDS.WORKFLOW_PREFER, action: { kind: "build", workflow: "direct" } });
  assert.equal(tuner.overrides().workflowPreference("build"), "direct");
  assert.equal(tuner.overrides().workflowPreference("research"), null);

  await tuner.apply({ id: "workflow:avoid:build:build_verify", kind: PROPOSAL_KINDS.WORKFLOW_AVOID, action: { kind: "build", workflow: "build_verify" } });
  assert.equal(tuner.overrides().workflowAvoid("build"), "build_verify");
});

test("proposals only cite the samples they rest on", () => {
  const learning = seed([
    ...outcomes({ model: "opencode/bad", count: 10, success: false, retries: 1 }),
    ...outcomes({ model: "opencode/good", count: 10, success: true }),
  ]);

  const proposals = propose({ learning });
  const exclusion = proposals.find((entry) => entry.id === "exclude:builder:opencode/bad");
  assert.ok(exclusion, "excludes the failing model");
  assert.equal(exclusion.kind, "model_exclude");
  assert.equal(exclusion.evidence.samples, 10);
  assert.equal(exclusion.evidence.value, 0);
  assert.equal(exclusion.reversible, true);

  const preference = proposals.find((entry) => entry.id === "prefer:builder:opencode/good");
  assert.ok(preference, "prefers the succeeding model");
  assert.equal(preference.kind, "model_prefer");
  assert.ok(Number.isFinite(preference.action.weight));
});

test("proposals do not fire below the sample floor", () => {
  const learning = seed([
    ...outcomes({ model: "opencode/bad", count: 3, success: false }),
    ...outcomes({ model: "opencode/good", count: 3, success: true }),
  ]);
  const proposals = propose({ learning });
  assert.equal(proposals.filter((entry) => entry.kind === "model_exclude").length, 0);
});

test("proposals never propose excluding an already excluded model", () => {
  const learning = seed([
    ...outcomes({ model: "opencode/bad", count: 10, success: false }),
    ...outcomes({ model: "opencode/good", count: 10, success: true }),
  ]);
  const tuner = createTuner({ store: newStore() });
  const proposals = propose({ learning, tuner: { get: () => ({ router: { excluded: ["opencode/bad"] } }) } });
  assert.equal(proposals.some((entry) => entry.id === "exclude:builder:opencode/bad"), false);
  assert.ok(tuner.get());
});

test("retry reduction is proposed from measured retry pressure", () => {
  const learning = seed([
    ...outcomes({ model: "opencode/a", count: 8, success: true, retries: 1 }),
    ...outcomes({ model: "opencode/b", count: 8, success: false, retries: 1 }),
  ]);
  const proposals = propose({ learning });
  const retry = proposals.find((entry) => entry.id === "retry:reduce");
  assert.ok(retry);
  assert.equal(retry.kind, "retry_policy");
  assert.equal(retry.action.maxRetries, 1);
});

test("workflow preference is proposed only when a cheaper workflow matches on success", () => {
  const learning = seed([
    ...outcomes({ model: "opencode/a", count: 8, success: true, costUSD: 0.001, workflow: "cheap_flow" }),
    ...outcomes({ model: "opencode/a", count: 8, success: true, costUSD: 0.01, workflow: "pricey_flow" }),
  ]);
  const proposals = propose({ learning });
  const workflow = proposals.find((entry) => entry.kind === "workflow_prefer");
  assert.ok(workflow, "proposes the cheaper matching workflow");
  assert.equal(workflow.action.workflow, "cheap_flow");
});

test("a thin proposal is rejected by the samples gate with a stated reason", () => {
  const proposal = { id: "exclude:builder:x", kind: "model_exclude", evidence: { samples: 2, value: 0.1, baseline: 0.9 }, action: { model: "x" }, reversible: true };
  const verdict = evaluateGates(proposal);
  assert.equal(verdict.allowed, false);
  assert.ok(verdict.reasons.some((reason) => reason.startsWith("samples:")));
});

test("an oversized weight change is rejected as unbounded", () => {
  const proposal = {
    id: "prefer:builder:x",
    kind: "model_prefer",
    evidence: { samples: 10, value: 0.9, baseline: 0.8 },
    action: { model: "x", weight: 9 },
    reversible: true,
  };
  const verdict = evaluateGates(proposal);
  assert.equal(verdict.allowed, false);
  assert.equal(verdict.gates.find((gate) => gate.id === "bounded").passed, false);
});

test("an irreversible proposal is rejected", () => {
  const proposal = { id: "retry:reduce", kind: "retry_policy", evidence: { samples: 10, value: 0.6, baseline: 0.5 }, action: { maxRetries: 1 }, reversible: false };
  const verdict = evaluateGates(proposal);
  assert.equal(verdict.allowed, false);
  assert.equal(verdict.gates.find((gate) => gate.id === "reversible").passed, false);
});

test("capability loss blocks excluding the last usable model", () => {
  const learning = seed(outcomes({ model: "opencode/only", count: 10, success: false }));
  const proposal = {
    id: "exclude:builder:opencode/only",
    kind: "model_exclude",
    evidence: { samples: 10, value: 0, baseline: 0.9 },
    action: { model: "opencode/only", role: "builder" },
    reversible: true,
  };
  const verdict = evaluateGates(proposal, { learning });
  assert.equal(verdict.allowed, false);
  assert.equal(verdict.gates.find((gate) => gate.id === "capability_preserved").passed, false);
});

test("an applied proposal is not applied twice, and a reverted one becomes eligible again", async () => {
  const tuner = createTuner({ store: newStore() });
  const learning = seed([
    ...outcomes({ model: "opencode/bad", count: 10, success: false }),
    ...outcomes({ model: "opencode/good", count: 10, success: true }),
  ]);
  const proposal = {
    id: "exclude:builder:opencode/bad",
    kind: "model_exclude",
    evidence: { samples: 10, value: 0, baseline: 1 },
    action: { model: "opencode/bad", role: "builder" },
    reversible: true,
  };

  assert.equal(evaluateGates(proposal, { learning, tuner }).allowed, true);
  await tuner.apply(proposal);
  assert.equal(evaluateGates(proposal, { learning, tuner }).gates.find((gate) => gate.id === "not_duplicate").passed, false);

  await tuner.revert(proposal.id);
  assert.equal(evaluateGates(proposal, { learning, tuner }).gates.find((gate) => gate.id === "not_duplicate").passed, true);
});

test("approval is only demanded when the policy demands it", () => {
  const proposal = { id: "retry:reduce", kind: "retry_policy", evidence: { samples: 10, value: 0.6, baseline: 0.5 }, action: { maxRetries: 1 }, reversible: true };
  assert.equal(evaluateGates(proposal).allowed, true);
  const guarded = evaluateGates(proposal, { policy: { requireApproval: true } });
  assert.equal(guarded.allowed, false);
  assert.equal(evaluateGates(proposal, { policy: { requireApproval: true }, approvals: ["retry:reduce"] }).allowed, true);
});

test("gate policy defaults are the frozen, documented ones", () => {
  assert.deepEqual(DEFAULT_POLICY, {
    minSamples: 8,
    minEffect: 0.1,
    maxChangesPerRun: 1,
    minAlternatives: 1,
    requireApproval: false,
    weightBand: [0.75, 1.5],
    retryRange: [0, 5],
  });
});

test("an experiment is confirmed when the change keeps performing", async () => {
  const clock = manualClock(1_000);
  const tuner = createTuner({ store: newStore(), clock });
  const learning = seed(outcomes({ model: "opencode/bad", count: 6, success: true }));
  const journal = createJournal({ now: () => clock.now() });

  const proposal = { id: "exclude:builder:opencode/bad", kind: "model_exclude", evidence: { samples: 8, metric: "successRate", value: 0.1, baseline: 0.5 }, action: { model: "opencode/bad" } };
  const experiment = openExperiment({ proposal, learning, clock });
  await tuner.apply(proposal, { experiment });

  for (let index = 0; index < 6; index += 1) {
    learning.record({ at: 2_000 + index, kind: "build", role: "builder", model: "opencode/bad", success: true });
  }
  const reviews = await reviewExperiments({ tuner, learning, journal, clock });
  assert.equal(reviews[0].status, "confirmed");
  assert.equal(tuner.experiments()[0].status, "confirmed");
  assert.equal(journal.count("improve") >= 1, true);
});

test("an experiment is reverted when the change does not hold up", async () => {
  const clock = manualClock(1_000);
  const tuner = createTuner({ store: newStore(), clock });
  const learning = seed([
    ...outcomes({ model: "opencode/x", count: 2, success: true }),
    ...outcomes({ model: "opencode/x", count: 4, success: false }),
  ]);
  const journal = createJournal({ now: () => clock.now() });

  const proposal = { id: "exclude:builder:opencode/x", kind: "model_exclude", evidence: { samples: 8, metric: "successRate", value: 0.9, baseline: 0.5 }, action: { model: "opencode/x" } };
  const experiment = openExperiment({ proposal, learning, clock });
  await tuner.apply(proposal, { experiment });

  for (let index = 0; index < 6; index += 1) {
    learning.record({ at: 3_000 + index, kind: "build", role: "builder", model: "opencode/x", success: false });
  }
  const reviews = await reviewExperiments({ tuner, learning, journal, clock });
  assert.equal(reviews[0].status, "reverted");
  assert.equal(tuner.experiments()[0].status, "reverted");
  assert.deepEqual(tuner.overrides().excluded, []);
});

test("a cost experiment is kept when no cost regression is measurable", () => {
  const learning = seed(outcomes({ model: "opencode/a", count: 8, success: true }));
  const experiment = { direction: "cost_down", baseline: 0.001, minSamples: 6 };
  const verdict = judge(experiment, learning);
  assert.equal(verdict.held, true);
  assert.equal(verdict.metric, "costPerStep");
});

test("the improvement loop applies at most the policy limit and opens a bounded experiment", async () => {
  const clock = manualClock(500);
  const tuner = createTuner({ store: newStore(), clock });
  const journal = createJournal({ now: () => clock.now() });
  const learning = seed([
    ...outcomes({ model: "opencode/bad", count: 10, success: false }),
    ...outcomes({ model: "opencode/good", count: 10, success: true }),
  ]);

  const result = await runImprovement({ learning, tuner, journal, clock, policy: { minSamples: 8, minEffect: 0.1, maxChangesPerRun: 1 } });
  assert.ok(result.proposals > 0);
  assert.equal(result.applied.length, 1);
  assert.equal(tuner.history().length, 1);
  assert.equal(tuner.experiments().length, 1);
  assert.equal(tuner.experiments()[0].status, "running");
  assert.ok(result.applied[0].experiment);
});

test("a dry run proposes without touching the tuner", async () => {
  const tuner = createTuner({ store: newStore() });
  const learning = seed([
    ...outcomes({ model: "opencode/bad", count: 10, success: false }),
    ...outcomes({ model: "opencode/good", count: 10, success: true }),
  ]);
  const result = await runImprovement({ learning, tuner, dryRun: true });
  assert.equal(result.dryRun, true);
  assert.equal(result.applied.length, 0);
  assert.equal(tuner.history().length, 0);
  assert.ok(result.proposals > 0);
});

test("rejected proposals carry the gate reason they failed", async () => {
  const tuner = createTuner({ store: newStore() });
  const learning = seed([
    ...outcomes({ model: "opencode/bad", count: 10, success: false }),
    ...outcomes({ model: "opencode/good", count: 10, success: true }),
  ]);
  const result = await runImprovement({ learning, tuner, policy: { minSamples: 8, minEffect: 0.1, requireApproval: true } });
  assert.ok(result.rejected.length > 0);
  assert.ok(result.rejected[0].reasons.length > 0);
  assert.equal(tuner.history().length, 0);
});

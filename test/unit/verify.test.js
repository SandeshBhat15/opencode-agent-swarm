/**
 * Verification engine: deterministic layer first, model judgement only when
 * required, and honest reporting when independence is impossible.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { evaluateCriterion, evaluateAll, needsModelJudgement } from "../../src/verify/criteria.js";
import { verify, buildVerdictPrompt, parseVerdict } from "../../src/verify/verifier.js";
import { createRouter } from "../../src/route/router.js";
import { createLearning } from "../../src/memory/learning.js";
import { createJournal, JOURNAL_KINDS } from "../../src/core/journal.js";
import { createBudget } from "../../src/core/budget.js";
import { FAKE_MODELS } from "../support/fake-context.js";

const nonEmpty = { id: "c_deliverable", text: "Deliverable exists", check: { type: "nonEmpty", target: "output" } };
const verdictCriterion = { id: "c_verified", text: "Independent check", check: { type: "verdict", target: "verdict" } };

function runtimeReturning(text) {
  return {
    calls: [],
    async delegate(input) {
      this.calls.push(input);
      return { text, usage: { tokens: 20, costUSD: 0.002, toolCalls: 1 }, model: input.model ?? "opencode/other", agent: input.agent };
    },
  };
}

test("criteria: nonEmpty, mentions and regex are decided by rules", () => {
  assert.equal(evaluateCriterion(nonEmpty, { output: "real work" }).passed, true);
  assert.equal(evaluateCriterion(nonEmpty, { output: "   " }).passed, false);
  assert.equal(
    evaluateCriterion({ check: { type: "mentionsAll", target: "output", values: ["alpha", "beta"] } }, { output: "alpha and beta" }).passed,
    true,
  );
  assert.equal(
    evaluateCriterion({ check: { type: "mentionsAll", target: "output", values: ["alpha", "gamma"] } }, { output: "alpha and beta" }).passed,
    false,
  );
  assert.equal(evaluateCriterion({ check: { type: "regex", target: "output", pattern: "v\\d+\\.\\d+" } }, { output: "released v2.1" }).passed, true);
  assert.equal(evaluateCriterion({ check: { type: "artifacts", min: 1 } }, { artifacts: [] }).passed, false);
});

test("criteria: a missing verdict never counts as a pass", () => {
  assert.equal(evaluateCriterion(verdictCriterion, {}).passed, false);
  assert.equal(evaluateCriterion(verdictCriterion, { verdict: { independence: "unavailable", passed: true } }).passed, false);
  assert.equal(evaluateCriterion(verdictCriterion, { verdict: { independence: "different_model", passed: true } }).passed, true);
});

test("criteria: evaluateAll aggregates and needsModelJudgement is precise", () => {
  const outcome = evaluateAll([nonEmpty, verdictCriterion], { output: "ok" });
  assert.equal(outcome.total, 2);
  assert.equal(outcome.passedCount, 1);
  assert.equal(outcome.passed, false);
  assert.equal(needsModelJudgement([nonEmpty]), false);
  assert.equal(needsModelJudgement([nonEmpty, verdictCriterion]), true);
});

test("verifier: deterministic criteria alone pass without any model call", async () => {
  const runtime = runtimeReturning("{}");
  const plan = { id: "p", task: "t", criteria: [nonEmpty] };
  const result = await verify(plan, { output: "the deliverable" }, { runtime });
  assert.equal(result.passed, true);
  assert.equal(result.independence, "not_required");
  assert.equal(runtime.calls.length, 0);
});

test("verifier: a decisive deterministic failure skips the model call", async () => {
  const runtime = runtimeReturning('{"passed": true}');
  const journal = createJournal();
  const plan = { id: "p", task: "t", criteria: [nonEmpty, verdictCriterion] };
  const result = await verify(plan, { output: "" }, { runtime, journal });
  assert.equal(result.passed, false);
  assert.equal(runtime.calls.length, 0, "no model call is spent on an already-failed delivery");
  assert.equal(journal.of(JOURNAL_KINDS.VERIFY).some((entry) => entry.data.skipped), true);
});

test("verifier: missing delegation is reported as unavailable, not a pass", async () => {
  const plan = { id: "p", task: "t", criteria: [nonEmpty, verdictCriterion] };
  const result = await verify(plan, { output: "work" }, { runtime: null });
  assert.equal(result.passed, false);
  assert.equal(result.independence, "unavailable");
  assert.match(result.reason, /delegation/i);
});

test("verifier: an independent model judgement can pass the delivery", async () => {
  const runtime = runtimeReturning('Here is my answer.\n{"passed": true, "reason": "criteria met", "failed": []}');
  const router = createRouter({ models: [FAKE_MODELS[2]], learning: createLearning({}) });
  const plan = { id: "p", task: "do a thing", criteria: [nonEmpty, verdictCriterion] };
  const result = await verify(plan, { output: "the work" }, {
    runtime,
    router,
    producerModel: "opencode/producer-model",
  });
  assert.equal(result.passed, true);
  assert.equal(result.independence, "different_model");
  assert.equal(runtime.calls[0].agent, "swarm/qc");
});

test("verifier: a same-model check is labelled honestly", async () => {
  const runtime = runtimeReturning('{"passed": true}');
  const router = createRouter({ models: [FAKE_MODELS[2]], learning: createLearning({}) });
  const plan = { id: "p", task: "t", criteria: [nonEmpty, verdictCriterion] };
  const result = await verify(plan, { output: "work" }, { runtime, router, producerModel: "opencode/nemotron-3-ultra-free" });
  assert.equal(result.passed, true);
  assert.equal(result.independence, "same_model_check");
});

test("verifier: a failed independent judgement fails the delivery", async () => {
  const runtime = runtimeReturning('{"passed": false, "reason": "no artefact found", "failed": ["c_verified"]}');
  const router = createRouter({ models: [FAKE_MODELS[2]], learning: createLearning({}) });
  const plan = { id: "p", task: "t", criteria: [nonEmpty, verdictCriterion] };
  const result = await verify(plan, { output: "some text" }, { runtime, router, producerModel: "opencode/other" });
  assert.equal(result.passed, false);
  assert.match(result.reason, /no artefact found/);
});

test("verifier: an exhausted budget refuses the judge call", async () => {
  const runtime = runtimeReturning('{"passed": true}');
  const router = createRouter({ models: [FAKE_MODELS[2]], learning: createLearning({}) });
  const plan = { id: "p", task: "t", criteria: [nonEmpty, verdictCriterion] };
  const budget = createBudget({ maxTokens: 10 });
  budget.charge({ tokens: 50 });

  const result = await verify(plan, { output: "work" }, {
    runtime,
    router,
    budget,
    producerModel: "opencode/producer",
  });

  assert.equal(result.passed, false);
  assert.equal(result.independence, "unavailable");
  assert.match(result.reason, /budget/i);
  // The judge is a model call like any other: an exhausted budget buys nothing.
  assert.equal(runtime.calls.length, 0);
});

test("verdict parsing: JSON preferred, keyword fallback, no false positives", () => {
  assert.equal(parseVerdict('text\n{"passed": false, "reason": "nope"}').passed, false);
  assert.equal(parseVerdict('{"passed": true}').passed, true);
  assert.equal(parseVerdict("The work is verified and confirmed.").passed, true);
  assert.equal(parseVerdict("").passed, false);
  assert.equal(parseVerdict("I could not tell.").passed, false);

  const prompt = buildVerdictPrompt({ id: "p", task: "Ship the parser" }, { output: "done" }, [nonEmpty], 1000);
  assert.match(prompt, /Ship the parser/);
  assert.match(prompt, /c_deliverable/);
  assert.match(prompt, /"passed"/);
});

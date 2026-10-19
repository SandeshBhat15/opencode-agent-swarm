/**
 * Decision engine: deterministic signals, classification, workflow selection,
 * planning. None of this is allowed to call a model.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { extractSignals } from "../../src/decide/signals.js";
import { classify, KINDS } from "../../src/decide/classifier.js";
import { ROLES, WORKFLOWS, selectWorkflow, workflowById } from "../../src/decide/workflows.js";
import { deriveCriteria, topKeywords, plan, isSpecialist } from "../../src/decide/planner.js";
import { createRouter } from "../../src/route/router.js";
import { createLearning } from "../../src/memory/learning.js";
import { FAKE_MODELS } from "../support/fake-context.js";

const router = () =>
  createRouter({ models: FAKE_MODELS, learning: createLearning({}) });

test("signals: detects research intent and long-form shape", () => {
  const signals = extractSignals("Research the latest WebAssembly tooling and compare the options, then report pros and cons");
  assert.ok(signals.domains.research.length > 0);
  assert.ok(signals.length > 40);
  assert.equal(signals.domainCount >= 1, true);
});

test("signals: detects file paths and urls", () => {
  const signals = extractSignals("Refactor /tmp/app/main.js and C:\\work\\api.py, see https://example.com/spec");
  assert.equal(signals.hasPaths, true);
  assert.equal(signals.hasUrls, true);
  assert.ok(signals.paths.some((p) => p.includes("main.js")));
});

test("signals: flags risk and ambiguity", () => {
  // "deploy", "production" and "credential" are three distinct risk terms.
  assert.equal(extractSignals("Deploy to production and rotate the credential").riskLevel, "high");
  assert.equal(extractSignals("Deploy the service for the team").riskLevel, "medium");
  assert.equal(extractSignals("Delete the production database and drop table users").riskLevel, "high");
  assert.equal(extractSignals("Do something, maybe whatever, etc").ambiguityLevel, "high");
});

test("signals: terms match whole words, not substrings", () => {
  // "build" contains "ui" and "latest" contains "test": neither is a real hit.
  const build = extractSignals("Build a REST API in Node with tests");
  assert.deepEqual(build.domains.visual, []);
  assert.deepEqual(extractSignals("Research the latest state of the art").domains.verify, []);
  // ...and the real words still match, including ordinary inflections.
  assert.ok(build.domains.verify.includes("test"));
  assert.deepEqual(extractSignals("Design a landing page").domains.visual, ["design", "landing page"]);
  assert.ok(extractSignals("Parse the CSV file").domains.data.includes("parse"));
});

test("classifier: trivial question takes the cheap path", () => {
  const result = classify("What is a monad?");
  assert.equal(result.kind, KINDS.TRIVIAL);
  assert.equal(result.needs.build, false);
  assert.ok(result.confidence >= 0.2 && result.confidence <= 0.98);
});

test("classifier: build request needs build and verification", () => {
  const result = classify("Build a REST API in Node with tests");
  assert.equal(result.kind, KINDS.BUILD);
  assert.equal(result.needs.build, true);
  assert.equal(result.needs.verify, true);
});

test("classifier: research request stays research", () => {
  const result = classify("Research the latest state of the art in vector databases and summarize what you find");
  assert.equal(result.kind, KINDS.RESEARCH);
  assert.equal(result.needs.research, true);
});

test("classifier: multi-domain request becomes mixed", () => {
  const result = classify("Research options, build a prototype, design the UI, parse the CSV data, then test everything");
  assert.equal(result.kind, KINDS.MIXED);
  assert.ok(result.complexity > 0.3);
});

test("workflows: cheapest sufficient flow is selected by a table", () => {
  assert.equal(selectWorkflow(classify("What is a monad?")).id, "direct");
  assert.equal(
    selectWorkflow(classify("Research the latest state of the art in vector databases and summarize what you find")).id,
    "research_only",
  );
  assert.equal(selectWorkflow(classify("Build a REST API in Node with tests")).id, "build_verify");
});

test("workflows: every workflow has concrete steps and a producer", () => {
  for (const flow of Object.values(WORKFLOWS)) {
    assert.ok(flow.id);
    assert.ok(flow.steps.length >= 1);
    for (const step of flow.steps) {
      assert.ok(step.id);
      assert.ok(step.produces);
    }
  }
  assert.equal(workflowById("nope"), null);
  assert.equal(workflowById("direct").id, "direct");
});

test("planner: derives objective criteria from the task itself", () => {
  const classification = classify("Refactor /tmp/app/main.js and keep the tests passing");
  const criteria = deriveCriteria(classification.signals.text, classification);
  const ids = criteria.map((criterion) => criterion.id);
  assert.ok(ids.includes("c_deliverable"));
  assert.ok(ids.includes("c_paths"), "a referenced path must be checked");
  assert.ok(criteria.every((criterion) => criterion.check && criterion.check.type));
});

test("planner: plan is a concrete, runnable structure", () => {
  const classification = classify("Research the latest state of the art and summarize");
  const flow = selectWorkflow(classification);
  const built = plan(classification.signals.text, { classification, workflow: flow, router: router() });
  assert.equal(built.workflow, flow.id);
  assert.equal(built.steps.length, flow.steps.length);
  assert.ok(built.criteria.length >= 1);
  assert.ok(built.id.startsWith("plan_"));
  assert.equal(built.routerHint.available, true);
  for (const step of built.steps) {
    assert.equal(step.status, "pending");
    assert.ok(Array.isArray(step.needs));
  }
});

test("planner: isSpecialist distinguishes direct work from delegation", () => {
  assert.equal(isSpecialist(ROLES.NONE), false);
  assert.equal(isSpecialist(ROLES.RESEARCHER), true);
  assert.equal(isSpecialist(ROLES.QC), true);
});

test("planner: keyword extraction ignores stopwords", () => {
  const keywords = topKeywords("Build the build script for the widget and the widget tests", 6);
  assert.ok(keywords.includes("build") || keywords.includes("widget"));
  assert.equal(keywords.includes("the"), false);
});

/**
 * Core primitives: Result, errors, clock, budget, journal, metrics, logger.
 *
 * The budget and metrics tests exist to lock in one rule from the brief:
 * missing telemetry is never treated as zero.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { ok, err, isOk, isErr, map, mapErr, unwrap, unwrapOr, attempt } from "../../src/core/result.js";
import {
  SwarmError,
  BudgetExceededError,
  TimeoutError,
  VerificationError,
  CapabilityError,
  ConfigError,
  CycleError,
  CancelledError,
} from "../../src/core/errors.js";
import { systemClock, manualClock } from "../../src/core/clock.js";
import { DEFAULT_LIMITS, createBudget } from "../../src/core/budget.js";
import { createJournal, JOURNAL_KINDS } from "../../src/core/journal.js";
import { summarize } from "../../src/core/metrics.js";
import { createLogger, silentLogger } from "../../src/core/logger.js";

test("result: ok/err and predicates", () => {
  assert.equal(isOk(ok(1)), true);
  assert.equal(isErr(ok(1)), false);
  assert.equal(isOk(err(new Error("x"))), false);
  assert.equal(isErr(err(new Error("x"))), true);
  assert.deepEqual(ok(5), { ok: true, value: 5 });
});

test("result: map and mapErr only touch the matching side", () => {
  assert.equal(unwrap(map(ok(2), (v) => v * 3)), 6);
  const failed = err(new Error("boom"));
  assert.equal(isErr(map(failed, () => 1)), true);
  assert.equal(mapErr(failed, () => "mapped").error, "mapped");
  assert.equal(unwrapOr(map(ok(7), (v) => v), 0), 7);
});

test("result: unwrap throws the original error", () => {
  const source = new Error("original");
  assert.throws(() => unwrap(err(source)), (error) => error === source);
});

test("result: attempt converts throws into failed results", async () => {
  const good = await attempt(async () => 42);
  assert.deepEqual(good, { ok: true, value: 42 });
  const boom = await attempt(async () => {
    throw new Error("nope");
  });
  assert.equal(isErr(boom), true);
  assert.equal(boom.error.message, "nope");
  const nonError = await attempt(async () => {
    throw "string failure";
  });
  assert.equal(nonError.error instanceof Error, true);
});

test("errors: every error carries a stable code", () => {
  assert.equal(new SwarmError("x").code, "swarm_error");
  assert.equal(new BudgetExceededError("tokens", { limit: 10, used: 11 }).code, "budget_exceeded");
  assert.equal(new TimeoutError("late").code, "timeout");
  assert.equal(new VerificationError("bad").code, "verification_failed");
  assert.equal(new CapabilityError("delegate").code, "capability_unavailable");
  assert.equal(new ConfigError("bad config").code, "config_error");
  assert.equal(new CycleError(["a", "b"]).code, "dependency_cycle");
  assert.equal(new CancelledError().code, "cancelled");
});

test("errors: structured detail is preserved", () => {
  const budget = new BudgetExceededError("costUSD", { limit: 2, used: 3 });
  assert.equal(budget.dimension, "costUSD");
  assert.equal(budget.limit, 2);
  assert.equal(budget.used, 3);
  assert.match(budget.message, /costUSD/);

  const capability = new CapabilityError("delegate");
  assert.equal(capability.capability, "delegate");

  const cycle = new CycleError(["x", "y"]);
  assert.deepEqual(cycle.nodes, ["x", "y"]);

  const verification = new VerificationError("failed", { failures: [{ id: "c1" }] });
  assert.deepEqual(verification.failures, [{ id: "c1" }]);
});

test("clock: systemClock reports increasing time and honours delays", async () => {
  const clock = systemClock();
  const first = clock.now();
  assert.equal(Number.isFinite(first), true);
  await clock.delay(5);
  assert.ok(clock.now() >= first);
});

test("clock: manual clock only advances on demand", async () => {
  const clock = manualClock(1000);
  assert.equal(clock.now(), 1000);
  let fired = false;
  const pending = clock.delay(50).then(() => {
    fired = true;
  });
  assert.equal(fired, false);
  clock.advance(50);
  await pending;
  assert.equal(fired, true);
  assert.equal(clock.now(), 1050);
});

test("budget: defaults are finite in every dimension", () => {
  for (const [key, value] of Object.entries(DEFAULT_LIMITS)) {
    assert.equal(Number.isFinite(value), true, `${key} must be finite`);
    assert.ok(value > 0, `${key} must be positive`);
  }
});

test("budget: observed usage accumulates", () => {
  const budget = createBudget({ maxTokens: 1000, maxCostUSD: 5 });
  budget.charge({ tokens: 100, costUSD: 0.25, ms: 10, toolCalls: 2, steps: 1 });
  budget.charge({ tokens: 50, costUSD: 0.5, ms: 5, toolCalls: 1, steps: 1 });
  assert.equal(budget.used.tokens, 150);
  assert.equal(budget.used.costUSD, 0.75);
  assert.equal(budget.used.toolCalls, 3);
  assert.equal(budget.used.steps, 2);
  assert.equal(budget.unknown.tokens, 0);
  assert.equal(budget.check().ok, true);
});

test("budget: unreported usage is unknown, never zero", () => {
  const budget = createBudget({ maxTokens: 1000 });
  budget.charge({ tokens: null, costUSD: undefined, ms: undefined });
  budget.charge({ tokens: null });
  // an omitted field is "not reported at all", not "reported as missing"
  budget.charge({ tokens: undefined });
  assert.equal(budget.used.tokens, 0, "no value was observed");
  assert.equal(budget.unknown.tokens, 2, "two samples lacked telemetry");
  assert.equal(budget.unknown.costUSD, 0);
  // An explicit measured zero is not the same as missing telemetry.
  budget.charge({ tokens: 0 });
  assert.equal(budget.used.tokens, 0);
  assert.equal(budget.unknown.tokens, 2);
});

test("budget: check reports exhaustion with the right dimension", () => {
  const budget = createBudget({ maxTokens: 10 });
  budget.charge({ tokens: 11 });
  const outcome = budget.check();
  assert.equal(outcome.ok, false);
  assert.equal(outcome.error.code, "budget_exceeded");
  assert.equal(outcome.error.dimension, "tokens");
});

test("budget: remaining and snapshot are bounded below by zero", () => {
  const budget = createBudget({ maxToolCalls: 3 });
  budget.charge({ toolCalls: 5 });
  assert.equal(budget.remaining().toolCalls, 0);
  const snapshot = budget.snapshot();
  assert.equal(snapshot.limits.maxToolCalls, 3);
  assert.equal(snapshot.used.toolCalls, 5);
  assert.deepEqual(snapshot.unknown, { tokens: 0, costUSD: 0, ms: 0 });
});

test("journal: append preserves order and kinds", () => {
  const journal = createJournal({ now: () => 7 });
  journal.append(JOURNAL_KINDS.TASK, { task: "a" });
  journal.append(JOURNAL_KINDS.DECISION, { strategy: "deterministic" });
  journal.append(JOURNAL_KINDS.DECISION, { strategy: "model" });
  assert.equal(journal.size, 3);
  assert.equal(journal.count(JOURNAL_KINDS.DECISION), 2);
  assert.equal(journal.of(JOURNAL_KINDS.DECISION)[0].data.strategy, "deterministic");
  assert.equal(journal.all()[0].seq, 1);
  assert.equal(journal.since(7).length, 3);
  assert.equal(journal.toJSON().length, 3);
});

test("metrics: efficiency ratios come from observed journal data", () => {
  const budget = createBudget({ maxTokens: 1000 });
  const journal = createJournal();
  journal.append(JOURNAL_KINDS.DECISION, { strategy: "deterministic" });
  journal.append(JOURNAL_KINDS.DECISION, { strategy: "model" });
  journal.append(JOURNAL_KINDS.STEP_END, { durationMs: 10, status: "ok" });
  journal.append(JOURNAL_KINDS.DIRECT, {});
  journal.append(JOURNAL_KINDS.VERIFY, { passed: true });
  journal.append(JOURNAL_KINDS.USAGE, { tokens: 100, costUSD: 0.1, toolCalls: 2, model: { providerID: "opencode", id: "big-pickle" } });

  const metrics = summarize(journal.all(), { budget });
  assert.equal(metrics.counts.decisions, 2);
  assert.equal(metrics.counts.deterministicResolutions, 1);
  assert.equal(metrics.efficiency.deterministicShare, 0.5);
  assert.equal(metrics.efficiency.verificationPassRate, 1);
  assert.equal(metrics.usage.tokens, 100);
  assert.equal(metrics.usage.tokensObserved, true);
  assert.equal(metrics.models[0].model, "opencode/big-pickle");
  assert.equal(metrics.efficiency.tokensPerVerifiedStep, 100);
});

test("metrics: unobserved tokens are reported as not observed", () => {
  const journal = createJournal();
  journal.append(JOURNAL_KINDS.STEP_END, { durationMs: 3 });
  const metrics = summarize(journal.all());
  assert.equal(metrics.usage.tokensObserved, false);
  assert.equal(metrics.usage.costObserved, false);
  assert.equal(metrics.efficiency.costPerVerifiedStep, null);
  assert.equal(metrics.efficiency.tokensPerVerifiedStep, null);
});

test("logger: silent logger never writes and child scoping works", () => {
  const lines = [];
  const logger = createLogger({ level: "info", sink: (line) => lines.push(line), scope: "swarm" });
  logger.info("hello", { a: 1 });
  logger.debug("hidden at info level");
  logger.child("child").warn("careful");
  assert.equal(lines.length, 2);
  assert.match(lines[0], /hello/);
  assert.match(lines[0], /swarm/);
  assert.match(lines[1], /child/);
  assert.doesNotThrow(() => silentLogger.error("ignored"));
});

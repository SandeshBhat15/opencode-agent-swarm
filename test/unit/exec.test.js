/**
 * Execution engine: scheduling, bounded concurrency, diagnosed retries,
 * timeouts, dependency skipping, budget enforcement and memory reuse.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { buildSchedule, createGate, dependentsOf } from "../../src/exec/scheduler.js";
import { defaultDiagnose, executePlan } from "../../src/exec/executor.js";
import { SPECIALISTS, specialistFor, isSpecialistRole, buildStepPrompt, truncate, stringify } from "../../src/exec/specialists.js";
import { createBudget } from "../../src/core/budget.js";
import { createJournal, JOURNAL_KINDS } from "../../src/core/journal.js";

const step = (id, over = {}) => ({ id, role: "builder", produces: id, needs: [], parallel: null, ...over });

test("scheduler: dependency levels become ordered groups", () => {
  const schedule = buildSchedule([
    step("research"),
    step("build", { needs: ["research"] }),
    step("verify", { needs: ["build"] }),
  ]);
  assert.equal(schedule.groups.length, 3);
  assert.equal(schedule.depth.build, 1);
  assert.deepEqual(schedule.order, ["research", "build", "verify"]);
});

test("scheduler: independent steps share a group, direct work stays serial", () => {
  const schedule = buildSchedule([
    step("a", { parallel: "wave1" }),
    step("b", { parallel: "wave1" }),
    step("direct", { role: "none" }),
  ], { maxConcurrency: 3 });
  const concurrentGroups = schedule.groups.filter((group) => group.length > 1);
  assert.equal(concurrentGroups.length, 1);
  assert.deepEqual(concurrentGroups[0].map((s) => s.id).sort(), ["a", "b"]);
  const directGroup = schedule.groups.find((group) => group.some((s) => s.id === "direct"));
  assert.equal(directGroup.length, 1, "role-less steps never run concurrently");
});

test("scheduler: a dependency cycle is rejected, not silently accepted", () => {
  assert.throws(
    () => buildSchedule([step("a", { needs: ["b"] }), step("b", { needs: ["a"] })]),
    (error) => error.code === "dependency_cycle",
  );
});

test("scheduler: dependents of a failure are identified transitively", () => {
  const steps = [step("a"), step("b", { needs: ["a"] }), step("c", { needs: ["b"] }), step("d")];
  const skipped = [...dependentsOf(steps, ["a"])].sort();
  assert.deepEqual(skipped, ["b", "c"]);
});

test("scheduler: the gate bounds real concurrency", async () => {
  const gate = createGate(2);
  let active = 0;
  let peak = 0;
  await Promise.all(
    Array.from({ length: 6 }, () =>
      gate.run(async () => {
        active += 1;
        peak = Math.max(peak, active);
        await new Promise((resolve) => setTimeout(resolve, 5));
        active -= 1;
      }),
    ),
  );
  assert.equal(peak, 2);
});

test("diagnose: capability and config problems are not retried", () => {
  const capability = Object.assign(new Error("no tools"), { code: "capability_unavailable" });
  assert.equal(defaultDiagnose({ error: capability }).retryable, false);
  assert.equal(defaultDiagnose({ error: new Error("transient network") }).retryable, true);
  assert.equal(defaultDiagnose({ error: new Error("boom") }).cause, "unknown_error");
});

test("specialists: roles map to concrete agent pages", () => {
  for (const role of ["researcher", "builder", "visual", "qc", "improve"]) {
    assert.equal(isSpecialistRole(role), true);
    assert.ok(specialistFor(role).agentId.startsWith("swarm/"));
  }
  assert.equal(isSpecialistRole("none"), false);
  assert.equal(SPECIALISTS.qc.agentId, "swarm/qc");
});

test("specialists: step prompts carry the task and dependencies", () => {
  const prompt = buildStepPrompt(
    { id: "build", role: "builder", produces: "artifact", needs: ["research"] },
    { task: "Ship the CSV importer", plan: { criteria: [{ text: "handles empty files" }] }, dependencies: { research: { findings: ["use a streaming parser"] } } },
  );
  assert.match(prompt, /Ship the CSV importer/);
  assert.match(prompt, /streaming parser/);
  assert.match(prompt, /handles empty files/);
  assert.equal(truncate("abcdef", 3), "abc");
  assert.match(stringify({ a: 1 }), /"a": 1/);
});

test("executor: independent steps run in parallel and both complete", async () => {
  const plan = { id: "p", workflow: "parallel_build_verify", steps: [step("a"), step("b")] };
  let live = 0;
  let peak = 0;
  const run = await executePlan(plan, {
    maxConcurrency: 2,
    stepTimeoutMs: 1000,
    runStep: async (current) => {
      live += 1;
      peak = Math.max(peak, live);
      await new Promise((resolve) => setTimeout(resolve, 10));
      live -= 1;
      return { value: `${current.id}-done`, usage: { tokens: 5, costUSD: 0.001 } };
    },
  });
  assert.equal(run.status, "ok");
  assert.equal(peak, 2);
  assert.equal(run.values.a, "a-done");
  assert.equal(run.values.b, "b-done");
});

test("executor: a transient failure is diagnosed and retried once", async () => {
  const journal = createJournal();
  let calls = 0;
  const run = await executePlan(
    { id: "p", workflow: "direct", steps: [step("do")] },
    {
      journal,
      maxRetries: 2,
      stepTimeoutMs: 1000,
      diagnose: () => ({ retryable: true, cause: "transient", delayMs: 1 }),
      runStep: async () => {
        calls += 1;
        if (calls === 1) throw Object.assign(new Error("flaky"), { code: "transient" });
        return { value: "recovered" };
      },
    },
  );
  assert.equal(calls, 2);
  assert.equal(run.status, "ok");
  assert.equal(run.results.get("do").status, "ok");
  assert.equal(run.attempts, 2);
  assert.equal(journal.count(JOURNAL_KINDS.DIAGNOSIS), 1);
  assert.equal(journal.count(JOURNAL_KINDS.RETRY), 1);
});

test("executor: a non-retryable failure skips only its dependents", async () => {
  const plan = {
    id: "p",
    workflow: "research_build_verify",
    steps: [step("a"), step("b", { needs: ["a"] }), step("c")],
  };
  const run = await executePlan(plan, {
    maxRetries: 3,
    stepTimeoutMs: 1000,
    runStep: async (current) => {
      if (current.id === "a") throw Object.assign(new Error("no capability"), { code: "capability_unavailable" });
      return { value: current.id };
    },
  });
  assert.equal(run.results.get("a").status, "failed");
  assert.equal(run.results.get("a").attempts, 1, "non-retryable failures are not retried");
  assert.equal(run.results.get("b").status, "skipped");
  assert.equal(run.results.get("c").status, "ok", "unrelated work still completes");
  assert.equal(run.status, "partial");
});

test("executor: a step that overruns its time budget is cut off", async () => {
  const run = await executePlan(
    { id: "p", workflow: "direct", steps: [step("slow")] },
    {
      maxRetries: 0,
      stepTimeoutMs: 25,
      runStep: async () => {
        await new Promise((resolve) => setTimeout(resolve, 300));
        return { value: "too late" };
      },
    },
  );
  assert.equal(run.results.get("slow").status, "failed");
  assert.equal(run.results.get("slow").errorCode, "budget_exceeded");
  assert.equal(run.status, "budget_exceeded");
});

test("executor: an exhausted budget stops the run before more work starts", async () => {
  const budget = createBudget({ maxTokens: 5 });
  budget.charge({ tokens: 6 });
  const run = await executePlan(
    { id: "p", workflow: "direct", steps: [step("do")] },
    { budget, runStep: async () => ({ value: "should not run" }) },
  );
  assert.equal(run.status, "budget_exceeded");
  assert.equal(run.results.size, 0);
});

test("executor: reused memory work is marked reused, not re-executed", async () => {
  const plan = {
    id: "p",
    workflow: "research_only",
    steps: [step("research", { role: "researcher", produces: "findings" }), step("deliver", { role: "none", produces: "result", needs: ["research"] })],
  };
  let ran = [];
  const run = await executePlan(plan, {
    stepTimeoutMs: 1000,
    preset: { research: "cached findings" },
    runStep: async (current) => {
      ran.push(current.id);
      return { value: `${current.id}-out`, deferred: current.role === "none" };
    },
  });
  assert.equal(run.results.get("research").status, "reused");
  assert.equal(run.values.research, "cached findings");
  assert.deepEqual(ran, ["deliver"]);
  assert.equal(run.results.get("deliver").status, "deferred");
});

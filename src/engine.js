/**
 * The Manager.
 *
 * One entry point — `submit(task)` — takes a natural-language request through
 * plan, decide, delegate, execute, verify and deliver. Nothing here requires a
 * special command: the manager decides what is needed on its own.
 *
 * Ordering reflects the efficiency requirement: deterministic work happens
 * first, model calls happen only where a rule cannot settle the question, and
 * the run stops as soon as the outcome is independently verified.
 */

import { CancelledError } from "./core/errors.js";
import { createBudget, DEFAULT_LIMITS } from "./core/budget.js";
import { systemClock } from "./core/clock.js";
import { createJournal, JOURNAL_KINDS } from "./core/journal.js";
import { silentLogger } from "./core/logger.js";
import { summarize } from "./core/metrics.js";
import { classify } from "./decide/classifier.js";
import { plan as buildPlan, isSpecialist } from "./decide/planner.js";
import { ROLES, selectWorkflow, workflowById, WORKFLOWS } from "./decide/workflows.js";
import { executePlan, normalizeUsage } from "./exec/executor.js";
import { buildStepPrompt, specialistFor, stringify, truncate } from "./exec/specialists.js";
import { runImprovement } from "./improve/loop.js";
import { createTuner } from "./improve/tuner.js";
import { createLearning } from "./memory/learning.js";
import { createTaskMemory, keywordsOf } from "./memory/tasks.js";
import { createRouter } from "./route/router.js";
import { verify as verifyDelivery } from "./verify/verifier.js";

export const DEFAULT_POLICY = Object.freeze({
  budget: { ...DEFAULT_LIMITS },
  stepTimeoutMs: 5 * 60 * 1000,
  /** Below this signal confidence the plan carries an explicit caveat. */
  modelPlanConfidence: 0.45,
  verify: true,
  /** Reuse prior verified research when keyword overlap is at least this. */
  reuseMemoryOverlap: 0.6,
  reuseMemoryMaxAgeMs: 7 * 24 * 60 * 60 * 1000,
  /** `interactive` leaves role-less steps to the manager agent in-session. */
  mode: "interactive",
  autoImprove: false,
});

export function createManager({
  runtime,
  store = null,
  clock = systemClock(),
  logger = silentLogger,
  policy = {},
  learning = null,
  tuner = null,
  taskMemory = null,
  /** Optional observer for dashboard/lifecycle events. Never affects the run. */
  onEvent = null,
} = {}) {
  if (!runtime) throw new TypeError("createManager requires a runtime");

  const settings = { ...DEFAULT_POLICY, ...policy, budget: { ...DEFAULT_POLICY.budget, ...(policy.budget ?? {}) } };
  const learningStore = learning ?? createLearning({ store, logger: logger.child?.("learning") ?? logger });
  const tunerStore = tuner ?? createTuner({ store, logger: logger.child?.("tuner") ?? logger, clock });
  const memory = taskMemory ?? createTaskMemory({ store, logger: logger.child?.("memory") ?? logger });

  const managerObserver = typeof onEvent === "function" ? onEvent : null;

  let bootstrapped = false;
  let lastRun = null;

  async function bootstrap() {
    if (bootstrapped) return;
    await Promise.all([learningStore.load?.(), tunerStore.load?.()]);
    bootstrapped = true;
  }

  async function submit(task, options = {}) {
    await bootstrap();

    const {
      sessionID = null,
      signal,
      onProgress = null,
      budget: budgetOverrides = {},
      mode = settings.mode,
      improve = settings.autoImprove,
      workflow: forcedWorkflow = null,
      acceptance = null,
      onEvent: callObserver = null,
    } = options;

    const startedAt = clock.now();
    const journal = createJournal({ now: () => clock.now() });
    const budget = createBudget({ ...settings.budget, ...budgetOverrides }, { now: () => clock.now() });
    const taskId = `task_${startedAt.toString(36)}_${Math.random().toString(36).slice(2, 7)}`;

    journal.append(JOURNAL_KINDS.TASK, { taskId, sessionID, task, mode });

    // Observation is optional and isolated: an observer that throws (or a
    // dashboard that is down) must never change the outcome of the run.
    const observer = typeof callObserver === "function" ? callObserver : managerObserver;
    const emit = (type, data = {}) => {
      if (!observer) return;
      try {
        observer({ type, at: clock.now(), taskId, ...data });
      } catch {
        // reporting must never fail the work
      }
    };
    const progress = (event) => {
      try {
        onProgress?.(event);
      } catch {
        // a caller's progress handler is not allowed to break the run
      }
      if (event?.phase === "step.start") {
        emit("step.start", { stepId: event.stepId, role: event.role, attempt: event.attempt });
      } else if (event?.phase === "step.end") {
        emit("step.end", {
          stepId: event.stepId,
          role: event.role,
          status: event.status,
          durationMs: event.durationMs,
        });
      } else if (event?.phase === "retry") {
        emit("retry", { stepId: event.stepId, role: event.role, attempt: event.attempt, delayMs: event.delayMs });
      }
    };

    emit("task.start", { mode, sessionID });

    // ---- 1. deterministic classification ---------------------------------
    const classification = classify(task);
    journal.append(JOURNAL_KINDS.DECISION, {
      phase: "classify",
      strategy: "deterministic",
      kind: classification.kind,
      complexity: classification.complexity,
      confidence: classification.confidence,
      needs: classification.needs,
    });
    progress({ phase: "classified", kind: classification.kind, complexity: classification.complexity });
    emit("classified", { kind: classification.kind, complexity: classification.complexity, confidence: classification.confidence });

    // ---- 2. durable memory: do not re-research a solved task -------------
    let reuse = null;
    if (settings.reuseMemoryOverlap > 0) {
      const similar = await memory.similar(task, { kind: classification.kind });
      if (
        similar &&
        similar.overlap >= settings.reuseMemoryOverlap &&
        similar.entry.verified === true &&
        clock.now() - similar.entry.at <= settings.reuseMemoryMaxAgeMs
      ) {
        reuse = similar;
        journal.append(JOURNAL_KINDS.DECISION, {
          phase: "memory",
          strategy: "deterministic",
          reusedTaskId: similar.entry.id,
          overlap: round(similar.overlap),
        });
      } else {
        journal.append(JOURNAL_KINDS.DECISION, {
          phase: "memory",
          strategy: "deterministic",
          reusedTaskId: null,
          overlap: similar ? round(similar.overlap) : 0,
        });
      }
    }

    // ---- 3. model catalog + router ---------------------------------------
    const modelsResult = await safeModels(runtime, logger);
    const models = modelsResult.models;
    journal.append(JOURNAL_KINDS.DECISION, {
      phase: "catalog",
      strategy: modelsResult.source === "live" ? "live" : "deterministic",
      modelCount: models.length,
      source: modelsResult.source,
      error: modelsResult.error ?? null,
    });

    const overrides = tunerStore.overrides?.() ?? {};
    const router = createRouter({
      models,
      learning: learningStore,
      weights: overrides.weights ?? {},
      excluded: overrides.excluded ?? [],
      preferred: overrides.preferred ?? null,
    });

    // ---- 4. workflow + plan ----------------------------------------------
    let flow = forcedWorkflow ? workflowById(forcedWorkflow) : selectWorkflow(classification);
    if (!forcedWorkflow && overrides.workflowAvoid?.(classification.kind) === flow.id) {
      flow = WORKFLOWS[overrides.workflowPreference?.(classification.kind)] ?? flow;
    }
    const plan = buildPlan(task, { classification, workflow: flow, budget, router });
    if (Array.isArray(acceptance) && acceptance.length) {
      for (const extra of acceptance) {
        plan.criteria.push({
          id: extra.id ?? `c_custom_${plan.criteria.length}`,
          text: extra.text ?? String(extra),
          check: extra.check ?? { type: "mentionsAny", target: "output", values: [String(extra.text ?? extra)] },
        });
      }
    }
    journal.append(JOURNAL_KINDS.PLAN, {
      planId: plan.id,
      workflow: plan.workflow,
      steps: plan.steps.map((step) => ({ id: step.id, role: step.role, needs: step.needs })),
      criteria: plan.criteria.map((criterion) => criterion.id),
      router: plan.routerHint,
      notes: plan.notes,
    });
    progress({ phase: "planned", workflow: plan.workflow, steps: plan.steps.length, planId: plan.id });
    emit("planned", {
      planId: plan.id,
      workflow: plan.workflow,
      steps: plan.steps.map((step) => ({ id: step.id, role: step.role, needs: step.needs })),
      criteria: plan.criteria.map((criterion) => criterion.id),
      models: models.length,
      catalogSource: modelsResult.source,
    });

    // ---- 5. execution -----------------------------------------------------
    const preset = {};
    if (reuse) {
      for (const step of plan.steps) {
        if (step.role === ROLES.RESEARCHER && reuse.entry.outputs?.findings) {
          preset[step.id] = reuse.entry.outputs.findings;
        }
      }
    }

    const runStep = createStepRunner({ runtime, router, plan, task, mode, budget, settings, journal, onProgress: progress, onEvent: emit });
    const run = await executePlan(plan, {
      runStep,
      budget,
      clock,
      journal,
      logger,
      signal,
      maxRetries: overrides.retry?.maxRetries ?? settings.budget.maxRetries,
      stepTimeoutMs: settings.stepTimeoutMs,
      maxConcurrency: settings.budget.maxConcurrency,
      onProgress: progress,
      preset,
    });

    progress({ phase: "executed", status: run.status, steps: run.results.size });
    for (const failure of run.failures) {
      emit("failure", { stepId: failure.stepId, code: failure.diagnosis?.cause ?? failure.error?.code ?? null });
    }

    // ---- 6. independent verification --------------------------------------
    const evidence = collectEvidence(plan, run);
    let verification = null;
    if (settings.verify && run.status !== "cancelled") {
      verification = await verifyDelivery(plan, evidence, {
        runtime,
        router,
        budget,
        journal,
        clock,
        producerModel: firstProducerModel(run),
      });
      progress({ phase: "verified", passed: verification.passed, independence: verification.independence });
      emit("verify", {
        passed: verification.passed === true,
        independence: verification.independence ?? null,
        // No free text here: a verifier's reason quotes the criterion, and a
        // criterion is derived from the user's prompt.
        failedCount: Array.isArray(verification.failed) ? verification.failed.length : null,
        durationMs: verification.durationMs ?? null,
      });
    }

    // ---- 7. delivery ------------------------------------------------------
    const verified = Boolean(verification?.passed);
    const deferred = [...run.results.values()].filter((record) => record.status === "deferred");
    const metrics = summarize(journal.all(), { budget });

    const delivery = {
      taskId,
      task,
      taskKind: classification.kind,
      workflow: plan.workflow,
      status: verified ? "delivered" : run.status === "cancelled" ? "cancelled" : "unverified",
      verified,
      verification,
      criteria: verification?.results ?? [],
      outputs: evidence.outputs,
      output: evidence.output,
      deferredSteps: deferred.map((record) => ({ stepId: record.stepId, instruction: record.value?.instruction ?? null })),
      failures: run.failures.map((failure) => ({ stepId: failure.stepId, error: failure.error, cause: failure.diagnosis?.cause ?? null })),
      skipped: run.skipped,
      notes: plan.notes,
      nextActions: buildNextActions(verification, deferred, run),
      metrics,
      durationMs: clock.now() - startedAt,
      budget: budget.snapshot(),
      reuse: reuse ? { taskId: reuse.entry.id, overlap: round(reuse.overlap) } : null,
    };

    journal.append(JOURNAL_KINDS.DELIVER, {
      taskId,
      status: delivery.status,
      verified,
      durationMs: delivery.durationMs,
      criteriaPassed: verification?.results?.filter((result) => result.passed).length ?? null,
      criteriaTotal: verification?.results?.length ?? null,
    });
    progress({ phase: "delivered", status: delivery.status });
    emit("deliver", {
      status: delivery.status,
      verified,
      durationMs: delivery.durationMs,
      tokens: metrics.usage.tokensObserved ? metrics.usage.tokens : null,
      costUSD: metrics.usage.costObserved ? metrics.usage.costUSD : null,
      toolCalls: metrics.usage.toolCalls ?? null,
      budget: budget.snapshot(),
    });
    emit("run.end", { status: delivery.status, verified });

    // ---- 8. persist memory + learning ------------------------------------
    await memory.save({
      id: taskId,
      at: clock.now(),
      task,
      kind: classification.kind,
      workflow: plan.workflow,
      verified,
      keywords: keywordsOf(task),
      outputs: verified ? evidence.outputs : {},
      summary: delivery.status,
    });
    recordLearning({ learning: learningStore, plan, run, verification, clock, classification });
    journal.append(JOURNAL_KINDS.LEARN, { taskId, samples: run.results.size });

    // ---- 9. bounded self-improvement (off the critical path by default) ---
    let improvement = null;
    if (improve) {
      improvement = await runImprovement({
        learning: learningStore,
        tuner: tunerStore,
        journal,
        clock,
        policy: options.improvementPolicy ?? {},
        approvals: options.approvals ?? [],
        dryRun: options.dryRunImprovement === true,
      });
    }

    const result = {
      ok: verified,
      taskId,
      classification,
      plan,
      run: summarizeRun(run),
      verification,
      delivery,
      improvement,
      journal: journal.all(),
      metrics: summarize(journal.all(), { budget }),
      budget: budget.snapshot(),
    };
    lastRun = result;
    return result;
  }

  return {
    submit,
    bootstrap,
    policy: settings,
    learning: learningStore,
    tuner: tunerStore,
    memory,
    lastRun() {
      return lastRun;
    },
    async improve(options = {}) {
      await bootstrap();
      return runImprovement({
        learning: learningStore,
        tuner: tunerStore,
        clock,
        policy: options.policy ?? {},
        approvals: options.approvals ?? [],
        dryRun: options.dryRun === true,
      });
    },
    async status() {
      await bootstrap();
      return {
        policy: settings,
        models: (await safeModels(runtime, logger)).models.length,
        learning: learningStore.overall(),
        learnedModels: learningStore.knownModels(),
        tuner: { version: tunerStore.get().version, excluded: tunerStore.get().router.excluded },
        experiments: tunerStore.experiments(),
      };
    },
  };
}

/* ------------------------------------------------------------------ helpers */

async function safeModels(runtime, logger) {
  if (typeof runtime.models !== "function") {
    return { models: [], source: "unavailable", error: "runtime does not expose a model catalog" };
  }
  try {
    const models = await runtime.models();
    return { models: Array.isArray(models) ? models : [], source: "live", error: null };
  } catch (error) {
    logger.warn?.(`model catalog unavailable: ${error?.message ?? error}`);
    // An unreachable catalog is reported as such, never as "zero models exist".
    return { models: [], source: "error", error: error?.message ?? String(error) };
  }
}

export function createStepRunner({ runtime, router, plan, task, mode, budget, settings, journal, onProgress, onEvent = null }) {
  return async function runStep(step, { signal, attempt, dependencies }) {
    if (budget?.check && budget.check().ok === false) throw budget.check().error;
    if (signal?.aborted) throw new CancelledError("step aborted");

    if (!isSpecialist(step.role)) {
      const instruction = directInstruction(step, plan, dependencies);
      if (mode === "headless") {
        if (typeof runtime.delegate !== "function") {
          throw new Error("headless mode requires a runtime that can delegate");
        }
        const model = router.routeForRole("manager")?.ref ?? null;
        const outcome = await runtime.delegate({
          agent: "swarm",
          prompt: `${task}\n\n${instruction}`,
          model,
          budget,
          signal,
          label: `manager:${step.id}`,
          timeoutMs: settings.stepTimeoutMs,
        });
        journal.append(JOURNAL_KINDS.DELEGATE, { stepId: step.id, agent: "swarm", mode: "headless", model: outcome?.model ?? model });
        onEvent?.("delegate", { stepId: step.id, role: "none", agent: "swarm", model: outcome?.model ?? model ?? null });
        return { value: outcome?.text ?? "", usage: outcome?.usage ?? null, model: outcome?.model ?? model, mode: "delegated_manager" };
      }
      journal.append(JOURNAL_KINDS.DIRECT, { stepId: step.id, deferred: true, attempt });
      return { value: { deferred: true, instruction }, deferred: true, mode: "direct" };
    }

    const specialist = specialistFor(step.role);
    if (typeof runtime.delegate !== "function") {
      const error = new Error(`cannot run ${step.role}: runtime has no delegation capability`);
      error.code = "capability_unavailable";
      error.retryable = false;
      throw error;
    }

    const route = router.routeForRole(step.role);
    const prompt = buildStepPrompt(step, { task, plan, dependencies });
    const outcome = await runtime.delegate({
      agent: specialist.agentId,
      prompt,
      model: route?.ref ?? null,
      budget,
      signal,
      label: step.id,
      timeoutMs: settings.stepTimeoutMs,
    });

    const resolvedModel = outcome?.model ?? route?.ref ?? null;
    journal.append(JOURNAL_KINDS.DELEGATE, {
      stepId: step.id,
      agent: specialist.agentId,
      model: resolvedModel,
      mode: "specialist",
    });
    onEvent?.("delegate", { stepId: step.id, role: step.role, agent: specialist.agentId, model: resolvedModel });
    onProgress?.({ phase: "delegated", stepId: step.id, role: step.role });

    return {
      value: outcome?.text ?? "",
      usage: outcome?.usage ?? null,
      model: outcome?.model ?? route?.ref ?? null,
      mode: "delegated",
      agent: specialist.agentId,
    };
  };
}

function directInstruction(step, plan, dependencies) {
  const inputs = Object.entries(dependencies)
    .map(([key, value]) => `--- ${key} ---\n${truncate(stringify(value), 6000)}`)
    .join("\n\n");
  return [
    `Step "${step.id}": produce "${step.produces}" in this session.`,
    inputs ? `\nInputs from earlier steps:\n${inputs}` : "",
    `\nAcceptance criteria:\n${(plan.criteria ?? []).map((criterion) => `- ${criterion.text}`).join("\n")}`,
  ]
    .filter(Boolean)
    .join("\n");
}

function collectEvidence(plan, run) {
  const outputs = {};
  for (const step of plan.steps) {
    const record = run.results.get(step.id);
    if (record && record.status === "ok" && record.value !== undefined) {
      outputs[step.produces ?? step.id] = record.value;
    }
  }
  const output = Object.entries(outputs)
    .map(([key, value]) => `### ${key}\n${truncate(stringify(value), 20_000)}`)
    .join("\n\n");
  return {
    output,
    outputs,
    artifacts: [],
    meta: { planId: plan.id, workflow: plan.workflow, status: run.status, steps: run.results.size },
  };
}

function firstProducerModel(run) {
  for (const record of run.results.values()) {
    if (record.status === "ok" && record.model) return record.model;
  }
  return null;
}

function buildNextActions(verification, deferred, run) {
  const actions = [];
  if (deferred.length) {
    actions.push({
      kind: "execute_in_session",
      detail: `${deferred.length} step(s) wait for the manager agent: ${deferred.map((record) => record.stepId).join(", ")}`,
    });
  }
  if (verification && !verification.passed) {
    actions.push({ kind: "repair", detail: verification.reason ?? "verification failed" });
  }
  if (run.failures.length) {
    actions.push({ kind: "review_failures", detail: run.failures.map((failure) => failure.stepId).join(", ") });
  }
  return actions;
}

function summarizeRun(run) {
  return {
    planId: run.planId,
    workflow: run.workflow,
    status: run.status,
    attempts: run.attempts,
    durationMs: run.durationMs,
    concurrency: run.concurrency,
    steps: [...run.results.values()].map((record) => ({
      stepId: record.stepId,
      status: record.status,
      attempts: record.attempts ?? 0,
      model: record.model ?? null,
      mode: record.mode ?? null,
      error: record.error ?? null,
    })),
  };
}

function recordLearning({ learning, plan, run, verification, clock, classification }) {
  const now = clock.now();
  for (const record of run.results.values()) {
    if (record.status === "skipped") continue;
    const specialist = isSpecialist(plan.steps.find((step) => step.id === record.stepId)?.role)
      ? plan.steps.find((step) => step.id === record.stepId)?.role
      : "manager";
    // Workers report the runtime's telemetry shape; flatten it before it is
    // stored, so the learning ledger holds real numbers (or honest nulls).
    const usage = normalizeUsage(record.usage);
    learning.record({
      at: now,
      kind: classification.kind,
      workflow: plan.workflow,
      role: specialist,
      model: record.model ?? null,
      success: record.status === "ok" ? true : record.status === "deferred" ? null : false,
      durationMs: record.durationMs ?? null,
      tokens: usage.tokens ?? null,
      costUSD: usage.costUSD ?? null,
      retries: Math.max(0, (record.attempts ?? 1) - 1),
      toolCalls: usage.toolCalls ?? null,
    });
  }
  learning.record({
    at: now,
    kind: classification.kind,
    workflow: plan.workflow,
    role: "manager",
    model: null,
    success: verification ? verification.passed === true : null,
    durationMs: run.durationMs ?? null,
    tokens: null,
    costUSD: null,
    retries: 0,
    toolCalls: null,
  });
}

function round(value) {
  return Number.isFinite(value) ? Math.round(value * 1000) / 1000 : value;
}

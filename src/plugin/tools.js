/**
 * The swarm tools exposed to the manager agent.
 *
 * These are the deterministic entry points the manager calls instead of
 * improvising: plan first (`swarm_plan`), delegate the specialist steps,
 * verify, then report. Every tool is thin — the logic lives in `src/engine.js`
 * and the modules it composes.
 */

import { classify } from "../decide/classifier.js";
import { extractSignals } from "../decide/signals.js";
import { selectWorkflow } from "../decide/workflows.js";
import { plan as buildPlan, deriveCriteria } from "../decide/planner.js";
import { createBudget, DEFAULT_LIMITS } from "../core/budget.js";
import { createRouter } from "../route/router.js";
import { verify } from "../verify/verifier.js";
import { specialistFor, isSpecialistRole } from "../exec/specialists.js";
import { parseRef } from "../route/catalog.js";

export const NAMESPACE = "swarm";
export const NAMESPACE_DESCRIPTION =
  "Manager-led swarm: deterministic planning, specialist delegation, independent verification, persistent memory and measured self-improvement.";

/**
 * @param {object} editor  OpenCode `ToolEditor`
 * @param {object} deps    `{ manager, runtime, logger, state }`
 */
export function registerTools(editor, { manager, runtime, logger, state }) {
  editor.namespace({ name: NAMESPACE, description: NAMESPACE_DESCRIPTION });

  // `editor.namespace()` only declares the namespace; a tool joins it solely
  // through `options.namespace`. Without this the tools register under their
  // bare names (`plan`, `status`, ...) and collide with everything else.
  const add = (tool) => editor.add({ ...tool, options: { namespace: NAMESPACE, ...(tool.options ?? {}) } });

  add({
    name: "plan",
    description:
      "Deterministically plan a task: choose the minimum sufficient workflow, list the specialist steps, derive acceptance criteria and route a model for each role. Costs no model call. Always call this before delegating.",
    input: {
      type: "object",
      properties: {
        task: { type: "string", description: "The user's task, in natural language." },
        acceptance: {
          type: "array",
          items: { type: "string" },
          description: "Optional explicit acceptance criteria the user stated.",
        },
      },
      required: ["task"],
      additionalProperties: false,
    },
    async execute(input, context) {
      const result = await runPlan({ manager, runtime, logger, task: input.task, acceptance: input.acceptance });
      state.plans.set(context.sessionID ?? "session", result);
      state.lastPlan = result;
      return textResult(result);
    },
  });

  add({
    name: "delegate",
    description:
      "Run one specialist step in its own worker session and return its result with measured usage. Use the role from the plan. Roles: researcher, builder, visual, qc, improve.",
    input: {
      type: "object",
      properties: {
        role: { type: "string", enum: ["researcher", "builder", "visual", "qc", "improve"] },
        prompt: { type: "string", description: "The full instruction for the specialist, including the task and any upstream results it needs." },
        model: { type: "string", description: 'Optional "providerID/modelID" override. Omit to use the routed model.' },
        timeoutMs: { type: "number", description: "Hard wall-clock bound for this worker. Defaults to the manager step timeout." },
        label: { type: "string", description: "Short label used for the worker session title and the journal." },
      },
      required: ["role", "prompt"],
      additionalProperties: false,
    },
    async execute(input, context) {
      const result = await runDelegate({
        manager,
        runtime,
        logger,
        role: input.role,
        prompt: input.prompt,
        model: input.model ?? null,
        timeoutMs: input.timeoutMs ?? null,
        label: input.label ?? null,
        signal: context?.signal ?? null,
        parentSessionID: context?.sessionID ?? null,
      });
      state.delegations.push(result);
      return textResult(result);
    },
  });

  add({
    name: "verify",
    description:
      "Independently check a produced result against acceptance criteria. Deterministic checks run first; an independent model judgement is used only when a criterion needs it. Returns whether the task may be reported as done.",
    input: {
      type: "object",
      properties: {
        task: { type: "string" },
        output: { type: "string", description: "The produced result to verify." },
        criteria: {
          type: "array",
          items: {
            type: "object",
            properties: {
              id: { type: "string" },
              text: { type: "string", description: "Human-readable statement of the criterion." },
              check: { type: "string", enum: ["nonEmpty", "mentionsAll", "mentionsAny", "regex", "artifacts", "verdict"] },
              values: { type: "array", items: { type: "string" } },
              pattern: { type: "string" },
            },
            required: ["id", "check"],
            additionalProperties: true,
          },
        },
        producerModel: { type: "string", description: "Model that produced the output, in \"providerID/modelID\" form." },
      },
      required: ["task", "output"],
      additionalProperties: false,
    },
    async execute(input, context) {
      const criteria = Array.isArray(input.criteria) && input.criteria.length
        ? input.criteria.map(toCriterion)
        : deriveCriteria(input.task, classify(input.task));
      const { router } = await buildRouter(manager, runtime, logger);
      const verdict = await verify(
        { task: input.task, id: "verify_ad_hoc", workflow: "ad-hoc", criteria },
        { output: input.output, artifacts: [], meta: { source: "swarm_verify" } },
        {
          runtime,
          router,
          budget: createBudget({ ...DEFAULT_LIMITS, maxMs: 120000, maxTokens: 60000, maxSteps: 2, maxConcurrency: 1 }),
          producerModel: input.producerModel ?? null,
          criteria,
        },
      );
      const payload = {
        passed: verdict.passed,
        independence: verdict.independence,
        reason: verdict.reason,
        failed: verdict.failed,
        criteriaCount: criteria.length,
        durationMs: verdict.durationMs,
      };
      state.verifications.push(payload);
      return textResult(payload);
    },
  });

  add({
    name: "status",
    description:
      "Report the swarm's real state: which OpenCode interfaces are available, the discovered model catalog, measured learning statistics, and the last run's efficiency metrics.",
    input: { type: "object", properties: {}, additionalProperties: false },
    async execute() {
      const catalog = await safeCatalog(runtime, logger);
      const last = manager.lastRun();
      const payload = {
        opencode: { version: runtime.capabilities.version, missingInterfaces: runtime.missing },
        capabilities: runtime.capabilities,
        catalog: {
          source: catalog.source,
          count: catalog.models.length,
          models: catalog.models.map((model) => ({
            ref: `${model.providerID}/${model.modelID ?? model.id}`,
            status: model.status,
            tools: model.capabilities?.tools ?? null,
            context: model.limit?.context ?? null,
          })),
        },
        learning: manager.learning.overall(),
        knownModels: manager.learning.knownModels(),
        tuner: manager.tuner.get(),
        lastRun: last
          ? { task: last.task, workflow: last.workflow, status: last.status, passed: last.delivery?.passed ?? null, metrics: last.metrics }
          : null,
        instrumentation: {
          plans: state.plans.size,
          delegations: state.delegations.length,
          verifications: state.verifications.length,
          toolCallsObserved: state.toolCallsObserved,
          toolFailuresObserved: state.toolFailuresObserved,
        },
      };
      return textResult(payload);
    },
  });

  add({
    name: "memory",
    description:
      "Read or write the swarm's persistent task memory: recall similar past tasks and evidence-based lessons.",
    input: {
      type: "object",
      properties: {
        action: { type: "string", enum: ["recall", "recent", "lessons"] },
        task: { type: "string", description: "For `recall`: the task to match against." },
        limit: { type: "number" },
      },
      required: ["action"],
      additionalProperties: false,
    },
    async execute(input) {
      const limit = Number.isFinite(input.limit) ? input.limit : 5;
      if (input.action === "recall") {
        const similar = input.task ? await manager.memory.similar(input.task, { limit }) : [];
        return textResult({ action: "recall", task: input.task ?? null, similar });
      }
      if (input.action === "recent") {
        return textResult({ action: "recent", entries: await manager.memory.recent(limit) });
      }
      return textResult({ action: "lessons", models: manager.learning.knownModels(), overall: manager.learning.overall() });
    },
  });

  add({
    name: "improve",
    description:
      "Run the bounded self-improvement loop: review open experiments, propose at most one evidence-backed change, gate it, and record it. Never edits agent pages or source code.",
    input: {
      type: "object",
      properties: {
        dryRun: { type: "boolean", description: "Propose and gate without applying. Defaults to true when the session is interactive." },
      },
      additionalProperties: false,
    },
    async execute(input) {
      const result = await manager.improve({ dryRun: input.dryRun !== false });
      return textResult(result);
    },
  });

  add({
    name: "run",
    description:
      "Execute a complete manager run for a task: plan, delegate, verify and deliver, with all budgets enforced. Use when you want the manager to carry the task through rather than coordinating step by step.",
    input: {
      type: "object",
      properties: {
        task: { type: "string" },
        acceptance: { type: "array", items: { type: "string" } },
        budget: {
          type: "object",
          properties: {
            maxMs: { type: "number" },
            maxTokens: { type: "number" },
            maxCostUSD: { type: "number" },
            maxToolCalls: { type: "number" },
            maxSteps: { type: "number" },
            maxRetries: { type: "number" },
            maxConcurrency: { type: "number" },
          },
          additionalProperties: false,
        },
      },
      required: ["task"],
      additionalProperties: false,
    },
    async execute(input, context) {
      const result = await manager.submit(input.task, {
        acceptance: input.acceptance,
        budget: input.budget,
        sessionID: context?.sessionID ?? null,
        signal: context?.signal ?? null,
      });
      state.runs.push(result);
      return textResult({
        task: result.delivery.task,
        status: result.delivery.status,
        verified: result.delivery.verified,
        workflow: result.delivery.workflow,
        delivery: {
          output: result.delivery.output,
          deferredSteps: result.delivery.deferredSteps,
          failures: result.delivery.failures,
          nextActions: result.delivery.nextActions,
        },
        verification: result.verification
          ? { passed: result.verification.passed, independence: result.verification.independence, reason: result.verification.reason }
          : null,
        metrics: result.metrics,
        run: result.run,
      });
    },
  });

  return [
    `${NAMESPACE}_plan`,
    `${NAMESPACE}_delegate`,
    `${NAMESPACE}_verify`,
    `${NAMESPACE}_status`,
    `${NAMESPACE}_memory`,
    `${NAMESPACE}_improve`,
    `${NAMESPACE}_run`,
  ];
}

/* ------------------------------------------------------------------ helpers */

export function createToolState() {
  return {
    plans: new Map(),
    delegations: [],
    verifications: [],
    runs: [],
    lastPlan: null,
    toolCallsObserved: 0,
    toolFailuresObserved: 0,
  };
}

/** Deterministic plan, no model call. */
export async function runPlan({ manager, runtime, logger = null, task, acceptance = null }) {
  const signals = extractSignals(task);
  const classification = classify(task, { signals });
  const { router, catalog } = await buildRouter(manager, runtime, logger);
  const workflow = selectWorkflow(classification);
  const built = buildPlan(task, { classification, workflow, router, budget: manager.policy.budget });
  const criteria = acceptance?.length
    ? [
        ...built.criteria,
        ...acceptance.map((value, index) => ({
          id: `c_user_${index + 1}`,
          text: String(value),
          check: { type: "mentionsAny", target: "output", values: [String(value)] },
        })),
      ]
    : built.criteria;

  const stepRoutes = built.steps.map((step) => {
    const routed = isSpecialistRole(step.role) ? router.routeForRole(step.role) : null;
    return {
      id: step.id,
      role: step.role,
      produces: step.produces,
      parallel: Boolean(step.parallel),
      needs: step.needs ?? [],
      agent: isSpecialistRole(step.role) ? specialistFor(step.role).agentId : null,
      model: routed ? { ref: routed.ref, score: routed.score, dropped: routed.dropped } : null,
    };
  });

  return {
    planId: built.id,
    task,
    kind: classification.kind,
    complexity: classification.complexity,
    confidence: classification.confidence,
    workflow: built.workflow,
    workflowDescription: built.workflowDescription,
    deterministic: true,
    catalog: { source: catalog.source, count: catalog.models.length },
    requirement: built.requirement,
    steps: stepRoutes,
    criteria,
    notes: built.notes,
    budget: manager.policy.budget,
  };
}

/** Delegate a single specialist step, with the routed model when none is given. */
export async function runDelegate({
  manager,
  runtime,
  logger = null,
  role,
  prompt,
  model = null,
  timeoutMs = null,
  label = null,
  signal = null,
  parentSessionID = null,
}) {
  if (!isSpecialistRole(role)) {
    return { ok: false, error: `unknown_role:${role}`, supported: ["researcher", "builder", "visual", "qc", "improve"] };
  }
  if (!runtime.canDelegate) {
    return { ok: false, error: "delegation_unavailable", missing: runtime.missing, instruction: prompt };
  }

  const specialist = specialistFor(role);
  const { router } = await buildRouter(manager, runtime, logger);
  const routed = router.routeForRole(role);
  const chosen = model ? parseRef(model) : routed?.model ?? null;

  const budget = createBudget(manager.policy.budget);
  const bounded = Number.isFinite(timeoutMs) && timeoutMs > 0 ? timeoutMs : manager.policy.stepTimeoutMs;
  const started = Date.now();

  try {
    const result = await runtime.delegate({
      agent: specialist.agentId,
      prompt,
      model: chosen,
      signal,
      timeoutMs: bounded,
      label: label ?? role,
      parentSessionID,
    });

    return {
      ok: true,
      role,
      agent: specialist.agentId,
      model: chosen ? `${chosen.providerID}/${chosen.id}` : null,
      routedBy: model ? "explicit" : "router",
      score: routed?.score ?? null,
      sessionID: result.sessionID,
      text: result.text,
      usage: result.usage,
      outcome: result.outcome,
      error: result.error ?? null,
      durationMs: result.durationMs ?? Date.now() - started,
      budget: budget.snapshot(),
    };
  } catch (error) {
    return {
      ok: false,
      role,
      agent: specialist.agentId,
      model: chosen ? `${chosen.providerID}/${chosen.id}` : null,
      sessionID: error?.sessionID ?? null,
      error: String(error?.code ?? "delegation_failed"),
      detail: String(error?.message ?? error),
      durationMs: Date.now() - started,
      retryable: !["capability_unavailable", "config_error", "cancelled", "budget_exceeded"].includes(error?.code),
    };
  }
}

/** Router built from the live catalog plus measured learning, with tuner overrides. */
export async function buildRouter(manager, runtime, logger = null) {
  const catalog = await safeCatalog(runtime, logger);
  const overrides = manager.tuner?.overrides?.() ?? {};
  const router = createRouter({
    models: catalog.models,
    learning: manager.learning,
    weights: overrides.weights ?? {},
    excluded: overrides.excluded ?? [],
    preferred: overrides.preferred ?? null,
  });
  return { router, catalog, overrides };
}

/** Normalizes a tool-supplied criterion into the engine's shape. */
export function toCriterion(input) {
  return {
    id: input.id,
    text: input.text ?? input.detail ?? `${input.check} check`,
    check: {
      type: input.check,
      target: input.target ?? (input.check === "verdict" ? "verdict" : "output"),
      values: input.values ?? undefined,
      pattern: input.pattern ?? undefined,
    },
  };
}

export async function safeCatalog(runtime, logger) {
  try {
    const models = await runtime.models();
    return { source: "live", models };
  } catch (error) {
    if (logger?.warn) logger.warn("model catalog unavailable", { error: String(error?.message ?? error) });
    return { source: "error", models: [], error: String(error?.code ?? error?.message ?? error) };
  }
}

export function textResult(payload) {
  const text = typeof payload === "string" ? payload : JSON.stringify(payload, null, 2);
  return { content: [{ type: "text", text }], metadata: { deterministic: true } };
}

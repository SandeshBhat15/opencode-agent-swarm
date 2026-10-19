/**
 * Execution engine.
 *
 * Guarantees, in order of importance:
 *  - finite: every step has a timeout, every failure a bounded retry count,
 *    and the whole run stops the moment the budget is exhausted;
 *  - diagnosed: a failure is classified before any retry is attempted, so
 *    retries are not blind;
 *  - honest: unreported usage is recorded as unknown, never as zero;
 *  - independent: a failed step skips only its dependents; unrelated work
 *    still completes.
 */

import { CancelledError, BudgetExceededError } from "../core/errors.js";
import { systemClock } from "../core/clock.js";
import { JOURNAL_KINDS } from "../core/journal.js";
import { buildSchedule, createGate, dependentsOf } from "./scheduler.js";

const NON_RETRYABLE = new Set(["capability_unavailable", "config_error", "cancelled", "budget_exceeded", "dependency_cycle"]);

export function defaultDiagnose({ error }) {
  const message = error?.message ?? String(error);
  const retryable = !NON_RETRYABLE.has(error?.code) && error?.retryable !== false;
  return {
    cause: error?.code ?? "unknown_error",
    message,
    retryable,
    strategy: retryable ? "retry_same_worker" : "escalate_or_report",
    hint: retryable
      ? "transient failure; retry with the same configuration"
      : "configuration or capability problem; retrying cannot fix it",
  };
}

/**
 * @param {object} plan
 * @param {object} options
 * @param {(step: object, ctx: object) => Promise<object>} options.runStep
 * @returns {Promise<object>} run record
 */
export async function executePlan(plan, {
  runStep,
  budget,
  clock = systemClock(),
  journal = null,
  logger = null,
  signal,
  maxRetries = budget?.limits?.maxRetries ?? 2,
  stepTimeoutMs = 5 * 60 * 1000,
  maxConcurrency = budget?.limits?.maxConcurrency ?? 3,
  diagnose = defaultDiagnose,
  onProgress = null,
  /** Step results supplied up front, e.g. research reused from task memory. */
  preset = {},
} = {}) {
  if (typeof runStep !== "function") throw new TypeError("executePlan requires runStep");

  const startedAt = clock?.now?.() ?? Date.now();
  const schedule = buildSchedule(plan.steps, { maxConcurrency });
  const gate = createGate(maxConcurrency);
  const results = new Map();
  const completedValues = {};
  const skipped = new Set();
  const failures = [];
  let status = "ok";
  let attempts = 0;

  for (const group of schedule.groups) {
    if (status !== "ok") break;

    const budgetCheck = budget?.check?.();
    if (budgetCheck && budgetCheck.ok === false) {
      status = "budget_exceeded";
      journal?.append(JOURNAL_KINDS.FAILURE, { reason: "budget", error: budgetCheck.error.message });
      break;
    }
    if (signal?.aborted) {
      status = "cancelled";
      break;
    }

    // Steps already answered (reused memory) and steps whose dependency failed.
    const runnable = [];
    for (const step of group) {
      if (Object.prototype.hasOwnProperty.call(preset, step.id)) {
        const value = preset[step.id];
        completedValues[step.id] = value;
        results.set(step.id, {
          stepId: step.id,
          status: "reused",
          attempts: 0,
          durationMs: 0,
          value,
          produced: step.produces,
        });
        journal?.append(JOURNAL_KINDS.NOTE, { stepId: step.id, reused: true, reason: "task_memory" });
        onProgress?.({ phase: "step.end", stepId: step.id, status: "reused" });
        continue;
      }
      if (skipped.has(step.id) || (step.needs ?? []).some((need) => skipped.has(need) || results.get(need)?.status === "failed")) {
        skipped.add(step.id);
        results.set(step.id, { stepId: step.id, status: "skipped", reason: "dependency_failed" });
        journal?.append(JOURNAL_KINDS.FAILURE, { stepId: step.id, reason: "dependency_failed" });
        continue;
      }
      runnable.push(step);
    }
    if (runnable.length === 0) continue;

    const settled = await Promise.all(
      runnable.map((step) => gate.run(() => runOneStep(step))),
    );

    for (const record of settled) {
      results.set(record.stepId, record);
      if (record.status === "ok" || record.status === "deferred") {
        completedValues[record.stepId] = record.value;
      }
      if (record.status === "failed") {
        failures.push(record);
        skipped.add(record.stepId);
        for (const dependent of dependentsOf(plan.steps, [record.stepId])) skipped.add(dependent.id);
        if (record.errorCode === "budget_exceeded") status = "budget_exceeded";
      }
    }
  }

  async function runOneStep(step) {
    const attemptsAllowed = Math.max(0, Math.floor(maxRetries)) + 1;
    let lastError = null;

    for (let attempt = 1; attempt <= attemptsAllowed; attempt += 1) {
      attempts += 1;
      const stepStartedAt = clock?.now?.() ?? Date.now();
      journal?.append(JOURNAL_KINDS.STEP_START, { stepId: step.id, role: step.role, attempt, mode: step.mode ?? null });
      onProgress?.({ phase: "step.start", stepId: step.id, role: step.role, attempt });

      const controller = new AbortController();
      const abortUpstream = () => controller.abort(new CancelledError("upstream abort"));
      signal?.addEventListener?.("abort", abortUpstream, { once: true });

      try {
        const outcome = await withTimeout(
          (stepSignal) =>
            runStep(step, {
              signal: stepSignal,
              attempt,
              dependencies: pickDependencies(step, completedValues),
              sessionID: step.sessionID,
            }),
          { timeoutMs: stepTimeoutMs, clock, signal: controller.signal },
        );

        const durationMs = (clock?.now?.() ?? Date.now()) - stepStartedAt;
        const limit = normalizeUsage(outcome?.usage);
        budget?.charge?.({ ms: durationMs, steps: 1, ...limit });

        if (outcome?.usage) {
          journal?.append(JOURNAL_KINDS.USAGE, {
            stepId: step.id,
            role: step.role,
            model: outcome.model ?? null,
            tokens: limit.tokens ?? null,
            costUSD: limit.costUSD ?? null,
            toolCalls: limit.toolCalls ?? null,
            source: outcome.usage.source ?? "unknown",
          });
        }

        journal?.append(JOURNAL_KINDS.STEP_END, {
          stepId: step.id,
          role: step.role,
          attempt,
          status: outcome?.deferred ? "deferred" : "ok",
          durationMs,
          mode: outcome?.mode ?? step.mode ?? null,
          model: outcome?.model ?? null,
        });
        onProgress?.({ phase: "step.end", stepId: step.id, status: outcome?.deferred ? "deferred" : "ok" });

        return {
          stepId: step.id,
          status: outcome?.deferred ? "deferred" : "ok",
          attempts: attempt,
          durationMs,
          value: outcome?.value,
          usage: outcome?.usage ?? null,
          model: outcome?.model ?? null,
          mode: outcome?.mode ?? null,
          produced: step.produces,
        };
      } catch (error) {
        const durationMs = (clock?.now?.() ?? Date.now()) - stepStartedAt;
        budget?.charge?.({ ms: durationMs });

        if (error?.code === "budget_exceeded") {
          journal?.append(JOURNAL_KINDS.FAILURE, { stepId: step.id, error: error.message, attempt });
          return { stepId: step.id, status: "failed", attempts: attempt, error: error.message, errorCode: error.code };
        }

        const diagnosis = diagnose({ step, error, attempt });
        journal?.append(JOURNAL_KINDS.DIAGNOSIS, { stepId: step.id, attempt, ...diagnosis });

        const canRetry = diagnosis.retryable && attempt < attemptsAllowed;
        if (!canRetry) {
          journal?.append(JOURNAL_KINDS.FAILURE, {
            stepId: step.id,
            error: error?.message ?? String(error),
            attempt,
            cause: diagnosis.cause,
          });
          onProgress?.({ phase: "step.end", stepId: step.id, status: "failed", error: error?.message });
          return {
            stepId: step.id,
            status: "failed",
            attempts: attempt,
            durationMs,
            error: error?.message ?? String(error),
            errorCode: error?.code ?? "unknown_error",
            diagnosis,
          };
        }

        const delayMs = Number.isFinite(diagnosis.delayMs) ? diagnosis.delayMs : Math.min(2000, 250 * 2 ** (attempt - 1));
        journal?.append(JOURNAL_KINDS.RETRY, { stepId: step.id, attempt, delayMs, cause: diagnosis.cause });
        budget?.charge?.({ retries: 1 });
        lastError = error;
        logger?.debug?.(`retrying ${step.id} after ${delayMs}ms`, { cause: diagnosis.cause });
        try {
          await clock.delay(delayMs, { signal: controller.signal });
        } catch {
          return { stepId: step.id, status: "cancelled", attempts: attempt, error: "aborted during backoff" };
        }
      } finally {
        signal?.removeEventListener?.("abort", abortUpstream);
      }
    }

    return {
      stepId: step.id,
      status: "failed",
      attempts: attemptsAllowed,
      error: lastError?.message ?? "exhausted retries",
      errorCode: lastError?.code ?? "retries_exhausted",
    };
  }

  const durationMs = (clock?.now?.() ?? Date.now()) - startedAt;
  if (status === "ok" && failures.length > 0) status = "partial";

  return {
    planId: plan.id,
    workflow: plan.workflow,
    status,
    results,
    values: completedValues,
    failures,
    skipped: [...skipped],
    attempts,
    durationMs,
    order: schedule.order,
    concurrency: maxConcurrency,
  };
}

async function withTimeout(factory, { timeoutMs, clock, signal }) {
  const controller = new AbortController();
  const onAbort = () => controller.abort(signal?.reason);
  signal?.addEventListener?.("abort", onAbort, { once: true });

  const timeout = Number.isFinite(timeoutMs) && timeoutMs > 0 ? timeoutMs : null;
  let timerReject = null;
  const timerPromise = timeout
    ? new Promise((_, reject) => {
        timerReject = reject;
        clock
          .delay(timeout, { signal: controller.signal })
          .then(() => reject(new BudgetExceededError("ms", { limit: timeout, used: timeout, details: { step: "timeout" } })))
          .catch(() => {});
      })
    : null;

  try {
    const work = factory(controller.signal);
    return timerPromise ? await Promise.race([work, timerPromise]) : await work;
  } finally {
    if (timerReject) timerReject = null;
    controller.abort(new CancelledError("step finished"));
    signal?.removeEventListener?.("abort", onAbort);
  }
}

function pickDependencies(step, values) {
  const picked = {};
  for (const need of step.needs ?? []) {
    if (values[need] !== undefined) picked[need] = values[need];
  }
  return picked;
}

/**
 * Normalizes whatever a step reported into the flat shape the budget and the
 * journal speak: `{tokens, costUSD, toolCalls}`.
 *
 * Workers report usage as the runtime's telemetry report — `tokens` is a
 * `{input, output, reasoning, total}` object. That must be flattened, otherwise
 * real usage silently counts as nothing. A value that was not reported stays
 * `undefined` (absent) so it is never turned into a measured zero.
 */
export function normalizeUsage(usage) {
  if (!usage) return {};
  const out = {};
  const tokens = totalTokens(usage.tokens);
  if (tokens !== undefined) out.tokens = tokens;
  if (Number.isFinite(usage.costUSD)) out.costUSD = usage.costUSD;
  else if (usage.costUSD === null) out.costUSD = null;
  if (Number.isFinite(usage.toolCalls)) out.toolCalls = usage.toolCalls;
  return out;
}

function totalTokens(tokens) {
  if (Number.isFinite(tokens)) return tokens;
  if (!tokens || typeof tokens !== "object") return tokens === null ? null : undefined;
  if (Number.isFinite(tokens.total)) return tokens.total;
  const parts = [tokens.input, tokens.output, tokens.reasoning].filter((value) => Number.isFinite(value));
  return parts.length ? parts.reduce((sum, value) => sum + value, 0) : null;
}

function numOrNull(value) {
  return Number.isFinite(value) ? value : null;
}

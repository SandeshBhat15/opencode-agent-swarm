/**
 * Finite execution budgets.
 *
 * Two rules make this honest:
 *  1. Every dimension has a finite default. Nothing is unbounded.
 *  2. Usage that the runtime did not report is counted as *unknown*, never as
 *     zero. `used.tokens` only grows when a real number was observed, so a
 *     report of "0 tokens" means "measured zero", and `unknown.tokens > 0`
 *     means "telemetry was unavailable for N samples".
 */

import { err, ok } from "./result.js";
import { BudgetExceededError } from "./errors.js";

export const DEFAULT_LIMITS = Object.freeze({
  maxMs: 15 * 60 * 1000,
  maxTokens: 400_000,
  maxCostUSD: 2,
  maxToolCalls: 200,
  maxSteps: 24,
  maxRetries: 2,
  maxConcurrency: 3,
});

const DIMENSIONS = Object.freeze([
  ["maxMs", "ms"],
  ["maxTokens", "tokens"],
  ["maxCostUSD", "costUSD"],
  ["maxToolCalls", "toolCalls"],
  ["maxSteps", "steps"],
]);

/** A usage sample. Absent or null numeric fields are recorded as unknown. */
export function sample({ ms, tokens, costUSD, toolCalls, steps, retries } = {}) {
  return { ms, tokens, costUSD, toolCalls, steps, retries };
}

export function createBudget(limits = {}, { now = () => Date.now() } = {}) {
  const resolved = { ...DEFAULT_LIMITS };
  for (const [key, value] of Object.entries(limits)) {
    if (key in DEFAULT_LIMITS && Number.isFinite(value) && value >= 0) resolved[key] = value;
  }
  const startedAt = now();
  const used = { ms: 0, tokens: 0, costUSD: 0, toolCalls: 0, steps: 0, retries: 0 };
  const unknown = { tokens: 0, costUSD: 0, ms: 0 };

  function charge(usage = {}) {
    if (Number.isFinite(usage.ms)) used.ms += usage.ms;
    else if (usage.ms !== undefined) unknown.ms += 1;

    if (Number.isFinite(usage.tokens)) used.tokens += usage.tokens;
    else if (usage.tokens !== undefined) unknown.tokens += 1;

    if (Number.isFinite(usage.costUSD)) used.costUSD += usage.costUSD;
    else if (usage.costUSD !== undefined) unknown.costUSD += 1;

    if (Number.isFinite(usage.toolCalls)) used.toolCalls += usage.toolCalls;
    if (Number.isFinite(usage.steps)) used.steps += usage.steps;
    if (Number.isFinite(usage.retries)) used.retries += usage.retries;
    return used;
  }

  function firstExceeded() {
    for (const [limitKey, usedKey] of DIMENSIONS) {
      const limit = resolved[limitKey];
      if (Number.isFinite(limit) && used[usedKey] > limit) {
        return { dimension: usedKey, limit, used: used[usedKey] };
      }
    }
    return null;
  }

  return {
    limits: resolved,
    used,
    unknown,
    charge,
    /** Current wall-clock consumption including time since creation. */
    elapsed() {
      return used.ms + (now() - startedAt);
    },
    check() {
      const exceeded = firstExceeded();
      if (exceeded) return err(new BudgetExceededError(exceeded.dimension, exceeded));
      const elapsedNow = this.elapsed();
      if (elapsedNow > resolved.maxMs) {
        return err(
          new BudgetExceededError("ms", { limit: resolved.maxMs, used: elapsedNow }),
        );
      }
      return ok(true);
    },
    remaining() {
      const out = {};
      for (const [limitKey, usedKey] of DIMENSIONS) {
        out[usedKey] = Number.isFinite(resolved[limitKey])
          ? Math.max(0, resolved[limitKey] - used[usedKey])
          : Infinity;
      }
      return out;
    },
    snapshot() {
      return {
        limits: { ...resolved },
        used: { ...used, ms: this.elapsed() },
        unknown: { ...unknown },
      };
    },
  };
}

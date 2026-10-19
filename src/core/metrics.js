/**
 * Efficiency measurement.
 *
 * Efficiency is a stated requirement, so it is measured from actual execution
 * data (journal + budget), never estimated. Where a number could not be
 * observed it is reported as `null` with a matching `unknown` count.
 */

import { JOURNAL_KINDS } from "./journal.js";

function ratio(numerator, denominator) {
  if (!Number.isFinite(numerator) || !Number.isFinite(denominator) || denominator <= 0) return null;
  return numerator / denominator;
}

export function summarize(entries, { budget } = {}) {
  const counts = {
    decisions: 0,
    deterministicResolutions: 0,
    modelResolutions: 0,
    steps: 0,
    delegated: 0,
    direct: 0,
    retries: 0,
    failures: 0,
    repairs: 0,
    verifications: 0,
    verificationsPassed: 0,
  };

  let tokens = 0;
  let tokensObserved = false;
  let costUSD = 0;
  let costObserved = false;
  let toolCalls = 0;
  let durationMs = 0;
  const durations = [];
  const models = new Map();

  for (const entry of entries) {
    switch (entry.kind) {
      case JOURNAL_KINDS.DECISION:
        counts.decisions += 1;
        if (entry.data?.strategy === "deterministic") counts.deterministicResolutions += 1;
        if (entry.data?.strategy === "model") counts.modelResolutions += 1;
        break;
      case JOURNAL_KINDS.STEP_END:
        counts.steps += 1;
        if (Number.isFinite(entry.data?.durationMs)) {
          durationMs += entry.data.durationMs;
          durations.push(entry.data.durationMs);
        }
        break;
      case JOURNAL_KINDS.DELEGATE:
        counts.delegated += 1;
        break;
      case JOURNAL_KINDS.DIRECT:
        counts.direct += 1;
        break;
      case JOURNAL_KINDS.RETRY:
        counts.retries += 1;
        break;
      case JOURNAL_KINDS.FAILURE:
        counts.failures += 1;
        break;
      case JOURNAL_KINDS.REPAIR:
        counts.repairs += 1;
        break;
      case JOURNAL_KINDS.VERIFY:
        counts.verifications += 1;
        if (entry.data?.passed === true) counts.verificationsPassed += 1;
        break;
      case JOURNAL_KINDS.USAGE: {
        const usage = entry.data ?? {};
        if (Number.isFinite(usage.tokens)) {
          tokens += usage.tokens;
          tokensObserved = true;
        }
        if (Number.isFinite(usage.costUSD)) {
          costUSD += usage.costUSD;
          costObserved = true;
        }
        if (Number.isFinite(usage.toolCalls)) toolCalls += usage.toolCalls;
        const key = usage.model ? `${usage.model.providerID}/${usage.model.id}` : null;
        if (key) {
          const prev = models.get(key) ?? { model: key, steps: 0, tokens: 0, costUSD: 0 };
          prev.steps += Number.isFinite(usage.steps) ? usage.steps : 1;
          if (Number.isFinite(usage.tokens)) prev.tokens += usage.tokens;
          if (Number.isFinite(usage.costUSD)) prev.costUSD += usage.costUSD;
          models.set(key, prev);
        }
        break;
      }
      default:
        break;
    }
  }

  durations.sort((a, b) => a - b);
  const snapshot = budget?.snapshot?.();

  const efficiency = {
    /** Share of decisions settled by rules instead of a model call. Higher is cheaper. */
    deterministicShare: ratio(counts.deterministicResolutions, counts.decisions),
    /** Share of steps that ran in the submitting session instead of a child session. */
    directShare: ratio(counts.direct, counts.steps),
    retryRate: ratio(counts.retries, counts.steps),
    repairRate: ratio(counts.repairs, counts.steps),
    verificationPassRate: ratio(counts.verificationsPassed, counts.verifications),
    /** Cost per independently verified step; null until both sides are observed. */
    costPerVerifiedStep: costObserved
      ? ratio(costUSD, counts.verificationsPassed)
      : null,
    tokensPerVerifiedStep: tokensObserved ? ratio(tokens, counts.verificationsPassed) : null,
    durationP50Ms: durations.length ? durations[Math.floor((durations.length - 1) * 0.5)] : null,
  };

  return {
    counts,
    usage: {
      tokens: tokensObserved ? tokens : 0,
      tokensObserved,
      costUSD: costObserved ? costUSD : 0,
      costObserved,
      toolCalls,
      durationMs,
    },
    unknown: snapshot?.unknown ?? { tokens: 0, costUSD: 0, ms: 0 },
    budget: snapshot ?? null,
    models: [...models.values()].sort((a, b) => b.steps - a.steps),
    efficiency,
  };
}

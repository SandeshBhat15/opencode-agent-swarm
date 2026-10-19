/**
 * Model router.
 *
 * Chooses among the models the runtime actually reports, scored on the five
 * dimensions the task calls for: capability, context, latency, cost and
 * measured success.
 *
 * The central honesty rule: a dimension with no observed data is *dropped and
 * its weight redistributed*, never scored as zero. A model is therefore not
 * punished for having no history, and not rewarded for a lucky single run.
 */

import { buildCatalog, parseRef, ref } from "./catalog.js";

export const DEFAULT_WEIGHTS = Object.freeze({
  capability: 0.3,
  context: 0.15,
  latency: 0.15,
  cost: 0.15,
  success: 0.25,
});

/** Role-level requirements, derived from what each specialist must do. */
export const ROLE_REQUIREMENTS = Object.freeze({
  researcher: { needsTools: true, minContext: 32_000, web: true, complexity: 0.5 },
  builder: { needsTools: true, minContext: 64_000, edit: true, complexity: 0.6 },
  visual: { needsTools: true, minContext: 32_000, modality: "image", complexity: 0.5 },
  qc: { needsTools: true, minContext: 64_000, complexity: 0.7, independent: true },
  improve: { needsTools: true, minContext: 32_000, complexity: 0.4 },
  manager: { needsTools: true, minContext: 64_000, complexity: 0.6 },
});

export function createRouter({
  models = [],
  learning = null,
  weights = {},
  preferred = null,
  excluded = [],
} = {}) {
  const catalog = buildCatalog(models);
  const merged = { ...DEFAULT_WEIGHTS, ...weights };
  const excludedSet = new Set(excluded);

  function candidates(requirement = {}) {
    const {
      needsTools = true,
      minContext = 0,
      complexity = 0,
      preferredRef = preferred,
      exclude = [],
    } = requirement;

    const blocked = new Set([...excludedSet, ...exclude.map(toRef).filter(Boolean)]);
    const pool = catalog.usable({ requireTools: needsTools }).filter((model) => {
      if (blocked.has(ref(model))) return false;
      if (Number.isFinite(minContext) && minContext > 0) {
        // A model with an unreported context limit is not assumed infinite.
        if (model.limit.context === null) return minContext <= 8000;
        if (model.limit.context < minContext) return false;
      }
      return true;
    });

    if (preferredRef) {
      const preferredModel = catalog.get(preferredRef);
      if (preferredModel && !blocked.has(ref(preferredModel))) {
        pool.sort((a, b) => (ref(a) === ref(preferredModel) ? -1 : ref(b) === ref(preferredModel) ? 1 : 0));
      }
    }
    return pool;
  }

  /** Measured stats for a model, or nulls. Never synthesised. */
  function measured(model, requirement) {
    if (!learning) return { latencyMs: null, successRate: null, costPerStep: null, samples: 0 };
    return learning.modelStats(ref(model), { role: requirement.role ?? null, kind: requirement.kind ?? null });
  }

  function scoreModel(model, requirement) {
    const observed = measured(model, requirement);

    const components = {
      capability: capabilityScore(model, requirement),
      context: contextScore(model, requirement),
      cost: costScore(model, requirement, observed),
      latency: latencyScore(observed),
      success: successScore(observed),
    };

    // Drop unobserved dimensions and renormalise the remaining weights.
    let weightSum = 0;
    let weighted = 0;
    const used = {};
    const dropped = [];
    // Only the five declared dimensions are scored. A stray key in the weight
    // overrides (for example a model reference written by an older tuner
    // version) is ignored rather than poisoning the score with NaN.
    for (const key of Object.keys(components)) {
      const component = components[key];
      if (component === null || component === undefined) {
        dropped.push(key);
        continue;
      }
      const weight = Number.isFinite(merged[key]) ? merged[key] : DEFAULT_WEIGHTS[key];
      weightSum += weight;
      weighted += weight * component;
      used[key] = weight;
    }

    const score = weightSum > 0 ? weighted / weightSum : 0;
    return { score, components, dropped, usedWeights: used, observed };
  }

  function rankCandidates(requirement = {}) {
    return candidates(requirement)
      .map((model) => ({ model, ...scoreModel(model, requirement) }))
      .sort((a, b) => {
        if (b.score !== a.score) return b.score - a.score;
        if (b.observed.samples !== a.observed.samples) return b.observed.samples - a.observed.samples;
        const costA = a.model.cost?.input ?? Infinity;
        const costB = b.model.cost?.input ?? Infinity;
        if (costA !== costB) return costA - costB;
        return ref(a.model).localeCompare(ref(b.model));
      });
  }

  function route(requirement = {}) {
    const ranked = rankCandidates(requirement);
    if (ranked.length === 0) return null;
    const [best, ...rest] = ranked;
    return {
      ref: ref(best.model),
      model: best.model,
      variant: best.model.variants?.[0] ?? null,
      score: round(best.score),
      components: mapValues(best.components, roundOrNull),
      dropped: best.dropped,
      observed: best.observed,
      rationale: explain(best, requirement),
      alternatives: rest.slice(0, 3).map((entry) => ({ ref: ref(entry.model), score: round(entry.score) })),
    };
  }

  function fallbackChain(requirement = {}, limit = 3) {
    return rankCandidates(requirement)
      .slice(0, limit)
      .map((entry) => ref(entry.model));
  }

  function describe(requirement = {}) {
    const best = route(requirement);
    if (!best) return { available: false, reason: "no model satisfies the requirement" };
    return {
      available: true,
      chosen: best.ref,
      score: best.score,
      droppedSignals: best.dropped,
      candidateCount: candidates(requirement).length,
    };
  }

  return {
    catalog,
    weights: merged,
    candidates,
    route,
    describe,
    rankCandidates,
    fallbackChain,
    routeForRole(role, extra = {}) {
      const requirement = { ...(ROLE_REQUIREMENTS[role] ?? {}), role, ...extra };
      return route(requirement);
    },
  };
}

function explain(entry, requirement) {
  const parts = [`score ${round(entry.score)}`];
  if (entry.dropped.length) parts.push(`no data for: ${entry.dropped.join(", ")}`);
  if (entry.observed.samples > 0) parts.push(`${entry.observed.samples} observed runs`);
  if (requirement.independent) parts.push("independent of producer");
  return parts.join("; ");
}

function capabilityScore(model, requirement) {
  // Everything in the candidate pool already has tools; score what remains.
  let score = 1;
  if (requirement.modality && !model.capabilities.input.includes(requirement.modality)) {
    // Text-only fallback is still possible for visual work, but it is a real penalty.
    score -= 0.35;
  }
  if (requirement.complexity >= 0.6) {
    // Capability proxy: a larger declared context usually tracks a larger model.
    const context = model.limit.context ?? 0;
    score *= context >= 200_000 ? 1 : context >= 100_000 ? 0.9 : 0.75;
  }
  return clamp(score, 0, 1);
}

function contextScore(model, requirement) {
  if (model.limit.context === null) return null;
  const need = requirement.minContext ?? 0;
  if (need <= 0) return 1;
  const headroom = model.limit.context / need;
  return clamp(Math.min(1, headroom / 4), 0, 1);
}

function costScore(model, requirement, observed) {
  const measuredCost = observed?.costPerStep;
  if (Number.isFinite(measuredCost) && measuredCost > 0) {
    return clamp(1 / (1 + measuredCost / 0.01), 0, 1);
  }
  const cost = model.cost;
  if (!cost) return null;
  if (cost.input === null && cost.output === null) return null;
  if (cost.input === 0 && cost.output === 0) return 1; // genuinely free and measured
  const blended = (cost.input ?? 0) * 0.6 + (cost.output ?? 0) * 0.4;
  if (!Number.isFinite(blended)) return null;
  return clamp(1 / (1 + blended / 3), 0, 1); // USD per million tokens
}

function latencyScore(observed) {
  if (!Number.isFinite(observed?.latencyP50Ms)) return null;
  return clamp(1 / (1 + observed.latencyP50Ms / 20_000), 0, 1);
}

function successScore(observed) {
  if (!Number.isFinite(observed?.successRate) || observed.samples < 2) return null;
  // Shrink towards 0.5 with few samples so one lucky run cannot win the route.
  const prior = 0.5;
  const weight = observed.samples / (observed.samples + 5);
  return clamp(prior * (1 - weight) + observed.successRate * weight, 0, 1);
}

function toRef(value) {
  if (typeof value === "string") return value.includes("/") ? value : null;
  const parsed = parseRef(`${value?.providerID}/${value?.id}`);
  return parsed ? `${parsed.providerID}/${parsed.id}` : null;
}

function mapValues(object, fn) {
  return Object.fromEntries(Object.entries(object).map(([key, value]) => [key, fn(value)]));
}

function round(value) {
  return Math.round(value * 1000) / 1000;
}

function roundOrNull(value) {
  return value === null ? null : round(value);
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

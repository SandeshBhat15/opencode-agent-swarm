/**
 * Evidence-based learning.
 *
 * Outcomes are recorded from the journal and aggregated per model, role and
 * workflow. Two rules keep this trustworthy:
 *
 *  1. the router reads a *synchronous in-memory snapshot* — persistence is
 *     fire-and-forget, so routing never blocks on disk;
 *  2. averages are computed only over *observed* samples. `samples` is
 *     reported alongside every metric, and a metric with no samples is `null`,
 *     never 0. The router then drops that dimension instead of scoring it.
 */

const OUTCOMES_KEY = "learning/outcomes";
const DEFAULT_MAX = 500;
/** Smallest subset considered representative before falling back to the wider set. */
const ROLE_SAMPLE_FLOOR = 3;

export function createLearning({ store = null, logger = null, maxOutcomes = DEFAULT_MAX } = {}) {
  let outcomes = [];
  let loaded = false;
  let revision = 0;
  const cache = new Map();

  function invalidate() {
    revision += 1;
    cache.clear();
  }

  async function load() {
    if (!store) {
      loaded = true;
      return outcomes;
    }
    outcomes = (await store.get(OUTCOMES_KEY, [])) ?? [];
    if (!Array.isArray(outcomes)) outcomes = [];
    loaded = true;
    invalidate();
    return outcomes;
  }

  function record(outcome) {
    const entry = {
      at: outcome.at ?? Date.now(),
      kind: outcome.kind ?? null,
      workflow: outcome.workflow ?? null,
      role: outcome.role ?? null,
      model: outcome.model ?? null,
      success: outcome.success === true ? true : outcome.success === false ? false : null,
      durationMs: Number.isFinite(outcome.durationMs) ? outcome.durationMs : null,
      tokens: Number.isFinite(outcome.tokens) ? outcome.tokens : null,
      costUSD: Number.isFinite(outcome.costUSD) ? outcome.costUSD : null,
      retries: Number.isFinite(outcome.retries) ? outcome.retries : 0,
      toolCalls: Number.isFinite(outcome.toolCalls) ? outcome.toolCalls : null,
    };
    outcomes.push(entry);
    if (outcomes.length > maxOutcomes) outcomes = outcomes.slice(outcomes.length - maxOutcomes);
    invalidate();

    if (store) {
      // Fire and forget: learning must never slow down or fail the run.
      store
        .set(OUTCOMES_KEY, outcomes)
        .catch((error) => logger?.warn?.(`learning persist failed: ${error?.message ?? error}`));
    }
    return entry;
  }

  function select({ model = null, role = null, kind = null, workflow = null } = {}) {
    return outcomes.filter((entry) => {
      if (model && entry.model !== model) return false;
      if (role && entry.role !== role) return false;
      if (kind && entry.kind !== kind) return false;
      if (workflow && entry.workflow !== workflow) return false;
      return true;
    });
  }

  function aggregate(samples) {
    const withSuccess = samples.filter((entry) => entry.success !== null);
    const successes = withSuccess.filter((entry) => entry.success).length;
    const durations = samples.map((entry) => entry.durationMs).filter(Number.isFinite).sort((a, b) => a - b);
    const costs = samples.map((entry) => entry.costUSD).filter(Number.isFinite);
    const tokens = samples.map((entry) => entry.tokens).filter(Number.isFinite);
    const retries = samples.reduce((total, entry) => total + (Number.isFinite(entry.retries) ? entry.retries : 0), 0);

    return {
      samples: samples.length,
      successSamples: withSuccess.length,
      successRate: withSuccess.length ? successes / withSuccess.length : null,
      latencyP50Ms: durations.length ? durations[Math.floor((durations.length - 1) * 0.5)] : null,
      latencyMaxMs: durations.length ? durations[durations.length - 1] : null,
      /** Mean over observed cost only; null when nothing was observed. */
      costPerStep: costs.length ? costs.reduce((a, b) => a + b, 0) / costs.length : null,
      costSamples: costs.length,
      tokensPerStep: tokens.length ? tokens.reduce((a, b) => a + b, 0) / tokens.length : null,
      tokenSamples: tokens.length,
      retriesPerStep: samples.length ? retries / samples.length : null,
      /** Explicit marker so callers cannot mistake "unreported" for "zero". */
      unknown: {
        outcome: samples.length - withSuccess.length,
        cost: samples.length - costs.length,
        tokens: samples.length - tokens.length,
      },
    };
  }

  function memo(key, producer) {
    const cached = cache.get(key);
    if (cached && cached.revision === revision) return cached.value;
    const value = producer();
    cache.set(key, { revision, value });
    return value;
  }

  return {
    async load() {
      return load();
    },
    get loaded() {
      return loaded;
    },
    record,
    /** Synchronous: safe to call from the router. */
    modelStats(modelRef, { role = null, kind = null } = {}) {
      if (!modelRef) return aggregate([]);
      const key = `model:${modelRef}:${role ?? ""}:${kind ?? ""}`;
      return memo(key, () => {
        const forModel = select({ model: modelRef });
        const narrow = forModel.filter(
          (entry) => (!role || entry.role === role) && (!kind || entry.kind === kind),
        );
        const chosen = narrow.length >= ROLE_SAMPLE_FLOOR ? narrow : forModel;
        return { ...aggregate(chosen), scope: narrow.length >= ROLE_SAMPLE_FLOOR ? "role" : "model" };
      });
    },
    roleStats(role) {
      return memo(`role:${role}`, () => aggregate(select({ role })));
    },
    workflowStats(workflow) {
      return memo(`workflow:${workflow}`, () => aggregate(select({ workflow })));
    },
    kindStats(kind) {
      return memo(`kind:${kind}`, () => aggregate(select({ kind })));
    },
    overall() {
      return memo("overall", () => aggregate(outcomes));
    },
    all() {
      return [...outcomes];
    },
    get size() {
      return outcomes.length;
    },
    revision() {
      return revision;
    },
    /** Distinct models that have at least one observed outcome. */
    knownModels() {
      const set = new Set();
      for (const entry of outcomes) if (entry.model) set.add(entry.model);
      return [...set].sort();
    },
  };
}

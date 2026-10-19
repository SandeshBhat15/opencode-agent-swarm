/**
 * Improvement proposals.
 *
 * A proposal is derived from measured outcomes only. Every proposal carries the
 * sample size it rests on and the exact size of the change it wants, so the
 * gate layer can reject anything thin, unbounded or irreversible.
 *
 * Design note: this is the Harness² idea — measured proposals, bounded
 * experiments, an evidence ledger — implemented in-tree with no external
 * runtime dependency.
 */

export const PROPOSAL_KINDS = Object.freeze({
  MODEL_EXCLUDE: "model_exclude",
  MODEL_PREFER: "model_prefer",
  RETRY_POLICY: "retry_policy",
  WORKFLOW_PREFER: "workflow_prefer",
  WORKFLOW_AVOID: "workflow_avoid",
});

/**
 * @param {object} options
 * @param {object} options.learning the learning store
 * @param {object} options.tuner current tuner state (for conflict checks)
 * @param {object} options.policy thresholds
 */
export function propose({ learning, tuner = null, policy = {} } = {}) {
  const thresholds = {
    minSamples: policy.minSamples ?? 8,
    lowSuccessRate: policy.lowSuccessRate ?? 0.6,
    highSuccessRate: policy.highSuccessRate ?? 0.85,
    minEffect: policy.minEffect ?? 0.15,
    ...policy,
  };
  const current = tuner?.get?.() ?? null;
  const proposals = [];

  for (const role of ["researcher", "builder", "visual", "qc", "improve"]) {
    const candidates = [];
    for (const model of learning.knownModels()) {
      const stats = learning.modelStats(model, { role });
      if (stats.samples < thresholds.minSamples) continue;
      candidates.push({ model, stats });
    }
    if (!candidates.length) continue;

    const best = [...candidates].sort((a, b) => (b.stats.successRate ?? 0) - (a.stats.successRate ?? 0))[0];

    for (const candidate of candidates) {
      const rate = candidate.stats.successRate;
      if (rate === null) continue;
      if (rate <= thresholds.lowSuccessRate && (best.stats.successRate ?? 0) - rate >= thresholds.minEffect) {
        if (current?.router?.excluded?.includes(candidate.model)) continue;
        proposals.push({
          id: `exclude:${role}:${candidate.model}`,
          kind: PROPOSAL_KINDS.MODEL_EXCLUDE,
          summary: `${candidate.model} fails ${Math.round((1 - rate) * 100)}% of ${role} steps; route ${role} elsewhere`,
          target: { role, model: candidate.model },
          action: { model: candidate.model, role },
          evidence: {
            samples: candidate.stats.samples,
            metric: "successRate",
            value: round(rate),
            baseline: round(best.stats.successRate ?? 0),
            baselineModel: best.model,
          },
          expectedGain: round(Math.min(0.3, (best.stats.successRate ?? 0) - rate) * 0.5),
          risk: "medium",
          reversible: true,
        });
      }
    }

    if (best.stats.successRate !== null && best.stats.successRate >= thresholds.highSuccessRate && candidates.length > 1) {
      proposals.push({
        id: `prefer:${role}:${best.model}`,
        kind: PROPOSAL_KINDS.MODEL_PREFER,
        summary: `${best.model} leads ${role} at ${Math.round(best.stats.successRate * 100)}% success; raise its weight`,
        target: { role, model: best.model },
        action: { model: best.model, role, weight: 1.15 },
        evidence: {
          samples: best.stats.samples,
          metric: "successRate",
          value: round(best.stats.successRate),
          baseline: round(thresholds.highSuccessRate),
        },
        expectedGain: 0.05,
        risk: "low",
        reversible: true,
      });
    }
  }

  const overall = learning.overall();
  if ((overall.samples ?? 0) >= thresholds.minSamples && overall.retriesPerStep !== null) {
    if (overall.retriesPerStep >= 0.5 && overall.successRate !== null && overall.successRate < 0.9) {
      proposals.push({
        id: "retry:reduce",
        kind: PROPOSAL_KINDS.RETRY_POLICY,
        summary: `retries are firing ${overall.retriesPerStep.toFixed(2)} times per step without lifting success`,
        target: { policy: "retry" },
        action: { maxRetries: 1, previous: 2 },
        evidence: {
          samples: overall.samples,
          metric: "retriesPerStep",
          value: round(overall.retriesPerStep),
          baseline: 0.5,
        },
        expectedGain: 0.1,
        risk: "low",
        reversible: true,
      });
    }
  }

  const workflowIds = [...new Set(learning.all().map((entry) => entry.workflow).filter(Boolean))];
  for (const kind of [...new Set(learning.all().map((entry) => entry.kind).filter(Boolean))]) {
    const perWorkflow = workflowIds
      .map((workflow) => ({ workflow, stats: learning.workflowStats(workflow) }))
      .filter((entry) => entry.stats.samples >= thresholds.minSamples);
    const cheap = perWorkflow.filter((entry) => entry.stats.successRate !== null && entry.stats.successRate >= thresholds.highSuccessRate - 0.05);
    const expensive = perWorkflow.filter((entry) => entry.stats.successRate !== null && entry.stats.successRate >= thresholds.highSuccessRate - 0.05);
    if (cheap.length > 1 && expensive.length > 1) {
      const priced = cheap.filter((entry) => entry.stats.costPerStep !== null).sort((a, b) => a.stats.costPerStep - b.stats.costPerStep);
      const priciest = [...priced].reverse()[0];
      const cheapest = priced[0];
      if (priciest && cheapest && priciest.workflow !== cheapest.workflow && priciest.stats.costPerStep > cheapest.stats.costPerStep * 1.5) {
        proposals.push({
          id: `workflow:prefer:${kind}:${cheapest.workflow}`,
          kind: PROPOSAL_KINDS.WORKFLOW_PREFER,
          summary: `${cheapest.workflow} matches ${priciest.workflow} on success at ${Math.round(cheapest.stats.costPerStep * 1e6) / 1e3} vs ${Math.round(priciest.stats.costPerStep * 1e6) / 1e3} micro-USD per step`,
          target: { kind, workflow: cheapest.workflow },
          action: { kind, workflow: cheapest.workflow },
          evidence: {
            samples: cheapest.stats.samples,
            metric: "costPerStep",
            value: cheapest.stats.costPerStep,
            baseline: priciest.stats.costPerStep,
            comparedWorkflow: priciest.workflow,
          },
          expectedGain: 0.15,
          risk: "low",
          reversible: true,
        });
      }
    }
  }

  return proposals;
}

function round(value) {
  return Number.isFinite(value) ? Math.round(value * 1000) / 1000 : null;
}

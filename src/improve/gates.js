/**
 * Improvement gates.
 *
 * Nothing reaches the tuner without passing every gate. This is what makes the
 * self-improvement *controlled* rather than merely automatic: thin evidence,
 * oversized deltas, irreversible edits and capability loss are all rejected
 * with a stated reason.
 */

export const DEFAULT_POLICY = Object.freeze({
  /** Outcomes that must support a proposal before it is considered. */
  minSamples: 8,
  /** Smallest change worth making. */
  minEffect: 0.1,
  /** At most this many changes may be applied per improvement run. */
  maxChangesPerRun: 1,
  /** A model may only be excluded when this many alternatives remain. */
  minAlternatives: 1,
  /** Requires an explicit approval token (manager/human in the loop). */
  requireApproval: false,
  /** Weight changes must stay inside this multiplicative band. */
  weightBand: [0.75, 1.5],
  /** Retry policy must stay in this absolute range. */
  retryRange: [0, 5],
});

function gate(id, passed, detail) {
  return { id, passed, detail };
}

export function evaluateGates(proposal, {
  policy = {},
  tuner = null,
  learning = null,
  approvals = [],
} = {}) {
  const rules = { ...DEFAULT_POLICY, ...policy };
  const gates = [];
  const evidence = proposal.evidence ?? {};

  gates.push(
    gate(
      "samples",
      Number.isFinite(evidence.samples) && evidence.samples >= rules.minSamples,
      `evidence has ${evidence.samples ?? 0} sample(s); ${rules.minSamples} required`,
    ),
  );

  const effect = measuredEffect(proposal);
  gates.push(
    gate(
      "effect",
      Number.isFinite(effect) && Math.abs(effect) >= rules.minEffect,
      `measured effect ${effect === null ? "unknown" : effect}; minimum ${rules.minEffect}`,
    ),
  );

  gates.push(
    gate(
      "reversible",
      proposal.reversible !== false,
      proposal.reversible === false ? "proposal declares itself irreversible" : "change can be reverted",
    ),
  );

  gates.push(gate("bounded", isBounded(proposal, rules), describeBound(proposal, rules)));

  if (proposal.kind === "model_exclude") {
    const known = learning?.knownModels?.() ?? [];
    const excluded = tuner?.get?.()?.router?.excluded ?? [];
    const remaining = known.filter((model) => !excluded.includes(model) && model !== proposal.action?.model).length;
    gates.push(
      gate(
        "capability_preserved",
        remaining >= rules.minAlternatives,
        `${remaining} alternative model(s) remain; ${rules.minAlternatives} required`,
      ),
    );
  }

  const history = tuner?.history?.() ?? [];
  const alreadyApplied = history.some(
    (entry) => entry.kind === proposal.kind && entry.id === proposal.id && entry.kind !== "revert",
  );
  const reverted = history.some((entry) => entry.id === `${proposal.id}:revert`);
  gates.push(
    gate(
      "not_duplicate",
      !alreadyApplied || reverted,
      alreadyApplied && !reverted ? "already applied and not reverted" : "not currently applied",
    ),
  );

  const approved = !rules.requireApproval || approvals.includes(proposal.id) || approvals.includes(proposal.kind);
  gates.push(
    gate("approval", approved, rules.requireApproval ? (approved ? "approved" : "awaiting approval") : "approval not required"),
  );

  const failed = gates.filter((entry) => !entry.passed);
  return {
    allowed: failed.length === 0,
    gates,
    reasons: failed.map((entry) => `${entry.id}: ${entry.detail}`),
  };
}

export function measuredEffect(proposal) {
  const evidence = proposal.evidence ?? {};
  const value = evidence.value;
  const baseline = evidence.baseline;
  // Rounded so that a delta which is exactly the threshold is not rejected by
  // binary floating point error (0.6 - 0.5 is 0.09999999999999998).
  if (Number.isFinite(value) && Number.isFinite(baseline)) return round(Math.abs(value - baseline));
  if (Number.isFinite(proposal.expectedGain)) return proposal.expectedGain;
  return null;
}

function round(value) {
  return Number.isFinite(value) ? Math.round(value * 1e6) / 1e6 : null;
}

function isBounded(proposal, rules) {
  const action = proposal.action ?? {};
  if (proposal.kind === "model_prefer") {
    const weight = action.weight;
    return Number.isFinite(weight) && weight >= rules.weightBand[0] && weight <= rules.weightBand[1];
  }
  if (proposal.kind === "retry_policy") {
    const retries = action.maxRetries;
    return Number.isInteger(retries) && retries >= rules.retryRange[0] && retries <= rules.retryRange[1];
  }
  if (proposal.kind === "model_exclude") return typeof action.model === "string" && action.model.length > 0;
  if (proposal.kind === "workflow_prefer" || proposal.kind === "workflow_avoid") {
    return typeof action.workflow === "string" && action.workflow.length > 0;
  }
  return false;
}

function describeBound(proposal, rules) {
  if (proposal.kind === "model_prefer") {
    const weight = proposal.action?.weight;
    return `weight ${weight} must sit in [${rules.weightBand[0]}, ${rules.weightBand[1]}]`;
  }
  if (proposal.kind === "retry_policy") {
    return `maxRetries ${proposal.action?.maxRetries} must be an integer in [${rules.retryRange[0]}, ${rules.retryRange[1]}]`;
  }
  return "change is within the supported parameter space";
}

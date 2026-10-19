/**
 * Bounded experiments.
 *
 * A gate-approved change is not trusted because it was approved; it is trusted
 * only after it proves itself on new evidence. Every applied change opens an
 * experiment with a metric, a baseline and a sample window. When the window
 * closes the change is either confirmed or automatically reverted.
 */

import { JOURNAL_KINDS } from "../core/journal.js";

export function openExperiment({ proposal, learning, clock, policy = {} }) {
  return {
    id: `exp:${proposal.id}:${clock?.now?.() ?? Date.now()}`,
    proposalId: proposal.id,
    kind: proposal.kind,
    metric: proposal.evidence?.metric ?? "successRate",
    baseline: proposal.evidence?.value ?? null,
    direction: directionFor(proposal),
    samplesAtStart: learning?.overall?.().samples ?? 0,
    minSamples: policy.experimentSamples ?? 6,
    deadlineAt: (clock?.now?.() ?? Date.now()) + (policy.experimentWindowMs ?? 3_600_000),
    status: "running",
    openedAt: clock?.now?.() ?? Date.now(),
  };
}

function directionFor(proposal) {
  if (proposal.kind === "model_exclude" || proposal.kind === "workflow_avoid") return "success_up";
  if (proposal.kind === "retry_policy") return "retries_down";
  if (proposal.kind === "workflow_prefer") return "cost_down";
  return "success_up";
}

/**
 * Closes experiments whose window has elapsed, or that have enough new
 * evidence. Reverts anything that did not hold up.
 */
export async function reviewExperiments({ tuner, learning, journal = null, clock, policy = {} }) {
  const reviews = [];
  for (const experiment of tuner.experiments()) {
    if (experiment.status !== "running") continue;

    const observed = learning.overall().samples - experiment.samplesAtStart;
    const now = clock?.now?.() ?? Date.now();
    const windowClosed = now >= experiment.deadlineAt;
    const enoughEvidence = observed >= (experiment.minSamples ?? 6);
    if (!windowClosed && !enoughEvidence) {
      reviews.push({ id: experiment.id, status: "running", observed, needed: experiment.minSamples - observed });
      continue;
    }

    const verdict = judge(experiment, learning);
    if (verdict.held) {
      await tuner.closeExperiment(experiment.id, { status: "confirmed", observed, at: now, detail: verdict.detail });
      journal?.append(JOURNAL_KINDS.IMPROVE, { phase: "experiment.confirmed", id: experiment.id, ...verdict });
      reviews.push({ id: experiment.id, status: "confirmed", ...verdict });
    } else {
      await tuner.revert(experiment.proposalId, { reason: `experiment did not hold: ${verdict.detail}` });
      await tuner.closeExperiment(experiment.id, { status: "reverted", observed, at: now, detail: verdict.detail });
      journal?.append(JOURNAL_KINDS.IMPROVE, { phase: "experiment.reverted", id: experiment.id, ...verdict });
      reviews.push({ id: experiment.id, status: "reverted", ...verdict });
    }
  }
  return reviews;
}

export function judge(experiment, learning) {
  const overall = learning.overall();
  switch (experiment.direction) {
    case "cost_down": {
      if (overall.costPerStep === null || experiment.baseline === null) {
        return { held: true, detail: "no cost evidence either way; kept (no regression measured)", metric: "costPerStep", value: null };
      }
      const held = overall.costPerStep <= experiment.baseline * 1.05;
      return {
        held,
        detail: `cost per step ${round(overall.costPerStep)} vs baseline ${round(experiment.baseline)}`,
        metric: "costPerStep",
        value: round(overall.costPerStep),
      };
    }
    case "retries_down": {
      const value = overall.retriesPerStep;
      if (value === null) return { held: true, detail: "no retry evidence; kept", metric: "retriesPerStep", value: null };
      const held = value <= (experiment.baseline ?? 0.5) * 1.1;
      return { held, detail: `retries per step ${round(value)} vs baseline ${round(experiment.baseline)}`, metric: "retriesPerStep", value: round(value) };
    }
    default: {
      const value = overall.successRate;
      if (value === null) return { held: true, detail: "no success evidence; kept", metric: "successRate", value: null };
      const held = value >= (experiment.baseline ?? 0.5) - 0.05;
      return { held, detail: `success rate ${round(value)} vs baseline ${round(experiment.baseline)}`, metric: "successRate", value: round(value) };
    }
  }
}

function round(value) {
  return Number.isFinite(value) ? Math.round(value * 1000) / 1000 : null;
}

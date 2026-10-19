/**
 * The improvement loop.
 *
 * One pass:
 *   1. close any experiment whose window elapsed (revert what did not hold),
 *   2. derive proposals from measured outcomes,
 *   3. gate them; apply at most `maxChangesPerRun`,
 *   4. open a bounded experiment for each applied change.
 *
 * The loop never edits its own code and never edits agent pages. It only moves
 * bounded, reversible parameters that the router and executor already read.
 */

import { JOURNAL_KINDS } from "../core/journal.js";
import { DEFAULT_POLICY, evaluateGates } from "./gates.js";
import { openExperiment, reviewExperiments } from "./experiment.js";
import { propose } from "./proposals.js";

export async function runImprovement({
  learning,
  tuner,
  journal = null,
  clock = null,
  policy = {},
  approvals = [],
  dryRun = false,
} = {}) {
  const rules = { ...DEFAULT_POLICY, ...policy };
  const experiments = await reviewExperiments({ tuner, learning, journal, clock, policy: rules });

  const proposals = propose({ learning, tuner, policy: rules });
  const evaluated = proposals.map((proposal) => ({
    proposal,
    gate: evaluateGates(proposal, { policy: rules, tuner, learning, approvals }),
  }));

  const allowed = evaluated
    .filter((entry) => entry.gate.allowed)
    .sort((a, b) => (b.proposal.expectedGain ?? 0) - (a.proposal.expectedGain ?? 0));

  const rejected = evaluated
    .filter((entry) => !entry.gate.allowed)
    .map((entry) => ({ id: entry.proposal.id, kind: entry.proposal.kind, reasons: entry.gate.reasons }));

  const budget = Math.max(0, Math.floor(rules.maxChangesPerRun) || 0);
  const toApply = allowed.slice(0, budget);
  const deferred = allowed.slice(budget).map((entry) => entry.proposal.id);

  const applied = [];
  if (!dryRun) {
    for (const { proposal } of toApply) {
      await tuner.apply(proposal, { evidence: proposal.evidence, policyId: "default" });
      const experiment = openExperiment({ proposal, learning, clock, policy: rules });
      await tuner.openExperiment(experiment);
      applied.push({ id: proposal.id, kind: proposal.kind, summary: proposal.summary, experiment: experiment.id });
      journal?.append(JOURNAL_KINDS.IMPROVE, {
        phase: "applied",
        id: proposal.id,
        kind: proposal.kind,
        summary: proposal.summary,
        evidence: proposal.evidence,
        experiment: experiment.id,
      });
    }
  }

  return {
    proposals: proposals.length,
    applied,
    deferred,
    rejected,
    experiments,
    dryRun,
    policy: rules,
  };
}

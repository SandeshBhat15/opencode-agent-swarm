/**
 * Tuner state.
 *
 * Everything the swarm may tune about itself lives here, versioned and
 * reversible. Changes are recorded with the evidence and the policy that
 * allowed them, so any later reader can reconstruct why a weight changed.
 */

const STATE_KEY = "improve/state";

export const INITIAL_STATE = Object.freeze({
  version: 0,
  updatedAt: null,
  router: { weights: {}, excluded: [], preferred: null },
  retry: { maxRetries: null },
  workflows: { prefer: {}, avoid: {} },
  history: [],
  experiments: [],
});

export function createTuner({ store = null, logger = null, clock = null } = {}) {
  let state = clone(INITIAL_STATE);

  return {
    async load() {
      if (!store) return state;
      const persisted = await store.get(STATE_KEY, null);
      if (persisted && typeof persisted === "object") {
        state = { ...clone(INITIAL_STATE), ...persisted };
      }
      return state;
    },
    get() {
      return clone(state);
    },
    /** Current overrides in the shape the router and executor consume. */
    overrides() {
      return {
        weights: state.router.weights ?? {},
        excluded: state.router.excluded ?? [],
        preferred: state.router.preferred ?? null,
        retry: Number.isFinite(state.retry.maxRetries) ? { maxRetries: state.retry.maxRetries } : {},
        workflowPreference: (kind) => state.workflows.prefer?.[kind] ?? null,
        workflowAvoid: (kind) => state.workflows.avoid?.[kind] ?? null,
      };
    },
    async apply(proposal, { evidence = null, policyId = null, experiment = null } = {}) {
      applyProposalToState(state, proposal);
      state.version += 1;
      state.updatedAt = clock?.now?.() ?? Date.now();
      const record = {
        version: state.version,
        at: state.updatedAt,
        kind: proposal.kind,
        id: proposal.id,
        summary: proposal.summary,
        action: proposal.action,
        evidence,
        policyId,
        reversible: proposal.reversible !== false,
      };
      state.history.push(record);
      if (state.history.length > 200) state.history = state.history.slice(-200);
      if (experiment) {
        state.experiments.push(experiment);
        if (state.experiments.length > 50) state.experiments = state.experiments.slice(-50);
      }
      await persist();
      return record;
    },
    /** Undoes a previously applied proposal by kind + target. */
    async revert(proposalId, { reason = "reverted" } = {}) {
      const record = [...state.history].reverse().find((entry) => entry.id === proposalId);
      if (!record) return { reverted: false, reason: "not found" };
      applyProposalToState(state, invertProposal(record));
      state.version += 1;
      state.updatedAt = clock?.now?.() ?? Date.now();
      state.history.push({
        version: state.version,
        at: state.updatedAt,
        kind: "revert",
        id: `${proposalId}:revert`,
        summary: `reverted ${proposalId} (${reason})`,
        action: record.action,
        reversible: true,
      });
      state.experiments = state.experiments.map((experiment) =>
        experiment.proposalId === proposalId ? { ...experiment, status: "reverted", reason } : experiment,
      );
      await persist();
      return { reverted: true, from: record };
    },
    async openExperiment(experiment) {
      state.experiments.push(experiment);
      await persist();
      return experiment;
    },
    async closeExperiment(id, patch) {
      state.experiments = state.experiments.map((experiment) =>
        experiment.id === id ? { ...experiment, ...patch } : experiment,
      );
      await persist();
      return state.experiments.find((experiment) => experiment.id === id) ?? null;
    },
    history() {
      return [...state.history];
    },
    experiments() {
      return [...state.experiments];
    },
  };

  async function persist() {
    if (!store) return;
    try {
      await store.set(STATE_KEY, state);
    } catch (error) {
      logger?.warn?.(`tuner persist failed: ${error?.message ?? error}`);
    }
  }
}

function applyProposalToState(state, proposal) {
  const action = proposal.action ?? {};
  switch (proposal.kind) {
    case "model_exclude": {
      const set = new Set(state.router.excluded ?? []);
      set.add(action.model);
      state.router.excluded = [...set];
      break;
    }
    case "model_include": {
      const set = new Set(state.router.excluded ?? []);
      set.delete(action.model);
      state.router.excluded = [...set];
      break;
    }
    case "model_prefer": {
      state.router.preferred = action.model ?? null;
      break;
    }
    case "prefer_reset": {
      state.router.preferred = action.previous ?? null;
      break;
    }
    case "retry_policy": {
      state.retry.maxRetries = action.maxRetries;
      break;
    }
    case "workflow_prefer": {
      state.workflows.prefer = { ...(state.workflows.prefer ?? {}), [action.kind]: action.workflow };
      break;
    }
    case "workflow_avoid": {
      state.workflows.avoid = { ...(state.workflows.avoid ?? {}), [action.kind]: action.workflow };
      break;
    }
    default:
      break;
  }
}

function invertProposal(record) {
  const action = record.action ?? {};
  switch (record.kind) {
    case "model_exclude":
      return { kind: "model_include", action };
    case "model_prefer":
      return { kind: "prefer_reset", action: { previous: null } };
    case "retry_policy":
      return { kind: "retry_policy", action: { maxRetries: action.previous ?? null } };
    case "workflow_prefer":
      return { kind: "workflow_avoid", action };
    case "workflow_avoid":
      return { kind: "workflow_prefer", action };
    default:
      return { kind: record.kind, action: {} };
  }
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

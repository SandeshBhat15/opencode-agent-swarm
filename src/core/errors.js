/**
 * Error taxonomy. Every error carries a stable `code` so that decisions
 * (retry? repair? stop?) can be driven by data rather than by string matching.
 */

export class SwarmError extends Error {
  constructor(message, { code = "swarm_error", details } = {}) {
    super(message);
    this.name = new.target.name;
    this.code = code;
    if (details !== undefined) this.details = details;
  }
}

/** A finite budget dimension was exhausted. */
export class BudgetExceededError extends SwarmError {
  constructor(dimension, { limit, used, details } = {}) {
    super(`budget exceeded: ${dimension} (used ${used} of ${limit})`, {
      code: "budget_exceeded",
      details: { dimension, limit, used, ...(details ?? {}) },
    });
    this.dimension = dimension;
    this.limit = limit;
    this.used = used;
  }
}

/** A step or the whole task exceeded its deadline. */
export class TimeoutError extends SwarmError {
  constructor(message, { details } = {}) {
    super(message, { code: "timeout", details });
  }
}

/** Acceptance criteria were not independently satisfied. */
export class VerificationError extends SwarmError {
  constructor(message, { failures = [], details } = {}) {
    super(message, { code: "verification_failed", details: { failures, ...(details ?? {}) } });
    this.failures = failures;
  }
}

/**
 * The running OpenCode build does not expose an interface this feature needs.
 * Raised instead of inventing an API or silently degrading to a fake success.
 */
export class CapabilityError extends SwarmError {
  constructor(capability, { details } = {}) {
    super(`runtime capability unavailable: ${capability}`, {
      code: "capability_unavailable",
      details: { capability, ...(details ?? {}) },
    });
    this.capability = capability;
  }
}

export class ConfigError extends SwarmError {
  constructor(message, { details } = {}) {
    super(message, { code: "config_error", details });
  }
}

/** A step dependency graph contained a cycle. */
export class CycleError extends SwarmError {
  constructor(nodes, { details } = {}) {
    super(`dependency cycle among: ${nodes.join(", ")}`, {
      code: "dependency_cycle",
      details: { nodes, ...(details ?? {}) },
    });
    this.nodes = nodes;
  }
}

/** The task was cancelled (caller signal or upstream failure). */
export class CancelledError extends SwarmError {
  constructor(message = "cancelled", { details } = {}) {
    super(message, { code: "cancelled", details });
  }
}

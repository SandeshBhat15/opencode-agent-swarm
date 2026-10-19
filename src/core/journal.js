/**
 * Append-only evidence log.
 *
 * Every decision, delegation, verification and failure is journalled. This is
 * the single source of truth for two later phases:
 *  - delivery/progress reporting (what actually happened),
 *  - learning (measured success/latency/cost per model, role and workflow).
 *
 * Nothing is inferred from telemetry that was not observed: entries record
 * `unknown` markers instead of zeros.
 */

export const JOURNAL_KINDS = Object.freeze({
  TASK: "task",
  DECISION: "decision",
  PLAN: "plan",
  STEP_START: "step.start",
  STEP_END: "step.end",
  RETRY: "retry",
  DELEGATE: "delegate",
  DIRECT: "direct",
  USAGE: "usage",
  DIAGNOSIS: "diagnosis",
  VERIFY: "verify",
  REPAIR: "repair",
  DELIVER: "deliver",
  LEARN: "learn",
  IMPROVE: "improve",
  FAILURE: "failure",
  NOTE: "note",
});

export function createJournal({ now = () => Date.now(), initial = [] } = {}) {
  const entries = [...initial];
  let seq = entries.length ? entries[entries.length - 1].seq : 0;

  return {
    append(kind, data = {}) {
      const entry = { seq: ++seq, at: now(), kind, data };
      entries.push(entry);
      return entry;
    },
    /** Entries of one or more kinds, in order. */
    of(...kinds) {
      const set = new Set(kinds);
      return entries.filter((entry) => set.has(entry.kind));
    },
    all() {
      return [...entries];
    },
    count(kind) {
      return entries.reduce((total, entry) => (entry.kind === kind ? total + 1 : total), 0);
    },
    get size() {
      return entries.length;
    },
    since(at) {
      return entries.filter((entry) => entry.at >= at);
    },
    toJSON() {
      return [...entries];
    },
  };
}

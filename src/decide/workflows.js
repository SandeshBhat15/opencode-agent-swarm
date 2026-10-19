/**
 * Workflow catalog and minimum-sufficient selection.
 *
 * A workflow is a named shape of work: which roles run, in what order, and what
 * each produces. The decision engine picks the *cheapest* workflow whose shape
 * can satisfy the classification. Nothing is added "just in case".
 */

import { KINDS } from "./classifier.js";

export const ROLES = Object.freeze({
  NONE: "none",
  RESEARCHER: "researcher",
  BUILDER: "builder",
  VISUAL: "visual",
  QC: "qc",
  IMPROVE: "improve",
});

/**
 * `steps[].role` = specialist that owns the step, `ROLES.NONE` means the
 * submitting session does the work directly with its own tools (cheapest).
 * `parallel` groups steps that may run at the same time.
 */
export const WORKFLOWS = Object.freeze({
  direct: {
    id: "direct",
    description: "Answer or act inside the submitting session; no child sessions.",
    steps: [{ id: "do", role: ROLES.NONE, produces: "result" }],
  },
  research_only: {
    id: "research_only",
    description: "One researcher gathers and cites; manager delivers.",
    steps: [
      { id: "research", role: ROLES.RESEARCHER, produces: "findings" },
      { id: "deliver", role: ROLES.NONE, produces: "result", needs: ["research"] },
    ],
  },
  build_verify: {
    id: "build_verify",
    description: "Build in the submitting session, then an independent check.",
    steps: [
      { id: "build", role: ROLES.NONE, produces: "artifact" },
      { id: "verify", role: ROLES.QC, produces: "verdict", needs: ["build"] },
    ],
  },
  visual_verify: {
    id: "visual_verify",
    description: "Visual specialist produces the artefact, QC inspects it.",
    steps: [
      { id: "visual", role: ROLES.VISUAL, produces: "artifact" },
      { id: "verify", role: ROLES.QC, produces: "verdict", needs: ["visual"] },
    ],
  },
  research_build_verify: {
    id: "research_build_verify",
    description: "Research feeds a build, an independent check closes it.",
    steps: [
      { id: "research", role: ROLES.RESEARCHER, produces: "findings" },
      { id: "build", role: ROLES.NONE, produces: "artifact", needs: ["research"] },
      { id: "verify", role: ROLES.QC, produces: "verdict", needs: ["build"] },
    ],
  },
  research_design_verify: {
    id: "research_design_verify",
    description: "Research informs a visual artefact, QC inspects it.",
    steps: [
      { id: "research", role: ROLES.RESEARCHER, produces: "findings" },
      { id: "visual", role: ROLES.VISUAL, produces: "artifact", needs: ["research"] },
      { id: "verify", role: ROLES.QC, produces: "verdict", needs: ["visual"] },
    ],
  },
  research_deliver_verify: {
    id: "research_deliver_verify",
    description: "Research, manager synthesises, independent check of the synthesis.",
    steps: [
      { id: "research", role: ROLES.RESEARCHER, produces: "findings" },
      { id: "deliver", role: ROLES.NONE, produces: "result", needs: ["research"] },
      { id: "verify", role: ROLES.QC, produces: "verdict", needs: ["deliver"] },
    ],
  },
  parallel_build_verify: {
    id: "parallel_build_verify",
    description: "Research and visual work run in parallel, then one independent check.",
    steps: [
      { id: "research", role: ROLES.RESEARCHER, produces: "findings", parallel: "wave1" },
      { id: "visual", role: ROLES.VISUAL, produces: "artifact", parallel: "wave1" },
      { id: "verify", role: ROLES.QC, produces: "verdict", needs: ["research", "visual"] },
    ],
  },
});

/**
 * Picks the cheapest workflow that covers the classification. The mapping is a
 * table, not a heuristic: same input always yields the same workflow.
 */
export function selectWorkflow(classification) {
  const { kind, needs, signals } = classification;
  const parallelWorthwhile = signals.subTaskEstimate >= 3 && signals.domainCount >= 2;

  if (kind === KINDS.TRIVIAL) return WORKFLOWS.direct;
  if (kind === KINDS.ANSWER && !needs.verify) return WORKFLOWS.direct;

  if (needs.research && needs.visual && !needs.build) {
    return parallelWorthwhile ? WORKFLOWS.parallel_build_verify : WORKFLOWS.research_design_verify;
  }
  if (needs.visual && !needs.research) {
    return needs.verify ? WORKFLOWS.visual_verify : WORKFLOWS.direct;
  }
  if (needs.research && needs.build) return WORKFLOWS.research_build_verify;
  if (needs.research) {
    return needs.verify ? WORKFLOWS.research_deliver_verify : WORKFLOWS.research_only;
  }
  if (needs.build || needs.data || kind === KINDS.MIXED) {
    return needs.verify ? WORKFLOWS.build_verify : WORKFLOWS.direct;
  }
  return WORKFLOWS.direct;
}

export function workflowById(id) {
  return WORKFLOWS[id] ?? null;
}

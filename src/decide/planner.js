/**
 * Planning.
 *
 * The planner turns (task, classification, workflow) into a plan the executor
 * can run: concrete steps, dependencies, per-step acceptance criteria and a
 * budget split. Criteria are derived by rules so that verification has
 * something objective to check before any work starts.
 */

import { classify } from "./classifier.js";
import { ROLES, selectWorkflow } from "./workflows.js";

let counter = 0;
function nextId(prefix) {
  counter += 1;
  return `${prefix}_${counter.toString(36)}${Date.now().toString(36).slice(-3)}`;
}

/** Objective criteria derivable without a model call. */
export function deriveCriteria(task, classification) {
  const criteria = [];
  const signals = classification.signals;

  criteria.push({
    id: "c_deliverable",
    text: "A concrete deliverable exists in the response (not a promise to do work).",
    check: { type: "nonEmpty", target: "output" },
  });

  if (signals.hasPaths) {
    criteria.push({
      id: "c_paths",
      text: `Each referenced path is addressed: ${signals.paths.slice(0, 5).join(", ")}`,
      check: { type: "mentionsAll", target: "output", values: signals.paths.slice(0, 5) },
    });
  }

  if (classification.needs.verify) {
    criteria.push({
      id: "c_verified",
      text: "An independent check confirms the acceptance criteria.",
      check: { type: "verdict", target: "verdict" },
    });
  }

  if (signals.riskLevel !== "low") {
    criteria.push({
      id: "c_risk",
      text: `Risky operation flagged and not performed blindly (${signals.risk.slice(0, 3).join(", ")}).`,
      check: { type: "mentionsAny", target: "output", values: [...signals.risk.slice(0, 4), "risk", "confirm"] },
    });
  }

  if (task && task.trim().length > 0) {
    const keywords = topKeywords(task, 6);
    if (keywords.length) {
      criteria.push({
        id: "c_scope",
        text: `Response stays on topic: ${keywords.join(", ")}`,
        check: { type: "mentionsAny", target: "output", values: keywords },
      });
    }
  }

  return criteria;
}

const STOPWORDS = new Set([
  "the","a","an","and","or","but","if","then","than","that","this","these","those","to","of","in","on","for","with","as","at","by","from","is","are","was","were","be","been","it","its","you","your","i","we","me","my","our","do","does","did","can","could","should","would","will","please","make","made","using","use","used","into","out","up","down","over","under","about","all","any","some","more","most","not","no","yes","just","also","very","them","they","he","she","his","her","have","has","had","when","where","which","who","what","how","why",
]);

export function topKeywords(text, limit = 6) {
  const counts = new Map();
  for (const raw of text.toLowerCase().match(/[a-z][a-z0-9_-]{2,}/g) ?? []) {
    if (STOPWORDS.has(raw)) continue;
    counts.set(raw, (counts.get(raw) ?? 0) + 1);
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, limit)
    .map(([word]) => word);
}

/**
 * @returns {object} plan
 */
export function plan(task, { classification = classify(task), workflow, budget, router } = {}) {
  const flow = workflow ?? selectWorkflow(classification);
  const steps = flow.steps.map((step) => ({
    id: step.id,
    role: step.role,
    produces: step.produces,
    needs: step.needs ?? [],
    parallel: step.parallel ?? null,
    status: "pending",
  }));

  const requirement = {
    needsTools: true,
    minContext: Math.max(8000, classification.signals.length * 8),
    role: null,
    complexity: classification.complexity,
  };

  const complaints = [];
  if (classification.signals.ambiguityLevel === "high") {
    complaints.push("task wording is ambiguous; state your interpretation before acting");
  }
  if (classification.confidence < 0.45) {
    complaints.push("deterministic signals were weak; confirm scope with the smallest useful step");
  }

  return {
    id: nextId("plan"),
    task,
    workflow: flow.id,
    workflowDescription: flow.description,
    kind: classification.kind,
    complexity: classification.complexity,
    confidence: classification.confidence,
    steps,
    criteria: deriveCriteria(task, classification),
    requirement,
    routerHint: router ? router.describe(requirement) : null,
    notes: complaints,
    budget: budget ?? null,
    createdAt: Date.now(),
  };
}

export function isSpecialist(role) {
  return role !== ROLES.NONE;
}

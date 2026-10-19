/**
 * Specialist roster.
 *
 * Each specialist maps to a real OpenCode subagent page
 * (`agents/swarm/<id>.md`) so delegation uses supported agent IDs. The
 * requirement profile mirrors `route/router.js` so the router and the agent
 * page cannot drift apart silently.
 */

export const SPECIALISTS = Object.freeze({
  researcher: {
    id: "researcher",
    agentId: "swarm/researcher",
    title: "Researcher",
    produces: "findings",
    requirement: { needsTools: true, minContext: 32_000, complexity: 0.5 },
    contract:
      "Gather only what the task needs. Cite sources or file paths for every claim. " +
      "State explicitly what you could not determine. Do not write production code. " +
      "Return findings, not a plan.",
  },
  builder: {
    id: "builder",
    agentId: "swarm/builder",
    title: "Builder",
    produces: "artifact",
    requirement: { needsTools: true, minContext: 64_000, complexity: 0.6 },
    contract:
      "Implement exactly the requested change and nothing more. Match surrounding conventions. " +
      "Run the narrowest real check that proves the change works, and report the raw result. " +
      "Never claim something works without evidence.",
  },
  visual: {
    id: "visual",
    agentId: "swarm/visual",
    title: "Visual specialist",
    produces: "artifact",
    requirement: { needsTools: true, minContext: 32_000, complexity: 0.5 },
    contract:
      "Produce the visual artefact. Look at the result before reporting it and describe what you " +
      "actually saw, including anything that is wrong. Keep the artefact reproducible.",
  },
  qc: {
    id: "qc",
    agentId: "swarm/qc",
    title: "Quality control",
    produces: "verdict",
    requirement: { needsTools: true, minContext: 64_000, complexity: 0.7, independent: true },
    contract:
      "You did not produce this artefact. Check each acceptance criterion against the evidence only. " +
      "Do not accept assertions, plans, or intent as proof. Report a per-criterion verdict and the " +
      "single most likely reason for any failure.",
  },
  improve: {
    id: "improve",
    agentId: "swarm/improve",
    title: "Improvement",
    produces: "lesson",
    requirement: { needsTools: true, minContext: 32_000, complexity: 0.4 },
    contract:
      "Review the run using only journalled evidence. Propose grounded, reversible changes with " +
      "the sample size behind them. Propose nothing you cannot support with measured data.",
  },
});

export function specialistFor(role) {
  return SPECIALISTS[role] ?? null;
}

export function isSpecialistRole(role) {
  return Boolean(SPECIALISTS[role]);
}

/**
 * Builds the delegation message for a specialist step. Dependencies are passed
 * as bounded excerpts, not full transcripts, to avoid redundant context.
 */
export function buildStepPrompt(step, { task, plan, dependencies = {}, maxDepChars = 6000 } = {}) {
  const specialist = specialistFor(step.role);
  const lines = [];

  lines.push(`Overall task:\n${task}`);
  lines.push(`\nYour step (${step.id}): create "${step.produces}" as the ${specialist?.title ?? step.role}.`);
  lines.push(`\nStanding contract:\n${specialist?.contract ?? "Do the step and report evidence."}`);

  const relevant = step.needs
    .map((need) => ({ need, value: dependencies[need] }))
    .filter((entry) => entry.value !== undefined && entry.value !== null);

  if (relevant.length) {
    lines.push("\nInputs from earlier steps:");
    for (const { need, value } of relevant) {
      lines.push(`\n--- ${need} ---\n${truncate(stringify(value), maxDepChars)}`);
    }
  }

  if (plan?.criteria?.length) {
    lines.push("\nAcceptance criteria you must satisfy:");
    for (const criterion of plan.criteria) lines.push(`- ${criterion.text}`);
  }

  lines.push(
    "\nFinish with: what you produced, the evidence you observed, and anything you could not do.",
  );
  return lines.join("\n");
}

export function stringify(value) {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/**
 * Truncates `text` to at most `max` characters.
 *
 * When the budget is large enough to say so, the cut is marked: a worker
 * receiving a silently amputated context has no way to know it is incomplete.
 * For tiny budgets there is no room for the marker, so it is a plain slice.
 */
export function truncate(text, max) {
  if (typeof text !== "string" || text.length <= max) return text;
  const omitted = text.length - max;
  const marker = `\n…[${omitted} characters omitted]`;
  if (max <= marker.length + 16) return text.slice(0, max);
  return `${text.slice(0, max - marker.length)}${marker}`;
}

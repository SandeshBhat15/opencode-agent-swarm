/**
 * Deterministic activation: decides whether a prompt should be handed to the
 * swarm manager, using rules only. No model call happens to make this decision.
 */

export const MANAGER_AGENT_IDS = Object.freeze(["swarm", "swarm/manager"]);

const ACK_WORDS = new Set([
  "ok", "okay", "k", "yes", "no", "yep", "nope", "sure", "thanks", "thank", "thx",
  "ty", "cheers", "cool", "nice", "great", "good", "done", "stop", "wait", "hmm",
  "hello", "hi", "hey", "yo", "morning", "evening", "continue", "go", "again",
]);

const ACK_PHRASES = Object.freeze([
  "thank you", "thanks!", "good job", "well done", "nice work", "sounds good",
  "go ahead", "carry on", "keep going", "never mind", "nevermind",
]);

/** ASCII-safe lowercase trim. */
function normalize(text) {
  return String(text ?? "").trim();
}

/**
 * @returns {{activate: boolean, reason: string, agent: string|null, internal: boolean}}
 */
export function shouldActivate({ text, agent = null, metadata = null } = {}) {
  const body = normalize(text);
  const agentID = agent ?? null;

  if (metadata?.swarm?.internal === true) {
    return { activate: false, reason: "internal_delegation", agent: agentID, internal: true };
  }

  if (!body) return { activate: false, reason: "empty", agent: agentID, internal: false };

  if (body.startsWith("/") || body.startsWith("!")) {
    return { activate: false, reason: "explicit_command", agent: agentID, internal: false };
  }

  if (agentID && !MANAGER_AGENT_IDS.includes(agentID)) {
    return { activate: false, reason: "explicit_agent", agent: agentID, internal: false };
  }

  if (isAcknowledgement(body)) {
    return { activate: false, reason: "acknowledgement", agent: agentID, internal: false };
  }

  return {
    activate: true,
    reason: agentID ? "manager_agent" : "default_manager",
    agent: agentID,
    internal: false,
  };
}

/** True for short greetings and acknowledgements that carry no task. */
export function isAcknowledgement(text) {
  const body = normalize(text);
  if (!body) return true;
  const lower = body.toLowerCase().replace(/[.!?,;:]+$/g, "");
  if (lower.length > 40) return false;
  for (const phrase of ACK_PHRASES) if (lower === phrase) return true;
  const words = lower.split(/\s+/).filter(Boolean);
  if (words.length === 0) return true;
  if (words.length <= 2) return words.every((word) => ACK_WORDS.has(word));
  return false;
}

export const DIRECTIVE_MARKER = "swarm-manager-contract";

/**
 * The manager contract appended to `event.system`. Deterministic and compact:
 * it is injected on every manager step, so it must stay cheap.
 */
export function managerDirective({ task = "", workflow = null, activate = true } = {}) {
  const lines = [
    `[${DIRECTIVE_MARKER}]`,
    "You are the swarm manager. You own the outcome of the user's task end to end.",
    "",
    "Method, in order:",
    "1. Plan deterministically first: call the `swarm_plan` tool with the user's task. It returns the workflow, the delegation steps, the acceptance criteria and the routed model for free, without spending a model call. Never delegate before you have a plan.",
    "2. Do the minimum sufficient work. If the plan's workflow is `direct`, just do the task yourself in this session and finish. Do not delegate, do not invent extra steps.",
    "3. Delegate only the steps the plan marks for a specialist, using `swarm_delegate` with that step's role, and pass the task plus any upstream results it needs. Do not re-do research that the plan reused from memory.",
    "4. Before you claim completion, call `swarm_verify` with the task and your produced output. If verification fails, fix the stated reason and verify again. Stop when verification passes.",
    "5. Report the result plus what was actually performed: steps run, tools called, whether verification passed.",
    "",
    "Efficiency rules:",
    "- Deterministic answers over model calls. Never call a specialist to decide something a rule can decide.",
    "- Never poll, never re-read what you already have, never retry the same failing action without first stating the diagnosed cause.",
    "- Report measured usage as measured. If usage was not reported, say it is unknown; never present missing telemetry as zero.",
  ];

  if (task) lines.push("", `Task: ${truncate(task, 600)}`);
  if (workflow) lines.push(`Planned workflow: ${workflow}`);
  lines.push("", "Acknowledge nothing. Start working, and keep the final answer short and factual.");
  return lines.join("\n");
}

export function truncate(text, max) {
  const value = String(text ?? "");
  if (value.length <= max) return value;
  return `${value.slice(0, Math.max(0, max - 1))}…`;
}

export default shouldActivate;

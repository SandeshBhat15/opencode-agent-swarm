/**
 * Dashboard events.
 *
 * The dashboard is a *view* of work that already happened. An event carries
 * identifiers, enum states, counters and measured numbers — never the text the
 * user or a worker produced. That rule is enforced here, in one place, so a new
 * emission site cannot leak prompts, tool output or file contents by accident.
 */

export const EVENT = Object.freeze({
  DASHBOARD: "dashboard", // started / stopped / disabled
  ACTIVATION: "activation", // a prompt was admitted or declined
  TOOL: "tool", // a tool call finished (name + status only)
  TASK_START: "task.start",
  CLASSIFIED: "classified",
  PLANNED: "planned",
  STEP_START: "step.start",
  STEP_END: "step.end",
  DELEGATE: "delegate",
  RETRY: "retry",
  FAILURE: "failure",
  VERIFY: "verify",
  DELIVER: "deliver",
  RUN_END: "run.end",
});

export const AGENTS = Object.freeze(["manager", "researcher", "builder", "visual", "qc", "improve"]);

/** Keys that must never leave the process, whatever a caller passes. */
const BLOCKED_KEYS = new Set([
  "reason",
  "message",
  "detail",
  "details",
  "error",
  "cause",
  "summary",
  "prompt",
  "prompts",
  "output",
  "outputs",
  "text",
  "task",
  "tasks",
  "system",
  "instruction",
  "instructions",
  "content",
  "contents",
  "messages",
  "artifacts",
  "result",
  "value",
  "token",
  "capabilityToken",
  "credentials",
  "url",
  "file",
  "path",
]);

const MAX_STRING = 96;
const MAX_ARRAY = 24;
const MAX_DEPTH = 3;

/**
 * Key names are matched case-insensitively and by suffix, because a nested
 * payload can spell the same field `Prompt`, `PROMPT` or `userPrompt`.
 */
function isBlockedKey(key) {
  const name = String(key).toLowerCase();
  if (BLOCKED_KEYS.has(name)) return true;
  return name.endsWith("prompt") || name.endsWith("output") || name.endsWith("content");
}

/**
 * Whitelist-shaped copy: primitives only, bounded strings, bounded arrays and
 * two levels of small objects. Anything else is dropped rather than guessed at.
 */
export function sanitizeEvent(data = {}, depth = 0) {
  const out = {};
  if (!data || typeof data !== "object" || depth > MAX_DEPTH) return out;

  for (const [key, value] of Object.entries(data)) {
    if (isBlockedKey(key)) continue;
    if (value === null || value === undefined) continue;

    if (typeof value === "number") {
      if (Number.isFinite(value)) out[key] = value;
      continue;
    }
    if (typeof value === "boolean") {
      out[key] = value;
      continue;
    }
    if (typeof value === "string") {
      // Short identifiers, role names, model refs and reasons only.
      out[key] = value.length > MAX_STRING ? `${value.slice(0, MAX_STRING)}…` : value;
      continue;
    }
    if (Array.isArray(value)) {
      const items = [];
      for (const item of value.slice(0, MAX_ARRAY)) {
        if (typeof item === "string") items.push(item.length > MAX_STRING ? item.slice(0, MAX_STRING) : item);
        else if (typeof item === "number" && Number.isFinite(item)) items.push(item);
        else if (typeof item === "boolean") items.push(item);
        else if (item && typeof item === "object") items.push(sanitizeEvent(item, depth + 1));
      }
      if (items.length) out[key] = items;
      continue;
    }
    if (typeof value === "object") {
      const nested = sanitizeEvent(value, depth + 1);
      if (Object.keys(nested).length) out[key] = nested;
    }
  }
  return out;
}

/** The one shape allowed on the wire. */
export function makeEvent(type, data = {}, { now = () => Date.now(), seq = 0 } = {}) {
  return { seq, at: now(), type, ...sanitizeEvent(data) };
}

/**
 * Usage telemetry.
 *
 * Two supported sources, in preference order:
 *  1. `session.get({sessionID})` → `SessionInfo.tokens` and `SessionInfo.cost`,
 *     the runtime's own aggregate for that session;
 *  2. the per-assistant-message `tokens`/`cost` fields from
 *     `session.context({sessionID})`.
 *
 * The rule that matters: a value that was not reported is `null` and counted in
 * `missing`. It is never coerced to 0, because 0 means "measured zero".
 */

/** Turn a `TokenUsageInfo` into a flat total plus cache breakdown. */
export function normalizeTokens(tokens) {
  if (!tokens || typeof tokens !== "object") return null;
  const input = numOrNull(tokens.input);
  const output = numOrNull(tokens.output);
  const reasoning = numOrNull(tokens.reasoning);
  if (input === null && output === null && reasoning === null) return null;
  return {
    input: input ?? 0,
    output: output ?? 0,
    reasoning: reasoning ?? 0,
    total: (input ?? 0) + (output ?? 0) + (reasoning ?? 0),
    cacheRead: numOrNull(tokens.cache?.read) ?? 0,
    cacheWrite: numOrNull(tokens.cache?.write) ?? 0,
  };
}

export function textOfMessage(message) {
  if (!message || !Array.isArray(message.content)) return "";
  return message.content
    .filter((part) => part?.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n");
}

export function toolCallsOfMessages(messages = []) {
  let calls = 0;
  for (const message of messages) {
    if (message?.type !== "assistant" || !Array.isArray(message.content)) continue;
    for (const part of message.content) if (part?.type === "tool") calls += 1;
  }
  return calls;
}

/**
 * Reads usage for a finished session. Never throws: an unreachable runtime
 * yields a null-valued report with `source: "unavailable"`.
 */
export async function readUsage({ session = null, sessionID = null, messages = null } = {}) {
  const report = {
    tokens: null,
    costUSD: null,
    toolCalls: null,
    samples: 0,
    missing: 0,
    source: "unavailable",
  };

  let info = null;
  if (session && sessionID && typeof session.get === "function") {
    try {
      info = await session.get({ sessionID });
    } catch {
      info = null;
    }
  }

  const fromInfo = normalizeTokens(info?.tokens);
  const infoCost = numOrNull(info?.cost);

  let list = Array.isArray(messages) ? messages : null;
  if (!list && session && sessionID && typeof session.context === "function") {
    try {
      list = await session.context({ sessionID });
    } catch {
      list = null;
    }
  }

  const assistantMessages = Array.isArray(list) ? list.filter((message) => message?.type === "assistant") : [];
  let messageTokens = null;
  let messageCost = null;
  let missing = 0;
  for (const message of assistantMessages) {
    const normalized = normalizeTokens(message.tokens);
    if (normalized) {
      messageTokens = messageTokens ?? { total: 0, input: 0, output: 0, reasoning: 0, cacheRead: 0, cacheWrite: 0 };
      messageTokens.total += normalized.total;
      messageTokens.input += normalized.input;
      messageTokens.output += normalized.output;
      messageTokens.reasoning += normalized.reasoning;
      messageTokens.cacheRead += normalized.cacheRead;
      messageTokens.cacheWrite += normalized.cacheWrite;
    } else {
      missing += 1;
    }
    if (Number.isFinite(message.cost)) messageCost = (messageCost ?? 0) + message.cost;
  }

  report.tokens = fromInfo ?? messageTokens;
  report.costUSD = infoCost ?? messageCost;
  report.toolCalls = Array.isArray(list) ? toolCallsOfMessages(list) : null;
  report.samples = fromInfo ? 1 : assistantMessages.length;
  report.missing = fromInfo ? 0 : missing;
  report.source = fromInfo
    ? "session.aggregate"
    : messageTokens || messageCost !== null
      ? "session.messages"
      : assistantMessages.length
        ? "session.messages.unreported"
        : "unavailable";

  return report;
}

/** Compact view for journalling and learning records. */
export function toLimitUsage(report) {
  if (!report) return {};
  return {
    tokens: report.tokens?.total ?? null,
    costUSD: Number.isFinite(report.costUSD) ? report.costUSD : null,
    toolCalls: Number.isFinite(report.toolCalls) ? report.toolCalls : null,
  };
}

function numOrNull(value) {
  return Number.isFinite(value) ? value : null;
}

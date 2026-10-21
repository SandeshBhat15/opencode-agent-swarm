/**
 * One browser tab per OpenCode process.
 *
 * `bootstrap()` is intentionally re-entrant: a reload, a second session or an
 * agent spawn can run `setup()` again, and each run gets a fresh module
 * instance. The decision to open a browser therefore cannot live in the
 * instance — it lives on `globalThis` under a symbol that survives module
 * reloads.
 *
 * Whoever claims first opens. Everyone else — a reload, a concurrent setup, a
 * later prompt — sees the claim and stays quiet, so the user never collects
 * duplicate tabs. The claim is never released: after a reload the dashboard is
 * a new server on a new ephemeral port, and re-opening would spawn exactly the
 * duplicate this exists to prevent.
 *
 * Nothing here can throw into the caller: the opener's error is caught,
 * recorded and logged as one concise warning. A missing browser never fails a
 * swarm task.
 */

import { openInBrowser } from "./open.js";

const KEY = Symbol.for("opencode-swarm.dashboard.autoopen.v1");

function record() {
  const existing = globalThis[KEY];
  if (existing && typeof existing === "object") return existing;
  const fresh = {
    claimed: false,
    attempts: 0,
    opened: false,
    failed: false,
    skipped: null,
    origin: null,
    error: null,
    at: null,
  };
  globalThis[KEY] = fresh;
  return fresh;
}

/** A copy, so a reader cannot mutate the process-wide record. */
export function autoOpenState() {
  return { ...record() };
}

/** Test seam: forget the claim so each test starts from a cold process. */
export function resetAutoOpenState() {
  delete globalThis[KEY];
}

/**
 * Claim the single process-wide open. Synchronous on purpose: two setups that
 * run in the same tick cannot both win, which is what makes concurrency safe
 * without a lock.
 *
 * @returns {object|null} the record when this call won, otherwise null.
 */
export function claimAutoOpen() {
  const state = record();
  if (state.claimed) return null;
  state.claimed = true;
  return state;
}

/** Origin only: a launch URL carries a ticket, and a log must never carry one. */
function originOf(url) {
  const match = /^http:\/\/[^/]+/.exec(String(url ?? ""));
  return match ? match[0] : null;
}

/**
 * Open the dashboard at most once for this process.
 *
 * @param {object} options
 * @param {string} options.url            launch URL (ticket, never the token)
 * @param {object|null} [options.logger]
 * @param {(url: string) => Promise<{opened: boolean, error?: string}>} [options.open]
 * @param {() => number} [options.now]
 * @returns {Promise<object>} the resulting process-wide record
 */
export async function openDashboardOnce({ url, logger = null, open = openInBrowser, now = Date.now } = {}) {
  const state = claimAutoOpen();
  if (!state) {
    return { ...autoOpenState(), skipped: autoOpenState().skipped ?? "already_attempted" };
  }

  state.attempts += 1;
  state.at = now();
  state.origin = originOf(url);

  let result;
  try {
    result = await open(url);
  } catch (error) {
    result = { opened: false, error: String(error?.message ?? error) };
  }

  if (result?.opened === true) {
    state.opened = true;
    state.failed = false;
    state.error = null;
    logger?.debug?.("dashboard opened in the default browser", { origin: state.origin });
    return autoOpenState();
  }

  // The server keeps running; only the convenience failed.
  state.failed = true;
  state.skipped = null;
  state.error = String(result?.error ?? "unknown");
  logger?.warn?.("could not open the dashboard in a browser", { origin: state.origin, error: state.error });
  return autoOpenState();
}

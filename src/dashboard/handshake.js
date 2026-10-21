/**
 * Single-use launch tickets.
 *
 * The dashboard token is a process-wide capability: whoever holds it can read
 * the whole mission-control state. Handing it to a browser through a command
 * line would publish it to every other local process, so the opener carries a
 * ticket instead:
 *
 *  - a ticket is 24 random bytes, held in memory, and never logged;
 *  - it is valid once, for a short window, and only for the page document;
 *  - redeeming it makes the server serve the page with the real token embedded
 *    in the body, so the address bar keeps a credential-free URL;
 *  - tickets are bounded, so a caller that issues in a loop cannot grow the
 *    map without limit.
 *
 * A stolen ticket therefore buys one page load inside the window, not the
 * standing capability. `Host` and `Origin` validation still run first, so a
 * page on another origin cannot use one either.
 */

import crypto from "node:crypto";

export const DEFAULT_TTL_MS = 120000;
export const MAX_PENDING = 8;

export function createHandshake({ ttlMs = DEFAULT_TTL_MS, max = MAX_PENDING, now = Date.now } = {}) {
  /** ticket -> expiry (ms). Insertion order is issue order. */
  const pending = new Map();

  function prune(at) {
    for (const [id, expiresAt] of pending) {
      if (expiresAt <= at) pending.delete(id);
    }
  }

  return {
    /** Mint one ticket. The caller must not log or persist the result. */
    issue() {
      const at = now();
      prune(at);
      while (pending.size >= max) {
        // Bound the map: the oldest outstanding ticket is dropped first.
        const oldest = pending.keys().next().value;
        pending.delete(oldest);
      }
      const id = crypto.randomBytes(24).toString("base64url");
      pending.set(id, at + ttlMs);
      return id;
    },

    /** Consume a ticket. True only for a ticket that exists, is unused and unexpired. */
    redeem(id) {
      if (typeof id !== "string" || id.length === 0) return false;
      const at = now();
      prune(at);
      const expiresAt = pending.get(id);
      if (expiresAt === undefined) return false;
      pending.delete(id);
      return true;
    },

    /** Outstanding, unexpired tickets. */
    size() {
      prune(now());
      return pending.size;
    },
  };
}

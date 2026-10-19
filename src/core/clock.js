/**
 * Injectable time. Every timeout, backoff and duration measurement goes through
 * a clock so tests are deterministic and free of sleeps.
 */

/** Real clock backed by Date/setTimeout. */
export function systemClock() {
  return {
    now: () => Date.now(),
    delay(ms, { signal } = {}) {
      if (signal?.aborted) return Promise.reject(abortReason(signal));
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          cleanup();
          resolve();
        }, Math.max(0, ms));
        const onAbort = () => {
          cleanup();
          reject(abortReason(signal));
        };
        const cleanup = () => {
          clearTimeout(timer);
          signal?.removeEventListener?.("abort", onAbort);
        };
        signal?.addEventListener?.("abort", onAbort, { once: true });
      });
    },
  };
}

function abortReason(signal) {
  return signal?.reason instanceof Error ? signal.reason : new Error("aborted");
}

/**
 * Manually advanced clock for tests. `delay()` only settles when `advance()`
 * moves time past the deadline, so retry/backoff logic can be tested instantly.
 */
export function manualClock(startMs = 0) {
  let current = startMs;
  let seq = 0;
  const pending = new Map();

  return {
    now: () => current,
    delay(ms, { signal } = {}) {
      if (signal?.aborted) return Promise.reject(abortReason(signal));
      const id = ++seq;
      return new Promise((resolve, reject) => {
        pending.set(id, { at: current + Math.max(0, ms), resolve, reject, signal });
      });
    },
    advance(ms) {
      current += Math.max(0, ms);
      for (const [id, entry] of [...pending]) {
        if (entry.at <= current) {
          pending.delete(id);
          entry.resolve();
        }
      }
    },
    get pendingDelays() {
      return pending.size;
    },
  };
}

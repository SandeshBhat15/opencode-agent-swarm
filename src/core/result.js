/**
 * Explicit success/failure values.
 *
 * The swarm uses Result for *expected* failures (budget exhaustion, timeouts,
 * failed verification, missing runtime capability) so that a caller can never
 * silently confuse "no data" with "success". Exceptions are reserved for
 * programming errors.
 */

/** @returns {{ok: true, value: any}} */
export function ok(value) {
  return { ok: true, value };
}

/** @returns {{ok: false, error: Error}} */
export function err(error) {
  return { ok: false, error };
}

export function isOk(result) {
  return Boolean(result) && result.ok === true;
}

export function isErr(result) {
  return !isOk(result);
}

export function map(result, fn) {
  return isOk(result) ? ok(fn(result.value)) : result;
}

export function mapErr(result, fn) {
  return isErr(result) ? err(fn(result.error)) : result;
}

/** Throws on failure. Use only where failure is genuinely exceptional. */
export function unwrap(result) {
  if (isErr(result)) throw result.error;
  return result.value;
}

export function unwrapOr(result, fallback) {
  return isOk(result) ? result.value : fallback;
}

/**
 * Runs `fn` and converts a thrown error into a failed Result. Never wide:
 * the caller still sees the original error object.
 */
export async function attempt(fn) {
  try {
    return ok(await fn());
  } catch (error) {
    return err(error instanceof Error ? error : new Error(String(error)));
  }
}

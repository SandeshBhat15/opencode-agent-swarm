/**
 * Dependency scheduling.
 *
 * Produces an ordered list of *groups*. Groups run in order; the steps inside a
 * group have no dependency between them and may run concurrently. Steps the
 * manager performs in the submitting session (`role === "none"`) are always
 * singleton groups because they mutate shared session state.
 */

import { CycleError } from "../core/errors.js";

/**
 * @param {{id:string, needs?:string[], role?:string}[]} steps
 * @param {{maxConcurrency?:number}} options
 * @returns {{groups: object[][], depth: Record<string, number>, order: string[]}}
 */
export function buildSchedule(steps = [], { maxConcurrency = 3 } = {}) {
  const limit = Math.max(1, Math.floor(maxConcurrency) || 1);
  const byId = new Map(steps.map((step) => [step.id, step]));
  const depth = new Map();
  const visiting = new Set();

  function depthOf(id, trail) {
    if (depth.has(id)) return depth.get(id);
    if (visiting.has(id)) throw new CycleError([...trail, id]);
    const step = byId.get(id);
    if (!step) return 0;
    visiting.add(id);
    const needs = (step.needs ?? []).filter((need) => byId.has(need));
    const value = needs.length ? Math.max(...needs.map((need) => depthOf(need, [...trail, id]))) + 1 : 0;
    visiting.delete(id);
    depth.set(id, value);
    return value;
  }

  for (const step of steps) depthOf(step.id, []);

  const levels = new Map();
  for (const step of steps) {
    const level = depth.get(step.id) ?? 0;
    if (!levels.has(level)) levels.set(level, []);
    levels.get(level).push(step);
  }

  const groups = [];
  for (const level of [...levels.keys()].sort((a, b) => a - b)) {
    const levelSteps = levels.get(level);
    const parallelizable = levelSteps.filter((step) => step.role && step.role !== "none");
    const serial = levelSteps.filter((step) => !step.role || step.role === "none");

    for (let index = 0; index < parallelizable.length; index += limit) {
      groups.push(parallelizable.slice(index, index + limit));
    }
    for (const step of serial) groups.push([step]);
  }

  const order = [];
  for (const group of groups) for (const step of group) order.push(step.id);

  return { groups, depth: Object.fromEntries(depth), order };
}

/**
 * Steps whose results are no longer reachable after `failedId` failed. Used to
 * skip dependents instead of running work that cannot succeed.
 */
export function dependentsOf(steps = [], failedIds = []) {
  const failed = new Set(failedIds);
  const skipped = new Set();
  let changed = true;
  while (changed) {
    changed = false;
    for (const step of steps) {
      if (failed.has(step.id) || skipped.has(step.id)) continue;
      if ((step.needs ?? []).some((need) => failed.has(need) || skipped.has(need))) {
        skipped.add(step.id);
        changed = true;
      }
    }
  }
  return skipped;
}

/** Async semaphore used to bound real concurrency. */
export function createGate(limit) {
  const size = Math.max(1, Math.floor(limit) || 1);
  let active = 0;
  const queue = [];

  const release = () => {
    active -= 1;
    const next = queue.shift();
    if (next) next();
  };

  return {
    get active() {
      return active;
    },
    async run(fn) {
      if (active >= size) await new Promise((resolve) => queue.push(resolve));
      active += 1;
      try {
        return await fn();
      } finally {
        release();
      }
    },
  };
}

/**
 * Acceptance criteria evaluation.
 *
 * Pure and free: everything here can be decided from the evidence already in
 * hand. The verifier only spends a model call when the deterministic layer
 * cannot settle a criterion.
 */

/** @typedef {{type:string, target?:string, values?:string[], pattern?:string, fn?:Function, min?:number}} Check */

/** @returns {{id:string, text:string, passed:boolean, detail:string}} */
export function evaluateCriterion(criterion, evidence = {}) {
  const check = criterion?.check ?? {};
  const target = resolveTarget(check.target, evidence);
  const base = { id: criterion?.id ?? "unnamed", text: criterion?.text ?? "", check: check.type };

  switch (check.type) {
    case "nonEmpty": {
      const text = toText(target).trim();
      const min = Number.isFinite(check.min) ? check.min : 1;
      return {
        ...base,
        passed: text.length >= min,
        detail: text.length >= min ? `${text.length} characters of output` : "no substantive output",
      };
    }
    case "mentionsAll": {
      const text = toText(target).toLowerCase();
      const values = check.values ?? [];
      const missing = values.filter((value) => !text.includes(String(value).toLowerCase()));
      return {
        ...base,
        passed: missing.length === 0,
        detail: missing.length === 0 ? `all ${values.length} referenced` : `missing: ${missing.slice(0, 5).join(", ")}`,
      };
    }
    case "mentionsAny": {
      const text = toText(target).toLowerCase();
      const values = check.values ?? [];
      const hit = values.filter((value) => text.includes(String(value).toLowerCase()));
      return {
        ...base,
        passed: hit.length > 0,
        detail: hit.length > 0 ? `matched: ${hit.slice(0, 5).join(", ")}` : `none of: ${values.slice(0, 5).join(", ")}`,
      };
    }
    case "regex": {
      if (!check.pattern) return { ...base, passed: false, detail: "no pattern supplied" };
      let matched = false;
      try {
        matched = new RegExp(check.pattern, check.flags ?? "").test(toText(target));
      } catch (error) {
        return { ...base, passed: false, detail: `invalid pattern: ${error.message}` };
      }
      return { ...base, passed: matched, detail: matched ? "pattern matched" : `pattern not found: ${check.pattern}` };
    }
    case "artifacts": {
      const artifacts = evidence.artifacts ?? [];
      const min = Number.isFinite(check.min) ? check.min : 1;
      return {
        ...base,
        passed: artifacts.length >= min,
        detail: `${artifacts.length} artefact(s); ${min} required`,
      };
    }
    case "verdict": {
      const verdict = evidence.verdict;
      if (!verdict) return { ...base, passed: false, detail: "no independent verdict was produced" };
      if (verdict.independence === "unavailable") {
        return { ...base, passed: false, detail: "independent verification was not possible" };
      }
      return {
        ...base,
        passed: verdict.passed === true,
        detail: verdict.passed ? "independent check passed" : `independent check failed: ${verdict.reason ?? "unspecified"}`,
      };
    }
    case "predicate": {
      if (typeof check.fn !== "function") return { ...base, passed: false, detail: "no predicate supplied" };
      try {
        const passed = Boolean(check.fn(evidence, criterion));
        return { ...base, passed, detail: passed ? "predicate satisfied" : "predicate not satisfied" };
      } catch (error) {
        return { ...base, passed: false, detail: `predicate threw: ${error.message}` };
      }
    }
    default:
      return { ...base, passed: false, detail: `unsupported check type: ${check.type}` };
  }
}

export function evaluateAll(criteria = [], evidence = {}) {
  const results = criteria.map((criterion) => evaluateCriterion(criterion, evidence));
  const failed = results.filter((result) => !result.passed);
  return {
    results,
    failed,
    passed: failed.length === 0,
    total: results.length,
    passedCount: results.length - failed.length,
  };
}

/** Criteria that a model must judge, because rules cannot settle them. */
export function needsModelJudgement(criteria = []) {
  return criteria.some((criterion) => criterion?.check?.type === "verdict");
}

function resolveTarget(target, evidence) {
  if (!target) return evidence.output ?? "";
  const value = evidence[target];
  if (value === undefined && target === "output") return evidence.output ?? "";
  return value ?? "";
}

function toText(value) {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

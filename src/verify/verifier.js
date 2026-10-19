/**
 * Verification engine.
 *
 * Independence is the whole point, so it is enforced structurally:
 *  1. the deterministic layer runs first and costs nothing — a criterion that
 *     rules already settle never triggers a model call;
 *  2. the model judgement runs in a *separate* session, under the QC agent, on
 *     a model chosen to differ from the producer's when an alternative exists;
 *  3. if independent verification is impossible, the result says so
 *     (`independence: "unavailable"`) and the criterion fails loudly instead of
 *     being waved through;
 *  4. a missing verdict is never treated as a pass.
 */

import { JOURNAL_KINDS } from "../core/journal.js";
import { evaluateAll, needsModelJudgement } from "./criteria.js";

export async function verify(plan, evidence, {
  runtime = null,
  router = null,
  budget = null,
  journal = null,
  clock = null,
  producerModel = null,
  criteria = plan?.criteria ?? [],
  maxEvidenceChars = 12_000,
} = {}) {
  const startedAt = clock?.now?.() ?? Date.now();

  const verdictCriterion = criteria.find((criterion) => criterion?.check?.type === "verdict");
  const requiresModel = Boolean(verdictCriterion) || needsModelJudgement(criteria);
  // A verdict criterion is settled by the model judgement, so it must not fail
  // the deterministic pass while a judge can still run. When no judge can run it
  // stays in the deterministic layer, where it fails loudly (rule 3 below).
  const judgeAvailable = Boolean(verdictCriterion) && typeof runtime?.delegate === "function";

  // Layer 1 — deterministic, free.
  const deterministic = evaluateAll(
    judgeAvailable ? criteria.filter((criterion) => criterion?.check?.type !== "verdict") : criteria,
    evidence,
  );
  journal?.append(JOURNAL_KINDS.VERIFY, {
    layer: "deterministic",
    passed: deterministic.passed,
    total: deterministic.total,
    failed: deterministic.failed.map((result) => result.id),
    deferredToJudge: judgeAvailable ? [verdictCriterion.id] : [],
  });

  if (!requiresModel) {
    return finalize({
      passed: deterministic.passed,
      independence: "not_required",
      deterministic,
      verdict: null,
      reason: deterministic.passed ? null : summarizeFailures(deterministic.failed),
      startedAt,
      clock,
    });
  }

  if (!judgeAvailable) {
    journal?.append(JOURNAL_KINDS.VERIFY, { layer: "model", skipped: true, reason: "delegation unavailable" });
    return finalize({
      passed: false,
      independence: "unavailable",
      deterministic,
      verdict: null,
      reason: "independent verification requires delegation, which this runtime does not expose",
      startedAt,
      clock,
    });
  }

  // A deterministic failure is already decisive: do not spend a model call.
  if (!deterministic.passed) {
    journal?.append(JOURNAL_KINDS.VERIFY, {
      layer: "model",
      skipped: true,
      reason: "deterministic criteria already failed",
    });
    return finalize({
      passed: false,
      independence: "not_required",
      deterministic,
      verdict: null,
      reason: summarizeFailures(deterministic.failed),
      startedAt,
      clock,
    });
  }

  // An exhausted budget buys nothing more. The judge is a model call like any
  // other, so it is refused here too; the criterion fails loudly rather than
  // being waved through or paid for.
  if (typeof budget?.check === "function") {
    const status = budget.check();
    if (status.ok === false) {
      journal?.append(JOURNAL_KINDS.VERIFY, { layer: "model", skipped: true, reason: "budget exhausted" });
      return finalize({
        passed: false,
        independence: "unavailable",
        deterministic,
        verdict: null,
        reason: `independent verification skipped: ${status.error?.message ?? "budget exhausted"}`,
        startedAt,
        clock,
      });
    }
  }

  // Layer 2 — independent model judgement.
  const requirement = {
    ...(router ? {} : {}),
    needsTools: true,
    minContext: 32_000,
    complexity: 0.6,
    role: "qc",
    independent: true,
    exclude: producerModel ? [producerModel] : [],
  };
  const route = router?.routeForRole?.("qc", { exclude: requirement.exclude }) ?? null;

  const prompt = buildVerdictPrompt(plan, evidence, criteria, maxEvidenceChars);
  const delegateStartedAt = clock?.now?.() ?? Date.now();

  const outcome = await runtime.delegate({
    agent: "swarm/qc",
    prompt,
    model: route?.ref ?? null,
    budget,
    label: `verify:${plan?.id ?? "task"}`,
    timeoutMs: 5 * 60 * 1000,
  });

  const durationMs = (clock?.now?.() ?? Date.now()) - delegateStartedAt;
  const verdict = parseVerdict(outcome?.text ?? outcome?.output ?? "");

  const independent = Boolean(route?.ref && producerModel && route.ref !== producerModel);
  journal?.append(JOURNAL_KINDS.VERIFY, {
    layer: "model",
    passed: verdict.passed,
    independence: independent ? "different_model" : "same_model",
    model: route?.ref ?? null,
    producerModel,
    durationMs,
    raw: verdict.raw?.slice(0, 500) ?? null,
  });

  const modelCriterionResult = [
    {
      id: verdictCriterion?.id ?? "c_verified",
      text: verdictCriterion?.text ?? "Independent verdict",
      passed: verdict.passed,
      detail: verdict.reason ?? "no reason given",
    },
  ];

  if (verdict.passed && independent) {
    return finalize({
      passed: true,
      independence: "different_model",
      deterministic,
      verdict,
      extraResults: modelCriterionResult,
      reason: null,
      startedAt,
      clock,
      durationMs,
    });
  }
  if (verdict.passed && !independent) {
    return finalize({
      passed: true,
      independence: "same_model_check",
      deterministic,
      verdict,
      extraResults: modelCriterionResult,
      reason: null,
      startedAt,
      clock,
      durationMs,
    });
  }
  return finalize({
    passed: false,
    independence: independent ? "different_model" : "same_model_check",
    deterministic,
    verdict,
    extraResults: modelCriterionResult,
    reason: verdict.reason ?? "independent check failed",
    startedAt,
    clock,
    durationMs,
  });
}

function finalize({
  passed,
  independence,
  deterministic,
  verdict,
  reason,
  extraResults = [],
  startedAt,
  clock,
  durationMs,
}) {
  // The judge's own result replaces the placeholder when it answered; otherwise
  // the original deterministic result is kept so a failure stays visible.
  const results = extraResults.length
    ? [...deterministic.results.filter((result) => result.check !== "verdict"), ...extraResults]
    : deterministic.results;
  return {
    passed,
    independence,
    reason,
    verdict,
    results,
    // Ids only, matching the deterministic-failure path and the model verdict:
    // callers must never have to guess whether `failed` holds ids or records.
    failed: results.filter((result) => !result.passed).map((result) => result.id),
    durationMs: durationMs ?? (clock?.now?.() ?? Date.now()) - startedAt,
  };
}

function summarizeFailures(failed) {
  if (!failed.length) return null;
  return failed.map((result) => `${result.id}: ${result.detail}`).join("; ");
}

export function buildVerdictPrompt(plan, evidence, criteria, maxEvidenceChars) {
  const lines = [];
  lines.push("Verify this delivery against the acceptance criteria.");
  lines.push(`\nTask:\n${plan?.task ?? "(unknown)"}`);
  lines.push("\nAcceptance criteria:");
  for (const criterion of criteria) lines.push(`- [${criterion.id}] ${criterion.text}`);

  lines.push("\nEvidence produced:");
  lines.push(trim(stringifyEvidence(evidence), maxEvidenceChars));

  lines.push(
    "\nAnswer with a single JSON object on the last line: " +
      '{"passed": true|false, "reason": "<one sentence>", "failed": ["criterion id", ...]}. ' +
      "Judge only what the evidence shows. Assertions, intentions and plans are not evidence.",
  );
  return lines.join("\n");
}

function stringifyEvidence(evidence) {
  const view = {
    output: evidence?.output ?? null,
    artifacts: evidence?.artifacts ?? null,
    meta: evidence?.meta ?? null,
  };
  try {
    return JSON.stringify(view, null, 2);
  } catch {
    return String(view.output ?? "");
  }
}

function trim(text, max) {
  if (typeof text !== "string" || text.length <= max) return text;
  return `${text.slice(0, max)}\n…[${text.length - max} characters omitted]`;
}

/** Tolerant parse: prefers the last JSON object, falls back to keyword scan. */
export function parseVerdict(text) {
  const raw = typeof text === "string" ? text : "";
  const matches = [...raw.matchAll(/\{[^{}]*"passed"[^{}]*\}/g)];
  for (let index = matches.length - 1; index >= 0; index -= 1) {
    try {
      const parsed = JSON.parse(matches[index][0]);
      if (typeof parsed.passed === "boolean") {
        return { passed: parsed.passed, reason: parsed.reason ?? null, failed: parsed.failed ?? [], raw, parsed: true };
      }
    } catch {
      // try the next candidate
    }
  }
  const lowered = raw.toLowerCase();
  if (/\bpassed\b|\bverified\b|\bconfirmed\b/.test(lowered) && !/\bnot passed\b|\bfailed\b/.test(lowered)) {
    return { passed: true, reason: "keyword match", failed: [], raw, parsed: false };
  }
  return { passed: false, reason: raw ? "no parsable verdict found" : "verifier returned nothing", failed: [], raw, parsed: false };
}

/**
 * Deterministic task classification.
 *
 * Runs before any model call. Produces a work profile, not a verdict about
 * quality: how much independent work the request implies, which specialist
 * domains are touched, and whether the cheap path is appropriate.
 */

import { extractSignals } from "./signals.js";

export const KINDS = Object.freeze({
  TRIVIAL: "trivial",
  ANSWER: "answer",
  RESEARCH: "research",
  BUILD: "build",
  VISUAL: "visual",
  DATA: "data",
  MIXED: "mixed",
});

/**
 * @param {string} text natural-language task
 * @returns {{kind:string, complexity:number, confidence:number, needs:object, signals:object}}
 */
export function classify(text, { signals = extractSignals(text) } = {}) {
  const { domains } = signals;
  const needResearch = domains.research.length > 0 && signals.length > 40;
  const needBuild = domains.build.length > 0;
  const needVisual = domains.visual.length > 0;
  const needData = domains.data.length > 0;
  const needVerify = domains.verify.length > 0 || signals.riskLevel !== "low";

  const active = [needResearch, needBuild, needVisual, needData].filter(Boolean).length;

  let kind;
  if (signals.wordCount <= 12 && signals.trivialShape && active === 0) kind = KINDS.TRIVIAL;
  else if (active >= 3 || signals.subTaskEstimate >= 4) kind = KINDS.MIXED;
  else if (active === 0) kind = KINDS.ANSWER;
  else if (needVisual && !needBuild && !needResearch) kind = KINDS.VISUAL;
  else if (needResearch && !needBuild && !needVisual) kind = KINDS.RESEARCH;
  else if (needData && !needBuild && !needVisual) kind = KINDS.DATA;
  else kind = KINDS.BUILD;

  const complexity = clamp(
    0.1 * signals.subTaskEstimate +
      0.15 * active +
      0.1 * (signals.hasPaths ? 1 : 0) +
      0.1 * (signals.riskLevel === "high" ? 1 : signals.riskLevel === "medium" ? 0.5 : 0) -
      0.15 * (kind === KINDS.TRIVIAL ? 1 : 0),
    0,
    1,
  );

  // Confidence is deterministic-signal coverage, not a model probability.
  const evidencePoints =
    (domains.research.length > 0 ? 1 : 0) +
    (domains.build.length > 0 ? 1 : 0) +
    (domains.visual.length > 0 ? 1 : 0) +
    (domains.data.length > 0 ? 1 : 0) +
    (signals.hasPaths ? 1 : 0) +
    (signals.subTaskEstimate > 1 ? 1 : 0);
  const confidence = clamp(0.35 + 0.11 * evidencePoints - (signals.ambiguityLevel === "high" ? 0.2 : 0), 0.2, 0.98);

  return {
    kind,
    complexity: round(complexity),
    confidence: round(confidence),
    needs: {
      research: needResearch || kind === KINDS.RESEARCH,
      build: needBuild || kind === KINDS.BUILD || kind === KINDS.MIXED,
      visual: needVisual || kind === KINDS.VISUAL,
      data: needData || kind === KINDS.DATA,
      verify: needVerify || kind === KINDS.MIXED || signals.riskLevel !== "low",
    },
    signals,
  };
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function round(value) {
  return Math.round(value * 100) / 100;
}

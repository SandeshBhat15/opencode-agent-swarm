/**
 * Deterministic signal extraction.
 *
 * This module is pure string/regex work: it costs nothing, has no latency and
 * never calls a model. Its output is what lets the decision engine resolve
 * most tasks without an expensive call at all.
 */

const RESEARCH_TERMS = [
  "research",
  "investigate",
  "find out",
  "compare",
  "survey",
  "benchmark",
  "evaluate options",
  "state of the art",
  "look up",
  "what is the best",
  "pros and cons",
  "latest",
];
const BUILD_TERMS = [
  "build",
  "implement",
  "create",
  "add",
  "write",
  "refactor",
  "fix",
  "patch",
  "migrate",
  "port",
  "scaffold",
  "wire up",
  "make",
];
const VISUAL_TERMS = [
  "design",
  "ui",
  "ux",
  "wireframe",
  "mockup",
  "logo",
  "icon",
  "diagram",
  "layout",
  "landing page",
  "presentation",
  "deck",
  "slide",
  "render",
  "3d",
  "shader",
  "animation",
  "banner",
  "screenshot",
];
const VERIFY_TERMS = [
  "test",
  "verify",
  "validate",
  "check",
  "prove",
  "ensure",
  "audit",
  "review",
  "benchmark",
  "measure",
];
const DATA_TERMS = [
  "data",
  "csv",
  "json",
  "database",
  "query",
  "etl",
  "pipeline",
  "analytics",
  "statistics",
  "parse",
  "extract",
  "transform",
];
const RISK_TERMS = [
  "production",
  "deploy",
  "release",
  "publish",
  "credential",
  "secret",
  "auth",
  "payment",
  "delete",
  "drop table",
  "irreversible",
  "security",
  "encrypt",
  "backup",
];
const AMBIGUITY_TERMS = ["something", "maybe", "whatever", "etc", "and so on", "somehow", "tbd"];

const TRIVIAL_PATTERNS = [
  /^(what|who|when|where|which|how many|how much)\b/i,
  /^(define|explain|describe|summari[sz]e|translate|list|name)\b/i,
  /^(is|are|does|do|did|can|could|should|would|will)\b/i,
];

const CONJUNCTIONS = /\b(and then|then|after that|also|plus|as well as|followed by)\b/i;
const SEQUENCE_MARKER = /(\d\.\s|\n[-*]\s)/;

/**
 * Word-boundary term matching.
 *
 * Matching on raw substrings is wrong in both directions here: "build" contains
 * "ui" (a visual term) and "latest" contains "test" (a verify term). Terms are
 * therefore matched against whole tokens, with ordinary inflections allowed,
 * and multi-word terms against the phrase with word boundaries.
 */
const TERM_SUFFIXES = ["", "s", "es", "ing", "ed"];

function termMatcher(lower, tokens) {
  const flat = lower.replace(/[-/]+/g, " ");
  const tokenSet = new Set(tokens);
  return (term) => {
    if (term.includes(" ")) return new RegExp(`\\b${escapeRegExp(term)}\\b`).test(flat);
    for (const suffix of TERM_SUFFIXES) {
      if (tokenSet.has(`${term}${suffix}`)) return true;
    }
    return false;
  };
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function extractSignals(text) {
  const input = typeof text === "string" ? text : "";
  const lower = input.toLowerCase();
  const words = lower.match(/[a-z0-9']+/g) ?? [];

  const matcher = termMatcher(lower, words);
  const hits = (terms) => terms.filter(matcher);
  const research = hits(RESEARCH_TERMS);
  const build = hits(BUILD_TERMS);
  const visual = hits(VISUAL_TERMS);
  const verify = hits(VERIFY_TERMS);
  const data = hits(DATA_TERMS);
  const risk = hits(RISK_TERMS);
  const ambiguity = hits(AMBIGUITY_TERMS);

  const paths = input.match(/(?:[A-Za-z]:\\[^\s"'`]+|\/[\w./-]{3,}|\b[\w.-]+\.(?:js|ts|tsx|py|json|md|css|html|yml|yaml|csv|txt)\b)/g) ?? [];
  const urls = input.match(/https?:\/\/[^\s"'`)]+/g) ?? [];
  const codeBlocks = (input.match(/```/g) ?? []).length >= 2;

  const bullets = SEQUENCE_MARKER.test(input) ? (input.match(/\n\s*[-*\d]/g) ?? []).length : 0;
  const conjunctionCount = (input.match(new RegExp(CONJUNCTIONS, "gi")) ?? []).length;
  const clauses = lower.split(/\b(?:and then|then|and|,|;)\b/).filter((part) => part.trim().length > 8);

  const trivialShape = TRIVIAL_PATTERNS.some((pattern) => pattern.test(input.trim()));

  return {
    text: input,
    length: input.length,
    wordCount: words.length,
    question: /\?\s*$/.test(input.trim()) || trivialShape,
    trivialShape,
    hasPaths: paths.length > 0,
    hasUrls: urls.length > 0,
    hasCode: codeBlocks,
    paths: paths.slice(0, 12),
    urls: urls.slice(0, 8),
    bullets,
    conjunctionCount,
    clauseCount: clauses.length,
    /** Deterministic estimate of how much independent work the request implies. */
    subTaskEstimate: Math.max(1, Math.min(8, bullets || clauses.length + conjunctionCount)),
    domains: { research, build, visual, verify, data },
    domainCount: [research, build, visual, data].filter((list) => list.length > 0).length,
    risk,
    riskLevel: risk.length >= 2 ? "high" : risk.length === 1 ? "medium" : "low",
    ambiguity,
    ambiguityLevel: ambiguity.length >= 2 ? "high" : ambiguity.length === 1 ? "medium" : "low",
  };
}

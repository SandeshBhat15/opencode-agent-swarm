/**
 * Derived dashboard state.
 *
 * Every field here is produced by an event the engine actually emitted. There
 * is no timer that advances progress and no default that invents activity: an
 * agent is `idle` until a real step starts, `unknown` when its last event said
 * so, and a metric stays `null` until a measurement arrives.
 */

import { AGENTS, EVENT } from "./events.js";

const ROLE_TO_AGENT = Object.freeze({
  none: "manager",
  manager: "manager",
  researcher: "researcher",
  builder: "builder",
  visual: "visual",
  qc: "qc",
  improve: "improve",
});

export function agentForRole(role) {
  return ROLE_TO_AGENT[role] ?? "unknown";
}

const HANDOFF_LIMIT = 24;

export function createDashboardState({ now = () => Date.now(), handoffLimit = HANDOFF_LIMIT } = {}) {
  let run = null;
  let counters = emptyCounters();
  let agents = initialAgents();
  let lastError = null;
  let budget = null;
  let verification = null;
  let plan = null;
  let handoffs = [];

  function reset(taskId, at) {
    run = { id: taskId ?? null, phase: "running", startedAt: at, endedAt: null, durationMs: null, kind: null, workflow: null };
    counters = emptyCounters();
    agents = initialAgents();
    lastError = null;
    budget = null;
    verification = null;
    plan = null;
    handoffs = [];
  }

  function touch(agent, patch) {
    const current = agents[agent];
    if (!current) return;
    agents[agent] = { ...current, ...patch };
  }

  function startAgent(role, at, stepId, attempt) {
    const agent = agentForRole(role);
    if (!agents[agent]) return agent;
    touch(agent, { state: "running", stepId: stepId ?? null, role: role ?? null, startedAt: at, endedAt: null, attempt: attempt ?? 1 });
    return agent;
  }

  function finishAgent(role, at, state) {
    const agent = agentForRole(role);
    if (!agents[agent]) return agent;
    const startedAt = agents[agent].startedAt;
    touch(agent, {
      state,
      endedAt: at,
      durationMs: Number.isFinite(startedAt) ? at - startedAt : null,
    });
    return agent;
  }

  function apply(event) {
    if (!event || typeof event !== "object") return;

    switch (event.type) {
      case EVENT.TASK_START:
        reset(event.taskId, event.at);
        break;

      case EVENT.CLASSIFIED:
        if (run) run.kind = event.kind ?? null;
        break;

      case EVENT.PLANNED:
        if (run) run.workflow = event.workflow ?? null;
        plan = Array.isArray(event.steps)
          ? event.steps.map((step) => ({
              id: step?.id ?? null,
              role: step?.role ?? null,
              agent: agentForRole(step?.role),
              needs: Array.isArray(step?.needs) ? step.needs.filter((need) => typeof need === "string") : [],
            }))
          : null;
        break;

      case EVENT.STEP_START: {
        const at = event.at ?? now();
        startAgent(event.role, at, event.stepId, event.attempt);
        recordHandoff(event.stepId, at);
        break;
      }

      case EVENT.DELEGATE: {
        const agent = agentForRole(event.role);
        if (agents[agent] && agents[agent].state !== "running") startAgent(event.role, event.at ?? now(), event.stepId);
        if (agents[agent]) agents[agent].model = event.model ?? null;
        counters.modelCalls += 1;
        break;
      }

      case EVENT.STEP_END: {
        const state = event.status === "failed" ? "failed" : "completed";
        finishAgent(event.role, event.at ?? now(), state);
        counters.steps += 1;
        if (event.status === "failed") counters.failures += 1;
        if (Number.isFinite(event.durationMs) && agents[agentForRole(event.role)]) {
          agents[agentForRole(event.role)].durationMs = event.durationMs;
        }
        break;
      }

      case EVENT.RETRY:
        counters.retries += 1;
        break;

      case EVENT.FAILURE:
        counters.failures += 1;
        lastError = {
          stepId: event.stepId ?? null,
          // An error code, never a message: messages can quote the task.
          code: typeof event.code === "string" ? event.code.slice(0, 48) : null,
          at: event.at ?? now(),
        };
        if (event.role) finishAgent(event.role, event.at ?? now(), "failed");
        break;

      case EVENT.VERIFY: {
        const passed = event.passed === true;
        verification = {
          passed,
          independence: event.independence ?? null,
          failedCount: Number.isFinite(event.failedCount) ? event.failedCount : null,
          at: event.at ?? now(),
        };
        finishAgent("qc", event.at ?? now(), passed ? "completed" : "failed");
        if (event.judged === true) counters.modelCalls += 1;
        break;
      }

      case EVENT.DELIVER: {
        const at = event.at ?? now();
        if (run) {
          run.phase = event.status === "cancelled" ? "cancelled" : event.verified === true ? "completed" : "failed";
          run.endedAt = at;
          run.durationMs = Number.isFinite(event.durationMs) ? event.durationMs : at - (run.startedAt ?? at);
        }
        // Measured only. `null` means the runtime did not report it.
        counters.tokens = Number.isFinite(event.tokens) ? event.tokens : null;
        counters.costUSD = Number.isFinite(event.costUSD) ? event.costUSD : null;
        counters.toolCalls = Number.isFinite(event.toolCalls) ? event.toolCalls : null;
        if (event.budget && typeof event.budget === "object") budget = event.budget;
        if (agents.manager.state === "idle" || agents.manager.state === "unknown") {
          touch("manager", { state: run?.phase === "completed" ? "completed" : "failed", ...(run?.endedAt ? { endedAt: run.endedAt } : {}) });
        }
        break;
      }

      case EVENT.RUN_END:
        if (run && event.status) run.phase = event.status === "cancelled" ? "cancelled" : run.phase;
        break;

      case EVENT.TOOL:
        if (Number.isFinite(event.toolCalls)) counters.toolCalls = event.toolCalls;
        break;

      case EVENT.DASHBOARD:
        if (event.state === "stopped") {
          if (run && run.phase === "running") run.phase = "unknown";
          // Agents that were mid-step when observation ended are unknown, not
          // completed: the dashboard stopped seeing them, nothing more.
          for (const [name, agent] of Object.entries(agents)) {
            if (agent.state === "running") agents[name] = { ...agent, state: "unknown" };
          }
        }
        break;

      default:
        break;
    }
  }

  function recordHandoff(stepId, at) {
    if (!plan) return;
    const step = plan.find((entry) => entry.id === stepId);
    if (!step || step.needs.length === 0) return;
    const target = agentForRole(step.role);
    for (const need of step.needs) {
      const producer = plan.find((entry) => entry.id === need);
      const from = agentForRole(producer?.role);
      if (!from || from === target) continue;
      const last = handoffs[handoffs.length - 1];
      if (last && last.from === from && last.to === target && last.stepId === stepId) continue;
      handoffs.push({ from, to: target, stepId, at });
    }
    if (handoffs.length > handoffLimit) handoffs = handoffs.slice(handoffs.length - handoffLimit);
  }

  function snapshot() {
    const nowMs = now();
    const elapsedMs = run ? (run.phase === "running" ? Math.max(0, nowMs - (run.startedAt ?? nowMs)) : run.durationMs) : null;
    return {
      version: 1,
      phase: run?.phase ?? "idle",
      run: run
        ? {
            id: run.id,
            kind: run.kind,
            workflow: run.workflow,
            startedAt: run.startedAt,
            endedAt: run.endedAt,
            elapsedMs,
          }
        : null,
      agents: agents,
      plan: plan ?? [],
      handoffs: [...handoffs],
      counters: { ...counters },
      budget: budget ? { limits: budget.limits ?? null, used: budget.used ?? null, unknown: budget.unknown ?? null } : null,
      verification,
      lastError,
      updatedAt: nowMs,
    };
  }

  return { apply, snapshot };
}

function emptyCounters() {
  return {
    modelCalls: 0,
    steps: 0,
    retries: 0,
    failures: 0,
    // Not observed yet is not zero.
    tokens: null,
    costUSD: null,
    toolCalls: null,
  };
}

function initialAgents() {
  const out = {};
  for (const agent of AGENTS) {
    out[agent] = {
      state: "idle",
      stepId: null,
      role: null,
      model: null,
      attempt: null,
      startedAt: null,
      endedAt: null,
      durationMs: null,
    };
  }
  return out;
}

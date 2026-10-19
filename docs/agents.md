# Agents

The swarm registers one primary agent and six subagents. Agent IDs match the roles the
planner selects, so `src/exec/specialists.js` and the agent pages cannot drift apart.

| Agent ID | Mode | Page | Role |
|---|---|---|---|
| `swarm` | primary | `agents/swarm.md` | manager: plan, orchestrate, verify, deliver |
| `swarm/researcher` | subagent | `agents/swarm/researcher.md` | find and cite facts |
| `swarm/builder` | subagent | `agents/swarm/builder.md` | implement one scoped change |
| `swarm/visual` | subagent | `agents/swarm/visual.md` | produce and check visual/3D output |
| `swarm/qc` | subagent | `agents/swarm/qc.md` | independently judge acceptance criteria |
| `swarm/improve` | subagent | `agents/swarm/improve.md` | retrospective lessons and bounded proposals |
| `swarm/CHANGELOG` | subagent (hidden) | `agents/swarm/CHANGELOG.md` | change log of policy and lessons |

## When each one runs

Roles are chosen deterministically by the workflow table in `src/decide/workflows.js`, never
by the manager's mood:

| Workflow | Steps |
|---|---|
| `direct` | one in-session step — no child sessions |
| `research_only` | researcher → in-session delivery |
| `build_verify` | in-session build → QC |
| `visual_verify` | visual → QC |
| `research_build_verify` | researcher → in-session build → QC |
| `research_design_verify` | researcher → visual → QC |
| `research_deliver_verify` | researcher → in-session delivery → QC |
| `parallel_build_verify` | researcher ‖ visual → QC |

A role of `none` means the step is done by the manager in its own session. That is the
default for simple work, which is why trivial requests create no child sessions and cost no
delegation overhead.

## Contract

Each specialist page states the output contract its caller relies on. In particular:

- `swarm/qc` must end its answer with a single JSON line
  `{"passed": bool, "reason": str, "failed": [ids]}`, which `src/verify/verifier.js` parses.
- `swarm/researcher` returns findings with sources and explicit uncertainty, because verified
  findings are what task memory later reuses.
- `swarm/improve` may only propose; it never edits source, agents or configuration.

## Installation

The pages live in `agents/` in this repository. `scripts/install.mjs` copies them to
`~/.config/opencode/agents/` (creating `agents/swarm/` for the nested pages), which is where
OpenCode V2 discovers them.

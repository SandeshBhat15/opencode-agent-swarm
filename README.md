# OpenCode Agent Swarm

**One plain-English prompt → a finished artifact.** An autonomous multi-agent orchestrator for [OpenCode](https://opencode.ai) that triages your request, fans the work out to a swarm of specialist subagents in parallel, gates delivery on evidence-based QC, and improves itself after every run.

Works across domains: web/apps · games · Blender 3D/video · AR/Lens Studio · research & reverse-engineering.

## What makes it a swarm

- **Orchestrator-first** — set it as your default agent (or call `/swarm <task>`) and never plan again: it decides, delegates, integrates, verifies, delivers.
- **5 specialist subagents** — `swarm/researcher`, `swarm/builder`, `swarm/visual`, `swarm/qc`, `swarm/improve` — spawned in parallel with strict file-boundary contracts.
- **Artifact-first delivery** — every run ends with the artifact itself plus verification evidence (build output, screenshots, passing tests), never a chat-only summary.
- **Evidence-based QC gate** — an independent QC agent (different model than the builders) reviews against the plan; failures bounce back with fix orders, max 2 cycles.
- **Self-improvement** — after every run, `swarm/improve` distills lessons into a global ledger that the orchestrator reads at intake of the next run, and can propose protocol changes.
- **Resumable** — all state lives in `.opencode/swarm/<date>-<slug>/` (`plan.md`, `workstreams.md`, `status.md`, `history.md`, `reports.md`); any session can resume a half-finished run from disk alone.

## Install

Copy these into your OpenCode global config (`~/.config/opencode/`, on Windows `C:\Users\<you>\.config\opencode\`):

```
agents/swarm.md            → agents/swarm.md          # the orchestrator
agents/swarm/*.md          → agents/swarm/            # researcher, builder, visual, qc, improve
commands/swarm.md          → commands/swarm.md        # /swarm  (manual handoff)
commands/swarm-status.md   → commands/swarm-status.md # /swarm-status (peek/resume)
swarm-lessons.md           → swarm-lessons.md         # optional seed for the lessons ledger
```

Then make it your default agent in `opencode.json`:

```json
{ "default_agent": "swarm" }
```

(Or skip that and invoke `/swarm <task>` any time — the commands already bind to the `swarm` agent.)

**Requirements:** OpenCode v2 with subagent support. The five worker pages must be present at `agents/swarm/*.md` so the orchestrator can spawn `swarm/<role>` names.

## Usage

Just talk to it:

> build me a WebAR diwali greeting with camera fireworks

The swarm handles everything after that — clarify only when truly ambiguous, ask only when truly blocked.

| Command | What it does |
| --- | --- |
| `/swarm <task>` | Hand the swarm a task explicitly |
| `/swarm-status` | Report the latest run's phase, or resume it to delivery |

## How a run works

```
INTAKE   name the finished artifact + its evidence in one sentence
PLAN     2–6 independent workstreams with disjoint file scopes + checkable acceptance criteria
MESH     spawn all builders in parallel (background, delegation contracts)
INTEGRATE  persist worker reports to disk, merge outputs, resolve conflicts
QC       independent reviewer gates on evidence — max 2 fix cycles, then escalate
RETRO    swarm/improve distills lessons into the global ledger
DELIVER  artifact + proof; status.md flips to DELIVERED
```

**Triage** happens first: trivial questions answer themselves (`R1`), research fans out to `researcher`s (`R2`), single-file jobs skip ceremony (`R3`), everything else runs the full pipeline (`R4`).

## Self-improvement

After each delivery (or abort), `swarm/improve`:

1. Appends distilled lessons to `swarm-lessons.md` (~60-line cap — a cheat-sheet, not an archive), e.g. *"never hardcode `model:` pins in subagent pages"*.
2. Updates the subagent pages' CHANGELOG.
3. Drafts protocol changes for the orchestrator — which are applied **only with user approval**, never silently.

The orchestrator reads the ledger at INTAKE of every run, so each project starts smarter than the last.

## File map

```
agents/swarm.md            orchestrator protocol (triage, phases, delegation template, guardrails)
agents/swarm/
├── researcher.md          parallel fact-finding, receipts, read-only
├── builder.md             implementation within a declared file boundary
├── visual.md              Blender/3D/video workstreams (lookdev → screenshot proof)
├── qc.md                  independent evidence-based review, fix orders
├── improve.md             retrospectives, lessons ledger, CHANGELOG, proposals
└── CHANGELOG.md           subagent-page change history
commands/swarm.md          /swarm      — manual task handoff
commands/swarm-status.md   /swarm-status — report or resume latest run
swarm-lessons.md           cross-run lessons ledger (grows over time)
```

## Notes

- **No `model:` pins** in subagent pages — free-tier model IDs go stale and break spawns; pages leave `model` unset so workers inherit the live default. Spawn failures retry once, then reroute to a `general` executor with the contract pasted inline.
- **State vs. chat** — anything not written to the run dir doesn't exist for QC or a resumed session; worker reports are persisted verbatim at INTEGRATE.
- The swarm never pushes to git, resets history, or silently edits its own orchestrator — protocol changes come only from approved `improve` proposals.

## License

MIT — see [LICENSE](LICENSE).

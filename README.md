# OpenCode Agent Swarm

**One plain-English prompt → a finished artifact.** An orchestrator for [OpenCode](https://opencode.ai)
that triages your request, fans work out to specialist subagents in parallel, gates delivery on
evidence, and improves itself after every run.

Works across domains: web/apps · games · Blender 3D/video · AR/Lens Studio · research & reverse-engineering.

---

## What is in the box

| | |
|---|---|
| **Orchestrator** | `agents/swarm.md` — triage, plan, delegate, integrate, gate, deliver |
| **5 specialists** | `agents/swarm/{researcher,builder,visual,qc,improve}.md` |
| **2 commands** | `/swarm <task>` and `/swarm-status` |
| **`swarmkit/`** | The mechanism layer. A stdlib-only Python package that makes the protocol *enforceable* |
| **`tests/`** | 163 tests, no network, no API spend |

### Why `swarmkit` exists

Before this audit the repo was 12 files of prose. The protocol said *"never mark DELIVERED without
verifying acceptance criteria"*, *"file boundaries must be disjoint"*, and *"on failure retry once"* —
and none of it was checkable. A model following prose will eventually skip the step it is thinking about,
and nothing would notice.

`swarmkit` turns those sentences into commands that fail:

| Protocol claim | What enforces it now |
|---|---|
| phases run in order | `run phase` refuses an illegal transition |
| verify before delivering | `run phase DELIVERED` refuses unless every criterion has passing evidence |
| disjoint file scopes | `scope plan --strict` fails closed on overlap |
| bounded retries | `budget retry`, `budget spawn` cap concurrency and retries |
| workers do not collide | `iso create` / `iso integrate` — real git worktrees, conflict refusal |
| nothing is lost on interrupt | atomic writes + journal + `run recover` |
| self-improvement is safe | `lesson add --failure`, `lesson promote --tests-passed` |

`swarmkit` makes **no model calls**. The subagents are spawned by OpenCode; `swarmkit` is the ledger,
gate, and budget they are held to. That is why the test suite needs no API budget.

---

## Requirements

- **OpenCode v2** with subagent support (requires `mode: subagent` pages and the `subagent` tool)
- **Python 3.11+** — verified on **3.14.0**; see [Compatibility](#compatibility)
- **git** — only for the optional worktree isolation (`iso`). Everything else works without it.
- No third-party Python packages. `swarmkit` imports stdlib only.

## Install

Copy into your global OpenCode config (`~/.config/opencode/`, on Windows `C:\Users\<you>\.config\opencode\`):

```
agents/swarm.md            → agents/swarm.md          # orchestrator
agents/swarm/*.md          → agents/swarm/            # 5 specialists
commands/swarm.md          → commands/swarm.md        # /swarm
commands/swarm-status.md   → commands/swarm-status.md # /swarm-status
swarm-lessons.md           → swarm-lessons.md         # cross-run lessons ledger
```

Then make the orchestrator your default agent in `opencode.json(c)`:

```json
{ "default_agent": "swarm" }
```

Or skip that and just use `/swarm <task>` any time.

Keep `swarmkit/` in this repo. The agent pages call `python -m swarmkit` from the project working
directory, so clone the repo somewhere and run it from there:

```sh
git clone https://github.com/SandeshBhat15/opencode-agent-swarm.git
cd opencode-agent-swarm
python -m swarmkit selftest      # verifies the gate holds
```

---

## Usage

Just talk to it:

> build me a WebAR diwali greeting with camera fireworks

### Triage

Every prompt takes exactly one lane:

| Lane | When | Path |
|---|---|---|
| **R1** trivial | question, explanation, snippet | answer in chat, no ceremony |
| **R2** research | the deliverable is knowledge | fan out `researcher`s in parallel |
| **R3** small build | ≤2 files, no protected paths | one `builder`, verify, deliver |
| **R4** project | everything else | full pipeline |

R2–R4 still fire RETRO so lessons accumulate. R1 only does when the answer is worth remembering.

### The pipeline

```
INTAKE    name the artifact + the evidence that will prove it
PLAN      2-6 independent workstreams, disjoint scopes, checkable criteria
MESH      spawn every workstream as a parallel background subagent
INTEGRATE persist worker reports, merge, resolve conflicts
QC        independent reviewer gates on evidence - max 2 fix cycles
FIX  <--> QC   the only bounded loop
RETRO     swarm/improve distils lessons
DELIVER   artifact + proof   (gated)
```

Any phase may go to `ABORTED`.

### Delegation contract

Every spawn is a filled-in contract, never a vibe:

```
AGENT: swarm/<role>
OBJECTIVE: <exactly what must exist when you're done>
OUTPUT FORMAT: <exact deliverable shape>
TOOLS & SKILLS: <which to use>
FILE BOUNDARY: <only these paths; edit nothing else>
ACCEPTANCE CRITERIA: <checkable list>
REPORT BACK: WHAT / FILES / HOW-VERIFIED / GAPS
```

A failed spawn retries once, then reroutes the **whole contract inline** to a `general` executor —
never a pointer to "the context above".

---

## Verification: the delivery gate

A criterion passes only if its **most recent** decisive check passed. An earlier green cannot mask a
later red, and a `skip` is not a pass.

```sh
python -m swarmkit criterion add --id build-passes --text "npm run build exits 0"
python -m swarmkit evidence add --criterion build-passes \
        --command "npm run build" --result "exit 0, vite 7.2.1"
python -m swarmkit gate check          # per-criterion verdict
python -m swarmkit run phase DELIVERED # refuses unless every criterion passed
```

`evidence add` refuses an empty `--result`, and refuses an entry with neither `--command` nor
`--source`. *"Looks fine"* is not evidence. Checks you genuinely could not run should be recorded as
`--status skip` with the reason, so the gap is visible instead of silently absent.

### Per-domain checks

- **Code** — build, typecheck, lint, tests. Whatever the project's own commands are.
- **Web / browser games** — launch it. Inspect console errors, loading, controls, responsiveness.
- **3D / visual** — validate the generated files, output settings, and a real screenshot or render.
- **Research** — validate the links, and separate verified fact from inference.

---

## Parallel execution safety

### Scope validation (fails closed)

```sh
python -m swarmkit scope plan --ws build=src/core/** --ws ui=src/ui/** --strict
```

Refuses unbounded globs (`*`, `**`), `..` escapes, protected trees (`.git`, `node_modules`, `.venv`,
`dist`, …) and the swarm's own files. Reports `definite` or `possible` overlap — `possible` means
"cannot be *proven* disjoint", which is treated as a conflict under `--strict`.

### Worktree isolation

```sh
python -m swarmkit iso create  --trees .swarm-trees --ws build --ws ui
python -m swarmkit iso patch   --wt .swarm-trees/build --out build.patch
python -m swarmkit iso integrate --patch build.patch
python -m swarmkit iso remove --wt .swarm-trees/build
```

`iso integrate` runs `git apply --check --3way` first and **never silently overwrites**. A conflict
returns `{"applied": false, "reason": …, "detail": …}` and becomes an INTEGRATE work item.

---

## Limits and cost

| Limit | Default |
|---|---|
| `max_concurrent_agents` | 4 |
| `max_spawns_total` | 40 |
| `max_retries_per_worker` | 2 |
| `max_fix_cycles` | 2 |
| `max_wall_clock_seconds` | 3600 |
| `max_tokens` | unlimited |
| `price_in_per_mtok` / `price_out_per_mtok` | unset |

Override in `swarm.config.json` at the workspace root:

```json
{ "limits": { "max_concurrent_agents": 6, "max_fix_cycles": 1,
              "price_in_per_mtok": 0.30, "price_out_per_mtok": 0.60 } }
```

Concurrency is enforced across processes — each `budget spawn` persists its lease, so a cap cannot be
evaded by running one command per worker. A lease whose process died is reclaimed after 15 minutes
rather than deadlocking the run.

**Cost is reported only when pricing is configured.** Without `price_*_per_mtok`, `budget show` prints
`estimated_cost_usd: null`. It never invents a number.

---

## Self-improvement, safely

Lessons are grounded or refused:

```sh
python -m swarmkit lesson add --rule "Never trust a guardrail you have not proven matches" \
        --failure "action 'bash' matched no V2 permission action, so 22 denies were inert" \
        --evidence "swarmkit lint reported 22 x SW003"

python -m swarmkit lesson audit   # ungrounded / duplicates / contradictions / unverified
```

Promotion requires regression tests to have actually run:

```sh
python -m swarmkit lesson promote --rule "..." --evidence "tests/test_gate.py OK" --tests-passed
```

Without `--tests-passed` the promotion is refused (`PROMOTION_BLOCKED`).

**Nothing ever auto-edits an agent page.** `swarm/improve` is propose-only; a page change needs your
explicit approval of that diff. This used to be contradictory — `improve` claimed it could auto-apply
edits to the four specialist pages, including `qc.md`, the very gate that judges its own output. That
loophole is closed.

The ledger is capped (60 lines) and rejects duplicates and contradictions, so it stays a cheat-sheet
rather than becoming an archive.

---

## Running the tests

```sh
python -m unittest discover -s tests -t .     # 163 tests
python -m swarmkit lint --root .              # contract lint over agents/ and commands/
python -m swarmkit selftest                   # quick end-to-end gate check
```

No network, no credentials, no API spend. Model calls are never made by `swarmkit`, so there is
nothing to mock. External model behaviour is covered by contract tests over the agent pages instead —
see `tests/test_contracts.py`.

> On Windows PowerShell, a successful run can still report exit code 1 because unittest writes its
> summary to stderr. Read the `OK` / `FAILED` line, not the exit code.

### CI

`.github/workflows/ci.yml` runs `lint` + `selftest` + the full suite on **ubuntu-latest and
windows-latest** with Python **3.14**, and no install step (there are no dependencies). A second
non-blocking job probes Python 3.12.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `RECOVERY_REQUIRED` on `run phase` | A previous process died mid-write. Read the message — it names the operation — then `run recover --note "..."` |
| `GateError: NO_CRITERIA` on DELIVERED | No criteria were registered in PLAN. Add them with `criterion add` |
| `GateError: GATE_CLOSED` | A criterion has no evidence, or its latest check failed. `gate check` shows which |
| `BudgetExceeded` on spawn | Concurrency cap, spawn cap, or the wall clock. `budget show` says which |
| `SCOPE_CONFLICT` | Two workstreams can write the same path. Re-cut the scopes |
| `SwarmError: RUN_EXISTS` | That slug already exists. Pick another slug, or resume the existing run |
| `SCOPE_CONFLICT_POSSIBLE` | Overlap that could not be proven disjoint. Fails closed on purpose — split the scope |
| Worker appears hung | Leases self-heal after 15 min. Check `budget show` and the worker's own log |

### Recovering an interrupted run

```sh
python -m swarmkit run list      # unfinished runs sort first
python -m swarmkit run show      # phase, next action, which criteria lack evidence
python -m swarmkit run recover --note "what you found"
python -m swarmkit run phase <NEXT>
```

State writes are atomic (temp file → fsync → rename), so you never resume from a half-written file.

---

## Compatibility

- **OpenCode:** v2 with subagent support. The contract linter enforces this — it knows the V2
  permission actions and rejects V1 spellings (`bash` → `shell`, `task` → `subagent`). Before the
  audit every page used `action: bash`, which **matched nothing**, so all 22 shell guardrails in the
  repo silently fell through to the base allow. `swarmkit lint` now makes that class of mistake a
  hard error (`SW003`).
- **Python:** 3.11+ claimed, **verified on 3.14.0 only.** The code uses no syntax or stdlib newer than
  3.11 (checked by hand). 3.12 and 3.13 are **not installed on this machine and were never run**, so
  CI probes them non-blockingly. Treat older-Python support as unverified until that probe is green.
- **Windows:** developed and tested on Windows. `git rev-parse` returns 8.3 short paths there; the
  worktree tests compare with `os.path.samefile` rather than string equality.

---

## Known limitations

Honest list, not marketing:

- **Token and cost tracking is manual.** `budget tokens --in N --out N` records usage; `swarmkit`
  cannot read it from the provider, so nothing feeds it automatically.
- **Cost is unknown by default.** Without configured `price_*_per_mtok`, `estimated_cost_usd` is
  `null`. It is never guessed.
- **Overlap detection is conservative.** Two globs that cannot be *proven* disjoint report `possible`,
  not `definite`. This is deliberate — it fails closed — but it will occasionally stop a plan that a
  human could have run safely. `--strict` off tolerates `possible`.
- **Three shipped lessons are `ungrounded`.** The original three entries predate the `--failure`
  requirement and carry no recorded origin. The audit reports them rather than inventing a cause.
  They are marked `UNVERIFIED (not promoted)`.
- **No end-to-end run against a live model has been performed.** Everything verified here is the
  mechanism layer, the CLI, and the agent-page contracts. The orchestration itself is unexercised.
- **Worktree isolation requires git and a git repo.** Without one, only scope validation applies.
- **`--json` is a global flag** and must precede the subcommand: `swarm --json run list`.

## Roadmap

- `swarmkit budget tokens --from-opencode` — read usage from a session export instead of by hand
- Persist a machine-readable run summary (`summary.json`) so `/swarm-status` needs no prose parsing
- Let `swarm/improve` emit a patch file for a page, ready to review, instead of a described change
- Promote the Python 3.12 CI probe to a gate once it is genuinely green

---

## License

MIT — see [LICENSE](LICENSE).
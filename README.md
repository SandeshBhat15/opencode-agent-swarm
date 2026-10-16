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
| **`tests/`** | 408 tests, no network, no API spend |

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
| bounded retries | `workers spawn` charges a retry atomically before the attempt; cap enforced |
| a dead worker is detected | `workers beat` / `workers status` → `stale` vs `slow`, `workers reclaim` |
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

### Start a run from one sentence

`run init --request` takes the plain-English text, decides the lane, builds the task graph,
and writes the acceptance criteria. You never pick agents, run phases, or invoke QC yourself.

```sh
python -m swarmkit run init --request "build me a WebAR diwali greeting" --auto-plan
#  -> {"lane":"R4","planned":true,"tasks":[...],"parallel_frontier":[...],
#      "next":"run phase UNDERSTAND"}
```

Add `--objective` to state a subtask yourself instead of letting the plan infer it.

```sh
python -m swarmkit run triage "what is the capital of France"   # -> R1, answer directly
python -m swarmkit run state                                      # where am I, what is outstanding
```

### The manager lifecycle

Nine stages, each one an enforced phase change rather than a paragraph:

```
UNDERSTAND -> PLAN -> DELEGATE -> EXECUTE -> MONITOR -> INTEGRATE -> VERIFY -> REPAIR -> DELIVER
```

`run init` lands you at INTAKE. `run phase <PHASE>` is what actually moves you, and an illegal
jump is refused. `UNDERSTAND` and `MONITOR` exist so the outcome and the supervision are
recorded decisions; `MESH -> MONITOR -> MESH` allows re-spawning after a reclaim.

### Approval boundary

Routine decisions are the manager's own. Consequential ones are screened first:

```sh
python -m swarmkit approve "run the test suite"          # {"approved": true}
python -m swarmkit approve "deploy to production"        # {"approved": false, "code":"APPROVAL_REQUIRED"}
python -m swarmkit approve "add a deploy button to the ui"  # {"approved": true}
```

A refusal stops that one step and is reported; everything else carries on. Approvals are never
persisted, so a one-off go-ahead cannot become standing permission.

### Triage

Every prompt takes exactly one lane:

| Lane | When | Path |
|---|---|---|
| **R1** trivial | question, explanation, snippet | answer in chat, no ceremony |
| **R2** research | the deliverable is knowledge | fan out `swarm/researcher`s in parallel |
| **R3** small build | ≤2 files, no protected paths | one `swarm/builder`, verify, deliver |
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

## The improvement engine

The lessons ledger above is a cheat-sheet. This is the machinery underneath it: structured records of
every run, tiered memory, a measurement-driven analyzer, and bounded experiments against a fixed
baseline.

### It captures on its own

The moment a run reaches `DELIVERED` or `ABORTED`, `run phase` writes a structured experience into
`.opencode/swarm-memory/experiences.jsonl`, read from the files the run actually produced — the state,
the plan, the criteria, the evidence, the budget counters, the worker records. Nobody has to remember
to teach it, and capture cannot claim something the run did not record. Credentials, tokens, bearer
headers, private keys and email addresses are redacted before anything is written.

```sh
python -m swarmkit experience list --category web
python -m swarmkit learn recall --category web     # most confident first, with provenance
python -m swarmkit learn schema                    # the record shape
python -m swarmkit experience challenge --run <run> --reason "<what the new evidence shows>"
```

| Tier | Means |
| --- | --- |
| `lesson` | A rule, extracted from a run that verified and carries a recorded command |
| `verified` | Proved out, but nobody has distilled a rule from it yet |
| `hypothesis` | Plausible, with no reproduced evidence |
| `history` | A raw structured record |

`lesson` is reserved for records that actually carry a rule. A verified run with no distilled rule says
`verified`, because `learn lessons` would otherwise report rules that do not exist — found by running
the engine end to end, not by reading it.

### It refuses to overclaim

`analyze` proposes a change only where a metric is both **measured** and **bad**, and it withholds any
conclusion from fewer than five runs. Agent counts, code volume and test counts are never scored — a
bigger engine is not a better one.

```sh
python -m swarmkit analyze --check-only   # is now a sensible moment? always says why
python -m swarmkit analyze                # metrics plus evidenced proposals, ranked
```

### Experiments are bounded, versioned and reversible

```sh
python -m swarmkit evolve candidate --proposal <id>        # id must come from `analyze`
python -m swarmkit evolve evaluate --proposal <id> --version <ver> \
        --candidate final_delivery=0.90 --baseline final_delivery=0.50 --safety-passed
python -m swarmkit evolve monitor  --version <ver> --observed final_delivery=0.20
python -m swarmkit evolve rollback --version <ver> --reason "..."
python -m swarmkit evolve history
python -m swarmkit evolve scope-check --path <file>...
```

- **Candidates cannot be hand-written.** `evolve candidate` reads the analyzer's output and refuses any
  id it did not raise (`PROPOSAL_NOT_FOUND`). An operator who can invent a proposal can invent its
  evidence, and the loop becomes a machine for believing whatever it is told.
- **Three gates, in order.** Safety first, so a change that improves throughput while failing a safety
  check is rejected as unsafe rather than promoted with a caveat. Then the benchmark, which demands a
  real margin on a guarded metric with no regression on any other. Then authorization.
- **`verification` and `recovery` never auto-promote.** That refusal is structural, not a default: a
  learner that can weaken the components judging it has no ground truth left to improve against.
- **Some refusals are permanent, some are not.** A change that measurably does not help is retired so
  nobody retries it blindly. A refusal caused by the environment — safety did not pass, authorization
  missing — leaves the version a `candidate` and can be re-evaluated once the cause is fixed.
- **Monitoring compares against what was promised**, not against the baseline. A change promoted at
  0.90 that collapses to 0.60 is caught even though 0.60 still beats the 0.50 baseline it replaced.
  Comparing to the baseline made the gate structurally unable to fire for the change it exists to police.
- **Two independent budgets.** `max_generations` caps promoted depth; `max_candidates` caps how many
  records may exist at all, so an engine proposing reworded versions of an idea that already lost three
  times cannot write files forever while the first limit stays satisfied.
- **Protected paths cannot be self-evolved**: the gate, budget, store, lessons ledger, security tests,
  changelog and `.git` (`PROTECTED_SCOPE_CHANGE`). Every experiment runs in an isolated workspace
  against a copied baseline.

```sh
python -m swarmkit evolve policy
python -m swarmkit evolve policy --max-generations 3 --max-candidates 10 --no-auto-promote
```

**What is deliberately absent:** the engine does not score how many agents, files or tests exist, does
not promote its own code, and never edits an agent page. A proposal for a page change reaches you as a
diff.

---

## Running the tests

```sh
python -m unittest discover -s tests -t .     # 408 tests
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
| `SCOPE_CONFLICT_POSSIBLE` | Overlap that could not be proven disjoint. Fails closed on purpose - split the scope |
| `CORRUPT_STATE` | `state.json` is unreadable or missing `slug`/`phase`. Move it aside and `run recover`, or delete the run - `run init` regenerates it |
| `CORRUPT_BUDGET` | `budget.json` is unreadable. Refused rather than reset, because these counters are what enforce the spawn/retry caps across CLI calls |
| `AMBIGUOUS_RUN` | A bare slug matched more than one run. Pass the full directory name, e.g. the `<date>-<slug>` form |
| `DUPLICATE_WORKER` | That worker is `alive` or `slow` - refusing to run two of them. Check `workers status` |
| `EPOCH_MISMATCH` | A `beat`/`finish` from a worker that was already reclaimed and respawned. Use the current epoch |
| `NO_SUCH_WORKER` | `workers beat`/`finish` named a worker that is not in the registry for this run |
| `BAD_STATE` | `workers finish --state` accepts only `ok` or `failed` |
| `CORRUPT_REGISTRY` | `workers.json` is unreadable. Move it aside to reset worker state |
| `LEASE_NOT_OWNED` | `budget release` given an epoch that does not own the lease |
| `LOCK_HELD` | A concurrent `counters.lock` did not free within `LOCK_WAIT_SECONDS` (5s). Retry |
| `NO_SUCH_WORKTREE` | `iso --ws` name is unknown. The message lists the workstreams the manifest actually holds |
| `BAD_USAGE` on `run phase` | Neither a positional phase nor `--to` was given, or the two disagreed |
| Worker appears hung | `workers status` classifies each worker `alive`/`slow`/`stale`/`timeout`. A `slow` worker is beating and is fine; a `stale` one stopped beating. `workers reclaim --name X` frees a dead worker's slot, then re-spawn it |

### Worker liveness and retries

```sh
python -m swarmkit workers spawn  --name build --role builder --timeout 900
#  -> {"worker": {"name":"build","attempt":1,"epoch":"9f2c..."}, "retry_delay": 2.0, ...}

python -m swarmkit workers beat   --name build --epoch 9f2c...   # every ~30s
python -m swarmkit workers progress --name build --epoch 9f2c... --units 1
python -m swarmkit workers status                              # alive/slow/wedged/stale/timeout
python -m swarmkit workers finish --name build --epoch 9f2c... --state ok
python -m swarmkit workers reclaim                            # frees only stale/timeout slots
python -m swarmkit workers reconcile                         # after a crash - see below
```

`workers progress` records forward movement. It matters because a heartbeat
only proves the worker is *breathing*, not that it is *advancing*: a task stuck
in a retry loop beats happily forever. `classify` reports **wedged** when a
worker has beaten recently but reported no progress for
`DEFAULT_PROGRESS_STALE_SECONDS` (600s). The guard that stops this from killing
legitimate long work is that wedging requires evidence of a spin — a worker that
has reported no progress *at all* is never called wedged, and reclaiming one
still needs an explicit `--include-wedged`, because a wedged worker is provably
alive and only unproductive.

`workers reconcile` is crash recovery for the state the lifecycle cannot reach
by itself. If a worker dies between `finish` and `release`, or after spawning
but before running, the slot is orphaned and the run eventually deadlocks on
`CONCURRENCY_LIMIT`. `reconcile` walks the registry against the leases and
releases what cannot be alive, matching a worker's results back to it through
the criterion's `workstream`. It is idempotent: running it twice releases
nothing the second time.

**Lock policy.** State-phase mutations (`run phase`) take the fail-fast lock and
refuse rather than queue. Registry and counter writes (`spawn`, `reclaim`,
`reconcile`, `progress`) take a bounded-wait lock, because several workers beat
at once and dropping all but the first would lose their progress. A lock whose
recorded pid is still alive is never stolen, however old its timestamp.

**`workers spawn` is the real spawn path, not `budget spawn`.** `budget spawn`
takes a lease but writes no `workers.json` record, so the run cannot see its own
workers: nothing beats, nothing reports progress, and nothing is reclaimable. The
agent pages were telling the orchestrator to use it for two rounds of
worker-liveness work that was therefore unreachable, and `lint` passed the whole
time because it reads frontmatter, not page bodies. `tests/test_contracts.py`
now asserts that every `workers` command and every worker's liveness contract
appears in the page that needs it.

**Evidence carries its provenance.** `evidence.jsonl` is append-only, so a worker
that was reclaimed and respawned can still append after its replacement finished.
Judged purely by recency, that zombie's late line is the newest and wins. Each
evidence entry therefore records `worker` and `attempt`, and the gate ignores any
attempt that a newer attempt of the same worker has superseded. Entries with no
worker or attempt - a human running `evidence add`, or an older run - are
unaffected.

- **`--epoch` is an ownership token.** A worker reclaimed and respawned gets a *new* epoch; the old
  one's `beat`/`finish` is refused with `EPOCH_MISMATCH`. A zombie cannot resurrect itself and a
  live worker cannot be finished by a stale predecessor.
- **Dead vs slow.** `alive` = beating recently. `slow` = beating, but past 80% of its timeout - still
  healthy, never reclaimed. `stale` = no beat for `stale_seconds` (default 120) - probably dead.
  `timeout` = outlived `timeout_s` regardless of beating, so an infinite loop cannot beat its way out.
- **Retries are automatic.** Re-spawning a worker whose predecessor is gone charges a retry *before*
  the attempt starts, so `max_retries_per_worker` stops it even if nobody calls `budget retry`.
- **Counters are atomic.** `budget.json` updates happen under `counters.lock`, so N concurrent
  `workers spawn` calls cannot lose an increment and slip past a cap. Covered by a test that races
  12 threads and asserts exactly 4 are granted at `max_concurrent_agents=4`.

### Recovering an interrupted run

```sh
python -m swarmkit run list      # unfinished runs sort first
python -m swarmkit run show      # phase, next action, which criteria lack evidence
python -m swarmkit run recover --note "what you found"
python -m swarmkit run phase <NEXT>
```

State writes are atomic (temp file → fsync → rename), so you never resume from a half-written file.

### Live-model smoke test (manual approval required)

`swarmkit` never calls a model, so nothing here has ever been exercised against a real one. This is
the procedure for doing that. **It spends real tokens. Ask before running it, and run it in a
throwaway directory - never against a project you cannot lose.**

```sh
# 0. Throwaway git repo, nothing of value in it.
mkdir smoke && cd smoke && git init -b main && git commit --allow-empty -m init

# 1. Caps set low on purpose, so a runaway run stops itself early.
cat > swarm.config.json <<'JSON'
{ "limits": { "max_concurrent_agents": 2, "max_spawns_total": 6,
              "max_retries_per_worker": 1, "max_fix_cycles": 1,
              "max_wall_clock_seconds": 600 } }
JSON

# 2. A task with an objective, checkable acceptance criterion.
python -m swarmkit run init --slug smoke --goal "a greeter script that prints Hello and exits 0"

# 3. Run the swarm against it, from the OpenCode session, with `swarm` as the agent.
#    Ask for something trivial. The point is to watch the machinery, not the output.

# 4. While it runs, from a second shell - this is what you are actually testing:
python -m swarmkit --json workers status
python -m swarmkit --json budget show
python -m swarmkit --json run show

# 5. After: confirm it refused to cheat.
python -m swarmkit --json gate check
```

**Pass criteria** - all must hold, or the smoke test found a real bug:

| Check | Expected |
|---|---|
| `run show` walks `PLAN → MESH → INTEGRATE → QC → RETRO → DELIVERED` in order | no phase skipped |
| Delivery is refused before evidence exists | `error [GATE_CLOSED]` |
| `workers status` showed every spawned worker | each reaches a terminal state |
| A worker that failed was reclaimed, not silently retried forever | `retries` ≤ cap, then `RETRY_LIMIT` |
| `budget.json` spawn count equals the workers actually spawned | counters not reset mid-run |
| The orchestrator obeyed its own instructions | no agent page edited, no git push |

**If any row fails**, that is a finding, not a flake. Re-run `tests/test_integration.py` with the
observed behaviour encoded as a mock first - that test is free, and a bug found there is cheaper
than a bug found here.

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
  mechanism layer, the CLI, the agent-page contracts, and `tests/test_integration.py` - which drives
  the real lifecycle against a mock worker adapter. The orchestration itself is unexercised.
  See [Live-model smoke test](#live-model-smoke-test-manual-approval-required) for the procedure.
- **Worker liveness is heartbeat + wall-clock, not process supervision.** `workers spawn` hands back an
  `epoch`; the worker must call `workers beat` with it. A worker that stops beating past
  `stale_seconds` is `stale`, and one that outlives its `timeout_s` is `timeout` - a worker in an
  infinite loop cannot beat its way out of the latter. `workers reclaim` frees the slot of a
  `stale`/`timeout` worker only; a `slow` worker that is still beating is never reclaimed.
  **Limit:** swarmkit does not watch the pid - it trusts the beats. A worker that keeps beating but
  makes no progress runs until `timeout_s`. There is no automatic re-spawn; you sweep with
  `workers reclaim` and re-spawn, or let the next `workers spawn` retry it.
- **Retries are charged inside `workers spawn`, not by a separate command.** Re-spawning a worker
  whose predecessor is gone increments its retry counter *before* the new attempt starts, under the
  same lock that takes the lease, so an exhausted budget stops the retry instead of starting a worker
  that is then killed. `budget retry` still exists for manual accounting, but the cap no longer
  depends on anyone remembering to call it. Backoff is returned as `retry_delay` on the spawn
  result - **honouring it is the caller's job**, nothing sleeps for you.
- **`lint` checks frontmatter only.** It validates agent pages' YAML (V2 permission actions, model
  pins, deny-before-allow ordering, command bindings). It never parses a page body, so it cannot
  verify that a documented CLI invocation actually works - `tests/test_cli.py` does that instead by
  executing the documented strings verbatim, and `TestAgentPagesReachEveryCommandTheyNeed` asserts the
  page an agent reads actually contains each command it needs.
- **The improvement engine has never run against a real corpus.** It is exercised end to end by
  `_e2e_engine.py` and 71 unit tests, but on generated runs and seeded baselines. Nobody has watched
  five real deliveries accumulate and checked whether its proposals are any good. This is the largest
  unverified claim in the repo.
- **Experience capture never invents a rule.** A delivered run with no distilled rule lands in the
  `verified` tier, so the engine starts out with evidence but no lessons. Something has to do the
  distillation - today that is `swarm/improve` writing to the flat ledger, and the two stores are not
  yet joined automatically.
- **Promotion is gated but not automatic.** `auto_promote` defaults to `False` and there is no code
  path that flips it on. There is no scheduler: nobody calls `analyze` or `evolve candidate` on a
  timer, so the engine improves when an agent runs the commands, not on a schedule.
- **Worktree isolation requires git and a git repo.** Without one, only scope validation applies.
- **`--json` is a global flag** and must precede the subcommand: `swarm --json run list`.

## Roadmap

- `swarmkit budget tokens --from-opencode` — read usage from a session export instead of by hand
- Join the experience store and the flat ledger, so a distilled rule carries its originating run
- Promote the Python 3.12 CI probe to a gate once it is genuinely green

---

## License

MIT — see [LICENSE](LICENSE).
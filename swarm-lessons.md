# Swarm lessons ledger

Standing rules distilled from past swarm runs. The orchestrator reads this at INTAKE of every run and obeys it. Maintained by `swarm/improve` after every delivery (or abort). Cap: ~60 lines — it is a cheat-sheet, not an archive.

## Lessons

## 2026-10-16 diwali-web-ar — pin death
Never hardcode a `model:` pin in a subagent page (catalog `status: active` does NOT prove a `:free` tier is spawnable); leave it unset so the unit inherits the live default, and if a spawn still fails, retry once, then reroute to a `general` executor with the full delegation contract pasted inline and log the reroute in `history.md`.
- status: UNVERIFIED (not promoted)

## 2026-10-16 diwali-web-ar — integrate persistence
At INTEGRATE, write every worker report into `reports.md` in the run dir verbatim — anything left only in the message stream is invisible to QC and to a resumed session.
- status: UNVERIFIED (not promoted)

## 2026-10-16 diwali-web-ar — evidence, not memory
Read the artifact before trusting any claim about it: for WebGL a screenshot is the only visual evidence (all-zero canvas `getImageData` is a `preserveDrawingBuffer:false` artifact, not a dead renderer); read the real DOM id / source text before dispatching an interaction or an edit, never a recalled one.
- status: UNVERIFIED (not promoted)

## 2026-10-16 audit — guardrails must be proven
Never trust a guardrail you have not proven matches — every `action: bash` deny in this repo matched no V2 permission action, so 22 rules silently blocked nothing while reading as hard walls.
- origin: contract lint reported 22 × SW003: 'bash' is not a V2 action and matches nothing
- evidence: python -m swarmkit lint --root . — 8 pages, 30 errors before the fix, 0 after
- status: UNVERIFIED (not promoted)

## 2026-10-16 audit — probe and action need different argv
Never reuse one argv for both the probe and the action it gates — `git apply --check` exits 0 while applying nothing, so a shared argument list made `integrate_patch` report success with an untouched working tree.
- origin: tests/test_isolator.py TestWorktrees reported applied=True while new.py did not exist
- evidence: python -m unittest tests.test_isolator.TestWorktrees — 3 errors before the fix, OK after
- status: UNVERIFIED (not promoted)

## 2026-10-16 audit — record the command, not the summary
Always record the failing command, not a summary of it — `integration passed` was true and meant nothing without the `git status` that disproved it.
- origin: the whole suite was green while integrate_patch was a silent no-op
- evidence: tests/test_isolator.py asserts the file exists on disk, not merely that apply returned 0
- status: UNVERIFIED (not promoted)

## 2026-10-16 audit — a validator is not a story
Write the check that can fail, not the paragraph that says it would — every phase rule in the orchestrator was prose until `swarmkit` made each one a command with an exit code.
- origin: all six original defects were invisible to the repo: no tests, no lint, no gate
- evidence: 160 unittest cases across phases, store, budget, gate, isolator, lessons, contracts, cli
- status: UNVERIFIED (not promoted)
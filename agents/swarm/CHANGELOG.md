# Swarm agent-page CHANGELOG

This file records **user-approved** changes to the five subagent pages. `swarm/improve` is
propose-only: it never writes to this file or to any agent page on its own. Changes to the
orchestrator (`agents/swarm.md`) are NEVER listed here — they require separate explicit approval.

Earlier revisions of this file were written by an auto-applying step that could rewrite
`qc.md` itself. That behaviour is removed; see "2026-10-16 — audit" below.

## 2026-10-16 — run `2026-10-16-diwali-web-ar`

### `agents/swarm/researcher.md`
- **Before:** frontmatter carried `model: tokenharbor/claude-haiku-5.5#free` (3rd line of frontmatter)
- **After:** the `model:` line is deleted; the agent inherits the session's live default model
- **Reason:** both researcher spawns (W1 + W2) died at spawn with "free offer ended" — a durable page-level pin against a volatile free-tier offer. Rerouting to `general` executors with the contract pasted inline worked 2/2. Note: the model catalog still reports this model as `status: active`, so catalog status is not spawnable-proof and re-pinning would have reproduced the failure.

### `agents/swarm/qc.md`
- **Before:** frontmatter carried `model: tokenharbor/deepseek-v4-flash#free` (3rd line of frontmatter)
- **After:** the `model:` line is deleted; the agent inherits the session's live default model
- **Reason:** the QC spawn died at spawn — "does not accept image input" — on a text-only prompt, a capability-routing check the hardcoded pin bypassed. Reroute to a `general` executor produced a real verdict (PASS).
- **Also, line 25:** "You run on a deliberately DIFFERENT model than the builders" → "Assume you run on a deliberately DIFFERENT model than the builders" (1 sentence). With the pin gone the original assertion was no longer guaranteed true; the intent (think independently, adversarially) is preserved. The frontmatter `description` still says "on a deliberately different model" to keep that selection intent visible to the orchestrator.

### `agents/swarm/builder.md` — NO EDIT
- **Before/After:** unchanged
- **Reason:** healthy. Declares no `model:` → inherits the live default → 0 spawn issues across all builds and the fix cycle, and honored its file boundary exactly (3 files, no out-of-scope writes). Confirms the unpinned pattern as the working reference.

### `agents/swarm/visual.md` — NO EDIT
- **Before/After:** unchanged
- **Reason:** no visual workstream this run; page already unpinned (no `model:` field), so it carries no pin-death risk. No evidence of a defect to fix — editing it would have been speculative.

### `agents/swarm/improve.md` — NO EDIT
- **Before/After:** unchanged
- **Reason:** its `model:` pin was removed during the 2026-10-16 audit (see that entry), not
  during the `diwali-web-ar` run. This run produced no change to the page.

## 2026-10-16 — protocol v1.2 (orchestrator, user-directed)

### `agents/swarm.md` (orchestrator) — user request
- **Change:** added **Lifecycle — nothing outlives the session** section: background jobs are session-scoped; REGISTER every background process in `status.md` under `## live processes` (PID/port/what/why); at DELIVER, stop everything the run started unless the user asked to keep it; never promise always-on URLs (start/stop commands instead); on RESUME, netstat registered ports and taskkill strays before spawning new servers.
- **Trigger:** user requirement — "agent should stop working after closing application or the session."
- **Also:** this CHANGELOG gained this orchestrator-entry precedent so future user-directed protocol changes are visible in one place.
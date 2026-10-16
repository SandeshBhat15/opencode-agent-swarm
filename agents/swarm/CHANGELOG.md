# Swarm agent-page CHANGELOG

Auto-applied minimal edits to `agents/swarm/*.md` by `swarm/improve`. Orchestrator (`agents/swarm.md`) changes are NEVER listed here — they require user approval.

## 2026-10-16 — run `2026-10-16-diwali-web-ar`

### `agents/swarm/researcher.md`
- **Before:** frontmatter carried `model: tokenharbor/claude-haiku-5.5:free` (3rd line of frontmatter)
- **After:** the `model:` line is deleted; the agent inherits the session's live default model
- **Reason:** both researcher spawns (W1 + W2) died at spawn with "free offer ended" — a durable page-level pin against a volatile free-tier offer. Rerouting to `general` executors with the contract pasted inline worked 2/2. Note: the model catalog still reports this model as `status: active`, so catalog status is not spawnable-proof and re-pinning would have reproduced the failure.

### `agents/swarm/qc.md`
- **Before:** frontmatter carried `model: tokenharbor/deepseek-v4-flash:free` (3rd line of frontmatter)
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
- **Reason:** its own pin (`tokenharbor/deepseek-v4.1-flash:free`) spawned fine this run; it is a `:free` pin of the same fragile class and should be unpinned at the next opportunity if it ever fails at spawn.
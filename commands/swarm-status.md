---
description: Peek at the latest swarm run — report phase and workstream status, and resume it to delivery if unfinished.
agent: swarm
---
$ARGUMENTS

Read the latest run dir under `.opencode/swarm/` in the workspace (most recent modification; if a run slug was given above, use that one). Then:

1. REPORT: from `status.md` + `workstreams.md` — current phase, per-workstream status, evidence so far. Compact.
2. If the run is UNFINISHED (status is not DELIVERED): resume it from disk — rebuild context from the state files and drive it to delivery without redoing finished work.
3. If no run dirs exist: reply with one line — "no swarm runs yet."

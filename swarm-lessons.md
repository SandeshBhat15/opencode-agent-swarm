# Swarm lessons ledger

Standing rules distilled from past swarm runs. The orchestrator reads this at INTAKE of every run and obeys it. Maintained by `swarm/improve` after every delivery (or abort). Cap: ~60 lines — it is a cheat-sheet, not an archive.

## Lessons

## 2026-10-16 diwali-web-ar — pin death
Never hardcode a `model:` pin in a subagent page (catalog `status: active` does NOT prove a `:free` tier is spawnable); leave it unset so the unit inherits the live default, and if a spawn still fails, retry once, then reroute to a `general` executor with the full delegation contract pasted inline and log the reroute in `history.md`.

## 2026-10-16 diwali-web-ar — integrate persistence
At INTEGRATE, write every worker report into `reports.md` in the run dir verbatim — anything left only in the message stream is invisible to QC and to a resumed session.

## 2026-10-16 diwali-web-ar — evidence, not memory
Read the artifact before trusting any claim about it: for WebGL a screenshot is the only visual evidence (all-zero canvas `getImageData` is a `preserveDrawingBuffer:false` artifact, not a dead renderer); read the real DOM id / source text before dispatching an interaction or an edit, never a recalled one.

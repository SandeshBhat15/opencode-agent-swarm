---
description: Give the swarm a task in plain English and it runs it end to end - understands, plans, delegates, supervises, verifies, repairs, delivers.
agent: swarm
---
Task: $ARGUMENTS

Take that task and RUN it end to end. Do not reply with a plan and stop.

1. Start the run. This triages the request, builds the task graph, and writes the
   acceptance criteria to disk so delivery can be gated:

   ```sh
   python -m swarmkit run init --request "<task text>" --auto-plan
   ```

   It prints the lane (R1-R4), the tasks, and which may start now. R1 means the
   request is a question or a snippet: answer it directly and stop.

2. Follow `agents/swarm.md` from whatever phase `run init` left you in. Drive
   every stage yourself. Never stop to ask whether to continue - the loop is
   the deliverable, not the plan.

3. Before any consequential step (deploy, publish, push, purchase, destructive
   delete) run `python -m swarmkit approve "<the action>"`. If it refuses, stop
   that step and report it; everything else carries on.

4. Deliver only when the gate is open, with the evidence attached. If something
   is blocked, finish everything else and say plainly what stopped you.

If you are resuming rather than starting, run `run state` first - it reports the
phase, the live workers and every outstanding obligation. Do not guess where an
interrupted run stopped.

If no task text was provided, reply with one line asking for the task.
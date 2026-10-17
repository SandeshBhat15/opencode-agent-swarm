---
description: Continue work a previous session stopped - resumes from the last durable checkpoint, never repeats finished work, never restarts on its own.
agent: swarm
---
$ARGUMENTS

The user asked to continue. Find what they mean, then continue it — never start a
second copy of anything.

1. Ask the engine which task this is. This command starts nothing, so it is safe
   to run before you know the answer:

   ```sh
   python -m swarmkit lifecycle resume-plan --hint "<the user's own words>"
   ```

2. If it reports `ambiguous`, ask ONE question naming the candidates and stop
   there. A resume on a guess re-does work the user did not ask to redo.

3. If it reports `none`, say plainly that nothing is unfinished here, and start a
   new run only if the user wants one.

4. Otherwise resume it and treat the brief it prints as your starting context —
   what is done, what is left, which artifacts already exist:

   ```sh
   python -m swarmkit lifecycle resume --task <task-id> --session <session-id>
   ```

5. VERIFY BEFORE YOU TRUST IT. `INTERRUPTED` means nobody chose to stop that
   work, so re-check what the brief claims instead of believing it.

6. Checkpoint every real step from here on, and include the epoch so a worker
   left over from the previous attempt is fenced out instead of writing into
   the resumed task:

   ```sh
   python -m swarmkit lifecycle checkpoint --task <task-id> --step <step-id> --epoch <epoch> --result "<what it produced>"
   ```

7. If the user asks to stop, stop deliberately:

   ```sh
   python -m swarmkit lifecycle cancel --task <task-id> --reason "<why>"
   ```

   It keeps the completed steps, the journal and every artifact, so the next
   "continue" starts from here rather than from zero.
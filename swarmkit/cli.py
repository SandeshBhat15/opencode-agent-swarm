"""``python -m swarmkit`` - the command surface the agent pages call.

Exit codes
----------
``0`` success · ``1`` refusal (gate closed, budget spent, scope conflict,
illegal transition, lint error) · ``2`` bad usage.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Sequence

from . import (analysis, contracts, evolve, experience, gate, isolator,
               lessons, lifecycle, manager, phases, store, workers)
from .budget import Budget, Limits
from .errors import SwarmError
from . import context, decision, delegation, knowledge, router, telemetry

DEFAULT_LESSONS = Path.home() / ".config" / "opencode" / "swarm-lessons.md"

# ABORTED is a legal target but is not part of phases.PHASES (that tuple is the
# linear pipeline). Every phase-taking parser must offer it.
_PHASE_CHOICES = list(phases.PHASES) + [phases.ABORTED]

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def dated_slug(slug: str) -> str:
    """Prefix a bare slug with today's date.

    ``agents/swarm.md`` documents the run dir as
    ``.opencode/swarm/<YYYY-MM-DD>-<short-slug>/``, which groups runs by day.
    ``store.init_run`` deliberately does not invent a date (a library caller
    controls its own naming), so the convention is applied here at the CLI
    boundary. A slug that already starts with a date is left alone.
    """
    if not slug:
        return slug
    head = slug[:10]
    if _ISO_DATE.match(head):
        return slug
    return f"{store.utcnow()[:10]}-{slug}"


def _emit(payload: dict[str, Any], as_json: bool, human: str = "", code: int = 0) -> int:
    if as_json:
        print(json.dumps(payload, indent=2))
    elif human:
        print(human)
    else:
        print(json.dumps(payload, indent=2))
    return code


def _run_dir(workspace: Path, slug: str | None) -> Path:
    if slug:
        return store.find_run(workspace, slug)
    latest = store.find_latest_run(workspace)
    if latest is None:
        raise SwarmError(
            f"no swarm runs under {store.runs_root(workspace)}; run `swarm run init` first",
            code="NO_SUCH_RUN",
        )
    return latest


def _limits_from(args: argparse.Namespace, run_dir: Path | None = None) -> Limits:
    cfg_path = Path(getattr(args, "config", None) or "swarm.config.json")
    # An explicit --config is relative to the caller; the default name is
    # relative to the workspace being operated on. Resolving the default
    # against the process CWD silently ignored a project's swarm.config.json
    # whenever the caller ran from elsewhere, leaving every cap at its default.
    if not getattr(args, "config", None) and getattr(args, "workspace", None):
        cfg_path = Path(args.workspace) / cfg_path
    if cfg_path.is_file():
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        return Limits.from_dict(raw.get("limits"))
    return Limits()


def _budget(args: argparse.Namespace, run_dir: Path) -> Budget:
    budget = Budget.load(run_dir, _limits_from(args, run_dir))
    budget.check_clock()
    return budget


# -- commands ------------------------------------------------------------


def cmd_approve(args: argparse.Namespace) -> int:
    """Screen one action against the approval boundary.

    The orchestrator's prose has said "ask before anything destructive" for
    several rounds without it ever being checked, which is why this exists as a
    command with an exit code the orchestrator can actually branch on. A
    `--once` approval is deliberately not persisted: a one-off go-ahead should
    not silently become standing permission for later runs.
    """
    decision = manager.needs_approval(args.action)
    payload = decision.to_dict()
    payload["action"] = args.action
    payload["persisted"] = False
    if decision.approved:
        return _emit(payload, args.json,
                     f"allowed: {decision.reason}")
    return _emit(payload, args.json,
                 f"REFUSED [{decision.code}]: {decision.reason}", code=1)


def cmd_run_init(args: argparse.Namespace) -> int:
    """Turn one plain-English request into a real, resumable run.

    Until this existed, "the manager" was a paragraph in `agents/swarm.md`: a
    request never became a run on disk, so nothing could inspect it, resume it,
    or prove it happened. Intaking the request here is what lets every later
    mechanism - phases, gate, budgets, reconciliation - attach to something
    real.

    `--objective` may be repeated; each becomes one planned task.
    """
    workspace = Path(args.workspace).resolve()
    request = " ".join([args.request] if isinstance(args.request, str)
                       else list(args.request or [])).strip()
    # `--goal` is the form agents/swarm.md documents; `--title` is kept as an alias.
    slug = store.slugify(args.slug or args.goal or args.title or request)
    slug = dated_slug(slug)

    # The REQUEST is not an action. "Add a deploy button" is ordinary work that
    # merely mentions deploying; gating on request text would train the user to
    # blanket-approve, which is how a real destructive step gets waved through.
    # Per-step screening happens when the manager decides to act - see
    # `swarm approve`.

    run_dir = store.init_run(workspace, slug)

    lane = manager.triage(request) if request else manager.TRIAGE_FULL
    nodes = manager.plan_tasks(args.objective or [request or slug],
                               retry_limit=args.retries)

    if args.auto_plan and nodes:
        manager.write_plan(run_dir, nodes, goal=request or slug)
        # Declared criteria are what the delivery gate later refuses to open
        # without. A run that never declares any can never be gated.
        if not gate.load_criteria(run_dir):
            for node in nodes:
                if node.criteria:
                    gate.add_criterion(
                        run_dir, gate.Criterion(f"{node.id}-done", node.criteria[0], node.id))

    frontier = [n.id for n in manager.parallel_frontier(nodes)]
    store.append_line(run_dir / store.HISTORY_FILE,
                      f"{store.now_hhmm()} INTAKE lane={lane} tasks={len(nodes)} "
                      f"frontier={len(frontier)} request={(request or slug)[:80]!r}\n")
    store.atomic_write_text(
        run_dir / store.STATUS_FILE,
        f"# {run_dir.name}\n\nrequest: {request or slug}\n"
        f"lane: {lane}\nphase: INTAKE\nnext: run phase UNDERSTAND\n"
        f"tasks: {len(nodes)} (frontier {len(frontier)})\n",
    )
    return _emit(
        {
            "slug": run_dir.name,
            "run_dir": str(run_dir),
            "lane": lane,
            "lifecycle": list(manager.LIFECYCLE),
            "tasks": [n.to_dict() for n in nodes],
            "parallel_frontier": frontier,
            "planned": bool(args.auto_plan),
            "next": "run phase UNDERSTAND",
        },
        args.json,
        f"initialised run {run_dir.name} (lane {lane}, {len(nodes)} task(s), "
        f"frontier {len(frontier)}) at {run_dir}\nnext: run phase UNDERSTAND",
    )


def cmd_run_state(args: argparse.Namespace) -> int:
    """Report what a resumed manager still owes: liveness, leases, evidence."""
    workspace = Path(args.workspace).resolve()
    run_dir = store.find_run(workspace, args.slug)
    state = manager.lifecycle_state(run_dir)
    lines = [f"phase: {state['phase']}", f"live workers: {', '.join(state['live_workers']) or 'none'}"]
    if state["obligations"]:
        lines.append("outstanding:")
        lines.extend(f"  - {o}" for o in state["obligations"])
    else:
        lines.append("outstanding: nothing")
    return _emit(state, args.json, "\n".join(lines))


def cmd_run_triage(args: argparse.Namespace) -> int:
    request = " ".join(args.request).strip()
    lane = manager.triage(request)
    human = {
        manager.TRIAGE_ANSWER: "R1 answer directly - no fan-out",
        manager.TRIAGE_RESEARCH: "R2 research fan-out - swarm/researcher only",
        manager.TRIAGE_SINGLE: "R3 single-file job - skip ceremony",
        manager.TRIAGE_FULL: "R4 full pipeline - plan, spawn, QC, deliver",
    }[lane]
    return _emit({"lane": lane}, args.json, human)


def cmd_run_list(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    rows = []
    for run_dir in store.list_runs(workspace):
        state = store.RunState.load(run_dir)
        rows.append(
            {
                "slug": state.slug,
                "phase": state.phase,
                "fix_cycles": state.fix_cycles,
                "finished": state.is_finished(),
                "updated_at": state.updated_at,
                "run_dir": str(run_dir),
            }
        )
    human = "\n".join(
        f"{r['slug']:<34} {r['phase']:<10} cycles={r['fix_cycles']} "
        f"{'finished' if r['finished'] else 'unfinished'}" for r in rows
        or ["(no runs)"]
    )
    return _emit({"runs": rows}, args.json, human)


def cmd_run_show(args: argparse.Namespace) -> int:
    run_dir = _run_dir(Path(args.workspace).resolve(), args.slug)
    state = store.RunState.load(run_dir)
    journal = store.pending_journal(run_dir)
    # agents/swarm.md promises run show prints the phase, the next action, the open
    # criteria, and which still lack evidence. That is the documented resume path,
    # so it has to actually carry them.
    try:
        result = gate.evaluate(run_dir)
    except SwarmError:
        result = gate.GateResult(results=[])
    open_ids = [r.criterion.id for r in result.results]
    unproven = [r.criterion.id for r in result.results if not r.passed]
    payload = {
        "slug": state.slug,
        "phase": state.phase,
        "fix_cycles": state.fix_cycles,
        "abort_reason": state.abort_reason,
        "run_dir": str(run_dir),
        "interrupted": journal,
        "workstreams": state.workstreams,
        "next_action": spec_for(state.phase),
        "criteria": open_ids,
        "criteria_needing_evidence": unproven,
        "gate_open": result.open,
    }
    lines = [
        f"run     {state.slug}",
        f"phase   {state.phase}",
        f"cycles  {state.fix_cycles}",
        f"dir     {run_dir}",
        f"next    {payload['next_action']}",
        f"gate    {'OPEN' if result.open else 'CLOSED'}",
        f"criteria{'' if not open_ids else ' ' + ', '.join(open_ids)}",
    ]
    if unproven:
        lines.append(f"unproven {', '.join(unproven)}")
    if not open_ids:
        lines.append("unproven (no criteria declared yet - add them in PLAN)")
    if journal:
        lines.append(f"!! INTERRUPTED mid-{journal.get('op')} on {journal.get('target')}")
        lines.append(f"   run: swarm run recover --slug {state.slug}")
    return _emit(payload, args.json, "\n".join(lines))


def cmd_run_phase(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    run_dir = _run_dir(workspace, args.slug)
    # agents/swarm.md documents `run phase <PHASE>`; `--to` remains supported.
    positional = getattr(args, "target", None)
    flag = getattr(args, "to", None)
    if positional and flag and positional.upper() != flag.upper():
        raise SwarmError(
            f"positional {positional} and --to {flag} disagree; pass exactly one",
            code="BAD_USAGE",
        )
    raw = positional or flag
    if not raw:
        raise SwarmError(
            "a target phase is required: `run phase <PHASE>` or `run phase --to <PHASE>`",
            code="BAD_USAGE",
        )
    target = raw.upper()
    store.require_clean(run_dir)
    state = store.RunState.load(run_dir)
    current = state.phase
    budget = _budget(args, run_dir)

    with store.mutation(run_dir, "phase", f"{current}->{target}"):
        if target == phases.ABORTED:
            if not args.reason:
                raise SwarmError("aborting requires --reason", code="MISSING_REASON")
            state.abort_reason = args.reason
        else:
            phases.assert_transition(current, target)
            if current == phases.QC and target == phases.FIX:
                state.fix_cycles += 1
                budget.check_fix_cycles(state.fix_cycles)
            if target in phases.DELIVERY_GATED:
                gate.assert_open(run_dir)
            # RECHECK: a run may never skip straight to delivery.
            phases.assert_transition(current, target)
        state.phase = target
        state.save(run_dir)
        store.append_line(
            run_dir / store.HISTORY_FILE,
            f"{store.now_hhmm()} {current} -> {target}"
            + (f" ({args.reason})" if args.reason else ""),
        )
        _rewrite_status(run_dir, state)
    captured: dict[str, Any] = {}
    if state.phase in (phases.DELIVERED, phases.ABORTED):
        captured = _capture_terminal(run_dir, workspace, state, args)
    human = f"phase {current} -> {state.phase}"
    if captured:
        human += f"  (experience {captured.get('tier', 'recorded')}"
        if not captured.get("ok"):
            human += " - CAPTURE FAILED, reported not raised"
        human += ")"
    return _emit({"phase": state.phase, "from": current, "fix_cycles": state.fix_cycles,
                  "experience": captured}, args.json, human)


def _rewrite_status(run_dir: Path, state: store.RunState) -> None:
    store.atomic_write_text(
        run_dir / store.STATUS_FILE,
        f"# Swarm run: {state.slug}\n\n"
        f"- phase: {state.phase}\n"
        f"- created: {state.created_at}\n"
        f"- updated: {state.updated_at}\n"
        f"- fix_cycles: {state.fix_cycles}\n"
        + (f"- abort_reason: {state.abort_reason}\n" if state.abort_reason else "")
        + f"\n## next action\n\n{spec_for(state.phase)}\n\n## live processes\n\n(none)\n",
    )


_NEXT = {
    phases.INTAKE: "INTAKE: name the finished artifact and its evidence.",
    phases.PLAN: "PLAN: write plan.md, declare workstreams + disjoint scopes, add criteria.",
    phases.MESH: "MESH: spawn workstreams in parallel inside the budget.",
    phases.INTEGRATE: "INTEGRATE: persist reports verbatim, merge via worktree patches.",
    phases.QC: "QC: run swarm/qc against the acceptance criteria and their evidence.",
    phases.FIX: "FIX: send fix orders to the owning workstream, then re-gate.",
    phases.RETRO: "RETRO: spawn swarm/improve with the run summary.",
    phases.DELIVERED: "DELIVERED: artifact + evidence handed over.",
    phases.ABORTED: "ABORTED: escalated. See history.md for the evidence.",
}


def spec_for(phase: str) -> str:
    return _NEXT.get(phase, f"unknown phase {phase}")


def cmd_run_recover(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    run_dir = _run_dir(workspace, args.slug)
    # `--note` is optional: agents/swarm.md documents a bare `run recover`.
    # Without one, derive a note from the journal so history.md stays truthful.
    note = args.note
    if not note:
        entry = store.pending_journal(run_dir) or {}
        note = (
            f"manual recovery: cleared stalled {entry.get('op', 'mutation')} "
            f"on {entry.get('target', 'unknown target')}"
        )
    result = store.recover(run_dir, note)
    return _emit(result, args.json, f"recovered {run_dir.name}: cleared {result['cleared'] or 'nothing'}")


def cmd_run_phases(args: argparse.Namespace) -> int:
    return _emit({"phases": list(phases.PHASES), "table": phases.describe()}, args.json,
                 phases.describe())


def cmd_criterion_add(args: argparse.Namespace) -> int:
    run_dir = _run_dir(Path(args.workspace).resolve(), args.slug)
    with store.mutation(run_dir, "criterion.add", args.id):
        crit = gate.add_criterion(
            run_dir, gate.Criterion(id=args.id, text=args.text, workstream=args.workstream)
        )
    return _emit(crit.to_dict(), args.json, f"criterion {crit.id} added")


def cmd_criterion_list(args: argparse.Namespace) -> int:
    run_dir = _run_dir(Path(args.workspace).resolve(), args.slug)
    rows = [c.to_dict() for c in gate.load_criteria(run_dir)]
    human = "\n".join(f"{r['id']:<8} {r['text']}" for r in rows or ["(none)"])
    return _emit({"criteria": rows}, args.json, human)


def cmd_evidence_add(args: argparse.Namespace) -> int:
    run_dir = _run_dir(Path(args.workspace).resolve(), args.slug)
    ev = gate.Evidence(
        criterion=args.criterion,
        status=args.status,
        command=args.command or "",
        result=args.result or "",
        source=args.source or "",
    )
    if not ev.command and not ev.source:
        raise SwarmError(
            "evidence needs --command or --source; an unverifiable claim is not evidence",
            code="UNSUPPORTED_EVIDENCE",
        )
    if not ev.result:
        raise SwarmError(
            "evidence needs --result (the actual output, not a summary of it)",
            code="EMPTY_EVIDENCE",
        )
    with store.mutation(run_dir, "evidence.add", args.criterion):
        gate.add_evidence(run_dir, ev)
    return _emit(ev.__dict__, args.json, f"evidence recorded for {args.criterion} ({args.status})")


def cmd_gate_check(args: argparse.Namespace) -> int:
    run_dir = _run_dir(Path(args.workspace).resolve(), args.slug)
    result = gate.evaluate(run_dir)
    payload = result.to_dict()
    return _emit(payload, args.json, result.render())


def cmd_gate_open(args: argparse.Namespace) -> int:
    run_dir = _run_dir(Path(args.workspace).resolve(), args.slug)
    result = gate.assert_open(run_dir)
    return _emit(result.to_dict(), args.json, "gate OPEN")


def cmd_budget_show(args: argparse.Namespace) -> int:
    run_dir = _run_dir(Path(args.workspace).resolve(), args.slug)
    budget = _budget(args, run_dir)
    snap = budget.snapshot()
    lines = [f"{k}: {v}" for k, v in snap.items() if k != "limits"]
    lines.append("limits: " + json.dumps(snap["limits"]))
    if snap["estimated_cost_usd"] is None:
        lines.append("estimated_cost_usd: null (no pricing configured - not estimated)")
    return _emit(snap, args.json, "\n".join(lines))


def cmd_budget_spawn(args: argparse.Namespace) -> int:
    run_dir = _run_dir(Path(args.workspace).resolve(), args.slug)
    budget = _budget(args, run_dir)
    with store.mutation(run_dir, "budget.spawn", args.name):
        budget.record_spawn(name=args.name, role=args.role)
        budget.lease(args.name, role=args.role, run_dir=run_dir)
        budget.persist(run_dir)
    return _emit(budget.snapshot(), args.json,
                 f"spawned {args.name} (inflight {budget.inflight}/{budget.limits.max_concurrent_agents})")


def cmd_budget_release(args: argparse.Namespace) -> int:
    run_dir = _run_dir(Path(args.workspace).resolve(), args.slug)
    budget = _budget(args, run_dir)
    with store.mutation(run_dir, "budget.release", args.name):
        budget.release(args.name, run_dir=run_dir)
        budget.persist(run_dir)
    return _emit(budget.snapshot(), args.json, f"released {args.name}")


def cmd_budget_retry(args: argparse.Namespace) -> int:
    run_dir = _run_dir(Path(args.workspace).resolve(), args.slug)
    budget = _budget(args, run_dir)
    with store.mutation(run_dir, "budget.retry", args.name):
        count = budget.record_retry(args.name)
        budget.persist(run_dir)
    return _emit({"worker": args.name, "retry": count}, args.json,
                 f"retry {count}/{budget.limits.max_retries_per_worker} for {args.name}")


def cmd_budget_tokens(args: argparse.Namespace) -> int:
    run_dir = _run_dir(Path(args.workspace).resolve(), args.slug)
    budget = _budget(args, run_dir)
    with store.mutation(run_dir, "budget.tokens", "tokens"):
        budget.record_tokens(args.tokens_in, args.tokens_out)
        budget.persist(run_dir)
    cost = budget.estimated_cost_usd()
    return _emit(budget.snapshot(), args.json,
                 f"tokens in={budget.tokens_in} out={budget.tokens_out} cost={cost}")


# -- workers --------------------------------------------------------------
# `budget spawn` only ever counted. These are the commands that make a worker
# observable: a dead worker is distinguishable from a slow one, and a retry is
# charged to the budget inside the same critical section that starts it.


def cmd_workers_spawn(args: argparse.Namespace) -> int:
    run_dir = _run_dir(Path(args.workspace).resolve(), args.slug)
    limits = _limits_from(args, run_dir)
    with store.mutation(run_dir, "workers.spawn", args.name):
        result = workers.spawn(
            run_dir,
            args.name,
            role=args.role,
            timeout_s=args.timeout,
            limits=limits,
            pid=args.pid,
        )
    payload = result.to_dict()
    payload["budget"] = Budget.load(run_dir, limits).snapshot()
    return _emit(payload, args.json,
                 f"spawned {args.name} attempt {result.worker.attempt} "
                 f"epoch {result.worker.epoch[:8]} "
                 f"(retry delay {result.retry_delay}s)")


def cmd_workers_beat(args: argparse.Namespace) -> int:
    run_dir = _run_dir(Path(args.workspace).resolve(), args.slug)
    with store.mutation(run_dir, "workers.beat", args.name):
        registry = workers.Registry(run_dir)
        worker = registry.heartbeat(args.name, args.epoch)
    return _emit({"worker": worker.to_dict()}, args.json,
                 f"beat {args.name} attempt {worker.attempt} "
                 f"({worker.beats} total)")


def cmd_workers_finish(args: argparse.Namespace) -> int:
    run_dir = _run_dir(Path(args.workspace).resolve(), args.slug)
    with store.mutation(run_dir, "workers.finish", args.name):
        registry = workers.Registry(run_dir)
        worker = registry.finish(args.name, args.epoch, args.state, args.note)
        budget = Budget.load(run_dir, _limits_from(args, run_dir))
        budget.release(args.name, epoch=args.epoch)
        budget.persist(run_dir)
    return _emit({"worker": worker.to_dict()}, args.json,
                 f"{args.name} finished {args.state}")


def cmd_workers_progress(args: argparse.Namespace) -> int:
    run_dir = _run_dir(Path(args.workspace).resolve(), args.slug)
    # Registry.progress takes COUNTERS_LOCK with a bounded wait itself. It must
    # NOT use store.mutation: that lock is fail-fast by design (a phase change
    # must not queue behind another), and progress arrives from every live
    # worker at once, so a fail-fast lock would drop most updates on the floor.
    worker = workers.Registry(run_dir).progress(
        args.name, args.epoch, units=args.units
    )
    return _emit(
        {"name": worker.name, "attempt": worker.attempt,
         "progress": worker.progress, "beats": worker.beats},
        args.json,
        f"progress {worker.name} attempt {worker.attempt} "
        f"({worker.progress} total, {worker.beats} beats)",
    )


def cmd_workers_status(args: argparse.Namespace) -> int:
    run_dir = _run_dir(Path(args.workspace).resolve(), args.slug)
    status = workers.Registry(run_dir).status(stale_seconds=args.stale)
    lines = [
        f"{w['name']:<16} attempt {w['attempt']:<3} {w['state']:<9} {w['liveness']}"
        for w in status["workers"]
    ]
    human = (
        "\n".join(lines)
        or "no workers registered"
    ) + (
        f"\nalive={status['alive']} slow={status['slow']} "
        f"wedged={status['wedged']} stale={status['stale']} "
        f"timeout={status['timed_out']}"
    )
    return _emit(status, args.json, human)


def cmd_workers_reconcile(args: argparse.Namespace) -> int:
    """Idempotently make registry, budget and evidence agree.

    A worker can die at four points, and each leaves a different mess:

    1. after ``spawn`` leased it, before it ever ran  -> lease held, no work
    2. while it was running                          -> lease held, stale beat
    3. after it wrote its result, before ``finish``   -> work done, slot held
    4. after ``finish`` but before the run resumed     -> nothing to do

    Running this twice must be a no-op the second time, so it may be called
    freely on resume instead of a human deciding which case they are in.
    """
    run_dir = _run_dir(Path(args.workspace).resolve(), args.slug)
    report = workers.reconcile(run_dir, limits=_limits_from(args, run_dir),
                               stale_seconds=args.stale)
    return _emit(report, args.json,
                 f"reconciled: released {len(report['released'])}, "
                 f"already consistent {len(report['consistent'])}")


def cmd_workers_reclaim(args: argparse.Namespace) -> int:
    """Free slots held by dead workers. Never touches a `slow` one."""
    run_dir = _run_dir(Path(args.workspace).resolve(), args.slug)
    report = workers.reclaim(
        run_dir,
        limits=_limits_from(args, run_dir),
        stale_seconds=args.stale,
        names=args.name,
        include_wedged=args.include_wedged,
    )
    reclaimed = ", ".join(
        f"{r['name']}({r['liveness']},attempt {r['attempt']})" for r in report["reclaimed"]
    )
    skipped = ", ".join(f"{s['name']}({s['liveness']})" for s in report["skipped"])
    human = f"reclaimed: {reclaimed or 'none'}"
    if skipped:
        human += f"\nleft alone: {skipped}"
    return _emit(report, args.json, human)


def cmd_scope_plan(args: argparse.Namespace) -> int:
    scopes: dict[str, list[str]] = {}
    for spec in args.ws:
        if "=" not in spec:
            raise SwarmError(f"--ws expects name=path[,path], got {spec!r}", code="BAD_WS")
        name, _, paths = spec.partition("=")
        scopes[name.strip()] = isolator.split_scope(paths)
    result = isolator.plan_scopes(scopes, strict=args.strict)
    human = "no scope conflicts"
    if result["possible_conflicts"]:
        human = "possible overlaps:\n" + "\n".join("  " + c for c in result["possible_conflicts"])
    return _emit(result, args.json, human)


ISO_MANIFEST = "swarm-worktrees.json"
ISO_TREES_DEFAULT = ".swarm-trees"


def _iso_manifest(tree_root: Path) -> dict[str, dict[str, str]]:
    try:
        raw = json.loads(Path(tree_root).joinpath(ISO_MANIFEST).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _iso_write_manifest(tree_root: Path, mapping: dict[str, dict[str, str]]) -> None:
    store.atomic_write_json(Path(tree_root) / ISO_MANIFEST, mapping)


def _iso_trees(args: argparse.Namespace) -> Path:
    """Resolve the worktree root against the repo, not the process CWD.

    The documented default is a relative `.swarm-trees`. Resolving it against the
    CWD scatters worktrees into whichever directory swarmkit happened to be invoked
    from, so `iso create` wrote a manifest somewhere `iso patch` could not find it.
    """
    raw = Path(args.trees)
    if raw.is_absolute():
        return raw
    return Path(args.repo).resolve() / raw


def _iso_resolve(trees: Path, name: str | None) -> Path:
    """Map a workstream name to its worktree path.

    agents/swarm.md tells the orchestrator to think in workstreams (`iso patch
    --ws build`), not in paths. Without this, every documented iso command after
    `create` would be unusable and the orchestrator would fall back to hand-running
    git against paths it invented.
    """
    if not name:
        raise SwarmError(
            "a workstream name is required: `iso ... --ws <name>` (or pass --wt/--patch directly)",
            code="BAD_USAGE",
        )
    mapping = _iso_manifest(trees)
    entry = mapping.get(name)
    if not entry or not entry.get("path"):
        known = ", ".join(sorted(mapping)) or "(none)"
        raise SwarmError(
            f"no worktree recorded for workstream {name!r}; known: {known}. "
            f"Run `iso create --ws {name} --trees {trees}` first.",
            code="NO_SUCH_WORKTREE",
        )
    return Path(entry["path"])


def cmd_iso_create(args: argparse.Namespace) -> int:
    repo = isolator.repo_root(Path(args.repo))
    # --ws MUST append. Without it argparse keeps the last occurrence and silently
    # drops every workstream but one, with exit 0 and no warning.
    names = [n.strip() for n in (args.ws if isinstance(args.ws, list) else [args.ws]) if n.strip()]
    if not names:
        raise SwarmError("iso create needs at least one --ws", code="BAD_USAGE")
    tree_root = _iso_trees(args)
    trees = isolator.create_worktrees(repo, tree_root, names)
    mapping = _iso_manifest(tree_root)
    for w in trees:
        mapping[w.name] = {"path": str(w.path), "branch": w.branch}
    _iso_write_manifest(tree_root, mapping)
    rows = [w.to_dict() for w in trees]
    human = "\n".join(f"{w.name:<16} {w.branch:<28} {w.path}" for w in trees)
    return _emit({"worktrees": rows, "trees": str(tree_root), "manifest": mapping}, args.json, human)


def _iso_confined_wt(args: argparse.Namespace, wt: str | None, ws: str | None) -> Path:
    """Resolve a worktree path and refuse anything outside the tree root.

    `--ws` is validated against the manifest by `_iso_resolve`, but `--wt`
    bypassed it entirely: `iso remove --wt C:\\...\\precious-wt` removed a
    registered worktree and destroyed uncommitted work, because
    `remove_worktree` always passes `git worktree remove --force`. Both CLI
    entry points that accept a raw `--wt` now go through here.
    """
    trees = _iso_trees(args)
    wt_path = Path(wt) if wt else _iso_resolve(trees, ws)
    resolved = wt_path.resolve()
    anchor = Path(trees).resolve()
    if resolved != anchor and anchor not in resolved.parents:
        raise SwarmError(
            f"worktree {resolved} is outside the worktree root {anchor}. "
            "Pass --ws with a name from the manifest, or point --trees at the "
            "root that owns it.",
            code="WORKTREE_OUTSIDE_ROOT",
        )
    return wt_path


def cmd_iso_patch(args: argparse.Namespace) -> int:
    repo = isolator.repo_root(Path(args.repo))
    wt_path = _iso_confined_wt(args, args.wt, args.ws)
    patch = isolator.worktree_patch(repo, wt_path, base=args.base)
    out = Path(args.out) if args.out else _iso_trees(args) / f"{wt_path.name}.patch"
    store.atomic_write_text(out, patch)
    return _emit({"patch": str(out), "bytes": len(patch), "worktree": str(wt_path)}, args.json,
                 f"wrote {out} ({len(patch)} bytes)")


def cmd_iso_integrate(args: argparse.Namespace) -> int:
    """Resolve `--ws` through the manifest and keep the patch inside it.

    `iso patch` and `iso remove` already routed `--ws` through `_iso_resolve`
    (manifest-only). This one built the path itself, so `--ws` could be any
    readable file on disk and it was handed straight to `git apply`. Route it
    the same way, then assert the resolved patch is inside the tree root.
    """
    repo = isolator.repo_root(Path(args.repo))
    trees = _iso_trees(args)
    if args.patch:
        patch = Path(args.patch)
    else:
        _iso_resolve(trees, args.ws)  # manifest-only: validates the name
        patch = trees / f"{args.ws}.patch"
    resolved = Path(patch).resolve()
    anchor = Path(trees).resolve()
    if anchor not in resolved.parents and resolved != anchor:
        raise SwarmError(
            f"patch {resolved} is outside the worktree root {anchor}",
            code="PATCH_OUTSIDE_TREES",
        )
    result = isolator.integrate_patch(repo, patch, three_way=not args.no_three_way)
    human = "applied" if result["applied"] else f"NOT applied: {result['reason']}\n{result['detail']}"
    return _emit(result, args.json, human)


def cmd_iso_remove(args: argparse.Namespace) -> int:
    repo = isolator.repo_root(Path(args.repo))
    wt_path = _iso_confined_wt(args, args.wt, args.ws)
    result = isolator.remove_worktree(repo, wt_path)
    return _emit(result, args.json, "removed" if result["removed"] else result["detail"])


def cmd_lesson_add(args: argparse.Namespace) -> int:
    path = Path(args.ledger)
    result = lessons.add_lesson(
        path,
        args.rule,
        run=args.run or "",
        failure=args.failure or "",
        evidence=args.evidence or "",
        cap=args.cap,
        allow_contradiction=args.allow_contradiction,
    )
    if not result["added"]:
        human = f"lesson NOT added ({result['reason']}): {result.get('detail') or result.get('existing', {}).get('rule', '')}"
        return _emit(result, args.json, human)
    return _emit(result, args.json, f"lesson added (UNVERIFIED until promoted): {args.rule}")


def cmd_lesson_promote(args: argparse.Namespace) -> int:
    result = lessons.promote(Path(args.ledger), args.rule, tests_passed=args.tests_passed,
                             evidence=args.evidence or "")
    return _emit(result, args.json, f"promoted: {args.rule}")


def cmd_lesson_audit(args: argparse.Namespace) -> int:
    result = lessons.audit(Path(args.ledger))
    human = (
        f"{result['total']} lesson(s), {result['lines']}/{result['cap']} lines\n"
        f"  ungrounded:     {len(result['ungrounded'])}\n"
        f"  duplicates:     {len(result['duplicates'])}\n"
        f"  contradictions: {len(result['contradictions'])}\n"
        f"  unverified:     {len(result['unverified'])}"
    )
    return _emit(result, args.json, human)


def _parse_plan(raw: str | None) -> list[dict[str, Any]]:
    """Parse a ``--plan`` argument into steps, refusing bad input cleanly.

    A raw ``JSONDecodeError`` here would escape :func:`main`, which only catches
    ``SwarmError``, and the caller would get a traceback instead of the
    ``{"error": {"code": ...}}`` envelope every other failure returns. The
    plugin parses the host's JSON too, so this path is reachable from a real
    integration, not just from a mistyped flag.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SwarmError(
            f"--plan is not valid JSON: {exc}", code="BAD_PLAN"
        ) from exc
    if not isinstance(parsed, list):
        raise SwarmError(
            f"--plan must be a JSON list of steps, got {type(parsed).__name__}",
            code="BAD_PLAN",
        )
    return [step for step in parsed if isinstance(step, dict)]


def cmd_lifecycle_session_close(args: argparse.Namespace) -> int:
    """Stop the work one session owns when that session ends.

    The plugin calls this from the session-close event. It is a separate command
    from ``lifecycle shutdown`` because closing one chat is not closing the
    application, and a user running two sessions at once must not have one
    closed conversation take the other one's work with it.
    """
    result = lifecycle.session_close(
        Path(args.workspace), args.session, reason=args.reason or "session closed"
    )
    human = (
        f"session {args.session}: cancelled {result['cancelled']} task(s); "
        f"{len(result['untouched'])} belonging to other sessions left running"
    )
    return _emit(result, args.json, human)


def cmd_lifecycle_shutdown(args: argparse.Namespace) -> int:
    """Stop every task this application owns, on real process shutdown.

    This is the cleanup function the OpenCode plugin returns from ``setup()``.
    It fires when the application itself goes away, which is the only moment at
    which every session in the process is ending together. Nothing is deleted:
    each record keeps its completed work and artifacts for a later resume.
    """
    result = lifecycle.shutdown(
        Path(args.workspace), reason=args.reason or "application shutdown"
    )
    human = f"shutdown: cancelled {result['cancelled']} live task(s); preserved {len(result['preserved'])}"
    return _emit(result, args.json, human)


def cmd_lifecycle_status(args: argparse.Namespace) -> int:
    """Real progress for one task, or for whatever a session is running.

    With ``--session`` there may be more than one candidate, and picking one
    would be a guess. An ambiguous session therefore answers with ``task: null``
    plus the candidates: a caller that renders progress has nothing truthful to
    show, and silence beats a confident number about the wrong task.
    """
    workspace = Path(args.workspace)
    if args.task:
        task = lifecycle.Task.load(workspace, args.task)
        return _emit({"task": task.progress()}, args.json, (
            f"task {task.record.task_id}: {task.record.state} "
            f"{task.record.completed_steps}/{task.record.total_steps} steps"
        ))
    if not args.session:
        raise SwarmError("status needs --task or --session", code="NO_TASK")
    live = [t for t in lifecycle.Task.for_session(workspace, args.session)
            if t.record.state in lifecycle.LIVE_STATES]
    if len(live) == 1:
        return _emit({"task": live[0].progress()}, args.json, (
            f"task {live[0].record.task_id}: {live[0].record.state} "
            f"{live[0].record.completed_steps}/{live[0].record.total_steps} steps"
        ))
    if not live:
        # Stopped or never started: still the truth worth reporting.
        known = lifecycle.Task.for_session(workspace, args.session)
        result = {"task": known[0].progress() if known else None,
                  "tasks": [t.progress() for t in known]}
        return _emit(result, args.json, "no live task in this session")
    return _emit(
        {"task": None, "ambiguous": True,
         "tasks": [t.progress() for t in live],
         "session_id": args.session},
        args.json,
        f"{len(live)} live tasks in this session; refusing to guess",
    )


def cmd_lifecycle_list(args: argparse.Namespace) -> int:
    result = {
        "tasks": [
            {
                "task_id": t.record.task_id,
                "session_id": t.record.session_id,
                "state": t.record.state,
                "completed_steps": t.record.completed_steps,
                "total_steps": t.record.total_steps,
                "goal": t.record.goal,
            }
            for t in lifecycle.Task.all(Path(args.workspace))
        ]
    }
    return _emit(result, args.json, f"{len(result['tasks'])} task(s) on record")


def cmd_lifecycle_start(args: argparse.Namespace) -> int:
    """Bind a new task to the session that asked for it.

    Every task must name an owning session. That is what makes the rest of the
    lifecycle possible: a task with no owner could not be stopped when its
    conversation closed, which is the failure this whole module exists to stop.
    """
    if not args.session:
        raise SwarmError(
            "--session is required: a task must name the session that owns it",
            code="NO_SESSION",
        )
    plan = _parse_plan(args.plan)
    task = lifecycle.Task.create(
        Path(args.workspace),
        task_id=args.task or "",
        session_id=args.session,
        goal=args.goal or "",
        project=args.project or str(Path(args.workspace)),
        parent_task_id=args.parent,
        plan=plan,
    )
    result = {"task": task.progress()}
    return _emit(result, args.json, f"task {task.record.task_id} bound to session {args.session}")


def cmd_lifecycle_checkpoint(args: argparse.Namespace) -> int:
    """Persist a checkpoint. Called on every step, so a crash loses at most one."""
    task = lifecycle.Task.load(Path(args.workspace), args.task)
    if args.step:
        task.complete_step(args.step, result=args.result or None, epoch=args.epoch or "")
    if args.phase:
        task.record.phase = args.phase
    if args.artifact:
        task.record.artifacts.append(args.artifact)
    if args.retry:
        key, _, count = args.retry.partition("=")
        task.record.retries[key] = int(count or task.record.retries.get(key, 0) + 1)
    entry = task.checkpoint(event=args.event or "checkpoint")
    result = {"task": task.progress(), "checkpoint": entry["seq"], "at": entry["at"]}
    return _emit(result, args.json, f"checkpoint {entry['seq']} at {entry['at']}")


def cmd_lifecycle_note(args: argparse.Namespace) -> int:
    """Count one real event from the host application.

    Counters only. An event name we do not model is still recorded rather than
    dropped, because an unrecognised event is evidence about what the host is
    doing, and discarding it would leave a gap we could not explain later.

    Addressed by ``--session`` as well as ``--task`` because the host reports
    events per session, not per task, and making the caller look up the task id
    first would be a step that has to be right every time.
    """
    if args.task:
        task = lifecycle.Task.load(Path(args.workspace), args.task)
    elif args.session:
        live = [
            t for t in lifecycle.Task.for_session(Path(args.workspace), args.session)
            if t.record.state in lifecycle.LIVE_STATES
        ]
        if len(live) != 1:
            # Zero or several live tasks for one session: picking one would be a
            # guess, and a guessed task gets its counters written.
            return _emit(
                {"noted": False, "session_id": args.session, "event": args.event,
                 "live_tasks": [t.record.task_id for t in live]},
                args.json,
                f"no single live task for session {args.session}; "
                f"{len(live)} candidate(s) - pass --task",
            )
        task = live[0]
    else:
        raise SwarmError("note needs --task or --session", code="NO_TASK")
    observed = task.note(args.event)
    result = {"task_id": task.record.task_id, "observed": observed, "noted": True}
    return _emit(result, args.json, f"{args.event}: {observed.get(args.event, 0)}")


def cmd_lifecycle_cancel(args: argparse.Namespace) -> int:
    result = lifecycle.Task.load(Path(args.workspace), args.task).cancel(
        reason=args.reason or "cancelled"
    )
    return _emit(result, args.json, f"task {args.task}: {result['state']}")


def cmd_lifecycle_resume_plan(args: argparse.Namespace) -> int:
    """Report the unfinished task a "continue" probably means. Starts nothing.

    The separation is the point. Finding the task is safe to do automatically;
    restarting it is not, so this command only ever produces a brief and the
    caller has to invoke ``lifecycle resume`` separately.
    """
    result = lifecycle.resume_plan(
        Path(args.workspace),
        session_id=args.session or "",
        project=args.project or "",
        hint=args.hint or "",
    )
    if result["status"] == "ambiguous":
        human = "ambiguous; ask which one: " + ", ".join(
            f"{c['task_id']} ({c['progress']})" for c in result["candidates"]
        )
    elif result["status"] == "none":
        human = "no unfinished task for this project"
    else:
        human = f"task {result['task_id']} at {result['progress']}"
    return _emit(result, args.json, human)


def cmd_lifecycle_resume(args: argparse.Namespace) -> int:
    """Continue a task. The only path that leaves PAUSED or INTERRUPTED.

    Reuses the completed work rather than repeating it, rotates the epoch so any
    worker left over from the previous attempt is fenced out, and hands back a
    brief built from the durable record.
    """
    task = lifecycle.Task.load(Path(args.workspace), args.task)
    task.resume(session_id=args.session or task.record.session_id, hint=args.hint or "")
    result = {"task": task.progress(), "brief": task.brief()}
    human = f"resumed {args.task}: {task.record.completed_steps}/{task.record.total_steps} steps already done"
    return _emit(result, args.json, human)


def cmd_lifecycle_recover(args: argparse.Namespace) -> int:
    """Mark crashed work INTERRUPTED. Never resumes it.

    Called on plugin startup. A task whose owner process is gone gets recorded
    as interrupted so that a later resume knows to re-verify it rather than
    trusting the last run -- and nothing is restarted here, because the
    application reopening is not the user asking to continue.
    """
    result = lifecycle.recover(Path(args.workspace), reason=args.reason or "startup")
    human = f"recovered {result['recovered']} interrupted task(s); {len(result['skipped'])} skipped"
    return _emit(result, args.json, human)


def cmd_lifecycle_prune(args: argparse.Namespace) -> int:
    """Drop expired records. Never artifacts, never live work, never automatic."""
    result = lifecycle.prune(Path(args.workspace), retention_days=args.retention_days)
    human = f"pruned {len(result['removed'])} expired record(s); kept {len(result['kept'])} live"
    return _emit(result, args.json, human)


def cmd_lint(args: argparse.Namespace) -> int:
    pages = contracts.validate_repo(Path(args.root))
    summary = contracts.summarise(pages)
    return _emit(summary, args.json, contracts.render(pages)) or (1 if summary["errors"] else 0)


def cmd_selftest(args: argparse.Namespace) -> int:
    """Cheap end-to-end smoke of the invariants, no network, no model calls."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        run_dir = store.init_run(ws, "selftest")
        state = store.RunState.load(run_dir)
        assert state.phase == phases.INTAKE
        gate.add_criterion(run_dir, gate.Criterion(id="C1", text="tests pass"))
        try:
            gate.assert_open(run_dir)
        except SwarmError:
            pass
        else:
            raise SwarmError("gate opened with no evidence", code="GATE_LEAK")
        gate.add_evidence(run_dir, gate.Evidence("C1", "pass", "pytest", "12 passed"))
        assert gate.assert_open(run_dir).open

        # -- session-bound lifecycle. No subprocess and no sleep: everything
        # below is either a refusal or on-disk state, both decided at once.
        task = lifecycle.Task.create(
            ws, task_id="selftest", session_id="ses_selftest",
            goal="a task that outlives the turn", plan=[{"id": "s1"}, {"id": "s2"}],
        )
        task.complete_step("s1", result="done")
        assert task.record.completed_steps == 1, "the completed step is not on record"
        try:
            task.set_state(lifecycle.COMPLETED)
        except SwarmError:
            pass  # half-done work is never reported as done
        else:
            raise SwarmError(
                "a half-executed task was marked COMPLETED", code="COMPLETION_LEAK"
            )
        try:
            task.complete_step("s2", epoch="epoch-from-a-superseded-attempt")
        except SwarmError:
            pass  # a superseded attempt cannot write its result
        else:
            raise SwarmError(
                "a superseded epoch recorded progress", code="FENCE_LEAK"
            )
        artifact = ws / "selftest-artifact.txt"
        artifact.write_text("produced", encoding="utf-8")
        task.record.artifacts.append(str(artifact))
        report = task.cancel(reason="selftest stop")
        assert report["state"] == lifecycle.PAUSED
        assert task.record.completed_steps == 1, "cancel must keep the completed step"
        assert artifact.exists(), "cancel must not delete a finished artifact"
        assert task.file.exists(), "cancel must not delete the saved state"
        recovery = lifecycle.recover(ws, reason="selftest")
        assert recovery["recovered"] == 0, "recovery must not touch stopped work"
        assert lifecycle.Task.load(ws, "selftest").record.state == lifecycle.PAUSED, (
            "recover marks interrupted work, it never resumes it"
        )
    return _emit({"selftest": "ok"}, args.json, "selftest ok")


# --- routing, context, delegation, knowledge, telemetry ---------------------
#
# Five engines that were implemented, tested and then left unreachable: no
# subcommand, no re-export. That is the exact failure this package keeps
# guarding against - a mechanism that works and nobody can call is not a
# mechanism. Each command is a thin translation from argv to the module's own
# public function so the behaviour lives in one place and the CLI only frames
# it. Nothing below invents a rule the module did not state.

_CHECKPOINT_MAX_TOKENS = 2000
_USAGE_FIELDS = frozenset(telemetry.Usage.__dataclass_fields__)


def _registry_root(workspace: Path) -> Path:
    """Where a run's model catalog, outcomes and router budget live.

    Workspace-scoped like ``_memory_root``/``_evolve_root``: one project's
    recorded outcomes must not steer another project's routing.
    """
    return workspace / router.DEFAULT_REGISTRY


def _knowledge_root(workspace: Path) -> Path:
    return workspace / knowledge.DEFAULT_ROOT


def _read_text(path: str | Path) -> str:
    target = Path(path)
    if not target.is_file():
        raise SwarmError(f"no such file: {target}", code="BAD_USAGE")
    return target.read_text(encoding="utf-8")


def _read_bytes(path: str | Path) -> bytes:
    target = Path(path)
    if not target.is_file():
        raise SwarmError(f"no such file: {target}", code="BAD_USAGE")
    return target.read_bytes()


def _flat(values: Any) -> list[str]:
    """Flatten ``--flag a b``/``--flag a --flag b`` into a single list."""
    out: list[str] = []
    for item in values or ():
        if isinstance(item, (list, tuple)):
            out.extend(str(value) for value in item)
        else:
            out.append(str(item))
    return out


# -- model: routing over a recorded catalog ----------------------------------


def cmd_model_import(args: argparse.Namespace) -> int:
    registry = _registry_root(Path(args.workspace).resolve())
    result = router.import_catalog(args.file, args.source, registry=registry)
    return _emit(result, args.json,
                 f"imported {result['count']} model(s) from {result['source']}")


def cmd_model_discover(args: argparse.Namespace) -> int:
    """Discover models from an OpenCode /api/model payload."""
    import json as _json
    import tempfile
    if args.file:
        raw = Path(args.file).read_text(encoding="utf-8-sig")
    else:
        raw = sys.stdin.read()
    data = _json.loads(raw)
    transformed = router.transform_opencode_catalog(data)
    tmp = Path(tempfile.mktemp(suffix=".json"))
    tmp.write_text(_json.dumps(transformed), encoding="utf-8")
    try:
        registry = _registry_root(Path(args.workspace).resolve())
        result = router.import_catalog(tmp, "opencode-api", registry=registry)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return _emit(result, args.json,
                  f"discovered {result['count']} model(s) from OpenCode")


def cmd_model_route(args: argparse.Namespace) -> int:
    registry = _registry_root(Path(args.workspace).resolve())
    decision = router.route(
        args.category,
        registry=registry,
        pin=args.pin,
        default=args.allow,
        budget_remaining=args.budget_remaining,
        accept_unknown_price=args.accept_unknown_price,
    )
    payload = {
        "task": args.task,
        "objective": args.objective,
        "role": args.role,
        "allow": args.allow,
        "request": {
            "category": args.category,
            "pin": args.pin,
            "budget_remaining": args.budget_remaining,
            "accept_unknown_price": bool(args.accept_unknown_price),
        },
        **decision.to_dict(),
    }
    return _emit(payload, args.json, f"{decision.status}: {decision.reason}")


def cmd_model_next(args: argparse.Namespace) -> int:
    registry = _registry_root(Path(args.workspace).resolve())
    cause = args.cause
    if cause is None:
        cause = router.diagnose(args.error) if args.error else "unknown"
    result = router.next(
        cause,
        tried=args.tried or (),
        registry=registry,
        escalations_used=args.model_failures,
        pin=args.pin,
        retries=args.retries,
    )
    payload = {
        "task": args.task,
        "error": args.error,
        "cause": cause,
        "tried": list(args.tried or ()),
        "request": {
            "retries": args.retries,
            "model_failures": args.model_failures,
            "pin": args.pin,
        },
        **result,
    }
    return _emit(payload, args.json, f"{result['status']}: {result['reason']}")


def cmd_model_diagnose(args: argparse.Namespace) -> int:
    cause = router.diagnose(args.error, args.retries)
    payload = {"error": args.error, "cause": cause, "retries": args.retries}
    return _emit(payload, args.json, f"cause: {cause}")


def cmd_model_record(args: argparse.Namespace) -> int:
    registry = _registry_root(Path(args.workspace).resolve())
    item = router.record(
        args.provider, args.model, args.category, args.outcome,
        retries=args.retries, qc_rejected=args.qc_rejected, cause=args.cause,
        registry=registry,
    )
    return _emit(item, args.json,
                 f"recorded {item['key']} {item['outcome']} for {item['category']}"
                 f" (evidence={item['evidence']})")


def cmd_model_history(args: argparse.Namespace) -> int:
    registry = _registry_root(Path(args.workspace).resolve())
    rows = router.history(args.category, registry=registry)
    lines = [
        f"{row['key']}  {row['verdict']:<8} verified={row['verified']}"
        f"/{row['attempts']} (need {router.MIN_COMPARABLE_ATTEMPTS})"
        for row in rows
    ] or ["(no recorded outcomes)"]
    return _emit({"category": args.category, "history": rows,
                  "min_comparable_attempts": router.MIN_COMPARABLE_ATTEMPTS},
                 args.json, "\n".join(lines))


def cmd_model_budget(args: argparse.Namespace) -> int:
    registry = _registry_root(Path(args.workspace).resolve())
    state = router.budget(max_cost=args.max_cost, max_tokens=args.max_tokens,
                          registry=registry)
    lines = [
        f"max_cost: {state['max_cost']}",
        f"spent_cost: {state['spent_cost']}",
        f"remaining_cost: {state['remaining_cost']}",
        f"max_tokens: {state['max_tokens']}",
        f"spent_tokens: {state['spent_tokens']}",
        f"remaining_tokens: {state['remaining_tokens']}",
    ]
    return _emit(state, args.json, "\n".join(lines))


# -- context: the minimal packet, the checkpoint, the ceiling ----------------


def cmd_context_packet(args: argparse.Namespace) -> int:
    instructions: list[str] = []
    if args.instructions:
        instructions = [line.strip() for line in _read_text(args.instructions).splitlines()
                        if line.strip()]
    packet = context.build_packet(
        args.objective,
        task_id=args.task,
        criteria=_flat(args.criteria),
        dependencies=_flat(args.dep),
        instructions=instructions,
        files=_flat(args.file),
    )
    payload = packet.to_dict()
    payload["sections"] = context.sections_from_packet(packet)
    payload["estimated_tokens"] = packet.estimated_tokens()
    return _emit(payload, args.json, packet.render())


def cmd_context_summarise(args: argparse.Namespace) -> int:
    try:
        sections = json.loads(_read_text(args.file))
    except json.JSONDecodeError as exc:
        raise SwarmError(
            f"{args.file} is not a JSON object of sections: {exc}", code="BAD_USAGE"
        ) from exc
    checkpoint = context.summarise(sections, args.max_tokens)
    return _emit(checkpoint.to_dict(), args.json, checkpoint.render())


def cmd_context_fits(args: argparse.Namespace) -> int:
    result = context.fits(args.limit, _read_text(args.file),
                          label=Path(args.file).name)
    payload = {
        "tokens": result.tokens,
        "limit": result.limit,
        "reserved": result.reserved,
        "label": result.label,
        "headroom": result.headroom(),
        "ok": result.ok,
        "method": context.TOKEN_ESTIMATE_METHOD,
    }
    return _emit(payload, args.json, result.render())


# -- delegate: run it here, or hand it off; repair or stop -------------------


def cmd_delegate_decide(args: argparse.Namespace) -> int:
    workstreams = 1
    if args.files and args.files > 1:
        workstreams = int(args.files)
    if args.independent:
        workstreams = max(workstreams, 2)
    signals = delegation.DelegationSignals(
        specialist_capability=bool(args.specialist),
        independent_workstreams=workstreams,
        risk_reduction=bool(args.risk and args.risk != "low"),
        lane=args.lane or "",
    )
    decision = delegation.decide(signals, depth=args.depth)
    payload = {
        "objective": args.objective,
        "specialist": args.specialist,
        "signals": signals.to_dict(),
        "decision": decision.to_dict(),
    }
    verdict = "DELEGATE" if decision.delegate else "DIRECT"
    return _emit(payload, args.json,
                 f"{verdict} [{decision.code}]: {decision.reason}")


def cmd_delegate_retry(args: argparse.Namespace) -> int:
    decision = delegation.retry_decision(args.attempt, args.limit, args.cause or "")
    verdict = "RETRY" if decision.retry else "STOP"
    return _emit(decision.to_dict(), args.json,
                 f"{verdict} [{decision.code}]: {decision.reason}")


# -- knowledge: the deduplicating doc cache ----------------------------------


def cmd_knowledge_cache(args: argparse.Namespace) -> int:
    store = knowledge.Knowledge(_knowledge_root(Path(args.workspace).resolve()))
    content = _read_text(args.content_file) if args.content_file else ""
    ttl = None if args.ttl_hours is None else float(args.ttl_hours) * 3600.0
    result = store.cache_doc(args.url, content, ttl=ttl)
    payload = {name: value for name, value in result.items() if name != "doc"}
    payload["doc"] = result["doc"].to_dict() if result.get("doc") else None
    return _emit(payload, args.json, f"{result['reason']}: {result['url']}")


def cmd_knowledge_lookup(args: argparse.Namespace) -> int:
    store = knowledge.Knowledge(_knowledge_root(Path(args.workspace).resolve()))
    result = store.lookup(args.url)
    payload = dict(result)
    doc = payload.get("doc")
    payload["doc"] = doc.to_dict() if doc is not None else None
    if result["hit"]:
        human = (f"hit ({result['matched_on']}, score {result['score']}): "
                 f"{result['url']} stale={result['stale']}")
    else:
        human = f"no cached answer for {args.url}"
    return _emit(payload, args.json, human)


def cmd_knowledge_freshness(args: argparse.Namespace) -> int:
    store = knowledge.Knowledge(_knowledge_root(Path(args.workspace).resolve()))
    if args.url:
        # ``is_stale`` refuses a URL that was never cached, so a miss is an
        # error the caller can branch on rather than a silent "fresh".
        stale = store.is_stale(args.url)
        payload = {
            "url": knowledge.normalise_url(args.url),
            "stale": stale,
            "freshness": store.lookup(args.url).get("freshness"),
        }
        human = f"{payload['url']}: {'STALE' if stale else 'fresh'}"
    else:
        payload = store.freshness()
        human = (f"{payload['fresh']} fresh, {payload['stale']} stale "
                 f"of {payload['total']} doc(s)")
    return _emit(payload, args.json, human)


# -- telemetry: what a run actually consumed ---------------------------------


def _sessions(payload: Any) -> list[Any]:
    """``telemetry.load_sessions`` with its ``ValueError`` surfaced as a SwarmError.

    ``TelemetryError`` subclasses ``ValueError``, which the CLI's error handler
    does not catch, so a malformed payload would escape as a traceback instead of
    a refusal a caller can branch on.
    """
    try:
        return telemetry.load_sessions(payload)
    except telemetry.TelemetryError as exc:
        raise SwarmError(str(exc), code=exc.code) from exc


def _measurement(path: str | Path) -> telemetry.Usage:
    """Read one measurement from a usage JSON, a baseline row, a session list or a run log.

    The formats a caller already has are accepted directly, so recording a
    baseline does not force the measurement to be reshaped first.
    """
    text = _read_text(path)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        inner = payload.get("usage")
        if isinstance(inner, dict):
            return telemetry.Usage(**{k: v for k, v in inner.items() if k in _USAGE_FIELDS})
        if any(name in payload for name in telemetry.MEASURED_FIELDS):
            return telemetry.Usage(**{k: v for k, v in payload.items() if k in _USAGE_FIELDS})
    if isinstance(payload, list):
        rows = _sessions(payload)
        return telemetry.combine((telemetry.from_session_record(row) for row in rows),
                                 label=str(path))
    return telemetry.parse_run_log(text).usage


def cmd_telemetry_sessions(args: argparse.Namespace) -> int:
    raw = _read_text(args.file) if args.file else sys.stdin.read()
    sessions = _sessions(raw)
    priced = telemetry.pricing_available(sessions)
    payload: dict[str, Any] = {"sessions": len(sessions), "pricing_available": priced}
    if args.rollup:
        rolled = telemetry.rollup(sessions)
        payload["rollup"] = {sid: usage.as_dict() for sid, usage in rolled.items()}
        lines = [
            f"{sid}  {usage.label or '(untitled)'}  tokens={usage.total_tokens}"
            f"  delegations={usage.delegations}"
            for sid, usage in rolled.items()
        ]
        human = "\n".join(lines) or "(no sessions)"
    else:
        usages = [telemetry.from_session_record(row) for row in sessions]
        payload["usage"] = [usage.as_dict() for usage in usages]
        human = "\n".join(telemetry.render(usage) for usage in usages) or "(no sessions)"
    if not priced:
        payload["note"] = "cost not measurable on this host"
    return _emit(payload, args.json, human)


def cmd_telemetry_run_log(args: argparse.Namespace) -> int:
    parsed = telemetry.parse_run_log(_read_bytes(args.file))
    payload = {
        "file": args.file,
        "malformed_lines": parsed.malformed_lines,
        "step_count": len(parsed.steps),
        "usage": parsed.usage.as_dict(),
        "steps": parsed.steps,
    }
    return _emit(payload, args.json, telemetry.render(parsed.usage))


def cmd_telemetry_compare(args: argparse.Namespace) -> int:
    before = _measurement(args.before)
    after = _measurement(args.after)
    comparison = telemetry.compare(before, after, category=args.category,
                                   tolerance=args.tolerance)
    payload = {
        "category": comparison.category,
        "verdict": comparison.verdict,
        "reason": comparison.reason,
        "metrics": comparison.metrics,
        "regressions": comparison.regressions,
        "improvements": comparison.improvements,
        "not_measured": comparison.not_measured,
    }
    return _emit(payload, args.json, comparison.render())


def cmd_telemetry_baseline(args: argparse.Namespace) -> int:
    target = Path(args.file)
    if args.task_log:
        usage = _measurement(args.task_log)
        written = telemetry.write_baseline(target, args.category, usage)
        payload = {
            "recorded": True,
            "file": str(written),
            "category": args.category,
            "usage": usage.as_dict(),
        }
        return _emit(payload, args.json,
                     f"baseline recorded for {args.category} in {written}")
    rows = telemetry.load_baselines(target, args.category)
    payload = {
        "recorded": False,
        "file": str(target),
        "category": args.category,
        "baselines": rows,
        "count": len(rows),
    }
    return _emit(payload, args.json,
                 f"{len(rows)} baseline(s) recorded for {args.category}")


# -- parser --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swarm", description="swarmkit run control")
    p.add_argument("--json", action="store_true", help="emit JSON")
    p.add_argument("--workspace", default=".", help="workspace root")
    p.add_argument("--config", default=None, help="path to swarm.config.json")
    sub = p.add_subparsers(dest="group", required=True)

    run = sub.add_parser("run", help="run lifecycle").add_subparsers(dest="cmd", required=True)
    i = run.add_parser("init"); i.add_argument("--slug"); i.add_argument("--title")
    i.add_argument("--goal", help="artifact-first goal sentence (the documented form)")
    i.add_argument("--request", nargs="+",
                   help="the user's plain-English task; this is what triage reads")
    i.add_argument("--objective", action="append",
                   help="one planned task; repeat for each")
    i.add_argument("--retries", type=int, default=2)
    i.add_argument("--auto-plan", action="store_true",
                   help="write plan.json + workstreams.md + criteria at intake")
    i.set_defaults(func=cmd_run_init)
    l = run.add_parser("list"); l.set_defaults(func=cmd_run_list)
    s = run.add_parser("show"); s.add_argument("--slug"); s.set_defaults(func=cmd_run_show)
    ph = run.add_parser("phase")
    ph.add_argument("target", nargs="?", choices=_PHASE_CHOICES,
                    help="target phase, e.g. `run phase DELIVERED`")
    ph.add_argument("--to", choices=_PHASE_CHOICES, help="alias for the positional form")
    ph.add_argument("--slug"); ph.add_argument("--reason")
    ph.add_argument("--request", nargs="+",
                    help="the original request, captured into memory on delivery")
    ph.add_argument("--corrected", action="append",
                    help="a correction the user made; repeatable. Recorded as a "
                         "lesson candidate only if it contradicts a real past run.")
    phacc = ph.add_mutually_exclusive_group()
    phacc.add_argument("--accepted", dest="user_accepted", action="store_true",
                       help="the user accepted this delivery")
    phacc.add_argument("--rejected", dest="user_accepted", action="store_false",
                       help="the user rejected this delivery")
    ph.set_defaults(func=cmd_run_phase)
    rc = run.add_parser("recover"); rc.add_argument("--slug"); rc.add_argument("--note")
    rc.set_defaults(func=cmd_run_recover)
    pt = run.add_parser("phases"); pt.set_defaults(func=cmd_run_phases)
    st = run.add_parser("state", help="what a resumed manager must still do")
    st.add_argument("--slug"); st.set_defaults(func=cmd_run_state)
    tr = run.add_parser("triage", help="which lane a request belongs in")
    tr.add_argument("request", nargs="+"); tr.set_defaults(func=cmd_run_triage)

    crit = sub.add_parser("criterion").add_subparsers(dest="cmd", required=True)
    ca = crit.add_parser("add"); ca.add_argument("--id", required=True); ca.add_argument("--text", required=True)
    ca.add_argument("--workstream"); ca.add_argument("--slug"); ca.set_defaults(func=cmd_criterion_add)
    cl = crit.add_parser("list"); cl.add_argument("--slug"); cl.set_defaults(func=cmd_criterion_list)

    ev = sub.add_parser("evidence").add_subparsers(dest="cmd", required=True)
    ea = ev.add_parser("add"); ea.add_argument("--criterion", required=True)
    ea.add_argument("--status", default="pass", choices=list(gate.VALID_STATUSES))
    ea.add_argument("--command"); ea.add_argument("--result"); ea.add_argument("--source")
    ea.add_argument("--slug"); ea.set_defaults(func=cmd_evidence_add)

    g = sub.add_parser("gate").add_subparsers(dest="cmd", required=True)
    gc = g.add_parser("check"); gc.add_argument("--slug"); gc.set_defaults(func=cmd_gate_check)
    go = g.add_parser("open"); go.add_argument("--slug"); go.set_defaults(func=cmd_gate_open)

    b = sub.add_parser("budget").add_subparsers(dest="cmd", required=True)
    bs = b.add_parser("show"); bs.add_argument("--slug"); bs.set_defaults(func=cmd_budget_show)
    # `--worker` is the documented spelling; `--name` stays as an alias.
    bp = b.add_parser("spawn"); bp.add_argument("--name", "--worker", dest="name", required=True)
    bp.add_argument("--role"); bp.add_argument("--slug"); bp.set_defaults(func=cmd_budget_spawn)
    br = b.add_parser("release"); br.add_argument("--name", "--worker", dest="name", required=True)
    br.add_argument("--slug"); br.set_defaults(func=cmd_budget_release)
    by = b.add_parser("retry"); by.add_argument("--name", "--worker", dest="name", required=True)
    by.add_argument("--slug"); by.set_defaults(func=cmd_budget_retry)
    bt = b.add_parser("tokens"); bt.add_argument("--in", dest="tokens_in", type=int, default=0)
    bt.add_argument("--out", dest="tokens_out", type=int, default=0)
    bt.add_argument("--slug"); bt.set_defaults(func=cmd_budget_tokens)

    w = sub.add_parser("workers").add_subparsers(dest="cmd", required=True)
    wst = w.add_parser("status"); wst.add_argument("--slug")
    wst.add_argument("--stale", type=float, default=workers.DEFAULT_STALE_SECONDS)
    wst.set_defaults(func=cmd_workers_status)
    wsp = w.add_parser("spawn")
    wsp.add_argument("--name", "--worker", dest="name", required=True)
    wsp.add_argument("--role"); wsp.add_argument("--slug")
    wsp.add_argument("--timeout", type=float, default=workers.DEFAULT_TIMEOUT_S)
    wsp.add_argument("--pid", type=int); wsp.set_defaults(func=cmd_workers_spawn)
    wbt = w.add_parser("beat"); wbt.add_argument("--name", "--worker", dest="name", required=True)
    wbt.add_argument("--epoch", required=True); wbt.add_argument("--slug")
    wbt.set_defaults(func=cmd_workers_beat)
    wpg = w.add_parser("progress")
    wpg.add_argument("--name", "--worker", dest="name", required=True)
    wpg.add_argument("--epoch", required=True)
    wpg.add_argument("--units", type=int, default=1)
    wpg.add_argument("--slug"); wpg.set_defaults(func=cmd_workers_progress)
    wfi = w.add_parser("finish"); wfi.add_argument("--name", "--worker", dest="name", required=True)
    wfi.add_argument("--epoch", required=True)
    wfi.add_argument("--state", choices=["ok", "failed"], required=True)
    wfi.add_argument("--note"); wfi.add_argument("--slug")
    wfi.set_defaults(func=cmd_workers_finish)
    wrc = w.add_parser("reclaim"); wrc.add_argument("--name", "--worker", dest="name", action="append")
    wrc.add_argument("--stale", type=float, default=workers.DEFAULT_STALE_SECONDS)
    wrc.add_argument("--include-wedged", action="store_true",
                     help="also reclaim workers that are alive but not advancing "
                          "(off by default: a wedged worker may be a slow but "
                          "legitimate long task)")
    wrc.add_argument("--slug"); wrc.set_defaults(func=cmd_workers_reclaim)

    wrn = w.add_parser("reconcile")
    wrn.add_argument("--stale", type=float, default=workers.DEFAULT_STALE_SECONDS)
    wrn.add_argument("--slug"); wrn.set_defaults(func=cmd_workers_reconcile)

    sc = sub.add_parser("scope").add_subparsers(dest="cmd", required=True)
    sp = sc.add_parser("plan"); sp.add_argument("--ws", action="append", required=True)
    sp.add_argument("--strict", action="store_true"); sp.set_defaults(func=cmd_scope_plan)

    iso = sub.add_parser("iso").add_subparsers(dest="cmd", required=True)
    ic = iso.add_parser("create"); ic.add_argument("--repo", default=".")
    ic.add_argument("--trees", default=ISO_TREES_DEFAULT)
    ic.add_argument("--ws", action="append", required=True); ic.set_defaults(func=cmd_iso_create)
    ip = iso.add_parser("patch"); ip.add_argument("--repo", default=".")
    ip.add_argument("--wt"); ip.add_argument("--out"); ip.add_argument("--base")
    ip.add_argument("--ws"); ip.add_argument("--trees", default=ISO_TREES_DEFAULT)
    ip.set_defaults(func=cmd_iso_patch)
    ii = iso.add_parser("integrate"); ii.add_argument("--repo", default=".")
    ii.add_argument("--patch"); ii.add_argument("--no-three-way", action="store_true")
    ii.add_argument("--ws"); ii.add_argument("--trees", default=ISO_TREES_DEFAULT)
    ii.set_defaults(func=cmd_iso_integrate)
    ir = iso.add_parser("remove"); ir.add_argument("--repo", default=".")
    ir.add_argument("--wt"); ir.add_argument("--ws")
    ir.add_argument("--trees", default=ISO_TREES_DEFAULT); ir.set_defaults(func=cmd_iso_remove)

    ls = sub.add_parser("lesson").add_subparsers(dest="cmd", required=True)
    la = ls.add_parser("add"); la.add_argument("--rule", required=True); la.add_argument("--failure", required=True)
    la.add_argument("--evidence"); la.add_argument("--run"); la.add_argument("--ledger", default=str(DEFAULT_LESSONS))
    la.add_argument("--cap", type=int, default=lessons.DEFAULT_CAP_LINES)
    la.add_argument("--allow-contradiction", action="store_true"); la.set_defaults(func=cmd_lesson_add)
    lp = ls.add_parser("promote"); lp.add_argument("--rule", required=True); lp.add_argument("--evidence", required=True)
    lp.add_argument("--tests-passed", action="store_true"); lp.add_argument("--ledger", default=str(DEFAULT_LESSONS))
    lp.set_defaults(func=cmd_lesson_promote)
    lau = ls.add_parser("audit"); lau.add_argument("--ledger", default=str(DEFAULT_LESSONS))
    lau.set_defaults(func=cmd_lesson_audit)

    li = sub.add_parser("lint"); li.add_argument("--root", default="."); li.set_defaults(func=cmd_lint)

    ap = sub.add_parser("approve", help="screen one action against the approval boundary")
    ap.add_argument("action", help="the action the manager wants to take")
    ap.add_argument("--once", action="store_true",
                    help="explicitly a one-off; nothing is persisted either way")
    ap.set_defaults(func=cmd_approve)

    # -- the improvement loop, reachable from the agent pages ------------
    xp = sub.add_parser("experience", help="structured record of finished runs")
    xps = xp.add_subparsers(dest="cmd", required=True)
    xr = xps.add_parser("record", help="capture a finished run into memory")
    xr.add_argument("--slug"); xr.add_argument("--request", nargs="+")
    xr.add_argument("--corrected", action="append",
                    help="a correction the user made; repeatable")
    acc = xr.add_mutually_exclusive_group()
    acc.add_argument("--accepted", dest="user_accepted", action="store_true")
    acc.add_argument("--rejected", dest="user_accepted", action="store_false")
    xr.set_defaults(func=cmd_experience_record)
    xl = xps.add_parser("list", help="list recorded experiences")
    xl.add_argument("--category"); xl.add_argument("--outcome"); xl.add_argument("--cause")
    xl.add_argument("--limit", type=int, default=20); xl.set_defaults(func=cmd_experience_list)
    xc = xps.add_parser("challenge", help="record evidence that contradicts a past conclusion")
    xc.add_argument("--run", required=True); xc.add_argument("--reason", required=True)
    xc.set_defaults(func=cmd_experience_challenge)

    ln = sub.add_parser("learn", help="what memory holds, and its shape")
    lns = ln.add_subparsers(dest="cmd", required=True)
    lr = lns.add_parser("recall", help="grounded material for a category, most confident first")
    lr.add_argument("--category"); lr.add_argument("--cause")
    lr.add_argument("--limit", type=int, default=5); lr.set_defaults(func=cmd_learn_recall)
    lsch = lns.add_parser("schema", help="the experience record schema")
    lsch.set_defaults(func=cmd_learn_schema)

    an = sub.add_parser("analyze", help="measure past runs and raise evidenced proposals")
    an.add_argument("--category"); an.add_argument("--check-only", action="store_true",
                                                    help="only answer whether now is a good time")
    an.set_defaults(func=cmd_analyze)

    ve = sub.add_parser("evolve", help="bounded experiments against a fixed baseline")
    ves = ve.add_subparsers(dest="cmd", required=True)
    vc = ves.add_parser("candidate", help="register an analyzer proposal as a candidate")
    vc.add_argument("--proposal", required=True)
    vc.add_argument("--category"); vc.add_argument("--kind", default="policy")
    vc.add_argument("--max-generations", type=int); vc.add_argument("--max-candidates", type=int)
    vc.set_defaults(func=cmd_evolve_candidate)
    vv = ves.add_parser("evaluate", help="safety gate, then benchmark, then authorization")
    vv.add_argument("--proposal", required=True); vv.add_argument("--version", required=True)
    vv.add_argument("--candidate", action="append", nargs="+", help="metric=value; repeatable, or several after one flag")
    vv.add_argument("--baseline", action="append", nargs="+", help="metric=value; repeatable, or several after one flag")
    vv.add_argument("--safety-passed", action="store_true")
    vv.add_argument("--authorized", action="store_true")
    vv.add_argument("--min-gain", type=float); vv.set_defaults(func=cmd_evolve_evaluate)
    vb = ves.add_parser("rollback", help="withdraw a promoted version")
    vb.add_argument("--version", required=True); vb.add_argument("--reason", default="")
    vb.set_defaults(func=cmd_evolve_rollback)
    vm = ves.add_parser("monitor", help="call the bet: compare post-promotion metrics")
    vm.add_argument("--version", required=True)
    vm.add_argument("--observed", action="append", nargs="+", help="metric=value; repeatable, or several after one flag")
    vm.set_defaults(func=cmd_evolve_monitor)
    vp = ves.add_parser("policy", help="show or set the limits on the improvement loop")
    vp.add_argument("--min-new-experiences", type=int); vp.add_argument("--cooldown-s", type=float)
    vp.add_argument("--max-candidates", type=int); vp.add_argument("--max-generations", type=int)
    vp.add_argument("--min-gain", type=float)
    vp.add_argument("--auto-promote", dest="auto_promote", action="store_true", default=None)
    vp.add_argument("--no-auto-promote", dest="auto_promote", action="store_false")
    vp.add_argument("--auto-promote-target", action="append")
    vp.set_defaults(func=cmd_evolve_policy)
    vh = ves.add_parser("history", help="audit trail, active pointer, budget counters")
    vh.add_argument("--limit", type=int, default=20); vh.set_defaults(func=cmd_evolve_history)
    vsc = ves.add_parser("scope-check", help="refuse a self-evolution edit of a protected path")
    vsc.add_argument("--path", action="append"); vsc.set_defaults(func=cmd_evolve_scope_check)

    lc = sub.add_parser("lifecycle", help="session-bound task ownership and shutdown")
    lcs = lc.add_subparsers(dest="cmd", required=True)
    lsc = lcs.add_parser("session-close", help="stop the work one session owns")
    lsc.add_argument("--session", required=True); lsc.add_argument("--reason")
    lsc.set_defaults(func=cmd_lifecycle_session_close)
    lsd = lcs.add_parser("shutdown", help="stop every task this application owns")
    lsd.add_argument("--reason"); lsd.set_defaults(func=cmd_lifecycle_shutdown)
    lst = lcs.add_parser("status", help="real progress for one task, or for a session's task")
    # The host plugin only knows the session it is watching, never the task id,
    # so `--session` has to be a first-class way in. Resolved inside the command
    # so that an ambiguous session reports the ambiguity instead of picking one.
    lst.add_argument("--task"); lst.add_argument("--session")
    lst.set_defaults(func=cmd_lifecycle_status)
    lli = lcs.add_parser("list", help="every task on record")
    lli.set_defaults(func=cmd_lifecycle_list)
    lsa = lcs.add_parser("start", help="bind a new task to its owning session")
    lsa.add_argument("--session", required=True); lsa.add_argument("--task")
    lsa.add_argument("--goal"); lsa.add_argument("--project"); lsa.add_argument("--parent")
    lsa.add_argument("--plan", help="JSON list of steps")
    lsa.set_defaults(func=cmd_lifecycle_start)
    lcp = lcs.add_parser("checkpoint", help="persist progress durably")
    lcp.add_argument("--task", required=True); lcp.add_argument("--step")
    lcp.add_argument("--phase"); lcp.add_argument("--artifact")
    lcp.add_argument("--retry", help="key=count"); lcp.add_argument("--epoch")
    lcp.add_argument("--result"); lcp.add_argument("--event")
    lcp.set_defaults(func=cmd_lifecycle_checkpoint)
    lno = lcs.add_parser("note", help="count one real host event")
    lno.add_argument("--task"); lno.add_argument("--session")
    lno.add_argument("--event", required=True)
    lno.set_defaults(func=cmd_lifecycle_note)
    lcn = lcs.add_parser("cancel", help="stop one task now; idempotent")
    lcn.add_argument("--task", required=True); lcn.add_argument("--reason")
    lcn.set_defaults(func=cmd_lifecycle_cancel)
    lrp = lcs.add_parser("resume-plan", help="find the task a 'continue' means; starts nothing")
    lrp.add_argument("--session"); lrp.add_argument("--project"); lrp.add_argument("--hint")
    lrp.set_defaults(func=cmd_lifecycle_resume_plan)
    lre = lcs.add_parser("resume", help="continue a task after the user asks")
    lre.add_argument("--task", required=True); lre.add_argument("--session")
    lre.add_argument("--hint"); lre.set_defaults(func=cmd_lifecycle_resume)
    lrc = lcs.add_parser("recover", help="mark crashed work INTERRUPTED; never resumes it")
    lrc.add_argument("--reason"); lrc.set_defaults(func=cmd_lifecycle_recover)
    lpr = lcs.add_parser("prune", help="drop expired records; artifacts are never touched")
    lpr.add_argument("--retention-days", type=int, default=lifecycle.RETENTION_DAYS)
    lpr.set_defaults(func=cmd_lifecycle_prune)

    st = sub.add_parser("selftest"); st.set_defaults(func=cmd_selftest)

    # -- routing, context, delegation, knowledge, telemetry --------------
    md = sub.add_parser("model", help="route work to a model from a recorded catalog")
    mds = md.add_subparsers(dest="cmd", required=True)
    mi = mds.add_parser("import", help="import a host model catalog")
    mi.add_argument("--file", required=True); mi.add_argument("--source", default="host")
    mi.set_defaults(func=cmd_model_import)
    mdis = mds.add_parser("discover", help="discover models from an OpenCode /api/model payload")
    mdis.add_argument("--file"); mdis.add_argument("--workspace", default=".")
    mdis.set_defaults(func=cmd_model_discover)
    mr = mds.add_parser("route", help="choose a model for a category, or refuse with a reason")
    mr.add_argument("--task", required=True); mr.add_argument("--objective", required=True)
    mr.add_argument("--role", required=True)
    mr.add_argument("--category", default="general", choices=list(router.CATEGORIES))
    mr.add_argument("--pin"); mr.add_argument("--allow")
    mr.add_argument("--budget-remaining", dest="budget_remaining", type=float)
    mr.add_argument("--accept-unknown-price", dest="accept_unknown_price", action="store_true")
    mr.set_defaults(func=cmd_model_route)
    mn = mds.add_parser("next", help="the next move after a failure, escalating only when warranted")
    mn.add_argument("--task", required=True); mn.add_argument("--error")
    mn.add_argument("--cause")
    # `--attempt` is the documented spelling of "attempts so far"; `--retries`
    # stays as the module-level name for the same count.
    mn.add_argument("--retries", "--attempt", dest="retries", type=int, default=0)
    mn.add_argument("--model-failures", dest="model_failures", type=int, default=0)
    mn.add_argument("--tried", action="append"); mn.add_argument("--pin")
    mn.set_defaults(func=cmd_model_next)
    mdg = mds.add_parser("diagnose", help="attribute an error to one cause")
    mdg.add_argument("--error", required=True); mdg.add_argument("--retries", type=int, default=0)
    mdg.set_defaults(func=cmd_model_diagnose)
    mrec = mds.add_parser("record", help="append one routing outcome")
    mrec.add_argument("--provider", required=True); mrec.add_argument("--model", required=True)
    mrec.add_argument("--category", required=True, choices=list(router.CATEGORIES))
    mrec.add_argument("--outcome", required=True, choices=list(router.OUTCOMES))
    mrec.add_argument("--cause"); mrec.add_argument("--retries", type=int, default=0)
    mrec.add_argument("--qc-rejected", dest="qc_rejected", action="store_true")
    mrec.set_defaults(func=cmd_model_record)
    mh = mds.add_parser("history", help="what the router believes, with the evidence")
    mh.add_argument("--category", required=True, choices=list(router.CATEGORIES))
    mh.set_defaults(func=cmd_model_history)
    mb = mds.add_parser("budget", help="spent vs the router's cost and token ceilings")
    mb.add_argument("--max-cost", dest="max_cost", type=float)
    mb.add_argument("--max-tokens", dest="max_tokens", type=int)
    mb.set_defaults(func=cmd_model_budget)

    cx = sub.add_parser("context", help="build minimal worker context, summarise, size-check")
    cxs = cx.add_subparsers(dest="cmd", required=True)
    cxp = cxs.add_parser("packet", help="the minimal context packet for one worker")
    cxp.add_argument("--task", required=True); cxp.add_argument("--objective", required=True)
    cxp.add_argument("--criteria", action="append", nargs="+", required=True)
    cxp.add_argument("--dep", action="append"); cxp.add_argument("--file", action="append")
    cxp.add_argument("--instructions")
    cxp.set_defaults(func=cmd_context_packet)
    cxsx = cxs.add_parser("summarise", help="compress completed phases into a checkpoint")
    cxsx.add_argument("--file", required=True)
    cxsx.add_argument("--max-tokens", dest="max_tokens", type=int, default=_CHECKPOINT_MAX_TOKENS)
    cxsx.set_defaults(func=cmd_context_summarise)
    cxf = cxs.add_parser("fits", help="check a payload against a context ceiling")
    cxf.add_argument("--file", required=True); cxf.add_argument("--limit", type=int, required=True)
    cxf.set_defaults(func=cmd_context_fits)

    dg = sub.add_parser("delegate", help="delegate-or-not, and repair-or-stop, with reasons")
    dgs = dg.add_subparsers(dest="cmd", required=True)
    dd = dgs.add_parser("decide", help="run it here, or hand it off")
    dd.add_argument("--objective", required=True)
    dd.add_argument("--lane", choices=list(manager.TRIAGE_LANES))
    dd.add_argument("--files", type=int); dd.add_argument("--independent", action="store_true")
    dd.add_argument("--specialist"); dd.add_argument("--risk", choices=["low", "med", "high"])
    dd.add_argument("--depth", type=int, default=0)
    dd.set_defaults(func=cmd_delegate_decide)
    dr = dgs.add_parser("retry", help="a targeted repair, or a stop")
    dr.add_argument("--attempt", type=int, required=True); dr.add_argument("--limit", type=int)
    dr.add_argument("--cause")
    dr.set_defaults(func=cmd_delegate_retry)

    kn = sub.add_parser("knowledge", help="deduplicating doc cache with reported freshness")
    kns = kn.add_subparsers(dest="cmd", required=True)
    kc = kns.add_parser("cache", help="store a fetched page under its normalised URL")
    kc.add_argument("--url", required=True); kc.add_argument("--content-file")
    kc.add_argument("--ttl-hours", dest="ttl_hours", type=float)
    kc.set_defaults(func=cmd_knowledge_cache)
    kl = kns.add_parser("lookup", help="the cached answer for a URL or a phrase")
    kl.add_argument("--url", required=True); kl.set_defaults(func=cmd_knowledge_lookup)
    kf = kns.add_parser("freshness", help="whether cached docs are past their TTL")
    kf.add_argument("--url"); kf.set_defaults(func=cmd_knowledge_freshness)

    tl = sub.add_parser("telemetry", help="measured usage, baselines and comparison")
    tls = tl.add_subparsers(dest="cmd", required=True)
    tse = tls.add_parser("sessions", help="usage from session records (file or stdin)")
    tse.add_argument("--file"); tse.add_argument("--rollup", action="store_true")
    tse.set_defaults(func=cmd_telemetry_sessions)
    trl = tls.add_parser("run-log", help="per-step usage from an opencode run log")
    trl.add_argument("--file", required=True); trl.set_defaults(func=cmd_telemetry_run_log)
    tc = tls.add_parser("compare", help="judge an after measurement against a before")
    tc.add_argument("--before", required=True); tc.add_argument("--after", required=True)
    tc.add_argument("--category", default="task"); tc.add_argument("--tolerance", type=float, default=0.10)
    tc.set_defaults(func=cmd_telemetry_compare)
    tb = tls.add_parser("baseline", help="record or list a category's baseline")
    tb.add_argument("--file", required=True); tb.add_argument("--category", required=True)
    tb.add_argument("--task-log", dest="task_log")
    tb.set_defaults(func=cmd_telemetry_baseline)

    dc = sub.add_parser("decide", help="one deterministic decision for a task (no LLM)")
    dcs = dc.add_subparsers(dest="cmd", required=True)
    dcc = dcs.add_parser("classify", help="classify a request along five dimensions")
    dcc.add_argument("--request", required=True)
    dcc.add_argument("--files", type=int); dcc.add_argument("--independent", type=int)
    dcc.set_defaults(func=cmd_decide_classify)
    dcd = dcs.add_parser("decide", help="the full decision (workflow, delegation, model, retry)")
    dcd.add_argument("--request", required=True)
    dcd.add_argument("--files", type=int); dcd.add_argument("--independent", type=int)
    dcd.add_argument("--registry"); dcd.add_argument("--enabled", action="store_true")
    dcd.set_defaults(func=cmd_decide_decide)
    # `--json` is a top-level flag, so it is only read before the subcommand.
    # These leaf parsers offer it after the subcommand too, which is the shape
    # the agent pages call them in; the dest is shared, so `swarmkit --json X`
    # keeps working unchanged.
    for leaf in (mi, mr, mn, mdg, mrec, mh, mb, cxp, cxsx, cxf,
                 dd, dr, kc, kl, kf, tse, trl, tc, tb, dcc, dcd, mdis):
        # SUPPRESS, not False: a leaf default of False would overwrite a
        # top-level `--json` (subparsers parse into a fresh namespace whose
        # every attribute is copied back), silently turning JSON off.
        leaf.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                          help="emit JSON")

    return p


# --- self-evolution: experience, memory, analysis, bounded experiments ------
#
# Everything below is the engine the spec asked for. It is wired into the
# manager lifecycle rather than left as a library nobody calls, because two
# audit rounds already found exactly that failure shape: a mechanism that works
# and is unreachable.


def _memory_root(workspace: Path) -> Path:
    return workspace / experience.MEMORY_ROOT


def _evolve_root(workspace: Path) -> Path:
    return workspace / evolve.EVOLUTION_ROOT


def _pairs(values: Any) -> dict[str, Any]:
    """``k=v`` pairs into a dict, accepting both ``--candidate a=1 --candidate b=2``
    and ``--candidate a=1 b=2``.

    Deliberately not ``--json``: quoting a JSON object on a PowerShell command
    line corrupts it often enough that the flag becomes a trap. ``k=v`` also
    keeps the caller from smuggling a structure the metrics never promised.
    """
    flat: list[str] = []
    if values is None:
        return {}
    if isinstance(values, str):
        flat.append(values)
    else:
        for item in values:
            flat.extend(item if isinstance(item, (list, tuple)) else [item])

    out: dict[str, Any] = {}
    for raw in flat:
        if "=" not in raw:
            raise SwarmError(
                f"expected key=value, got {raw!r}", code="BAD_USAGE")
        key, _, value = raw.partition("=")
        key = key.strip()
        if not key:
            raise SwarmError(f"empty key in {raw!r}", code="BAD_USAGE")
        try:
            out[key] = float(value)
        except ValueError:
            out[key] = value
    return out


def _experience_row(record: experience.Experience) -> dict[str, Any]:
    return {
        "run": record.run,
        "category": record.category,
        "outcome": record.outcome,
        "complexity": record.complexity,
        "tier": record.tier,
        "confidence": record.confidence,
        "verified": record.verified,
        "failure_cause": record.failure_cause,
        "root_cause_established": record.root_cause_established,
        "lessons": record.lessons,
        "hypotheses": record.hypotheses,
        "retries": record.retries,
        "at": record.at,
    }


def _capture_terminal(run_dir: Path, workspace: Path,
                      state: store.RunState, args: argparse.Namespace) -> dict[str, Any]:
    """Capture a finished run into memory. Never raises.

    Called when a run reaches a terminal phase so the engine learns without
    anyone having to remember to teach it. It is wrapped because a run that
    genuinely delivered must not be reported as failed because a memory write
    failed - but the failure is reported rather than swallowed, because a
    silently dead learner looks exactly like a learner with nothing to say.
    """
    try:
        record = experience.capture_run(
            run_dir,
            request=getattr(args, "request", "") or "",
            user_corrections=getattr(args, "corrected", None) or (),
            user_accepted=getattr(args, "user_accepted", None),
        )
        experience.ExperienceStore(_memory_root(workspace)).record(record)
        return {"ok": True, "run": record.run, "outcome": record.outcome,
                "tier": record.tier, "confidence": record.confidence,
                "verified": record.verified}
    except Exception as exc:  # noqa: BLE001 - deliberately total
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def cmd_experience_record(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    run_dir = _run_dir(workspace, args.slug)
    result = _capture_terminal(run_dir, workspace, store.RunState.load(run_dir), args)
    if not result.get("ok"):
        return _emit({"experience": result}, args.json,
                     f"experience capture FAILED: {result.get('error')}", code=1)
    return _emit({"experience": result}, args.json,
                 f"recorded experience for {result['run']} "
                 f"(tier {result['tier']}, confidence {result['confidence']})")


def cmd_experience_list(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    found = experience.ExperienceStore(_memory_root(workspace)).search(
        category=args.category, outcome=args.outcome,
        failure_cause=args.cause, limit=args.limit)
    rows = [_experience_row(item) for item in found]
    lines = [f"{r['run']}  {r['outcome']:<11} {r['tier']:<10} conf={r['confidence']:.2f}"
             f"  {r['failure_cause']}" for r in rows] or ["(no experiences recorded)"]
    return _emit({"experiences": rows, "count": len(rows)}, args.json, "\n".join(lines))


def cmd_experience_challenge(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    result = experience.Memory(_memory_root(workspace)).challenge(args.run, args.reason)
    return _emit(result, args.json,
                 f"challenged {args.run}: {result.get('reason', '')}")


def cmd_learn_recall(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    memory = experience.Memory(_memory_root(workspace))
    recalled = memory.recall(category=args.category, failure_cause=args.cause,
                             limit=args.limit)
    lessons = memory.lessons()
    hypotheses = memory.hypotheses()
    lines = [f"[{r['confidence']:.2f}] {r['rule']}  ({r['origin']})" for r in recalled]
    lines.append(f"-- {len(lessons)} lesson(s), {len(hypotheses)} hypothesis(es) in memory")
    return _emit({"recall": recalled, "lessons": len(lessons),
                  "hypotheses": len(hypotheses)}, args.json, "\n".join(lines))


def cmd_learn_schema(args: argparse.Namespace) -> int:
    schema = analysis.describe_schema()
    return _emit(schema, args.json, json.dumps(schema, indent=2, default=str))


def cmd_analyze(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    root = _memory_root(workspace)
    store_ = experience.ExperienceStore(root)
    should, why = analysis.should_analyze(store_.all())
    if args.check_only:
        return _emit({"should_analyze": should, "reason": why}, args.json,
                     f"{'analyze' if should else 'hold off'}: {why}")
    result = analysis.analyze(root, category=args.category)
    lines = [f"{k}: {v}" for k, v in result["metrics"].items() if not isinstance(v, (dict, list))]
    lines.append(f"sample_sufficient: {result['sample_sufficient']} "
                 f"(min {result['min_sample']})")
    for proposal in result["proposals"]:
        flag = "  [NEEDS AUTHORIZATION]" if proposal["needs_authorization"] else ""
        lines.append(f"- [{proposal['target']}] {proposal['id']}: "
                     f"{proposal['problem']}{flag}")
    if not result["proposals"]:
        lines.append("no proposal: nothing measured is currently bad")
    return _emit(result, args.json, "\n".join(lines))


def cmd_evolve_candidate(args: argparse.Namespace) -> int:
    """Register a proposal that the analyzer actually produced.

    Candidates are never hand-written. An operator who can invent a proposal
    can invent its evidence, and then the loop is just a machine for
    believing whatever it is told - which is the whole failure mode a
    self-improvement engine has to avoid.
    """
    workspace = Path(args.workspace).resolve()
    root = _memory_root(workspace)
    result = analysis.analyze(root, category=args.category)
    match = next((p for p in result["proposals"] if p["id"] == args.proposal), None)
    if match is None:
        known = [p["id"] for p in result["proposals"]]
        raise SwarmError(
            f"no analyzer proposal {args.proposal!r}; available: {known or 'none'}. "
            "Run `swarm analyze` first - candidates come from measurements, "
            "not from hand-written wishes.",
            code="PROPOSAL_NOT_FOUND")
    policy = evolve.load_policy(_evolve_root(workspace))
    version = evolve.create_candidate(
        analysis.Proposal(**{k: match[k] for k in (
            "id", "target", "problem", "evidence", "change",
            "expected_benefit", "risks", "evaluation", "rollback")}),
        evolve.VersionStore(_evolve_root(workspace)),
        kind=args.kind,
        max_generations=args.max_generations or policy.max_generations,
        max_candidates=args.max_candidates or policy.max_candidates)
    return _emit({"version": version, "generation": version["generation"]}, args.json,
                 f"candidate {version['id']} registered (generation "
                 f"{version['generation']}, state {version['state']})")


def cmd_evolve_evaluate(args: argparse.Namespace) -> int:
    store_ = evolve.VersionStore(_evolve_root(Path(args.workspace).resolve()))
    # `--min-gain` is optional, and an absent flag must become the documented
    # default rather than None. Passing None straight through reaches
    # `compare`, where `gain < min_gain` raises TypeError instead of rejecting
    # a too-small gain - an unusable benchmark gate dressed up as one.
    min_gain = evolve.DEFAULT_MIN_GAIN if args.min_gain is None else args.min_gain
    result = evolve.evaluate(args.proposal, args.version,
                             _pairs(args.candidate), _pairs(args.baseline),
                             store_, safety_passed=args.safety_passed,
                             authorized=args.authorized, min_gain=min_gain)
    verdict = "PROMOTED" if result.get("promoted") else f"rejected: {result.get('reason')}"
    return _emit(result, args.json,
                 f"{args.version}: {verdict}"
                 + (f" (gain {result['gain']})" if result.get("gain") is not None else ""),
                 code=0 if result.get("promoted") else 1)


def cmd_evolve_rollback(args: argparse.Namespace) -> int:
    store_ = evolve.VersionStore(_evolve_root(Path(args.workspace).resolve()))
    result = evolve.rollback(args.version, store_, reason=args.reason)
    # `active()` returns a Version, not a dict. Getting this wrong turns a
    # successful rollback into a traceback - the withdrawal happened on disk
    # and then the process died while describing it.
    active = store_.active()
    return _emit(result, args.json,
                 f"rolled back {args.version} -> active is now "
                 f"{active.id if active else 'none'}")


def cmd_evolve_monitor(args: argparse.Namespace) -> int:
    store_ = evolve.VersionStore(_evolve_root(Path(args.workspace).resolve()))
    result = evolve.monitor(args.version, _pairs(args.observed), store_)
    return _emit(result, args.json,
                 f"monitor {args.version}: {'healthy' if result.get('ok') else 'REGRESSED'}"
                 + (f" - {result.get('detail', '')}" if result.get("detail") else ""),
                 code=0 if result.get("ok") else 1)


def cmd_evolve_policy(args: argparse.Namespace) -> int:
    root = _evolve_root(Path(args.workspace).resolve())
    policy = evolve.load_policy(root)
    touched = any(v is not None for v in (
        args.min_new_experiences, args.cooldown_s, args.max_candidates,
        args.max_generations, args.min_gain, args.auto_promote))
    if touched:
        for name in ("min_new_experiences", "cooldown_s", "max_candidates",
                     "max_generations", "min_gain"):
            value = getattr(args, name)
            if value is not None:
                setattr(policy, name, value)
        if args.auto_promote is not None:
            policy.auto_promote = args.auto_promote
        if args.auto_promote_target is not None:
            policy.auto_promote_targets = tuple(args.auto_promote_target)
        evolve.save_policy(policy, root)
        return _emit(policy.to_dict(), args.json, f"policy saved: {policy.to_dict()}")
    return _emit(policy.to_dict(), args.json,
                 "\n".join(f"{k}: {v}" for k, v in policy.to_dict().items()))


def cmd_evolve_history(args: argparse.Namespace) -> int:
    store_ = evolve.VersionStore(_evolve_root(Path(args.workspace).resolve()))
    active = store_.active()
    payload = {
        "active": active.to_dict() if active else None,
        "promoted_generation": store_.generations(),
        "candidate_records": store_.candidate_count(),
        "rejected_rules": sorted(store_.rejected_rules()),
        "events": store_.history()[-args.limit:],
    }
    lines = [f"active: {active.id if active else 'none'}",
             f"promoted generation: {payload['promoted_generation']}",
             f"candidate records: {payload['candidate_records']}",
             f"rejected rules: {len(payload['rejected_rules'])}"]
    lines += [f"  {e.get('at', '?')} {e.get('event', '?')} "
              f"{e.get('version') or e.get('proposal') or ''}".rstrip()
              for e in payload["events"]]
    return _emit(payload, args.json, "\n".join(lines))


def cmd_evolve_scope_check(args: argparse.Namespace) -> int:
    """Refuse a self-evolution edit that touches a protected path."""
    paths = args.path or []
    allowed = evolve.assert_scope_allowed(paths)
    return _emit({"allowed": allowed, "protected": sorted(evolve.PROTECTED_PATHS)},
                 args.json,
                 f"scope ok: {allowed}" if allowed else "scope refused")


# -- decide: one deterministic decision for a task -----------------------------


def cmd_decide_classify(args: argparse.Namespace) -> int:
    profile = decision.classify(args.request, file_count=args.files or 0,
                             independent_workstreams=args.independent or 1)
    d = decision.decide(args.request, enabled=False, file_count=args.files or 0,
                        independent_workstreams=args.independent or 1)
    return _emit(profile.to_dict(), args.json, decision.render(d))


def cmd_decide_decide(args: argparse.Namespace) -> int:
    result = decision.decide(
        args.request,
        enabled=bool(args.enabled) or decision.is_enabled(),
        file_count=args.files or 0,
        independent_workstreams=args.independent or 1,
        registry=Path(args.registry) if args.registry else None,
    )
    return _emit(result.to_dict(), args.json, decision.render(result))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args) or 0)
    except SwarmError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": exc.as_dict()}, indent=2))
        else:
            print(f"error [{exc.code}] {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:  # pragma: no cover
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
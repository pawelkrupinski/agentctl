# agentctl, part `reduntracked`: a check never green is not tracked: the never-green record and untracking.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.context import Context
from agentctl.statemachines import is_terminal
import json
import time
from typing import Any
from agentctl.core import attempt, err
from agentctl.redrecord import canon_job, red_obs_for
from agentctl.gating import main_workflow_files
from agentctl.redrepos import red_is_other
from agentctl.red import red_move, red_stop_holder
from agentctl.ciwhy import decided_runs, registry_name

from agentctl.redseen import red_obs_of_units
from agentctl.redknobs import knob_ever_green_recheck


# A CHECK THAT HAS NEVER BEEN GREEN IS NOT TRACKED (the owner, 2026-11-19, of "Alert backtest #9 -> IN PROGRESS ... fix
# 4528ed2 awaiting next scheduled run at 12:41Z": "remove it from tracked reds and don't alert in status line until it
# gets green for the first time, remember that for the future"). Alert backtest was born red on 2026-11-18 and failed
# its first nine runs: every one of them a red with offers, fixers, refuted fixes and a status-line mention, for a check
# that had never once passed. Now a red of a workflow that has NEVER had a green run on main -- or, in a workflow that
# has, of a job that never concluded success in a run on main -- is UNTRACKED: no item opens (so no offer, fixer, owner
# ask, inbox line, status-line mention or watchdog invariant), an open one is closed quietly, and ci-why and the status
# line leave it out; `agentctl red status -v` lists it under "untracked (never green)", for information. The first green
# run makes its next red an ordinary one. "Ever green" is GitHub's run history on main: a green run among the snapshot's
# runs, else `run list --status success`; a job, its success in the newest green run or any run of the snapshot's list.
# A green once seen is kept in the store (meta EVER_GREEN_META) for good; a never-green answer is asked again after
# EVER_GREEN_RECHECK. Anything unread keeps the red tracked: the rule never hides a red on an unknown. It applies to the
# reds of runs on main (gate, outside, stuck, a branch's prediction); main's local checks and flakes are tracked as ever.
EVER_GREEN_META = "ever_green"        # store meta: {repo: {"flows": {workflow: run id}, "jobs": {workflow: {job: run id}}}}
UNTRACKED_META = "red_untracked"      # store meta: {repo: [{key, workflow, jobs, run, why}]}: red status -v's list
EVER_GREEN_JOB_RUNS = 10              # runs of the snapshot's list read for a job's success, at most
UNTRACKED_KINDS = ("gate", "outside", "stuck-run", "predicts")
_UNREAD = object()  # newest_green_main raised: the answer is unknown, not 'never green'


def ever_green_record(store):
    try:
        v = json.loads(store.meta_get(EVER_GREEN_META) or "{}")
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def ever_green_note(store, o_r, workflow, run_id, jobs=()):
    """Records workflow (and its green jobs) as green once, in the store; only a change is written."""
    rec = ever_green_record(store)
    mine = rec.setdefault(o_r, {})
    flows, js = mine.setdefault("flows", {}), mine.setdefault("jobs", {}).setdefault(workflow, {})
    fresh = [j for j in jobs if j not in js]
    if workflow in flows and not fresh:
        return
    flows.setdefault(workflow, run_id)
    for j in fresh:
        js[j] = run_id
    store.meta_set(EVER_GREEN_META, json.dumps(rec, sort_keys=True))


def green_job_names(jobs):
    return sorted({canon_job(j.get("name")) for j in jobs or [] if j.get("conclusion") == "success" and j.get("name")})


def job_matches(name, job):
    a, b = canon_job(name), canon_job(job)
    return a == b or registry_name(a) == registry_name(b) and " / " not in b


def decided_run_jobs(o_r, rid, *, ctx):
    return ctx.runs.run_jobs(o_r, rid)


def newest_green_main(o_r, workflow, *, ctx):
    """The newest green run of workflow on main from GitHub (its file when main names it), or None."""
    wfile = main_workflow_files(o_r).get(workflow, workflow) if not red_is_other(o_r) else workflow
    # listing: GitHub's own success filter; [] is 'never green' only from this complete filtered answer
    runs = ctx.runs.main_runs(o_r, wfile, 1, status="success")
    return next((r for r in runs or [] if r.get("conclusion") == "success"), None)


def _completed_green(runs: Any) -> Any:
    """The first completed run of runs that concluded success, or None."""
    return next((r for r in runs or [] if r.get("status") == "completed" and r.get("conclusion") == "success"), None)


def workflow_ever_green(store, o_r, workflow, runs=(), now_=None, *, ctx):
    """True when workflow has had a green run on main, False when GitHub says it never had, None when unread."""
    t = now_ if now_ is not None else ctx.clock.time()
    if workflow in (ever_green_record(store).get(o_r) or {}).get("flows", {}):
        return True
    green = _completed_green(runs)
    if not green:
        if _never_green_recently(ctx.caches.never_green_at, (o_r, workflow), t):
            return False
        # SystemExit: NoToken
        green = attempt(lambda: newest_green_main(o_r, workflow, ctx=ctx), _UNREAD, catch=(Exception, SystemExit))
        if green is _UNREAD:
            return None
    if not green:
        ctx.caches.never_green_at[(o_r, workflow)] = t
        return False
    ever_green_note(store, o_r, workflow, green.get("databaseId"))
    return True


def _never_green_recently(never_green_at: dict[Any, Any], key: tuple[str, ...], t: float) -> bool:
    """True when the workflow (or job) of `key` was found never green within EVER_GREEN_RECHECK before t."""
    at = never_green_at.get(key)
    return at is not None and t - at < knob_ever_green_recheck()


def _has_success(runs: list[Any]) -> bool:
    return any(r.get("conclusion") == "success" for r in runs)


def _job_candidate_runs(o_r: Any, workflow: Any, runs: Any, *, ctx: Context) -> list[Any]:
    """The runs whose jobs are read for a job's green: the decided ones, green first (EVER_GREEN_JOB_RUNS of them), led
    by the newest green run on main when none of them is green."""
    cands = sorted(decided_runs(runs), key=lambda r: r.get("conclusion") != "success")[:EVER_GREEN_JOB_RUNS]
    if not _has_success(cands):
        g = attempt(lambda: newest_green_main(o_r, workflow, ctx=ctx), None, catch=(Exception, SystemExit))
        cands = ([g] if g else []) + cands
    return cands


def _job_green_in_runs(store: Any, o_r: Any, workflow: Any, job: Any, cands: list[Any], *, ctx: Context) -> bool | None:
    """True when a run of cands has job green (every green job found is noted), None when a green run could not be read
    (its jobs may hold this one), False when none of them has it."""
    for r in cands:
        rid = r.get("databaseId")
        try:
            jobs = decided_run_jobs(o_r, rid, ctx=ctx)
        except (Exception, SystemExit):
            if r.get("conclusion") == "success":
                return None
            continue
        greens = green_job_names(jobs)
        if greens:
            ever_green_note(store, o_r, workflow, rid, greens)
        if any(job_matches(g, job) for g in greens):
            return True
    return False


def job_ever_green(store, o_r, workflow, job, runs=(), now_=None, *, ctx):
    """True when job of (ever green) workflow concluded success in a run on main; False when it did in none of the runs
    read -- at least one green run among them, every green one read; None (unknown, tracked) otherwise."""
    t = now_ if now_ is not None else ctx.clock.time()
    known = ((ever_green_record(store).get(o_r) or {}).get("jobs") or {}).get(workflow) or {}
    if any(job_matches(k, job) for k in known):
        return True
    if _never_green_recently(ctx.caches.never_green_at, (o_r, workflow, job), t):
        return False
    cands = _job_candidate_runs(o_r, workflow, runs, ctx=ctx)
    if not _has_success(cands):
        return None  # no green run to read its jobs from: unknown, tracked
    seen = _job_green_in_runs(store, o_r, workflow, job, cands, ctx=ctx)
    if seen is False:
        ctx.caches.never_green_at[(o_r, workflow, job)] = t
    return seen


def _never_green_jobs(store: Any, o_r: Any, ob: Any, wf: Any, runs: Any, now_: Any, *, ctx: Context) -> Any:
    """Why a gate or outside red is untracked when every one of its jobs never passed on main, or None."""
    jobs = [j for j in ob.get("jobs") or [ob.get("job")] if j and j not in ("?", "infra", "stuck")]
    if jobs and all(job_ever_green(store, o_r, wf, j, runs, now_, ctx=ctx) is False for j in jobs):
        return f"never green: {', '.join(jobs)} of {wf} never passed on main"
    return None


def red_never_green(store, o_r, ob, now_=None, *, ctx):
    """Why observation ob is untracked ("never green: ..."), or None (tracked). Its workflow never green on main; or, a
    gate or outside red, every one of its jobs never green there."""
    if ob.get("red_kind") not in UNTRACKED_KINDS or not ob.get("workflow"):
        return None
    wf, runs = ob["workflow"], ob.get("runs_list") or []
    g = workflow_ever_green(store, o_r, wf, runs, now_, ctx=ctx)
    if g is False:
        return f"never green: {wf} has had no green run on main"
    if g is None or ob.get("red_kind") not in ("gate", "outside"):
        return None
    return _never_green_jobs(store, o_r, ob, wf, runs, now_, ctx=ctx)


def red_split_untracked(store, o_r, obs, now_=None, *, ctx):
    """(tracked obs, {key: (ob, why)} untracked): the observations of checks never green on main set apart."""
    tracked, untracked = {}, {}
    for key, ob in (obs or {}).items():
        try:
            why = red_never_green(store, o_r, ob, now_, ctx=ctx)
        except (Exception, SystemExit) as e:
            err(f"red: could not tell whether {key} was ever green ({str(e)[:120]}): tracked")
            why = None
        if why:
            untracked[key] = (ob, why)
        else:
            tracked[key] = ob
    return tracked, untracked


def red_note_untracked(store, o_r, untracked):
    """The untracked reds of o_r kept in the store (meta UNTRACKED_META) for red status -v; written only on a change."""
    try:
        cur = json.loads(store.meta_get(UNTRACKED_META) or "{}")
        cur = cur if isinstance(cur, dict) else {}
    except (ValueError, TypeError):
        cur = {}
    rows = [{"key": k, "workflow": ob["workflow"], "jobs": ob.get("jobs") or [], "why": why,
             "run": {x: (ob.get("run") or {}).get(x) for x in ("number", "databaseId")}}
            for k, (ob, why) in sorted(untracked.items())]
    if cur.get(o_r, []) == rows:
        return
    if rows:
        cur[o_r] = rows
    else:
        cur.pop(o_r, None)
    store.meta_set(UNTRACKED_META, json.dumps(cur, sort_keys=True))


def red_untracked_list(store):
    """[(repo, row)] of every untracked red (red_note_untracked's)."""
    try:
        cur = json.loads(store.meta_get(UNTRACKED_META) or "{}")
    except (ValueError, TypeError):
        return []
    return [(o_r, r) for o_r, rows in sorted((cur or {}).items()) for r in rows or []]


def red_untracked_runs(store, o_r, units, *, ctx):
    """{(workflow, run id): why} of the runs whose every red is untracked (ci-why and the status line leave them out)."""
    obs = red_obs_for(o_r, red_obs_of_units(units))
    _tracked, untracked = red_split_untracked(store, o_r, obs, ctx=ctx)
    runs = {}
    for ob in obs.values():
        k = (ob["workflow"], ob["run"].get("databaseId"))
        runs.setdefault(k, []).append(untracked.get(ob["key"], (None, None))[1])
    return {k: whys[0] for k, whys in runs.items() if all(whys)}


def red_active_untracked(store, o_r, it, untracked, obs, now_=None, *, ctx):
    """Why an open red is untracked now (its observation is, or its workflow/jobs were never green on main), else None.
    Only on a read observation (obs not None): an unknown never closes a red."""
    if obs is None or is_terminal("red", it["state"]):
        return None
    d = it["data"]
    if d["key"] in untracked:
        return untracked[d["key"]][1]
    if d["key"] in obs:
        return None
    wf = d.get("workflow")
    ob = {"red_kind": d.get("red_kind"), "workflow": wf, "jobs": d.get("jobs") or [d.get("job")],
          "runs_list": ctx.runs.main_runs_of(o_r).runs_of(wf)}
    try:
        return red_never_green(store, o_r, ob, now_, ctx=ctx)
    except (Exception, SystemExit):
        return None


def red_untrack(store, it, why):
    """An open red of a check never green on main, closed quietly: its offer closed, its holder told to stop, no inbox
    line (the owner: no alert until it is green once)."""
    store.close_offer(f"red:{it['id']}", "cleared")
    red_stop_holder(store, it["data"]["key"], f"untracked: {why}", "red cleared")
    return red_move(store, it, "cleared", f"untracked: {why}", severity="info")

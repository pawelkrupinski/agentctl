# agentctl, part `landwatch`: land's watch.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
from agentctl.clock import epoch_now
from agentctl.timefmt import parse_time
from agentctl.watchknobs import knob_watch_conv_start
from agentctl.core import err
from agentctl.repos import movies_repo_path, red_repo_clone, slug
from agentctl.clones import main_clone
from agentctl.forge import is_cancelled
from agentctl.context import Context
from agentctl.ciwhy import run_no_verdict
from agentctl.onmain import contains


# ---------------------------------------------------------------- land: the watch after LANDED
# The owner (2026-10-26): "whenever an agent pushed to main, make a script watch the corresponding convergence and main
# build and tell the agent if it fails". With no pre-merge CI, main's CI is where a change is tested in full: land keeps
# running after LANDED until Main and the convergence say green or red, so the agent (land in the background) hears it.
# Every kind but `main` is a dispatched suite (pick_convergence_run): the convergence, kicked by Main, and Identity takes
# (the takes rows split out of it, 2026-11-08), dispatched by the convergence's preflight.
WATCH = {"pawelkrupinski/movies": {"main": ("Main", "main.yml"),
                                   "convergence": ("Identity model convergence", "identity-model-convergence.yml"),
                                   "takes": ("Identity takes", "identity-takes.yml")}}
WATCH_MAIN_REFETCH = 300         # a run head the clone lacks fetches main at most this often (onmain.contains)
WATCH_SLACK = 120                 # clock skew between the laptop and GitHub's createdAt


def pick_main_run(repo, sha, runs):
    """Main's run for sha: its own push run, unless that was cancelled or never came (a newer push superseded it); then the
    newest run whose head contains sha."""
    own = next((r for r in runs if r.get("headSha") == sha and not is_cancelled(r)), None)
    if own:
        return own
    later = [r for r in runs if not is_cancelled(r) and contains(repo, sha, r.get("headSha"), WATCH_MAIN_REFETCH)]
    return max(later, key=lambda r: r["databaseId"]) if later else None


def pick_convergence_run(repo, sha, runs, pushed_at, o_r=None, *, ctx):
    """The FIRST convergence run on main created after the push whose head contains sha -- this commit's kick, a later
    push's, or the daily run (the owner, 2026-10-26: never hunt the sha's own kick). A cancelled one (the lane replaced
    it) passes to the next, and so does an incomplete one (run_no_verdict: only `verdict complete` failed; o_r given)."""
    after = [r for r in runs if r.get("createdAt") and parse_time(r["createdAt"]).timestamp() >= pushed_at - WATCH_SLACK]
    ok = [r for r in sorted(after, key=lambda r: r["databaseId"]) if not is_cancelled(r) and contains(repo, sha, r.get("headSha"), WATCH_MAIN_REFETCH)
          and not (o_r and run_no_verdict(o_r, r, ctx=ctx))]
    return ok[0] if ok else None


def failed_jobs(o_r, rid, *, ctx):
    return [j["name"] for j in ctx.runs.run_jobs(o_r, rid) if j.get("conclusion") in ("failure", "timed_out", "cancelled")]


def start_watch(st, repo, sha, pushed_at, worktree=None, close=None, session=None, o_r=None, follower=None):
    """The landed sha's watch item: created once, a second start leaves it as it is (the sha's ONE watch, whichever
    host or follower starts it). follower: {followed_by, followed_until}, a `land --follow` that watches it itself
    (claim_watch)."""
    o_r = o_r or slug(repo)
    if not WATCH.get(o_r):
        return None
    return st.put_item("watch", sha, "watching", {"repo": main_clone(repo), "o_r": o_r, "pushed_at": pushed_at or epoch_now(),
                                                       "worktree": worktree or repo, "close": close, "session": session,
                                                       "next_at": 0, **(follower or {})})


def watch_clone(dd):
    """THIS host's clone of a watch item's repo. The item names its repo by o_r, its GitHub slug -- the same on every
    host -- and `repo`, the clone path of the host that made it, is used only where it is that repo here (review round
    7: a laptop follower's item, taken over by ci-runner's tick, named /Users/... there: every look failed, and the
    watch ended UNKNOWN at its deadline)."""
    o_r, p = dd["o_r"], dd.get("repo")
    if p and os.path.isdir(p):
        try:
            if slug(p) == o_r:
                return p
        except (Exception, SystemExit) as e:
            err(f"land watch: no slug for {p} ({str(e)[:160]}); looking for the clone")
    c = red_repo_clone(o_r)
    if c:
        return c
    raise RuntimeError(f"no clone of {o_r} on {os.uname().nodename} (red-repos.json `clone`, {movies_repo_path()})")


def watch_event(it, verdict, severity="info", code=0):
    dd = it["data"]
    return {"text": verdict.splitlines()[0], "severity": severity,
            "audience": {"worktree": dd.get("worktree"), "session": dd.get("session")},
            "refs": {"verdict": verdict.splitlines()[0], "tail": verdict.splitlines()[1:25], "exit": code,
                     "command": "watch", "sha": it["id"], "close": dd.get("close")}}


def watch_outcome(it):
    """A watch item's verdict: its state, or -- an `unknown` item a follower re-watched (WatchClaim) -- that
    follower's `outcome`, kept on the item (a terminal state is never left)."""
    return (it.get("data") or {}).get("outcome") or it["state"]


def _pick_run(repo: Any, sha: Any, o_r: Any, kind: Any, wfile: Any, pushed_at: Any, *, ctx: Context) -> Any:
    """The run of flow `kind` (its workflow file wfile) that contains sha, or None."""
    # listing: the newest 30 of the flow on main; a run past them is not this push's
    runs = ctx.runs.main_runs(o_r, wfile)
    return pick_main_run(repo, sha, runs) if kind == "main" else pick_convergence_run(repo, sha, runs, pushed_at, o_r, ctx=ctx)


def _unstarted(kind: Any, now_: Any, since: Any, conv_start: Any) -> Any:
    """A convergence (not main) that has not started CONV_START s after `since`."""
    return kind != "main" and now_ - float(since) >= conv_start


def _decide(done: Any, kind: Any, run: Any) -> Any:
    """A completed (not cancelled) run decides its flow: (run, green?)."""
    if run.get("status") == "completed" and not is_cancelled(run):
        done[kind] = (run, run.get("conclusion") == "success")


def watch_look(repo, sha, o_r, flows, done, pushed_at, since, now_, on_run=None, *, ctx):
    """ONE look at the runs that contain sha (the tick's watch item and `land --follow`'s own watch alike): each
    undecided flow's run is picked; a completed (not cancelled) one goes into `done` as (run, green?), a convergence
    that has not started CONV_START s after `since` as (None, None). on_run(kind, run or None) hears each pick.
    Returns {kind: (run id, status, updatedAt)} of the runs followed (the stall judge's state)."""
    conv_start = knob_watch_conv_start()
    seen = {}
    for kind, (name, wfile) in flows.items():
        if kind in done:
            continue
        run = _pick_run(repo, sha, o_r, kind, wfile, pushed_at, ctx=ctx)
        if run is not None:
            seen[kind] = (run.get("databaseId"), run.get("status"), run.get("updatedAt"))
            _decide(done, kind, run)
        elif _unstarted(kind, now_, since, conv_start):
            done[kind] = (None, None)
        else:
            continue  # nothing to follow yet
        if on_run:
            on_run(kind, run)
    return seen


def _watch_red_lines(o_r: Any, sha: Any, flows: Any, reds: Any, *, ctx: Context) -> Any:
    lines = []
    for kind, r in reds:
        try:
            failed = failed_jobs(o_r, r["databaseId"], ctx=ctx)
        except (Exception, SystemExit) as e:  # the verdict stands without the job names
            err(f"could not read the jobs of run {r['databaseId']} ({str(e)[:120]})")
            failed = []
        lines.append(f"WATCH RED: {flows[kind][0]} #{r.get('number')} {', '.join(failed) or r.get('conclusion')} "
                     f"(it contains your {sha[:10]}: if your change broke it, fix forward now with `Fixes-Red: "
                     f"{r.get('number')}`)")
    return "\n".join(lines)


def watch_verdict(o_r, sha, flows, done, *, ctx):
    """('red', lines, [(workflow, number)]) when a decided run is red, ('ok', line, []) when every flow is decided
    green (or the convergence never started), else None."""
    reds = [(k, r) for k, (r, ok) in done.items() if ok is False]
    if reds:
        return "red", _watch_red_lines(o_r, sha, flows, reds, ctx=ctx), [(flows[k][0], r.get("number")) for k, r in reds]
    if len(done) == len(flows):
        parts = [f"{flows[k][0].split()[0] if k == 'main' else k} " +
                 (f"#{r.get('number')} green" if r else "none started") for k, (r, ok) in done.items()]
        return "ok", "WATCH OK: " + ", ".join(sorted(parts, key=lambda x: not x.startswith("Main"))), []
    return None


def watch_eval(repo, sha, pushed_at, o_r, flows, now_, *, ctx):
    """The tick's look at a watch item: ('ok', line, []), ('red', lines, reds) or None (undecided)."""
    done = {}
    watch_look(repo, sha, o_r, flows, done, pushed_at, pushed_at, now_, ctx=ctx)
    v = watch_verdict(o_r, sha, flows, done, ctx=ctx)
    return (v[0], v[1] + f" ({sha[:10]})", v[2]) if v and v[0] == "ok" else v

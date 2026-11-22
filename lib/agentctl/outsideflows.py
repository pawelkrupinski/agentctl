# agentctl, part `outsideflows`: the workflows outside the gate: their runs, their history and timeout bounds, a stuck run, the outside reds
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
import time
from agentctl.core import err, git, process_cache, remote
from agentctl.forge import RED_CONCLUSIONS, RUN_FIELDS, gh_json
from agentctl.repos import scan_repo_for
from agentctl.workflows import GITHUB_JOB_TIMEOUT
from agentctl.gating import _flow_meta, main_workflow_files
from agentctl.redrepos import red_gate_flows
from agentctl.ciwhy import _outside_lists, red_streak


OUTSIDE_FLOW_RECHECK = 1800  # a workflow outside the newest-200 window is listed on its own this often (audit #7)


#                                  "callers": {called file's basename: [caller names]}}
_flow_runs = process_cache({})    # workflow file -> (listed at, its runs on main)


_flow_history, _flow_bounds = process_cache({}), process_cache({})


def stuck_history(o_r, name):
    """The workflow's own last 30 finished runs on main (stuck_limit's, for a workflow the newest-200 list holds too few
    of): listed at most every OUTSIDE_FLOW_RECHECK; [] when its file is unknown or the list fails."""
    wfile = main_workflow_files(o_r).get(name)
    if not wfile:
        return []
    at, runs = _flow_history.get(wfile, (0.0, None))
    if runs is None or time.time() - at >= OUTSIDE_FLOW_RECHECK:
        try:
            # listing: samples of a workflow's run lengths (the stuck limit); fewer samples widen it, never narrow it
            runs = gh_json(o_r, "run", "list", "--workflow", wfile, "--branch", "main", "--status", "completed",
                           "--limit", "30", "--json", RUN_FIELDS)
            _flow_history[wfile] = (time.time(), runs)
        except (Exception, SystemExit) as e:
            err(f"stuck limit: could not list {wfile} ({str(e)[:120]})")
            runs = runs or []
    return runs


def workflow_timeout_bound(txt):
    """Seconds a run of this workflow file can take at most by its jobs' own `timeout-minutes` (summed: jobs may run
    one after another). A job setting none counts GitHub's 360-minute default (review 2026-11-07: it counted 0 beside
    a job that set one); a reusable-workflow call (`uses:`, which takes none) counts 0. None without a jobs section."""
    lines = [l for l in txt.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    try:
        i = next(n for n, l in enumerate(lines) if re.match(r"^jobs:\s*$", l))
    except StopIteration:
        return None
    ind = lambda l: len(l) - len(l.lstrip())  # noqa: E731
    job_ind = prop_ind = None
    jobs = []  # per job: [its timeout-minutes in s or None, a reusable call]
    for l in lines[i + 1:]:
        n = ind(l)
        if n == 0:
            break
        if job_ind is None:
            job_ind = n
        if n == job_ind:
            prop_ind = None
            jobs.append([None, False])
            continue
        if prop_ind is None and n > job_ind:
            prop_ind = n
        if n != prop_ind or not jobs:
            continue
        m = re.match(r"^\s*timeout-minutes:\s*(\d+)\s*$", l)
        if m:
            jobs[-1][0] = int(m.group(1)) * 60
        elif re.match(r"^\s*uses:", l):
            jobs[-1][1] = True
    total = sum(t if t is not None else 0 if uses else GITHUB_JOB_TIMEOUT for t, uses in jobs)
    return total or GITHUB_JOB_TIMEOUT


def stuck_timeout(o_r, name):
    """workflow_timeout_bound of the workflow's file on origin/main (cached per main sha), or None."""
    repo = scan_repo_for(o_r)
    path = main_flow_meta(o_r)["paths"].get(name)
    if not repo or not path:
        return None
    main = git(repo, "rev-parse", f"refs/remotes/{remote()}/main", check=False).stdout.strip()
    if (main, path) not in _flow_bounds:
        if len(_flow_bounds) > 200:
            _flow_bounds.clear()
        txt = git(repo, "show", f"{main}:{path}", check=False).stdout
        _flow_bounds[(main, path)] = workflow_timeout_bound(txt) if txt else None
    return _flow_bounds[(main, path)]


def main_flow_meta(o_r):
    """main_workflow_files' by-products for origin/main: callable-only names, each name's path, each file's callers."""
    main_workflow_files(o_r)
    return next(iter(_flow_meta.values()), {"callable": set(), "paths": {}, "callers": {}})


def main_callable_flows(o_r):
    """The workflows on origin/main whose only trigger is workflow_call: they never run on their own, so no newer run
    of theirs can ever clear a red run of theirs on main (red_callable_evidence does)."""
    return main_flow_meta(o_r)["callable"]


def outside_gate(o_r):
    """[(workflow, newest decided run, since, count)] for each workflow on main OUTSIDE the gate whose newest decided
    run is red: not blocking a push, but red, and for how long (the owner, 2026-10-27: a red nobody is shown is a red
    that gets ignored). EVERY workflow file on main is read (audit #7): those in the newest 200 runs from that list,
    the others listed on their own (at most every OUTSIDE_FLOW_RECHECK)."""
    gated = {n for n, _ in red_gate_flows(o_r)}
    # listing: the newest 200 across workflows; a workflow absent from it is listed on its own below
    runs = gh_json(o_r, "run", "list", "--branch", "main", "--limit", "200", "--json", RUN_FIELDS)
    lists = {}
    for r in runs:
        if r.get("workflowName"):
            lists.setdefault(r["workflowName"], []).append(r)
    for name, wfile in main_workflow_files(o_r).items():
        if name in lists or name in gated:
            continue
        at, mine = _flow_runs.get(wfile, (0.0, None))
        if mine is None or time.time() - at >= OUTSIDE_FLOW_RECHECK:
            try:
                # listing: the workflow's own newest 30, read because the cross-workflow 200 left it out
                mine = gh_json(o_r, "run", "list", "--workflow", wfile, "--branch", "main", "--limit", "30",
                               "--json", RUN_FIELDS)
                _flow_runs[wfile] = (time.time(), mine)
            except Exception as e:  # this workflow unread this time: the others stand
                err(f"outside the gate: could not list {wfile} ({str(e)[:120]})")
                mine = mine or []
        if mine:
            lists[name] = mine
    out = []
    for name, mine in lists.items():
        if name in gated:
            continue
        _outside_lists[name] = mine
        since, count = red_streak(mine)
        if count:
            decided = next(r for r in mine if r.get("status") == "completed" and r.get("conclusion") in RED_CONCLUSIONS)
            out.append((name, decided, since, count))
    return sorted(out, key=lambda x: str(x[2]))

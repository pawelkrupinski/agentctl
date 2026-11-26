# agentctl, part `outsideflows`: the workflows outside the gate: their runs, their history and timeout bounds, a stuck run, the outside reds
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import re
import time
from typing import Any
from agentctl.core import err, git, process_cache, remote
from agentctl.forge import RED_CONCLUSIONS, RUN_FIELDS, gh_json
from agentctl.repos import scan_repo_for
from agentctl.workflows import BOUND_JOBS, GITHUB_JOB_TIMEOUT, JobBlock, scan_jobs
from agentctl.gating import _flow_meta, main_workflow_files
from agentctl.redrepos import red_gate_flows
from agentctl.ciwhy import main_runs_of, red_streak


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
    if runs is None or epoch_now() - at >= OUTSIDE_FLOW_RECHECK:
        try:
            # listing: samples of a workflow's run lengths (the stuck limit); fewer samples widen it, never narrow it
            runs = gh_json(o_r, "run", "list", "--workflow", wfile, "--branch", "main", "--status", "completed",
                           "--limit", "30", "--json", RUN_FIELDS)
            _flow_history[wfile] = (epoch_now(), runs)
        except (Exception, SystemExit) as e:
            err(f"stuck limit: could not list {wfile} ({str(e)[:120]})")
            runs = runs or []
    return runs


JOB_BOUND_LINE = re.compile(r"^\s*timeout-minutes:[ \t]*(\d+)[ \t]*(?:#.*)?$")  # a trailing comment, as workflow_jobs reads it
JOB_USES_LINE = re.compile(r"^\s*uses:")


def _job_bound(block: JobBlock) -> int:
    """One job's share of a run's length, in s: its own `timeout-minutes` (the last one), a reusable-workflow call
    (`uses:`, which takes none) 0, else GitHub's default."""
    bound = None
    for line in block.props:
        m = JOB_BOUND_LINE.match(line)
        if m:
            bound = int(m.group(1)) * 60
    if bound is not None:
        return bound
    return 0 if any(JOB_USES_LINE.match(line) for line in block.props) else GITHUB_JOB_TIMEOUT


def workflow_timeout_bound(txt: str) -> Any:
    """Seconds a run of this workflow file can take at most by its jobs' own `timeout-minutes` (summed: jobs may run
    one after another). A job setting none counts GitHub's 360-minute default (review 2026-11-07: it counted 0 beside
    a job that set one); a reusable-workflow call (`uses:`, which takes none) counts 0. None without a jobs section."""
    jobs = scan_jobs(txt, BOUND_JOBS)
    if jobs is None:
        return None
    return sum(map(_job_bound, jobs)) or GITHUB_JOB_TIMEOUT


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


def _own_runs(o_r: Any, wfile: str) -> Any:
    """The workflow file's own newest 30 runs on main, listed at most every OUTSIDE_FLOW_RECHECK ([] when the list fails)."""
    at, mine = _flow_runs.get(wfile, (0.0, None))
    if mine is None or epoch_now() - at >= OUTSIDE_FLOW_RECHECK:
        try:
            # listing: the workflow's own newest 30, read because the cross-workflow 200 left it out
            mine = gh_json(o_r, "run", "list", "--workflow", wfile, "--branch", "main", "--limit", "30",
                           "--json", RUN_FIELDS)
            _flow_runs[wfile] = (epoch_now(), mine)
        except Exception as e:  # this workflow unread this time: the others stand
            err(f"outside the gate: could not list {wfile} ({str(e)[:120]})")
            mine = mine or []
    return mine


def _outside_runs(o_r: Any, gated: set[str]) -> dict[str, list[Any]]:
    """{workflow: its runs on main, newest first} of every workflow file on main: the newest 200 runs grouped, the files
    missing from them (and not gated) listed on their own."""
    # listing: the newest 200 across workflows; a workflow absent from it is listed on its own below
    runs = gh_json(o_r, "run", "list", "--branch", "main", "--limit", "200", "--json", RUN_FIELDS)
    lists: dict[str, list[Any]] = {}
    for r in runs:
        if r.get("workflowName"):
            lists.setdefault(r["workflowName"], []).append(r)
    for name, wfile in main_workflow_files(o_r).items():
        if name in lists or name in gated:
            continue
        mine = _own_runs(o_r, wfile)
        if mine:
            lists[name] = mine
    return lists


def outside_gate(o_r: Any) -> Any:
    """[(workflow, newest decided run, since, count)] for each workflow on main OUTSIDE the gate whose newest decided
    run is red: not blocking a push, but red, and for how long (the owner, 2026-10-27: a red nobody is shown is a red
    that gets ignored). EVERY workflow file on main is read (audit #7): those in the newest 200 runs from that list,
    the others listed on their own (at most every OUTSIDE_FLOW_RECHECK)."""
    gated = {n for n, _ in red_gate_flows(o_r)}
    out = []
    for name, mine in _outside_runs(o_r, gated).items():
        if name in gated:
            continue
        main_runs_of(o_r).outside[name] = mine
        since, count = red_streak(mine)
        if count:
            decided = next(r for r in mine if r.get("status") == "completed" and r.get("conclusion") in RED_CONCLUSIONS)
            out.append((name, decided, since, count))
    return sorted(out, key=lambda x: str(x[2]))

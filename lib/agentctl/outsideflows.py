# agentctl, part `outsideflows`: the workflows outside the gate: their runs, their history and timeout bounds, a stuck run, the outside reds
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import re
import time
from typing import Any
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


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _jobs_block(txt: str) -> list[str] | None:
    """The code lines under a workflow file's top-level `jobs:` (to the next top-level key), None without that section."""
    lines = [l for l in txt.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    try:
        i = next(n for n, l in enumerate(lines) if re.match(r"^jobs:\s*$", l))
    except StopIteration:
        return None
    block = []
    for l in lines[i + 1:]:
        if _indent(l) == 0:
            break
        block.append(l)
    return block


class _TimeoutScan:
    """The jobs of a `jobs:` block, each [its timeout-minutes in s or None, a reusable call], fed line by line: a line at
    the first line's indent starts a job, one at the indent of a job's first property sets that property."""

    def __init__(self) -> None:
        self.job_ind: int | None = None
        self.prop_ind: int | None = None
        self.jobs: list[list[Any]] = []

    def feed(self, line: str) -> None:
        n = _indent(line)
        if self.job_ind is None:
            self.job_ind = n
        if n == self.job_ind:
            self.prop_ind = None
            self.jobs.append([None, False])
        else:
            self._property(n, line)

    def _property(self, n: int, line: str) -> None:
        assert self.job_ind is not None
        if self.prop_ind is None and n > self.job_ind:
            self.prop_ind = n
        if n != self.prop_ind:
            return
        m = re.match(r"^\s*timeout-minutes:\s*(\d+)\s*$", line)
        if m:
            self.jobs[-1][0] = int(m.group(1)) * 60
        elif re.match(r"^\s*uses:", line):
            self.jobs[-1][1] = True


def workflow_timeout_bound(txt: str) -> Any:
    """Seconds a run of this workflow file can take at most by its jobs' own `timeout-minutes` (summed: jobs may run
    one after another). A job setting none counts GitHub's 360-minute default (review 2026-11-07: it counted 0 beside
    a job that set one); a reusable-workflow call (`uses:`, which takes none) counts 0. None without a jobs section."""
    block = _jobs_block(txt)
    if block is None:
        return None
    scan = _TimeoutScan()
    for line in block:
        scan.feed(line)
    total = sum(t if t is not None else 0 if uses else GITHUB_JOB_TIMEOUT for t, uses in scan.jobs)
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
        _outside_lists[name] = mine
        since, count = red_streak(mine)
        if count:
            decided = next(r for r in mine if r.get("status") == "completed" and r.get("conclusion") in RED_CONCLUSIONS)
            out.append((name, decided, since, count))
    return sorted(out, key=lambda x: str(x[2]))

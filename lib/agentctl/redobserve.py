# agentctl, part `redobserve`: everything red on main now (red_observe), with main's own local-checks red (reds/local.json): its record, whether it is stale against origin/main, its red units
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.core import err, git, reds_dir, remote
from agentctl.statefile import read_record
from agentctl.workflows import LOCAL_FLOW
from agentctl.ciwhy import _gate_lists, _outside_lists, gate_from_snap, gate_report_only, outside_from_snap
from agentctl.redseen import gate_red_units, predict_units, red_obs_of_units, red_units, stuck_obs, unkicked_obs
from agentctl.runsigs import signature


def red_observe(o_r, repo, snap):
    """{key: observation} of everything red on main now: the gate, every other workflow (its newest decided run, audit
    #7), main's local checks, the branch runs that predict main red, and the stuck runs (audit #8). A report-only gate
    run's reds (gate_report_only) are outside the gate's."""
    rows = [(n, d, _gate_lists.get(n, []), not gate_report_only(o_r, n, d)[0]) for n, d, _nw, _s in gate_from_snap(snap)]
    others = [(n, r, _outside_lists.get(n, []), False) for n, r, _s, _c in outside_from_snap(snap)]
    units = gate_red_units(o_r, rows, others) + local_units(repo) + predict_units(o_r, snap)
    obs = red_obs_of_units(units)
    obs.update(stuck_obs(snap))
    try:
        obs.update(unkicked_obs(o_r, repo, snap))
    except (Exception, SystemExit) as e:
        err(f"red: could not tell whether the convergence was kicked ({str(e)[:120]})")
    return obs


def local_red():
    return read_record(reds_dir("local.json")) or {}


def current_main(repo):
    r = git(repo, "rev-parse", "--verify", "-q", f"{remote()}/main", check=False) if repo else None
    return r.stdout.strip() if r is not None and r.returncode == 0 else None


def local_stale(repo):
    """origin/main's sha when the recorded local red was computed on an older one (the record is stale), else None."""
    rec = local_red()
    if not rec.get("checks"):
        return None
    cur = current_main(repo)
    return cur if cur and cur != rec.get("main") else None


def local_units(repo=None):
    """main's own local-checks red (reds/local.json) as red units: one per failing check, run `local`. A record
    computed on an older main is stale: no units (shown as `stale, rechecking`, never RED)."""
    if repo and local_stale(repo):
        return []
    rec = local_red()
    units = []
    for check, c in sorted((rec.get("checks") or {}).items()):
        line = (c.get("lines") or [""])[0]
        s = {"job": check, "kind": "test", "line": line, "sig": signature(LOCAL_FLOW, check, line)}
        run = {"number": "local", "databaseId": f"local-{str(c.get('main'))[:10]}", "headSha": c.get("main"),
               "status": "completed", "conclusion": "failure"}
        units += red_units("?", LOCAL_FLOW, run, [], [s], True)
    return units

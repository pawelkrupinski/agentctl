# agentctl, part `redgate`: the red tick's gate reading: the open flows, the newest decided main run, the gate snapshot and its observation
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.statemachines import is_terminal, open_items
from agentctl.redrepos import red_gate_flows
from agentctl.ciwhy import gate_from_snap, gate_runs, gate_snap_rows
from agentctl.redseen import gate_red_units, red_obs_of_units, stuck_obs


def red_open_flows(o_r, store):
    """The workflows of o_r's open reds that are not its gate's: their newest decided run on main must be read too, or
    such a red can never clear (core Dependabot Updates #154, 2026-11-02: green #155/#156 came, nothing read them)."""
    try:
        its = open_items(store, "red")
    except (Exception, SystemExit):
        return []
    gate = {n for n, _f in red_gate_flows(o_r)}
    return sorted({it["data"].get("workflow") for it in its if not is_terminal("red", it["state"])
                   and it["data"].get("repo") == o_r and it["data"].get("workflow")} - gate)


def newest_decided_main(o_r, workflow, runs):
    """The newest decided (success / failure / timed_out) run of a workflow on main, or None."""
    # listing: None here means none decided among the newest 10 -- the callers treat it as unread, not green
    listed = runs.main_runs(o_r, workflow, 10)
    return next((r for r in listed if r.get("status") == "completed"
                 and r.get("conclusion") in ("success", "failure", "timed_out")), None)


def red_gate_snap(o_r, store, *, ctx):
    """Another watched repo's snapshot: its gate workflows only (red-repos.json), read once, into its own MainRuns
    (workflow names repeat across repos: movies has a `Stage NixOS closures` too)."""
    gate = gate_runs(o_r, red_gate_flows(o_r), ctx=ctx)
    fields = ("number", "databaseId", "conclusion", "headSha", "updatedAt")
    latest = {n: {k: d.get(k) for k in fields} for n, d, _nw, _st in gate if d}
    for wf in red_open_flows(o_r, store):  # an open red's own workflow, outside the gate: read so it can clear
        try:
            r = newest_decided_main(o_r, wf, ctx.runs)
        except (Exception, SystemExit):
            r = None
        if r:
            latest[wf] = {k: r.get(k) for k in fields}
    return {"at": ctx.clock.time(), "repo": o_r, "outside": [], "outside_error": None,
            "gate": gate_snap_rows(o_r, gate, runs=ctx.runs),
            "latest": latest}


def red_observe_gate(o_r, snap, *, ctx):
    """{key: observation} of another watched repo: its gate workflows' red runs (no outside workflows, local checks,
    branch predictions or convergence kicks -- those are the primary's), and its stuck gate runs."""
    gate = gate_from_snap(snap, runs=ctx.runs)
    rows = [(n, d, ctx.runs.main_runs_of(snap["repo"]).gate.get(n, []), True) for n, d, _nw, _s in gate]
    obs = red_obs_of_units(gate_red_units(o_r, rows))
    obs.update(stuck_obs(snap))
    return obs

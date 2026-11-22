# agentctl, part `redfamilies`: RED FAMILIES: the reds of one family (by workflow, job, or outside the gate) go to one fixer; the family config
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
import re
from agentctl.core import err, process_cache
from agentctl.selfcode import TOOL_PATH
from agentctl.repos import RED_PRIMARY, red_repo_conf, red_repo_of
from agentctl.workflows import LOCAL_FLOW
from agentctl.redrepos import red_gate_flows
from agentctl.classify import norm_line


# RED FAMILIES (the owner, 2026-10-29: "give all convergence red work to one agent"): the reds of one family -- by
# workflow, or by a failing job's name (Verify scrape recording's `convergence (<cc>)` is convergence work too) -- go to
# ONE fixer. While it is alive, the dispatcher puts a new red of its family under its lease (a sibling it renews with
# its own) and tells it in RED-INBOX.md in its worktree, never a second fixer; when it ends, the next fixer of the
# family goes on in its worktree (previous_fixer_tree). The families are config: red-families.json beside agentctl
# ($AGENTCTL_RED_FAMILIES names another file). `"outside_gate": true` matches every red of a workflow the gate does
# not read (red_outside_gate; the owner, 2026-11-07: every non-gating red to ONE agent).
_families_cache = process_cache({})


def red_families():
    path = os.environ.get("AGENTCTL_RED_FAMILIES") or os.path.join(os.path.dirname(TOOL_PATH), "red-families.json")
    try:
        m = os.path.getmtime(path)
    except OSError:
        return []
    hit = _families_cache.get(path)
    if not hit or hit[0] != m:
        try:
            with open(path) as f:
                fams = json.load(f).get("families") or []
            fams = [dict(x, _jobs=[re.compile(j) for j in x.get("jobs") or []]) for x in fams if x.get("name")]
        except (OSError, ValueError, re.error) as e:
            err(f"red families: {path} unreadable ({e}): none apply")
            fams = []
        hit = _families_cache[path] = (m, fams)
    return hit[1]


def red_family(d):
    """The family of a red (its data, its picture, a fixer record: workflow, job, jobs), or None."""
    if not d:
        return None
    o_r = red_repo_of(d)
    if o_r != RED_PRIMARY:  # ANOTHER WATCHED REPO: its red-repos.json family, every red of it (red-families.json's
        return (red_repo_conf(o_r) or {}).get("family") or None  # families are the primary's)
    jobs = [str(j) for j in (d.get("jobs") or []) + [d.get("job")] if j]
    for fam in red_families():  # the first family that matches: its workflows, jobs or outside_gate (if it names any)
        named = bool(fam.get("workflows") or fam["_jobs"] or fam.get("outside_gate"))  # AND its kinds
        by_name = d.get("workflow") in (fam.get("workflows") or []) or any(rx.search(j) for rx in fam["_jobs"] for j in jobs) \
            or bool(fam.get("outside_gate") and red_outside_gate(d))
        by_kind = not fam.get("kinds") or d.get("kind") in fam["kinds"]
        if (by_name or not named) and by_kind and (named or fam.get("kinds")):
            return fam["name"]
    return None


def red_outside_gate(d):
    """A red of a workflow THE GATE does not read (the owner, 2026-11-07: "move all these to single agent"): the repo's
    gate is red_gate_flows (movies' gating checks registry: Main, Identity model convergence, Identity takes -- the set ci-why's "Outside the gate"
    leaves out); the local checks are the landing gate's own, never outside it. No workflow named: cannot tell, no."""
    wf = d.get("workflow")
    return bool(wf) and wf != LOCAL_FLOW and wf not in {n for n, _f in red_gate_flows(red_repo_of(d))}


def red_identity(d):
    """A red's identity fields: its signature (the normalized first error line, None when none was read) and its
    configured family (None: its run is its queue family)."""
    return {"signature": norm_line(d.get("line")) or None, "family": red_family(d)}

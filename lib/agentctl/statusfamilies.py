# agentctl, part `statusfamilies`: the status model's families section: each red family's holder
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.statefile import read_record
from agentctl.redrepos import red_family
from agentctl.redrecord import lease_is_live, red_active
from agentctl.leases import lease_meta
from agentctl.owners import owner_record_path
from agentctl.orphanreap import holder_work
from agentctl.reddispatch import red_family_holder


def family_holder_row(st: Any, f: Any, t: Any, active: Any, leases: Any) -> Any:
    """The live lease holding a family: its primary one, else an owner's (or any other) on one of its open reds."""
    row = red_family_holder(st, f, t, active=active)
    if row:
        return row
    return next((r for r in leases if (active.get(r["name"].split("/", 1)[1]) or {}).get("data")
                 and red_family(active[r["name"].split("/", 1)[1]]["data"]) == f), None)


def family_holder_worktree(row: Any, f: Any) -> Any:
    """The holder's worktree: its lease's, an owner's ONE worktree from its record."""
    wt = lease_meta(row).get("worktree")
    if not wt and lease_meta(row).get("kind") in ("owned", "owner"):
        wt = (read_record(owner_record_path(lease_meta(row).get("family") or f)) or {}).get("worktree")
    return wt


def status_families(st: Any, t: Any) -> Any:
    """Each family with open reds: how many, and its ONE fixer (the live primary lease), if any."""
    active = red_active(st)
    fams: dict[Any, Any] = {}
    for it in active.values():
        f = red_family(it["data"])
        if f:
            fams[f] = fams.get(f, 0) + 1
    out = []
    leases = [r for r in st.list("red/") if lease_is_live(r, t)]
    cache: dict[Any, Any] = {}
    for f in sorted(fams):
        row = family_holder_row(st, f, t, active, leases)
        work, why = holder_work(row, t, cache) if row else (None, None)
        out.append({"family": f, "open": fams[f], "holder": row["holder"] if row else None,
                    "worktree": family_holder_worktree(row, f) if row else None, "at_work": work, "work": why})
    return out

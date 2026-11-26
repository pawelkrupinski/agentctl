# agentctl, part `redrecord`: a red's record: its lease's name and liveness, the names it answers to and what it is.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
from typing import Any
from agentctl.repos import RED_PRIMARY, red_repo_of, red_repo_short
from agentctl.workflows import LOCAL_FLOW
from agentctl.statemachines import machine_state, open_items, is_terminal
from agentctl.classify import norm_line
from agentctl.redrepos import red_is_other, red_repo_key



def red_lease(key: str) -> str:
    return f"red/{key}"


def lease_is_live(row: dict[str, Any] | None, t: float) -> bool:
    """THE LEASE LIVENESS: a holder, and an expiry after t (the caller's clock: the store's, or the tick's)."""
    return bool(row and row.get("holder") and row["expires_at"] > t)


def red_names(d: dict[str, Any]) -> set[Any]:
    return set(d.get("runs") or []) | {d.get("key")} | ({"local"} if d.get("workflow") == LOCAL_FLOW else set())


def red_what(d: dict[str, Any]) -> str:
    jobs = [str(j) for j in d.get("jobs") or [d.get("job")] if j]
    o_r = red_repo_of(d)
    return ("" if o_r == RED_PRIMARY else f"{red_repo_short(o_r)} ") + f"{d.get('workflow')} #{(d.get('last_run') or {}).get('number')}" + (f" [{', '.join(jobs)}]" if jobs else "")


def red_active(store: Any) -> dict[str, dict[str, Any]]:
    """{key: item} of the non-terminal reds (one per key)."""
    out = {}
    for it in open_items(store, "red"):
        out[it["data"].get("key") or it["id"]] = it
    return out


RED_RECURRING = 3            # landed fixes the same failure outlived: `recurring`, and its fixer hunts the root cause


def red_of_key(store, key):
    """The red item of key now: its open spell, else its newest closed one. A store that does not answer raises
    (review 2026-11-26: it read as "no such red", and a wait on that red was ended as if the red were gone)."""
    for it in store.items("red"):
        if it["data"].get("key") == key and not is_terminal("red", it["state"]):
            return it
    return red_closed_of(store, key)


def red_closed_of(store, key):
    """The newest closed (terminal) red item of a red key or id -- `key`, `key.2`, ... -- or None."""
    base = re.sub(r"\.\d+$", "", key)
    last, n = None, 1
    while n < 200:
        it = store.item("red", base if n == 1 else f"{base}.{n}")
        if it is None:
            break
        last, n = it, n + 1
    if last is None or not machine_state("red", last["state"]).terminal:
        return None
    return last


def canon_job(job):
    """A job's name as red keys have always spelled it: ci.yml's page tests were `page tests (<engine> / <variant>)` until
    2026-11-01 and are now `page-tests-<engine> (<variant>)`; the new name keys as the old, so an open red keeps its key."""
    import re
    m = re.fullmatch(r"((?:\S+ / )*)page-tests-(safari|chrome) \((.+)\)", job or "")
    return f"{m.group(1)}page tests ({m.group(2)} / {m.group(3)})" if m else job


def red_key(workflow, job, line):
    """A red's identity: its workflow and normalized first error line (the failing test or step); the job only when no
    line was read. Never the job name alone: matrix jobs failing the same way are one red."""
    import hashlib
    n = norm_line(line)
    return hashlib.sha1((f"{workflow}\0{n}" if n else f"{workflow}\0job {canon_job(job)}").encode()).hexdigest()[:12]


def red_active_of(store, o_r):
    """red_active, only o_r's reds: another watched repo's own, else the primary's (a red with no `repo`)."""
    mine = o_r if red_is_other(o_r) else RED_PRIMARY
    return {k: it for k, it in red_active(store).items() if red_repo_of(it["data"]) == mine}


def red_obs_for(o_r, obs):
    """Observations keyed for the store: another repo's keys bound to it (red_repo_key), the primary's as they are."""
    if not red_is_other(o_r):
        return obs
    out = {}
    for key, ob in obs.items():
        k = red_repo_key(o_r, key)
        out[k] = dict(ob, key=k)
    return out


def red_item_of_run(store, n, workflow=None):
    """The non-terminal reds a run number or id names (newest spell first)."""
    n = str(n).lstrip("#")
    return [it for it in red_active(store).values() if n in set(it["data"].get("runs") or [])
            and (workflow is None or it["data"].get("workflow") == workflow)]

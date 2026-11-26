# agentctl, part `redrecord`: a red's record: its lease's name and liveness, its terminal states, the names it answers to and what it is.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
from typing import Any
from agentctl.repos import RED_PRIMARY, red_repo_of, red_repo_short
from agentctl.workflows import LOCAL_FLOW
from agentctl.statemachines import machine_state


RED_TERMINAL = ("verified", "cleared")


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
    for it in store.items("red"):
        if it["state"] not in RED_TERMINAL:
            out[it["data"].get("key") or it["id"]] = it
    return out


RED_RECURRING = 3            # landed fixes the same failure outlived: `recurring`, and its fixer hunts the root cause


def red_of_key(store, key):
    """The red item of key now: its open spell, else its newest closed one."""
    try:
        for it in store.items("red"):
            if it["data"].get("key") == key and it["state"] not in RED_TERMINAL:
                return it
        return red_closed_of(store, key)
    except Exception:
        return None


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

# agentctl, part `redrecord`: a red's record: its lease's name and liveness, its terminal states, the names it answers to and what it is.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import time
from typing import Any
from agentctl.repos import RED_PRIMARY, red_repo_of, red_repo_short
from agentctl.workflows import LOCAL_FLOW


RED_TERMINAL = ("verified", "cleared")


def red_lease(key: str) -> str:
    return f"red/{key}"


def lease_is_live(row: dict[str, Any] | None, t: float | None = None) -> bool:
    return bool(row and row.get("holder") and row["expires_at"] > (t if t is not None else time.time()))


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

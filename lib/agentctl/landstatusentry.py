# agentctl, part `landstatusentry`: `land status <id>`: one change, open or decided
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.statemachines import machine_state
from agentctl.timefmt import hhmmz
from agentctl.agewords import fmt_age
from agentctl.changekind import HOLD_SHOWN
from agentctl.queuesubmit import landed_sha_of
from agentctl.checkrun import remote_check_lines
from agentctl.invariants import change_carrying_batch


def check_lines(st: Any, it: dict[str, Any], t: float) -> list[str]:
    """What a checking change's check is: its batch's, for a backlog batch's member (no remote check of its own --
    2026-11-23: ci-row-scope-py's in batch #107 read as a dead check), else its remote check's lines."""
    b = change_carrying_batch(st, it["id"])
    if b:
        return [f"check: in batch {b['id']} ({b['state']}): the batch's check is its check"]
    return list(remote_check_lines((it.get("data") or {}).get("check"), t) or [
        "check: no remote check recorded (a local check tree, or its executor is starting)"])


def _matching_changes(items: list[dict[str, Any]], entry: str) -> list[dict[str, Any]]:
    """The change whose id is `entry`, else every one whose id contains it."""
    return [it for it in items if it["id"] == entry] or [it for it in items if entry in it["id"]]


def _print_open_status(st: Any, it: dict[str, Any], d: dict[str, Any], t: float) -> None:
    """What an open change waits on: its state and age, its hold, its deadline, and (checking) its check."""
    hold = (d.get("hold") or {}).get("why") if it["state"] in HOLD_SHOWN else None
    print(f"OPEN: {it['state']} for {fmt_age(t - float(it.get('entered_at') or t))}"
          + (f", held: {hold}" if hold else "") + f"; deadline in {int((it['deadline'] - t) // 60)} min")
    if it["state"] == "checking":
        print("\n".join(check_lines(st, it, t)))


def _print_status_verdict(st: Any, it: dict[str, Any], d: dict[str, Any], t: float) -> None:
    """A change's verdict: LANDED <sha>, its NOT LANDED / SUPERSEDED line when decided, else what it waits on."""
    if it["state"] == "landed":
        print(f"LANDED {d.get('landed_sha') or landed_sha_of(it['id'], d)} (queued {it['id']})")
    elif machine_state("change", it["state"]).terminal:
        why = str(d.get("why") or f"{it['id']} {it['state']}")
        print(why if why.startswith(("NOT LANDED", "SUPERSEDED")) else f"NOT LANDED: {why}")
    else:
        _print_open_status(st, it, d, t)


def land_status_entry(st: Any, entry: str, t: float) -> int:
    """`land status <entry>`: ONE change -- the item whose id is `entry`, else the one whose id contains it -- open or
    decided: its state, worktree and head, then its verdict (LANDED <sha> / NOT LANDED: <why>) or what it waits on.
    A VERDICT IS DURABLE (queueuntil.py): an agent whose wait died reads it here after the fact. 1 when none or several
    match."""
    hits = _matching_changes(st.items("change"), entry)
    if len(hits) != 1:
        print(f"no single change matches {entry!r}" + (f": {', '.join(it['id'] for it in hits[:8])}" if hits else "")
              + " (`agentctl land status` lists the open ones)")
        return 1
    it = hits[0]
    d = it.get("data") or {}
    print(f"{it['id']}  {it['state']}  {d.get('worktree')}  {str(d.get('head') or '')[:10]}")
    _print_status_verdict(st, it, d, t)
    for r in d.get("rechecks") or []:  # MINIMAL RECHECKS (queuerecheck.py): why each round after the first ran, or did not
        print(f"recheck: {hhmmz(r.get('at') or 0)} {r.get('kind')} on {r.get('main') or '?'}: {r.get('why')}")
    return 0

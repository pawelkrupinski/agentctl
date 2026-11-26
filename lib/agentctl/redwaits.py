# agentctl, part `redwaits`: a red waiting on another red's commit: its wait chain, and the step that resolves it
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.statemachines import is_terminal
from agentctl.timefmt import iso_at
from agentctl.redrecord import red_of_key, red_what
from agentctl.maincontain import MainContainment
from agentctl.redmove import red_move
from agentctl.redopen import red_offer


# A RED WHOSE FIX IS ANOTHER RED'S COMMIT (2026-11-16, Order independence #78, e948278d144a): its fix was the main-tests
# fixer's 715ab5a1a for red e1be6bd9fd46, not on main yet. The owner answered "hold until 715ab5a1a is on main"; the
# fixer held as decided, and its `NOT LANDED: holding ...` read as THE DECIDED ACTION FAILED -- a fresh WAITING ON YOU,
# twice (ev10700, ev10709). Now that hold is a state: fix-pending with a fix of status `waits-on` {sha, red}, from the
# verdict `WAITS ON <sha> (red <key>)` -- or a NOT LANDED / GAVE UP naming `<sha> (... red <key>)` of another red --
# and the reconciler resolves it itself (red_waits_on_step): the sha, or its rebased copy, or the other red's own
# landed fix on main -> a landed fix verified against the next run; the sha set aside by the other red, or that red
# closed without it on main -> offered again. It waits as long as the other red is open: that red has its own holder.
RED_WAITS_ON = "waits-on"


# A WAIT CYCLE HAS NO EXIT (review 2026-11-16): red A waiting on red B's commit while B waits (directly or through others)
# on A's -- neither lands first, each one's holder is "the red it waits on" (red_fix_held), so W2 never fires. A wait
# that would close a cycle is refused at the verdict (red_waits_on_of), and one found standing is offered again.
RED_WAIT_CHAIN_MAX = 20


def _waits_on(store: Any, key: Any) -> Any:
    """The red key that red `key` waits on, None when it is gone, closed or waits on nothing."""
    o = red_of_key(store, key)
    f = ((o or {}).get("data") or {}).get("fix") or {}
    if not o or is_terminal("red", o["state"]) or f.get("status") != RED_WAITS_ON:
        return None
    return f.get("red")


def red_wait_cycle(store, own_key, other):
    """The keys from `other` back to own_key when other waits (through its waits-on chain) on own_key, else None."""
    chain, cur = [], other
    while cur and len(chain) < RED_WAIT_CHAIN_MAX:
        chain.append(cur)
        cur = _waits_on(store, cur)
        if cur == own_key:
            return chain
        if cur in chain:
            return None  # a cycle among others: theirs to break
    return None


def _waits_on_landed(store: Any, it: dict[str, Any], sha: str, other: Any, ofix: dict[str, Any], repo: Any, t: float) -> Any:
    """The fix it waited on (or the other red's own landed one) is on main: verified against the next run; else None."""
    d = it["data"]
    for cand in [sha] + ([ofix["sha"]] if ofix.get("status") == "landed" and ofix.get("sha") else []):
        on = MainContainment(repo).carrying(cand)
        if on and MainContainment(repo).contains(on) is True:
            line = f"{red_what(d)}: red {other}'s fix {cand[:10]} is on main ({on[:10]}): verifying"
            red_move(store, it, "fix-pending", line, data={"fix": {"entry": None, "status": "landed", "sha": on,
                                                                    "at": t, "verify_since": t, "waited_on": other}})
            return [line]
    return None


def _waits_on_aside(od: dict[str, Any], sha: str) -> bool:
    """The other red set the fix `sha` aside (refuted, or unverified), or a rebased copy of it."""
    aside = set(od.get("refuted") or []) | set(od.get("unverified") or [])
    return any(x.startswith(sha) or sha.startswith(x) for x in aside if x and sha)


def _waits_on_ended(store: Any, d: dict[str, Any], o: Any, other: Any, sha: str) -> str | None:
    """Why the wait is over (the other red is gone, waits on this one in turn, set the fix aside, or closed without it on
    main), None while it stands."""
    cycle = red_wait_cycle(store, d.get("key"), other)
    if not o:
        return f"red {other}, whose fix {sha[:10]} it waited on, is gone"
    if cycle:
        return (f"red {other} waits on this red in turn ({' -> '.join(cycle + [str(d.get('key'))])}), a wait cycle neither "
                f"side can end: land the fix here, or say why not")
    if _waits_on_aside(o.get("data") or {}, sha):
        return f"red {other} set aside the fix {sha[:10]} it waited on"
    return _waits_on_closed(o, other, sha)


def _waits_on_closed(o: dict[str, Any], other: Any, sha: str) -> str | None:
    if is_terminal("red", o["state"]):
        return f"red {other} closed ({o['state']}) without the fix {sha[:10]} it waited on reaching main"
    return None


def red_waits_on_step(store: Any, it: dict[str, Any], f: dict[str, Any], repo: Any, t: float) -> Any:
    """A fix-pending red waiting on another red's commit (RED_WAITS_ON, above): resolved, or left waiting."""
    d = it["data"]
    sha, other = f.get("sha") or "", f.get("red")
    o = red_of_key(store, other)
    landed = _waits_on_landed(store, it, sha, other, ((o or {}).get("data") or {}).get("fix") or {}, repo, t)
    if landed:
        return landed
    why = _waits_on_ended(store, d, o, other, sha)
    if why:
        h = {"at": iso_at(t), "holder": f"waits-on:{other}", "outcome": f"{why}: offered again"}
        red_offer(store, it, f"{red_what(d)}: {h['outcome']}", data={"fix": None, "history": (d.get("history") or []) + [h]})
        return [f"re-offered {red_what(d)} ({why[:80]})"]
    if t >= it["deadline"]:  # still waiting while the other red is open: its own holder works on it; said quietly
        red_move(store, it, "fix-pending", f"{red_what(d)}: waits on red {other}'s fix {sha[:10]} (not on main yet)",
                 severity="info")
    return []

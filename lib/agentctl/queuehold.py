# agentctl, part `queuehold`: the records of why a queued change waits (its hold on its item) and when a hold is stuck, broken or recovers itself
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from contextlib import suppress
from typing import Any, Optional
from agentctl.statemachines import StaleVersion, machine_state
from agentctl.holdgraph import cycle_breaker, cycle_text, hold_cycles, hold_record, recorded_hold_edges
from agentctl.knobs import env_num
from agentctl.core import fmt_age
from agentctl.waitingdir import save_entry, wlog
from agentctl.policy import HOLDS, hold_kind


# A HELD WAIT IS NO ESCALATION (2026-11-19: phase3-agreement, -f2 and -enrichment2 each escalated to the owner every
# 15 min -- "queued for 45 min, waiting on a check slot" -- while each was held behind an older change it overlaps:
# F2 behind agreement on coupling-baseline.tsv, E2 behind F2 on TestWiring.scala). `queued`'s deadline loops into
# itself, and every lap raised the level and notified. Now each tick records WHY a queued change waits on its item
# (`hold`: the overlap and its blocking change and files, the batch, every check tree busy, the governor) -- `land
# status` and `status` show it -- and a lap past the deadline is QUIET while that hold is live: its blocker still in
# flight (whose own machine escalates should IT stick), a batch or busy trees (their holders' deadlines bound them).
# It escalates only when the hold is stuck: nothing named holds it, its blocking change is gone, or it is older than
# CHANGE_HOLD_MAX (the `waiting` state's own bound).
CHANGE_HOLD_MAX = 6 * 3600  # s ($AGENTCTL_CHANGE_HOLD_MAX)


# NO HOLD YET IS NO STUCK HOLD (2026-11-23 04:19Z/04:44Z: py-restore-corpus, checked, was pushed by the next tick 40 s
# and 27 s later; W1 read it in between as "nothing holds it, yet it has not pushed"). A change that has just entered
# its state has no recorded hold until a tick pushes, starts or holds it: past this grace with none, it is stuck.
UNHELD_GRACE = 10 * 60  # s ($AGENTCTL_UNHELD_GRACE)


def note_hold(st: Any, e: Any, kind: Any, why: Any, t: Any, blocker: Any = None, files: Any = None, batch: Any = None) -> None:
    """Record why queued change e waits (None: nothing holds it now), on its item, when that changed. kind is a row of
    THE HOLD TABLE (policy.py HOLDS): an unregistered kind is refused (ValueError), so no wait goes unbounded."""
    if kind is not None:
        hold_kind(kind)
    it = st.item("change", e["id"])
    if not it or machine_state("change", it["state"]).terminal:
        return
    cur = (it.get("data") or {}).get("hold") or {}
    if (cur.get("why") or None) == why:
        return
    rec = hold_record(cur, kind, why, blocker, files, batch, t)
    with suppress(StaleVersion):  # the next tick records it
        st.update_item("change", e["id"], it["version"], {"hold": rec})


def break_hold_cycles(st: Any, d: Any, todo: Any) -> Any:
    """A HOLD CYCLE IS BROKEN, NEVER WAITED OUT (holdgraph.py): every cycle among the holds recorded on the open
    changes' items, its oldest change let go of its wait on its blocker in it -- for good (`cycle_ignore` on its
    entry) --, the cycle said on that change's item (`cycle`, shown by `status`) and in the log. Returns the lines."""
    by_id = {e["id"]: e for e in todo}
    edges, why, items = recorded_hold_edges(st, todo)
    did = []
    for cyc in hold_cycles(edges):
        brk = cycle_breaker(cyc, {x: by_id[x]["seq"] for x in cyc})
        did.append(cycle_text(cyc, brk, why))
        break_cycle_at(st, d, by_id[brk], items[brk], edges[brk], did[-1])
    return did


def break_cycle_at(st: Any, d: Any, e: Any, it: Any, blocker: Any, text: Any) -> None:
    """Change e lets go of its wait on blocker for good (cycle_ignore), the cycle said on its item and in the log."""
    e["cycle_ignore"] = sorted(set(e.get("cycle_ignore") or []) | {blocker})
    save_entry(d, e)
    wlog(d, text)
    try:
        st.transition("change", e["id"], it["state"], it["version"], data={"cycle": text, "hold": None},
                      event={"kind": "change.cycle", "text": text, "severity": "info",
                             "audience": {"worktree": e.get("worktree")}})
    except StaleVersion:
        pass  # its entry carries the break: the hold is re-derived without that wait all the same


def restart_hold(st: Any, e: Any, t: Any) -> Any:
    """The recorded hold of change e, its clock (since) restarted at t: re-derived this tick (stuck_recovery)."""
    it = st.item("change", e["id"])
    hold = ((it or {}).get("data") or {}).get("hold")
    if not hold:
        return
    try:
        st.update_item("change", e["id"], it["version"], {"hold": {**hold, "since": t}})
    except StaleVersion:
        pass


def unheld_stuck(it: dict[str, Any], t: float) -> Optional[str]:
    """Why a change with no recorded hold is stuck, or None within UNHELD_GRACE in its state (NO HOLD YET IS NO STUCK
    HOLD: a tick pushes, starts or holds it)."""
    grace = env_num("AGENTCTL_UNHELD_GRACE", UNHELD_GRACE)
    if grace > 0 and t - float(it.get("entered_at") or t) < grace:
        return None
    return "nothing holds it, yet it has not " + ("pushed" if it["state"] == "checked" else "started")


def change_hold_stuck(st: Any, hold: Any, it: Any, t: Any) -> Any:
    """Why a queued or checked change's hold is stuck (an escalation), or None (a legitimate wait: quiet)."""
    if not hold:
        return unheld_stuck(it, t)
    if hold_recovers_itself(hold):
        return None  # its own recovery runs at its bound (regen_hold_overdue): never a stuck lap, the owner told once
    if t - float(it.get("entered_at") or t) > env_num("AGENTCTL_CHANGE_HOLD_MAX", CHANGE_HOLD_MAX):
        return f"held for over {fmt_age(env_num('AGENTCTL_CHANGE_HOLD_MAX', CHANGE_HOLD_MAX))}"
    return _hold_blocker_or_bound_stuck(st, hold, t)


def _hold_blocker_or_bound_stuck(st: Any, hold: Any, t: Any) -> Any:
    """Why a hold is stuck on its blocking change or past its kind's bound, or None."""
    if hold.get("blocker"):
        b = st.item("change", hold["blocker"])
        if b is None or machine_state("change", b["state"]).terminal:
            return f"its blocking change {hold['blocker']} is {'gone' if b is None else b['state']}"
    hk = HOLDS.get(hold.get("kind"))
    if hk and hold.get("since") and t - float(hold["since"]) > hk.bound:
        return f"held on {hk.label} for over {fmt_age(hk.bound)} (that hold's bound)"
    return None


def hold_recovers_itself(hold: Any) -> bool:
    """A hold whose kind's recovery is `recover` (THE HOLD TABLE): the change tick runs it at the bound itself."""
    hk: Any = HOLDS.get((hold or {}).get("kind"))
    return bool(hk) and hk.recovery == "recover"


def stuck_recovery(hold: Any, stuck: Any) -> Any:
    """What the change tick does with a stuck hold (THE HOLD TABLE's recovery): `rederive` -- the recorded hold is
    dropped and this tick decides afresh -- when its blocker is gone or its kind's own bound passed and the table says
    so; else `escalate` (nothing recorded, the 6-h cap: the wait is real, or agentctl's bug)."""
    if not hold or not stuck or stuck.startswith("held for over"):
        return "escalate"
    if stuck.startswith("its blocking change"):
        return "rederive"
    hk = HOLDS.get(hold.get("kind"))
    return hk.recovery if hk else "escalate"

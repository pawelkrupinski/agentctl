# agentctl, part `landhold`: the land hold (`land hold|release`): an item in the store, so ci-runner's reconciler honours a hold made on the laptop
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import time
from agentctl.clock import epoch_now
import os
from typing import Any
from agentctl.fixerrecords import agent_name
from agentctl.statemachines import ARCHIVE, MACHINES, StaleVersion, StateSpec, TerminalState


LAND_HOLD_DEFAULT = 4 * 3600  # s a `land hold` lasts unless --for says otherwise: it then lifts itself (expired)


LAND_HOLD_MAX = 24 * 3600     # s: the longest --for a hold takes


# the land hold (`land hold|release`): an item in the store, so ci-runner's reconciler honours a hold made on the laptop
# (the PAUSED file it replaced was the laptop's own, read by no tick since the phase 2 cutover). Its deadline is its
# expiry: a forgotten hold lifts itself (`expired`), never a manual-ack-only state.
MACHINES["hold"] = {
    "held": StateSpec(LAND_HOLD_DEFAULT, "expired", ("held", "released", "expired")),
    "released": StateSpec(ARCHIVE, "released", (), terminal=True),
    "expired": StateSpec(ARCHIVE, "expired", (), terminal=True),
}


def hold_by() -> str:
    """Who holds or releases: the agent's name, else the user and host."""
    try:
        name = agent_name()
    except (Exception, SystemExit):
        name = None
    return name or f"{os.environ.get('USER', '?')}@{os.uname().nodename.split('.')[0]}"


def land_hold_set(st: Any, secs: int, why: str | None, by: str) -> Any:
    """Hold, or extend the hold in force to now + secs. Returns the hold item. ONE HOLD (2026-11-26): "make it unless
    one is in force" is one store transaction (Store.open_singleton), so two holders at once -- the laptop and
    ci-runner -- never make two holds; the one that finds a hold extends it."""
    secs = max(60, min(int(secs), LAND_HOLD_MAX))
    data = {"by": by, "why": why or "", "at": epoch_now()}
    land_hold(st)  # a hold past its deadline expires first (its event says so)
    for _ in range(2):
        it = _hold_or_extend(st, data, secs, why, by)
        if it is not None:
            return it
    return _hold_or_extend(st, data, secs, why, by, last=True)


def _hold_or_extend(st: Any, data: dict[str, Any], secs: int, why: str | None, by: str, last: bool = False) -> Any:
    """The hold made or extended, or None when the hold found expired before it was extended (review 2026-11-26:
    move_held's transition out of `expired` raised TerminalState, `land hold` died with no hold in force): try again.
    last: that raises."""
    it, new = st.open_singleton("hold", "held", data, "hold_seq", "land#", deadline_s=secs)
    if new and secs == LAND_HOLD_DEFAULT:
        return it
    said = f"by {by} for {secs // 60} min" if new else f"extended by {by}"  # the maker's own expiry, or an extension
    try:
        move_held(st, it, data, secs, f"land hold {it['id']} {said}: {why or 'no reason given'}")
    except TerminalState:
        if last:
            raise
        return None
    return st.item("hold", it["id"])


def move_held(st: Any, it: dict[str, Any], data: dict[str, Any], secs: int, text: str) -> None:
    """The hold in force re-deadlined to now + secs, the owner's move, `text` its event. The other holder's own move may
    land between this holder's read and its write -- the maker giving the hold its expiry, or the other extending it
    first -- so the version is then stale: read it again and move (2026-11-26: the extender's stale read lost its extension, 70cab8f; then the
    MAKER's stale read raised StaleVersion in its thread and lost its expiry, #289 / #292, test_seq_atomic)."""
    for attempt in range(3):
        try:
            st.transition("hold", it["id"], "held", it["version"], data=data, deadline_s=secs,
                          event={"cause": "owner", "text": text})
            return
        except StaleVersion:
            if attempt == 2:
                raise
            it = st.item("hold", it["id"])


def land_hold_release(st: Any, by: str) -> list[str]:
    """Release every hold in force. Returns their ids."""
    out = []
    for it in land_holds(st):
        try:
            st.transition("hold", it["id"], "released", it["version"], data={"released_by": by},
                          event={"cause": "owner", "text": f"land hold {it['id']} released by {by}"})
            out.append(it["id"])
        except (StaleVersion, TerminalState):
            pass
    return out


def tick_land_hold(st: Any) -> list[str]:
    """The reconciler's step: a hold past its deadline expires even while no batch ticks."""
    land_hold(st)
    return []


def land_holds(st: Any) -> list[dict[str, Any]]:
    return st.items("hold", ["held"])


def land_hold(st: Any, now_: float | None = None, expire: bool = True) -> dict[str, Any] | None:
    """The land hold in force, or None. A hold past its deadline is moved to `expired` here (an event says so): every
    reader -- the tick, `land status`, the snapshot -- lifts a forgotten hold the moment it is due. expire=False: only
    read (the status page never writes)."""
    t = now_ if now_ is not None else epoch_now()
    live = None
    for it in land_holds(st):
        if it["deadline"] <= t:
            if expire:
                expire_land_hold(st, it)
        elif live is None or it["deadline"] > live["deadline"]:
            live = it
    return live


def expire_land_hold(st: Any, it: dict[str, Any]) -> None:
    """A hold past its deadline moved to `expired`, an event saying who held it and why (one already moved: left)."""
    try:
        st.transition("hold", it["id"], "expired", it["version"],
                      event={"cause": "timeout", "text": f"land hold {it['id']} expired (held by {it['data'].get('by')}: "
                                     f"{it['data'].get('why') or 'no reason given'})"})
    except (StaleVersion, TerminalState):
        pass


def hold_text(h: Any, t: float | None = None) -> str:
    """`HELD until HH:MM (by, why)` for a hold row ({deadline, data} or the snapshot's {until, by, why})."""
    until = h.get("until") or h.get("deadline")
    d = h.get("data") or h
    return (f"HELD until {time.strftime('%H:%M', time.localtime(float(until)))} (`agentctl land release`; "
            f"by {d.get('by') or '?'}" + (f": {d['why']}" if d.get("why") else "") + ")")


def hold_row(it: dict[str, Any] | None) -> dict[str, Any] | None:
    return {"id": it["id"], "until": it["deadline"], "by": it["data"].get("by"), "why": it["data"].get("why")} if it else None

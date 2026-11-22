# agentctl, part `landhold`: the land hold (`land hold|release`): an item in the store, so ci-runner's reconciler honours a hold made on the laptop
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import time
from typing import Any
from agentctl.store import open_store
from agentctl.fixerrecords import agent_name
from agentctl.queue import land_hold, land_holds
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
    """Hold, or extend the hold in force to now + secs. Returns the hold item."""
    secs = max(60, min(int(secs), LAND_HOLD_MAX))
    cur = land_hold(st)
    data = {"by": by, "why": why or "", "at": time.time()}
    if cur:
        st.transition("hold", cur["id"], "held", cur["version"], data=data, deadline_s=secs,
                      event={"text": f"land hold {cur['id']} extended by {by}: {why or 'no reason given'}",
                             "dedupe": f"hold:{cur['id']}:extend:{cur['version'] + 1}"})
        return st.item("hold", cur["id"])
    n = int(st.meta_get("hold_seq", 0) or 0) + 1
    st.meta_set("hold_seq", n)
    it = st.put_item("hold", f"land#{n}", "held", data)
    if secs != LAND_HOLD_DEFAULT:  # its own expiry: a self-transition re-deadlines it
        st.transition("hold", it["id"], "held", it["version"], deadline_s=secs,
                      event={"text": f"land hold {it['id']} by {by} for {secs // 60} min: {why or 'no reason given'}"})
        it = st.item("hold", it["id"])
    return it


def land_hold_release(st: Any, by: str) -> list[str]:
    """Release every hold in force. Returns their ids."""
    out = []
    for it in land_holds(st):
        try:
            st.transition("hold", it["id"], "released", it["version"], data={"released_by": by},
                          event={"text": f"land hold {it['id']} released by {by}"})
            out.append(it["id"])
        except (StaleVersion, TerminalState):
            pass
    return out


def tick_land_hold(st=None):
    """The reconciler's step: a hold past its deadline expires even while no batch ticks."""
    land_hold(st or open_store())
    return []

# agentctl, part `snapshot`: the tick's part of state.json: the open items, the tick's own time and code version
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import time
from typing import Any
from agentctl.selfcode import code_version
from agentctl.owned import start_rev
from agentctl.reverts import revert_brief
from agentctl.queue import hold_row, land_hold
from agentctl.changekind import HOLD_SHOWN
from agentctl.cistate import state_update
from agentctl.ask import owner_asks
from agentctl.queueclose import change_rows
from agentctl.statemachines import MACHINES, machine_state


def _open_items(st: Any, typ: str) -> list[Any]:
    """The store's items of machine `typ` not yet terminal."""
    return [i for i in st.items(typ) if typ in MACHINES and not machine_state(typ, i["state"]).terminal]


def _brief(i: Any) -> dict[str, Any]:
    """One open item as the snapshot shows it: its state, deadline, level, worktree and (a held one's) why."""
    data = i["data"] or {}
    hold = {"hold": data["hold"].get("why")} if i["state"] in HOLD_SHOWN and data.get("hold") else {}
    return {"id": i["id"], "state": i["state"], "deadline": i["deadline"], "level": i["level"],
            "worktree": data.get("worktree"), **hold}


def _briefs(st: Any, typ: str, brief: Any = _brief) -> list[Any]:
    """The briefs of machine `typ`'s open items; none for a machine this build has not got."""
    return [brief(i) for i in _open_items(st, typ)] if typ in MACHINES else []


def write_snapshot(d: str, store: Any) -> None:
    """The tick's part of state.json: the open items and the tick's own time and code version."""
    st = store
    rows = change_rows(st)  # ONE QUEUE: the store's items, never a host's entry files
    state_update(tick_at=epoch_now(), version=code_version(), rev=start_rev(),
                 changes=[_brief(i) for i in _open_items(st, "change")], batch=[_brief(i) for i in _open_items(st, "batch")],
                 reds=_briefs(st, "red"), conditions=_briefs(st, "condition"),
                 reverts=_briefs(st, "revert", revert_brief),
                 asks=owner_asks(st) if "red" in MACHINES else [],  # THE OWNER'S PENDING DECISIONS (ask.py)
                 hold=hold_row(land_hold(st)),
                 waiting=[r["worktree"] for r in rows if r["state"] == "waiting"],
                 queued=[r["worktree"] for r in rows if r["state"] != "waiting"])

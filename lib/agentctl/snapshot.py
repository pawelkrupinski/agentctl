# agentctl, part `snapshot`: the tick's part of state.json: the open items, the tick's own time and code version
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import time
from typing import Any
from agentctl.store import MACHINES, machine_state, open_store
from agentctl.selfcode import code_version
from agentctl.owned import start_rev
from agentctl.reverts import revert_brief
from agentctl.queue import hold_row, land_hold
from agentctl.changekind import HOLD_SHOWN
from agentctl.cistate import state_update
from agentctl.ask import owner_asks
from agentctl.queueclose import change_rows


def write_snapshot(d: str, store: Any = None) -> None:
    """The tick's part of state.json: the open items and the tick's own time and code version."""
    st = store or open_store()
    open_ = lambda typ: [i for i in st.items(typ) if typ in MACHINES and not machine_state(typ, i["state"]).terminal]
    brief = lambda i: {"id": i["id"], "state": i["state"], "deadline": i["deadline"], "level": i["level"],
                       "worktree": (i["data"] or {}).get("worktree"),
                       **({"hold": ((i["data"] or {}).get("hold") or {}).get("why")}
                          if i["state"] in HOLD_SHOWN and (i["data"] or {}).get("hold") else {})}
    rows = change_rows(st)  # ONE QUEUE: the store's items, never a host's entry files
    state_update(tick_at=time.time(), version=code_version(), rev=start_rev(),
                 changes=[brief(i) for i in open_("change")], batch=[brief(i) for i in open_("batch")],
                 reds=[brief(i) for i in open_("red")] if "red" in MACHINES else [],
                 conditions=[brief(i) for i in open_("condition")] if "condition" in MACHINES else [],
                 reverts=[revert_brief(i) for i in open_("revert")] if "revert" in MACHINES else [],
                 asks=owner_asks(st) if "red" in MACHINES else [],  # THE OWNER'S PENDING DECISIONS (ask.py)
                 hold=hold_row(land_hold(st)),
                 waiting=[r["worktree"] for r in rows if r["state"] == "waiting"],
                 queued=[r["worktree"] for r in rows if r["state"] != "waiting"])

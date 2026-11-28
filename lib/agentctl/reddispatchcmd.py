# agentctl, part `reddispatchcmd`: `agentctl red dispatch <run>`: its reds offered at top priority and the dispatcher run now
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
from agentctl.repos import scan_repo_for
from agentctl.context import Context
from agentctl.fixerblockers import dispatch_blocker
from agentctl.redrecord import red_what
from agentctl.red import red_put_offer
from agentctl.redopen import red_offer
from agentctl.redclaim import red_targets
from agentctl.offerdispatch import red_dispatcher


def red_cmd_dispatch(a: Any, st: Any, o_r: Any, *, ctx: Context) -> Any:
    """`red dispatch <run>`: its reds offered at top priority, the dispatcher run now."""
    b = dispatch_blocker()
    if b:
        print(f"NOT DISPATCHED: {b}")
        return 2
    items = red_targets(st, o_r, a.run, ctx=ctx)
    if not items:
        print(f"NOT DISPATCHED: #{a.run} names no red on main")
        return 1
    for it in items:
        offer_by_hand(st, it)
    repo = scan_repo_for(o_r, getattr(a, "repo", None) and os.path.abspath(a.repo))
    lines = red_dispatcher(st, o_r, repo, st.clock(), ctx.caches.status.blocker)
    print("\n".join(lines) if lines else "NOT DISPATCHED: " + "; ".join(
        (st.offer_of(f"red:{it['id']}") or {}).get("reason") or it["state"] for it in items))
    return 0 if lines else 1


def offer_by_hand(st: Any, it: Any) -> Any:
    """One red offered at top priority (the dispatcher takes it now)."""
    if it["state"] == "red":
        it = red_offer(st, it, f"{red_what(it['data'])}: dispatched by hand")
    store_it = st.item("red", it["id"])
    st.update_item("red", it["id"], store_it["version"], {"priority": -1, "author_tried": True})
    if it["state"] == "offered":
        red_put_offer(st, st.item("red", it["id"]))

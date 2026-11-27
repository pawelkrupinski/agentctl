# agentctl, part `storemetrics`: the store's Prometheus text: the ages and counts its alerts read
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.context import Context
from agentctl.statemachines import MACHINES, machine_state, open_items
from agentctl.tickknobs import knob_tick
from agentctl.core import err
from agentctl.cistate import state_read
from agentctl.redrecord import red_lease
from agentctl.redhold import red_unheld


def metrics_text(st, now_=None, *, ctx):
    """The Prometheus text the module's agentctl-metrics copies to node-exporter (agentctl.rules' gauges)."""
    t = now_ if now_ is not None else st.clock()
    two = 2 * knob_tick()
    rows = [("agentctl_reconciler_tick_timestamp", "unix time the last tick finished", float(state_read().get("tick_at") or 0)),
            ("agentctl_oldest_change_age_seconds", "age of the oldest non-terminal change, not counting red main time",
             oldest_change_age(st, t)),
            ("agentctl_red_unleased_seconds", "longest a red on main has had no lease, fix, block or offer",
             longest_unleased_red(st, t, ctx=ctx)),
            ("agentctl_leases_expired_unreaped", "leases past their TTL by more than 2 ticks with their item unmoved",
             unreaped_leases(st, t, two)),
            ("agentctl_items_past_deadline", "items past their state's deadline by more than 2 ticks",
             items_past_deadline(st, t, two))]
    out = []
    for name, help_, v in rows:
        out += [f"# HELP {name} {help_}.", f"# TYPE {name} gauge", f"{name} {float(v):.0f}"]
    return "\n".join(out) + "\n"


def oldest_change_age(st: Any, t: float) -> float:
    """The age of the oldest non-terminal change, not counting the time main was red (the red accumulator's growth)."""
    acc = float(st.meta_get("red_accum", 0) or 0)
    ages = []
    for it in open_items(st, "change"):
        dd = it.get("data") or {}
        since = float(dd.get("submitted_at") or it["entered_at"])
        acc0 = dd.get("red_accum0")
        ages.append(max(0.0, (t - since) - max(0.0, acc - float(acc if acc0 is None else acc0))))
    return max(ages) if ages else 0


def longest_unleased_red(st: Any, t: float, *, ctx: Context) -> float:
    """The longest a red on main has had no lease, fix, block or offer (0 when the store cannot say)."""
    unleased = 0.0
    try:
        for it in red_unheld(st, ctx=ctx):
            unleased = max(unleased, t - float(it.get("entered_at") or t))
    except Exception as e:
        err(f"the unleased red age could not be read ({str(e)[:160]}); 0")
    return unleased


def unreaped_leases(st: Any, t: float, two: float) -> int:
    """How many leased reds and checking changes hold a lease that expired more than `two` s ago with the item unmoved."""
    unreaped = 0
    for typ, state, lease in (("red", "leased", lambda i: red_lease(i["data"]["key"])),
                              ("change", "checking", lambda i: f"check/{i['id']}")):
        for it in st.items(typ, [state]):
            try:
                row = st.get(lease(it))
            except (KeyError, TypeError):
                continue
            if row and row["expires_at"] < t - two and row["expires_at"] > it["entered_at"]:
                unreaped += 1
    return unreaped


def items_past_deadline(st: Any, t: float, two: float) -> int:
    """How many non-terminal items are past their state's deadline by more than `two` s."""
    return sum(1 for typ in MACHINES for it in st.items(typ)
               if not machine_state(typ, it["state"]).terminal and it["deadline"] < t - two)

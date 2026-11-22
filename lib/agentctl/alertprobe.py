# agentctl, part `alertprobe`: whether an open alert still stands: its red, condition or gate probed, cleared when what it said is over
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
from agentctl.core import cherry_landed, env_num, remote
from agentctl.timefmt import iso_epoch
from agentctl.statefile import read_record
from agentctl.inboxentry import CONDITION_KINDS
from agentctl.fixersession import TRUST_ASK_PREFIX, claude_trust_gap
from agentctl.cistate import state_path
from agentctl.statemachines import MACHINES, machine_state
from agentctl.redrecord import red_what
from agentctl.leases import red_block_of
from agentctl.conditions import open_conditions
from agentctl.redask import red_ask
from agentctl.alertroute import (
    ALERT_OWNER_KINDS, ALERT_RED_KINDS, alert_condition_audience, alert_kind_of, alert_red_audience)


# ---------------------------------------------------------------- the probes: each live alert closes itself
def alert_probe(st, item, e, t, ctx=None):
    """(closed why or None, the audience now or None): what the item's own state says of its live alert."""
    if item.startswith("red:"):
        it = st.item("red", item[4:])
        if it is None:
            return "its red is gone", None
        if machine_state("red", it["state"]).terminal:
            return f"its red is {it['state']}", None
        kind = alert_kind_of(e)
        if kind == "BLOCKED" and it["state"] != "blocked-external":  # WAITING ON YOU, but its red waits on nobody now
            return f"its red is {it['state']} now (no longer waiting on you)", None  # (resumed, decided, its fix landed)
        if kind == "BLOCKED" and red_ask(it, red_block_of(it, st)) is None:
            # AN ASK IS RETRACTED WITH ITS CONDITION (2026-11-19: `decide vault-movies here`, run off the inbox and the
            # owner-decisions hook, said "no red waits on the movies vault"): its block lapsed or no longer asks the
            # owner, while the red still read blocked-external for a tick -- closed now, not when the red moves on
            return "its question is no longer asked (its block lapsed or waits on no owner action)", None
        if kind in ("RED_UNCLAIMED", "RED_STARVED") and it["state"] != "offered":
            return f"its red is {it['state']} now (taken)", None
        if kind in ALERT_RED_KINDS:
            return None, alert_red_audience(e, it, st, t)
        return None, None
    if item.startswith("condition:"):
        key = item[len("condition:"):]
        cond = next((c for c in open_conditions(st) if c.get("key") == key or c.get("id") == key), None)
        if cond is None:
            return "its condition closed", None
        kind = alert_kind_of(e)
        if kind in CONDITION_KINDS and kind not in ALERT_OWNER_KINDS:
            return None, alert_condition_audience(kind, e, cond.get("opened_at"), t)
        return None, None
    if item.startswith("change:"):
        it = st.item("change", item[len("change:"):])
        if it and it["state"] in ("landed", "superseded", "cancelled"):
            return f"its change is {it['state']}", None
        return None, None
    if item.startswith(TRUST_ASK_PREFIX):  # the owner was asked to trust a clone (fixersession.py trust_asks)
        path = item[len(TRUST_ASK_PREFIX):]
        return ("claude trusts it now" if not claude_trust_gap(path) else None), None
    if item.startswith("wt:"):
        wt = item[3:].split(":", 1)[0]
        if wt and os.path.isabs(wt) and not os.path.isdir(wt):
            return "its worktree is gone", None
    if item.startswith("revert-owner:"):  # a revert notice's orchestrator / escalation alert (reverts.py)
        item = "revert:" + item[len("revert-owner:"):].split("/", 1)[0]
    typ, _, iid = item.partition(":")
    if typ in MACHINES and iid:
        # ANY STORE ITEM WITH A STATE (2026-11-19: issue 55799d's escalation stayed unread a day after the issue was
        # closed -- no probe knew `issue:`): an issue, a hold, an explorer flip, a branch red, a batch... -- its alert
        # retires once the item is terminal or gone from the store
        it = st.item(typ, iid)
        if it is None:
            return f"its {typ} is gone", None
        if machine_state(typ, it["state"]).terminal:
            return f"its {typ} is {it['state']}", None
    return alert_probe_cleared(st, item, e, t, ctx if ctx is not None else {}), None


# ---------------------------------------------------------------- an owner alert whose condition has cleared
# The owner, 2026-11-06: "why is there always 30 unread in inbox" -- 44 owner alerts, nearly all for what had cleared
# days before: 20 RED BLOCKED ON OWNER whose reds were closed, 17 UNLANDED ORPHAN of Forgejo-era worktrees, 3 NETWORK
# long recovered, 3 WATCH UNKNOWN whose commits later runs had decided, a HELD batch that moved on. Their items were
# keyed before the probes knew them (`run:`, `text:`, `orphan:`), so nothing ever closed them. Now each such alert's
# CONDITION is probed from its kind and verdict, whatever its item, and closed with the reason once it has cleared.
# What cannot be told (no red named, no gate read, git failing) stays as it is: never closed on a guess.
ALERT_ORPHAN_PROBE_EVERY = 600  # s between git probes of one orphan's worktree ($AGENTCTL_ALERT_ORPHAN_PROBE)


ALERT_NETWORK_GRACE = 600       # s a NETWORK alert stays before "no NETWORK condition open" closes it


ALERT_RED_PROBED = {"BLOCKED", "RED_BUDGET", "DIAGNOSED", "FIXER_FAILED", "FIX_FAILED", "FIX_UNVERIFIED",
                    "FIX_NOT_COUNTED", "RED_DISPATCHED", "LAUNCH_FAILED", "RED_FLAKE", "RED_AUTHOR", "RED_REVERT",
                    "RED_BLAME", "RED_RELAND"} | ALERT_RED_KINDS


def alert_ctx_get(ctx, name, load):
    """One read per reconcile pass of what several alerts' probes ask (the open reds, conditions, batches, the gate)."""
    if name not in ctx:
        ctx[name] = load()
    return ctx[name]


def alert_open_reds(st):
    return st.items("red", [s for s, spec in MACHINES["red"].items() if not spec.terminal])


def alert_red_named(d, verdict):
    """Does verdict name red d: its `decide <key>`, or `<workflow> #<run>` for ANY run the red carries -- one red item
    spans every run failing its way (red_attach_obs moves last_run on), and an alert names the run it was written
    against (review 2026-11-07: a FIX FAILED for #2128 closed as 'its red is closed' once the open red was at #2140)."""
    key = str(d.get("key") or "")
    if key and re.search(r"\bdecide " + re.escape(key) + r"\b", verdict):
        return True
    prefix = red_what(d).split(" [", 1)[0].rsplit(" #", 1)[0]  # '[<repo> ]<workflow>'
    nums = {str((d.get("last_run") or {}).get("number") or "")} | {str(r) for r in d.get("runs") or []}
    return any(re.search(re.escape(f"{prefix} #{n}") + r"(?!\d)", verdict) for n in nums if n and n != "None")


def alert_gate():
    return ((read_record(state_path()) or {}).get("ci") or {}).get("gate") or []


def alert_gate_decided_after(gate, at):
    """True when every gate workflow has a run decided that was created after `at` (main is linear: it covers every
    commit landed before); None when the gate snapshot (alert_gate) cannot be read."""
    if not gate:
        return None
    for g in gate:
        created = ((g.get("decided") or {}).get("createdAt")) or ""
        c = iso_epoch(created)
        if c is None:
            return False
        if c <= at:
            return False
    return True


def alert_probe_cleared(st, item, e, t, ctx):
    """Why the alert's condition has cleared, or None (still live, or nothing to tell by)."""
    kind, v = alert_kind_of(e), str(e.get("verdict") or "")
    at = float(e.get("first_at") or e.get("at") or t)
    if kind in ALERT_RED_PROBED and not item.startswith("red:"):
        if not (re.search(r" #\d+", v) or re.search(r"\bdecide \w+", v)):
            return None
        reds = alert_ctx_get(ctx, "reds", lambda: alert_open_reds(st))
        return None if any(alert_red_named(r["data"], v) for r in reds) else "its red is closed (no open red names it)"
    if kind == "UNLANDED_ORPHAN" or item.startswith("orphan:"):
        m = re.search(r"(~?/\S+)", item[len("orphan:"):] if item.startswith("orphan:") else v)
        if not m:
            return None
        wt = os.path.expanduser(m.group(1).rstrip(":,;"))
        if not os.path.isdir(wt):
            return "its worktree is gone"
        probed = ctx.setdefault("probed", {})
        if t - float(probed.get(item) or 0) < env_num("AGENTCTL_ALERT_ORPHAN_PROBE", ALERT_ORPHAN_PROBE_EVERY):
            return None
        probed[item] = t
        commits, left = cherry_landed(wt, f"refs/remotes/{remote()}/main", "HEAD")
        return "its commits are on main" if commits is not None and not left else None
    if v.startswith("WATCH UNKNOWN") or item.startswith("watch:"):
        gate = alert_ctx_get(ctx, "gate", alert_gate)
        return "a later gate run has decided (its commit is covered)" if alert_gate_decided_after(gate, at) else None
    if kind == "NETWORK":
        if t - at < env_num("AGENTCTL_ALERT_NETWORK_GRACE", ALERT_NETWORK_GRACE):
            return None
        conds = alert_ctx_get(ctx, "conditions", lambda: open_conditions(st))
        return None if any(c.get("kind") == "NETWORK" for c in conds) else "the network is back (no NETWORK condition open)"
    if v.startswith("HELD:") or item.startswith("batch:"):
        held = alert_ctx_get(ctx, "held", lambda: st.items("batch", ["held"]))
        m = re.match(r"HELD: the batch check of ([0-9a-f]{7,})", v)
        bid = item[len("batch:"):] if item.startswith("batch:") else (re.match(r"HELD: the batch (\S+) ", v) or [None, None])[1]
        if m:
            live = any(str((b["data"] or {}).get("tip") or "").startswith(m.group(1)) for b in held)
        elif bid:
            live = any(b["id"] == bid for b in held)
        else:
            return None
        return None if live else "its batch moved on (no longer held)"
    return None

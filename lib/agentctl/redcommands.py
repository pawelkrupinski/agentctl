# agentctl, part `redcommands`: the `red` subcommands' handlers: block, claim, auto, dispatch and the rest
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from agentctl.core import reds_dir
from agentctl.timefmt import ts
from agentctl.repos import scan_repo_for
from agentctl.worktrees import claimant
from agentctl.dispatchblock import _blocker_cache, dispatch_blocker
from agentctl.redrecord import red_what
from agentctl.redmove import red_put_offer
from agentctl.redopen import red_offer
from agentctl.redclaim import bind_fixer_session, red_beat, red_claim, red_release, red_targets
from agentctl.blamerecords import blame_withdraw
from agentctl.redvault import red_block, red_owner_push, red_unblock
from agentctl.reddispatch import repo_dispatch_off_all
from agentctl.redview import repo_dispatch_set
from agentctl.redstatus import red_status
from agentctl.offerdispatch import red_dispatcher


def red_cmd_block(a, st, o_r):
    return red_block(st, o_r, a)


def red_cmd_unblock(a, st, o_r):
    rc, lines = red_unblock(st, o_r, a.run, a.why)
    print("\n".join(lines))
    return rc


def red_cmd_owner_push(a, st, o_r):
    rc, lines = red_owner_push(st, o_r, a.run, a.note or "", getattr(a, "worktree", None))
    print("\n".join(lines))
    return rc


def red_cmd_lease(a, st, o_r):
    """`red claim|beat|release`: the lease's line(s) and exit code."""
    if a.rcmd == "claim":
        if not claimed_by_its_fixer(a):
            return 2
        rc, lines = red_claim(st, o_r, a.run, a.note or "", getattr(a, "worktree", None))
    elif a.rcmd == "beat":
        rc, lines = red_beat(st, o_r, a.run, a.note or "")
    else:
        rc, lines = red_release(st, o_r, a.run, a.any)
    print("\n".join(lines))
    return rc


def claimed_by_its_fixer(a):
    """The dispatched fixer says who it is (its prompt): its record must be a live one of this session (bound to it).
    False, said, when it is not; True for a claim naming no fixer."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if getattr(a, "fixer", None) and sid and not bind_fixer_session(sid, os.path.realpath(a.fixer)):
        print(f"NOT CLAIMED: {a.fixer} is no live fixer record of this session ({sid[:8]})")
        return False
    return True


def red_cmd_status(a, st, o_r):
    return red_status(st, getattr(a, "verbose", False), getattr(a, "live", False))


def red_cmd_auto(a, st, o_r):
    """`red auto [on|off]`: the one repo's dispatch (the owner, 2026-11-19: "no bitcashier builds"), in the store: the
    server's tick reads it; else the automatic dispatch as a whole, a file."""
    if getattr(a, "repo", None):
        if a.state in ("on", "off"):
            repo_dispatch_set(st, o_r, a.state == "off", claimant())
        offs = repo_dispatch_off_all(st)
        print(f"dispatch off for {o_r} (owner, since {offs[o_r].get('at')})" if o_r in offs else
              f"dispatch on for {o_r}")
        return 0
    off = reds_dir("DISPATCH_OFF")
    os.makedirs(reds_dir(), exist_ok=True)
    if a.state == "off":
        open(off, "w").write(ts() + "\n")
    elif a.state == "on" and os.path.exists(off):
        os.unlink(off)
    _blocker_cache.clear()
    b = dispatch_blocker()
    print(f"automatic dispatch: {'ON' if not b else 'OFF -- ' + b}")
    return 0


def red_cmd_unblame(a, st, o_r):
    items = red_targets(st, o_r, a.run, create=False)
    if not items:
        print(f"NOT WITHDRAWN: #{a.run} names no red")
        return 1
    rc = 0
    for it in items:
        ok, line = blame_withdraw(st, it, a.why, scan_repo_for(o_r, None))
        print(line)
        rc = rc or (0 if ok else 1)
    return rc


def red_cmd_dispatch(a, st, o_r):
    """`red dispatch <run>`: its reds offered at top priority, the dispatcher run now."""
    b = dispatch_blocker()
    if b:
        print(f"NOT DISPATCHED: {b}")
        return 2
    items = red_targets(st, o_r, a.run)
    if not items:
        print(f"NOT DISPATCHED: #{a.run} names no red on main")
        return 1
    for it in items:
        offer_by_hand(st, it)
    repo = scan_repo_for(o_r, getattr(a, "repo", None) and os.path.abspath(a.repo))
    lines = red_dispatcher(st, o_r, repo, st.clock())
    print("\n".join(lines) if lines else "NOT DISPATCHED: " + "; ".join(
        (st.offer_of(f"red:{it['id']}") or {}).get("reason") or it["state"] for it in items))
    return 0 if lines else 1


def offer_by_hand(st, it):
    """One red offered at top priority (the dispatcher takes it now)."""
    if it["state"] == "red":
        it = red_offer(st, it, f"{red_what(it['data'])}: dispatched by hand")
    store_it = st.item("red", it["id"])
    st.update_item("red", it["id"], store_it["version"], {"priority": -1, "author_tried": True})
    if it["state"] == "offered":
        red_put_offer(st, st.item("red", it["id"]))

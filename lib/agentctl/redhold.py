# agentctl, part `redhold`: a red's fix held to its owner: the red-fix invariant (WR), who holds a red, the enforcement of an unheld one
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
import time
from agentctl.core import SHORT, env_num, fmt_age
from agentctl.store import StaleVersion, machine_state, open_store
from agentctl.repos import red_repo_of
from agentctl.policy import HOLDS
from agentctl.redrecord import RED_TERMINAL, lease_is_live, red_active, red_lease, red_what
from agentctl.leases import red_block_of
from agentctl.queueslots import checker_alive
from agentctl.redfixread import FIX_OPEN
from agentctl.fixchange import CHANGE_ENDED, red_change, red_change_read
from agentctl.redfixes import red_fix_of
from agentctl.changekind import HOLD_SHOWN, change_hold_visible, classify_change, kind_text, queue_facts
from agentctl.red import red_fix_giveup
from agentctl.redmove import red_put_offer
from agentctl.ciwhy import ci_state_load
from agentctl.redopen import red_offer
from agentctl.redverify import red_fix_since, red_fix_unverifiable
from agentctl.redpending import RED_FIX_REQUEUE_GRACE, RED_WAITS_ON
from agentctl.queuebatch import change_hold_stuck
from agentctl.reddispatch import repo_dispatch_off
from agentctl.redstep import red_lease_ended


WR_MAX = 40 * 60        # a red fix still unlanded this long after it was queued ($AGENTCTL_WR_MAX; a CI-tier check ~17 min)


# W1 AND WR READ THE TABLES (2026-11-20, the owner: "fix agentctl issues structurally"; W1 fired 18 times in three weeks,
# 13 of them on a change whose checks were simply running past 2 h -- a live checker lease, renewed on progress, its own
# bound 3 h -- each worked out by hand). A change is judged by its STATE's own table: `checking` with a live checker
# lease progresses; `queued` / `checked` waits on a recorded hold within that hold kind's bound (policy.py HOLDS), which
# the change tick re-derives itself when it goes stale (stuck_recovery); `waiting` waits for a red gate. Only a change
# that is past its bound with none of that -- what the reconciler's own recoveries did not fix -- violates, and so does
# any change past W1_HARD_FACTOR x the bound whatever it waits on (a real wait that long is the owner's to know).
W1_HARD_FACTOR = 3


def change_progress(st, it, t):
    """None when open change item `it` progresses as its state's table says (a live checker; a hold within its bound;
    waiting on a red gate is the caller's), else why not."""
    state = it["state"]
    if state in ("checking", "pushing"):
        return None if checker_alive(it["id"]) or state == "pushing" else "its checks run with no live checker"
    if state in HOLD_SHOWN:
        hold = change_hold_visible(st, it, {}, t)
        stuck = change_hold_stuck(st, hold, it, t)
        return stuck
    return None


def snap_reds(snap):
    """The names (numbers and ids) of the gate's red runs on main in a CI snapshot: each red gate workflow's newest
    decided run and the failed runs of its streak since its last green; None when there is no snapshot (unread)."""
    if not snap:
        return None
    out = set()
    for g in snap.get("gate") or []:
        d = g.get("decided") or {}
        if not d or d.get("conclusion") == "success":
            continue
        out |= {str(d.get("number")), str(d.get("databaseId"))}
        for r in g.get("runs") or []:
            if r.get("conclusion") == "success":
                break
            if r.get("conclusion") in ("failure", "timed_out"):
                out |= {str(r.get("number")), str(r.get("databaseId"))}
    return out - {"None"}


def inv_wr(store=None, reds=None, now_=None):
    """WR: A RED FIX NEVER WAITS FOR GREEN (the owner, 2026-11-16). No open change whose KIND (changekind.py: its
    `Fixes-Red:` against the gate's reds and every red the store has open) is a gate red's fix is on `waiting`, and no
    fix of any open red -- the gate's or one outside it (08010c, 2026-11-19) -- is still unlanded WR_MAX after it was
    submitted."""
    if reds is None:
        reds = snap_reds(ci_state_load(SHORT["movies"]))
    st = store or open_store()
    facts = queue_facts(reds or set(), st)
    if not reds and not facts["open"]:
        return True, "main green (or its gate unread) and no red open: no red fix to hold"
    t = now_ if now_ is not None else time.time()
    lim = env_num("AGENTCTL_WR_MAX", WR_MAX)
    bad = []
    for it in st.items("change"):
        data = it.get("data") or {}
        if machine_state("change", it["state"]).terminal:
            continue
        k = classify_change(data, facts, t)
        if not k["red_fix"]:
            continue
        age = t - float(data.get("submitted_at") or it.get("entered_at") or t)
        what = f"{it['id']} ({kind_text(k)})"
        hold = (data.get("hold") or {}) if it["state"] in HOLD_SHOWN else {}
        hk = HOLDS.get(hold.get("kind"))
        if it["state"] == "waiting" and k["gate_fix"]:
            bad.append(f"{what} is waiting for main to turn green")
        elif hk and not hk.red_fix_ok:  # THE HOLD TABLE: a red fix waits on its own resources only, never other work
            bad.append(f"{what} is held behind other work: {hold.get('why')}")
        elif age > lim and (change_progress(st, it, t) is not None or age > W1_HARD_FACTOR * lim):
            # its own check running, or its own tree busy, within bounds, is no wait behind anything
            bad.append(f"{what} is {it['state']} {fmt_age(age)} after it was queued"
                       + (f" ({change_progress(st, it, t)})" if change_progress(st, it, t) else ""))
    if not bad:
        return True, "no red fix waits"
    return False, "a red fix waits: " + "; ".join(bad[:3]) + " -- `agentctl land status`, `agentctl ci-why`"


def red_fix_held(it, t, facts=None, store=None):
    """A fix-pending red's holder, or None: its fix change open in the queue (or ended within RED_FIX_REQUEUE_GRACE:
    its worktree may queue it again, as red_fix_pending waits), or a fix landed / a verify run dispatched (or a gate run
    due) within its give-up of entering fix-pending. With facts the queue decides; without (the watchdog), the fix the
    item recorded."""
    d = it["data"]
    f = d.get("fix") or {}
    if f.get("status") == RED_WAITS_ON and f.get("red"):  # held by the red it waits on (red_waits_on_step resolves it)
        return f"waits on red {f['red']}'s fix {str(f.get('sha'))[:10]}"
    if store is not None and f.get("entry") and f.get("status") in FIX_OPEN:
        ch, unread = red_change_read(store, f["entry"])
        if unread:  # an unread store is no ended change: the fix stands as recorded
            return f"fix {f['entry']} {f.get('status')} (its change unread: {unread[:80]})"
        if ch and ch["state"] in CHANGE_ENDED and \
                t - float(ch.get("entered_at") or 0) < env_num("AGENTCTL_RED_FIX_REQUEUE_GRACE", RED_FIX_REQUEUE_GRACE):
            return f"fix {f['entry']} {ch['state']}: its worktree may queue it again"  # the red's own step waits it out
        if ch and ch["state"] not in CHANGE_ENDED and ch["state"] != "landed":
            # THE STORE'S QUEUE DECIDES AN OPEN CHANGE: the facts' entries are the queue's files, which only ci-runner
            # has; a change the store holds queued or checking is on its way whatever the facts read.
            return f"fix {f['entry']} {ch['state']}"
    if facts is not None and facts.get("queue_unread") and f.get("entry") and f.get("status") in FIX_OPEN:
        return f"fix {f['entry']} {f.get('status')} (the queue unread: {str(facts['queue_unread'])[:80]})"
    if facts is not None and not f.get("waited_on"):  # a waited-on fix that landed is no candidate of its own
        fx = red_fix_of(it, facts)
        if fx and fx["status"] in FIX_OPEN:
            return f"fix {fx.get('entry')} {fx['status']}"
        if fx is None and f.get("status") not in ("verify", "owner"):  # owner: filed with the owner (red owner-push)
            return None
    elif f.get("status") in FIX_OPEN and f.get("entry"):
        return f"fix {f.get('entry')} {f.get('status')}"
    if not f or (not f.get("entry") and not f.get("sha") and f.get("status") != "verify"):
        return None  # fix None: nothing is on its way
    if f.get("status") == "verify" and str(f.get("verify") or "").startswith("FIX UNVERIFIED:"):
        return None  # its verifying run was refused: none will come
    if t - red_fix_since(it) >= red_fix_giveup(d):
        return None
    return f"fix {f.get('entry') or (f.get('sha') or '')[:10] or 'verify'} {f.get('status')}"


# A HOLDER IS ONLY A LIVE LEASE (2026-10-30: Main #2647 sat offered for hours, its offer saying "its previous fixer pid
# 20496 ... is still at work on it: it stays the holder" -- a dead fixer of another, closed red, and no lease anywhere).
# An open offer whose reason names a holder, while the red has no live lease, is a violation: its reason is cleared
# and the dispatcher takes it again.
HOLDER_CLAIM = re.compile(r"stays the holder|still at work on it|is its holder", re.I)


def offer_names_holder(off):
    return bool(off and HOLDER_CLAIM.search(str(off.get("reason") or "")))


def red_unheld(store, t=None, facts=None):
    """The non-terminal reds with NOTHING live behind them: no live lease, no open offer, no fix on its way (red_fix_held),
    no live block. The tick's invariant (never NOBODY), enforced by red_enforce; a red the tick has not looked at yet
    (state `red`) counts after 2 min."""
    t = t if t is not None else store.clock()
    out = []
    for it in red_active(store).values():
        st = it["state"]
        if st == "red" and t - it["entered_at"] < 120:
            continue
        if lease_is_live(store.get(red_lease(it["data"]["key"])), t):
            continue
        if st == "fix-pending" and red_fix_held(it, t, facts, store):
            continue
        if st == "blocked-external" and red_block_of(it, store):
            continue
        if st == "dispatch-off" and repo_dispatch_off(store, red_repo_of(it["data"])):
            continue  # its repo's dispatch is off by the owner: that holds it (red auto off --repo)
        if st == "offered" and (off := store.offer_of(f"red:{it['id']}")) and not offer_names_holder(off):
            continue
        out.append(it)
    return out


def red_fix_gone_why(store, it, t):
    """Why a fix-pending red has no fix on its way, as its re-offer says it. 2026-11-22, Identity takes #349: "no fix on
    its way for 23 min in fix-pending" read as a queued fix overlooked, while its fix ...-779be4 had been REJECTED at
    04:28 (its landing check's itrest row) and the requeue grace had just run out: the change and its verdict are named."""
    f = it["data"].get("fix") or {}
    ch = red_change(store, f.get("entry")) if f.get("entry") else None
    if ch and ch["state"] in CHANGE_ENDED:
        why = str((ch.get("data") or {}).get("why") or "").strip()
        return (f"INVARIANT: its fix {f['entry']} was {ch['state']} {fmt_age(t - float(ch['entered_at']))} ago and not "
                f"queued again" + (f" ({why[:200]})" if why else ""))
    return f"INVARIANT: no fix on its way for {fmt_age(t - it['entered_at'])} in fix-pending"


def red_enforce(store, facts, t):
    """THE INVARIANT, SELF-RECOVERING (the owner, 2026-10-29): a red with no live holder (red_unheld) is offered again,
    escalated, its dead fix set aside -- the dispatcher, next in the same tick, gives it a fixer. Runs after every red's
    own step, so it only catches what the step's own transitions left resting."""
    acts = []
    for it in red_unheld(store, t, facts):
        it = store.item("red", it["id"])
        if not it or it["state"] in RED_TERMINAL:
            continue
        d, st = it["data"], it["state"]
        try:
            if st == "offered":
                off = store.offer_of(f"red:{it['id']}")
                if offer_names_holder(off):  # a stated holder with no live lease: no holder at all
                    store.offer_reason(off["id"], None)
                    acts.append(f"INVARIANT: {red_what(d)}: its offer named a holder with no live lease "
                                f"({str(off.get('reason'))[:120]}): offered again")
                else:
                    red_put_offer(store, it)  # an offered red always has its open offer
                    acts.append(f"INVARIANT: {red_what(d)}: its offer reopened")
            elif st == "fix-pending":
                acts += red_fix_unverifiable(store, it, red_fix_gone_why(store, it, t), t)
            elif st == "leased":
                acts += red_lease_ended(store, it, store.get(red_lease(d["key"])), red_block_of(it, store), t)
            else:
                acts.append(f"INVARIANT: {red_what(d)}: {st} with no holder: offered")
                red_offer(store, it, f"INVARIANT: {red_what(d)}: {st} with no holder for "
                                     f"{fmt_age(t - it['entered_at'])}: offered", escalate=st != "red",
                          data={"cause": None, "block": None} if st == "blocked-external" else None)
        except StaleVersion:
            pass
    return acts

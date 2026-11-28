# agentctl, part `redhold`: a red's fix held to its owner: the red-fix invariant (WR), who holds a red, the enforcement of an unheld one
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
import time
from contextlib import suppress
from typing import Any
from agentctl.statemachines import StaleVersion, is_terminal, open_items
from agentctl.timefmt import parse_time
from agentctl.watchknobs import knob_wr_max
from agentctl.redknobs import RED_FIX_REQUEUE_GRACE, knob_red_fix_requeue_grace, knob_red_liveness_max
from agentctl.core import SHORT
from agentctl.repos import red_repo_of
from agentctl.policy import HOLDS
from agentctl.cistate import ci_state_load, snapshot_gate_reds
from agentctl.queuehold import change_hold_stuck
from agentctl.context import Context
from agentctl.redfixread import FIX_OPEN
from agentctl.redrecord import lease_is_live, red_active, red_lease, red_what
from agentctl.leases import red_block_of
from agentctl.fixchange import CHANGE_ENDED, red_change, red_change_read
from agentctl.red import red_fix_giveup, red_put_offer
from agentctl.queueslots import checker_alive
from agentctl.redfixes import red_fix_of
from agentctl.changekind import HOLD_SHOWN, change_hold_visible, classify_change, facts_for, kind_text
from agentctl.redopen import red_offer
from agentctl.redverify import red_fix_since, red_fix_unverifiable
from agentctl.redwaits import RED_WAITS_ON
from agentctl.reddispatch import repo_dispatch_off
from agentctl.redlease import red_lease_ended
from agentctl.agewords import fmt_age


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
        return None if checker_alive(st, it["id"]) or state == "pushing" else "its checks run with no live checker"
    if state in HOLD_SHOWN:
        hold = change_hold_visible(st, it, {}, t)
        stuck = change_hold_stuck(st, hold, it, t)
        return stuck
    return None


def _red_streak_names(g: Any) -> Any:
    """The names (number, id) of one gate workflow's newest decided run and its failed streak, when that run is red."""
    d = g.get("decided") or {}
    if not d or d.get("conclusion") == "success":
        return set()
    out = {str(d.get("number")), str(d.get("databaseId"))}
    for r in g.get("runs") or []:
        if r.get("conclusion") == "success":
            break
        if r.get("conclusion") in ("failure", "timed_out"):
            out |= {str(r.get("number")), str(r.get("databaseId"))}
    return out


def snap_reds(snap):
    """The names (numbers and ids) of the gate's red runs on main in a CI snapshot: each red gate workflow's newest
    decided run and the failed runs of its streak since its last green; None when there is no snapshot (unread)."""
    if not snap:
        return None
    out = set().union(*(_red_streak_names(g) for g in snap.get("gate") or []))
    return out - {"None"}


def inv_wr(clock: Any, st: Any, reds: Any = None, *, ctx: Context) -> Any:
    """WR: A RED FIX NEVER WAITS FOR GREEN (the owner, 2026-11-16). No open change whose KIND (changekind.py: its
    `Fixes-Red:` against the gate's reds and every red the store has open) is a gate red's fix is on `waiting`, and no
    fix of any open red -- the gate's or one outside it (08010c, 2026-11-19) -- is still unlanded WR_MAX after it was
    submitted. The queue tick HEALS first (batch.py release_red_fixes: a fix the open red names goes back to the queue);
    a violation that is left names the rule that holds the fix (`wait_rule`)."""
    if reds is None:
        reds = snap_reds(ci_state_load(SHORT["movies"]))
    facts = facts_for(reds or set(), st, ctx.caches.tick_facts)
    if not reds and not facts["open"]:
        return True, "main green (or its gate unread) and no red open: no red fix to hold"
    bad = red_fix_stalls(st, clock.time(), facts, ctx=ctx)
    if not bad:
        return True, "no red fix waits"
    return False, "a red fix waits: " + "; ".join(bad[:3]) + " -- `agentctl land status`, `agentctl ci-why`"


def red_fix_stalls(st: Any, t: float, facts: dict[str, Any], *, ctx: Context) -> list[str]:
    """Why each open red fix does not move, as WR words it: [text], the changes WR violates on (inv_wr, the liveness line)."""
    lim = knob_wr_max()
    out = []
    for it in open_items(st, "change"):
        k = classify_change(it.get("data") or {}, facts, t, ctx=ctx)
        why = k and k["red_fix"] and red_fix_stall(st, it, k, t, lim)
        if why:
            out.append(f"{it['id']} ({kind_text(k)}) {why}")
    return out


def red_fix_stall(st: Any, it: Any, k: dict[str, Any], t: float, lim: float) -> str | None:
    """Open change item `it`, a red fix of kind k: why it does not move, else None."""
    data = it.get("data") or {}
    rule = f": {data['wait_rule']}" if it["state"] == "waiting" and data.get("wait_rule") else ""
    if it["state"] == "waiting" and k["gate_fix"]:
        return f"is waiting for main to turn green{rule}"
    return held_stall(it, data) or slow_stall(st, it, data, t, lim, rule)


def held_stall(it: Any, data: dict[str, Any]) -> str | None:
    """THE HOLD TABLE: a red fix waits on its own resources only, never other work."""
    hold = (data.get("hold") or {}) if it["state"] in HOLD_SHOWN else {}
    hk = HOLDS.get(hold.get("kind"))
    return f"is held behind other work: {hold.get('why')}" if hk and not hk.red_fix_ok else None


def slow_stall(st: Any, it: Any, data: dict[str, Any], t: float, lim: float, rule: str) -> str | None:
    """Past WR_MAX since it was queued with its own check not running (or its own tree not busy), within bounds no wait
    behind anything; past W1_HARD_FACTOR x that, whatever it waits on."""
    age = t - float(data.get("submitted_at") or it.get("entered_at") or t)
    progress = change_progress(st, it, t) if age > lim else None
    if age > lim and (progress is not None or age > W1_HARD_FACTOR * lim):
        return f"is {it['state']} {fmt_age(age)} after it was queued" + (f" ({progress})" if progress else "") + rule
    return None


def red_fix_liveness(st: Any, t: float, snap: Any = None, *, ctx: Context) -> str | None:
    """THE LIVENESS LINE (the owner, 2026-11-25: the gate sat red for hours with its fix waiting and nothing said): ONE line
    when the gate has been red for over RED_LIVENESS_MAX while a red-fix change is not progressing -- the change and the
    exact rule blocking it (red_fix_stalls) -- else None. `agentctl status` and `red status` print it."""
    snap = ci_state_load(SHORT["movies"]) if snap is None else snap
    rows = [g for g in snapshot_gate_reds(snap) if g.get("since")] if snap else []
    if not rows:
        return None
    age_since = t - min(parse_time(g["since"]).timestamp() for g in rows)
    if age_since <= knob_red_liveness_max():
        return None
    stalls = red_fix_stalls(st, t, facts_for(snap_reds(snap), st, ctx.caches.tick_facts), ctx=ctx)
    if not stalls:
        return None
    return (f"RED FIX STALLED: the gate has been red {fmt_age(age_since)}; {stalls[0]}"
            + (f" (and {len(stalls) - 1} more)" if len(stalls) > 1 else ""))


_UNDECIDED: Any = object()  # a stage of red_fix_held with no verdict: the next one looks


def _requeue_pending(ch: Any, t: float) -> bool:
    """The fix's change ended only just now: its worktree may queue it again (as redpending.FixPending waits)."""
    return bool(ch and ch["state"] in CHANGE_ENDED
                and t - float(ch.get("entered_at") or 0) < knob_red_fix_requeue_grace())


def _change_open(ch: Any) -> bool:
    return bool(ch and ch["state"] not in CHANGE_ENDED and ch["state"] != "landed")


def _store_holder(f: dict[str, Any], t: float, store: Any) -> Any:
    """The fix change as the store holds it: the holder text when it stands, else _UNDECIDED."""
    if store is None or not f.get("entry") or f.get("status") not in FIX_OPEN:
        return _UNDECIDED
    ch: Any
    ch, unread = red_change_read(store, f["entry"])
    if unread:  # an unread store is no ended change: the fix stands as recorded
        return f"fix {f['entry']} {f.get('status')} (its change unread: {unread[:80]})"
    if _requeue_pending(ch, t):
        return f"fix {f['entry']} {ch['state']}: its worktree may queue it again"  # the red's own step waits it out
    if _change_open(ch):
        # THE STORE'S QUEUE DECIDES AN OPEN CHANGE: the facts' entries are the queue's files, which only ci-runner
        # has; a change the store holds queued or checking is on its way whatever the facts read.
        return f"fix {f['entry']} {ch['state']}"
    return _UNDECIDED


def _unread_queue_holder(f: dict[str, Any], facts: Any) -> Any:
    """The fix stands as recorded when the queue's files were unread."""
    if facts is not None and facts.get("queue_unread") and f.get("entry") and f.get("status") in FIX_OPEN:
        return f"fix {f['entry']} {f.get('status')} (the queue unread: {str(facts['queue_unread'])[:80]})"
    return _UNDECIDED


def _queue_holder(it: Any, f: dict[str, Any], facts: Any, *, ctx: Context) -> Any:
    """With facts the queue's own fix decides (None: nothing is on its way); without (the watchdog), the one recorded."""
    if facts is not None and not f.get("waited_on"):  # a waited-on fix that landed is no candidate of its own
        fx = red_fix_of(it, facts, ctx=ctx)
        if fx and fx["status"] in FIX_OPEN:
            return f"fix {fx.get('entry')} {fx['status']}"
        if fx is None and f.get("status") not in ("verify", "owner"):  # owner: filed with the owner (red owner-push)
            return None
    elif f.get("status") in FIX_OPEN and f.get("entry"):
        return f"fix {f.get('entry')} {f.get('status')}"
    return _UNDECIDED


def _no_fix_on_its_way(f: dict[str, Any]) -> bool:
    """The red records no fix, or one with neither entry nor sha that is no verify run; or its verifying run was refused
    (none will come)."""
    if not f or (not f.get("entry") and not f.get("sha") and f.get("status") != "verify"):
        return True  # fix None: nothing is on its way
    return f.get("status") == "verify" and str(f.get("verify") or "").startswith("FIX UNVERIFIED:")


def _recorded_holder(it: Any, d: dict[str, Any], f: dict[str, Any], t: float) -> str | None:
    """The fix as the red recorded it: on its way until it was refused or its give-up passed."""
    if _no_fix_on_its_way(f) or t - red_fix_since(it) >= red_fix_giveup(d):
        return None
    return f"fix {f.get('entry') or (f.get('sha') or '')[:10] or 'verify'} {f.get('status')}"


def red_fix_held(it, t, facts = None, store = None, *, ctx):
    """A fix-pending red's holder, or None: its fix change open in the queue (or ended within RED_FIX_REQUEUE_GRACE:
    its worktree may queue it again, as redpending.FixPending waits), or a fix landed / a verify run dispatched (or a gate run
    due) within its give-up of entering fix-pending. With facts the queue decides; without (the watchdog), the fix the
    item recorded."""
    d = it["data"]
    f = d.get("fix") or {}
    if f.get("status") == RED_WAITS_ON and f.get("red"):  # held by the red it waits on (red_waits_on_step resolves it)
        return f"waits on red {f['red']}'s fix {str(f.get('sha'))[:10]}"
    held = _store_holder(f, t, store)
    if held is not _UNDECIDED:
        return held
    held = _unread_queue_holder(f, facts)
    if held is not _UNDECIDED:
        return held
    held = _queue_holder(it, f, facts, ctx=ctx)
    return _recorded_holder(it, d, f, t) if held is _UNDECIDED else held


# A HOLDER IS ONLY A LIVE LEASE (2026-10-30: Main #2647 sat offered for hours, its offer saying "its previous fixer pid
# 20496 ... is still at work on it: it stays the holder" -- a dead fixer of another, closed red, and no lease anywhere).
# An open offer whose reason names a holder, while the red has no live lease, is a violation: its reason is cleared
# and the dispatcher takes it again.
HOLDER_CLAIM = re.compile(r"stays the holder|still at work on it|is its holder", re.I)


def offer_names_holder(off) -> bool:
    return bool(off and HOLDER_CLAIM.search(str(off.get("reason") or "")))


def red_unheld(store, t = None, facts = None, *, ctx):
    """The non-terminal reds with NOTHING live behind them: no live lease, no open offer, no fix on its way (red_fix_held),
    no live block. The tick's invariant (never NOBODY), enforced by red_enforce; a red the tick has not looked at yet
    (state `red`) counts after 2 min."""
    t = t if t is not None else store.clock()
    return [it for it in red_active(store).values() if not _red_held(store, it, t, facts, ctx=ctx)]


def _offer_holds(store, it, t, facts, ctx):
    """An open offer holds an `offered` red unless it names a holder (the dispatcher takes it again)."""
    off = store.offer_of(f"red:{it['id']}")
    return bool(off) and not offer_names_holder(off)


# what holds a red of each state, besides a live lease: (store, red item, t, facts, context) -> held
RED_STATE_HOLDS = {
    "fix-pending": lambda store, it, t, facts, ctx: red_fix_held(it, t, facts, store, ctx=ctx),
    "blocked-external": lambda store, it, t, facts, ctx: red_block_of(it, store),
    # its repo's dispatch is off by the owner: that holds it (red auto off --repo)
    "dispatch-off": lambda store, it, t, facts, ctx: repo_dispatch_off(store, red_repo_of(it["data"])),
    "offered": _offer_holds,
}


def _red_held(store, it, t, facts, *, ctx: Context) -> bool:
    """Whether something live is behind the red: a young one (state `red`, under 2 min), a live lease, or its state's hold."""
    st = it["state"]
    if st == "red" and t - it["entered_at"] < 120:
        return True
    if lease_is_live(store.get(red_lease(it["data"]["key"])), t):
        return True
    hold = RED_STATE_HOLDS.get(st)
    return bool(hold and hold(store, it, t, facts, ctx))


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


def _enforce_unheld(store: Any, it: Any, t: Any, acts: Any, *, ctx: Context) -> Any:
    """red_enforce's step for one unheld red, its actions appended to `acts` as they are taken."""
    d, st = it["data"], it["state"]
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
        acts += red_lease_ended(store, it, store.get(red_lease(d["key"])), red_block_of(it, store), t, ctx=ctx)
    else:
        acts.append(f"INVARIANT: {red_what(d)}: {st} with no holder: offered")
        red_offer(store, it, f"INVARIANT: {red_what(d)}: {st} with no holder for "
                             f"{fmt_age(t - it['entered_at'])}: offered", escalate=st != "red",
                  data={"cause": None, "block": None} if st == "blocked-external" else None)


def red_enforce(store, facts, t, *, ctx):
    """THE INVARIANT, SELF-RECOVERING (the owner, 2026-10-29): a red with no live holder (red_unheld) is offered again,
    escalated, its dead fix set aside -- the dispatcher, next in the same tick, gives it a fixer. Runs after every red's
    own step, so it only catches what the step's own transitions left resting."""
    acts: list[str] = []
    for it in red_unheld(store, t, facts, ctx=ctx):
        it = store.item("red", it["id"])
        if not it or is_terminal("red", it["state"]):
            continue
        with suppress(StaleVersion):
            _enforce_unheld(store, it, t, acts, ctx=ctx)
    return acts

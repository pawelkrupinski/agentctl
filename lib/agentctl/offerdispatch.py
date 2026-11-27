# agentctl, part `offerdispatch`: the red dispatcher's queue pass: each open fixer offer handed to its family's owner, joined to its fixer, left waiting, or taken by a new fixer
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.escalationtable import crash_loop_hold
from agentctl.repos import red_repo_clone, red_repo_of
from agentctl.procs import socket_host
from agentctl.fixerblockers import cached_dispatch_blocker
from agentctl.fixersession import claude_trust_gap
from agentctl.leases import LeaseKinds, lease_meta
from agentctl.owners import OWNER_TTL, owner_alive, owner_lease
from agentctl.ownerleases import family_owned, owner_hand, owner_holder
from agentctl.red import RED_CLAIM_MAX, RED_CLAIM_TTL, RED_FIXER_MAX, RED_FIXER_TTL
from agentctl.redseen import KIND_ORDER
from agentctl.redopen import red_family_tell
from agentctl.blame import blame_waiting
from agentctl.redcrash import red_queue_family
from agentctl.reddispatch import (
    red_backoff_why, red_capacity, red_dispatch_meta, red_family_holder, red_spawn, repo_dispatch_off)
from agentctl.owner import OWNER_MAX, owner_spawn, owner_still_ending
from agentctl.paths import tilde


def red_dispatcher(store: Any, o_r: str, repo: str, t: float) -> list[str]:
    """THE QUEUE (see ONE RED MODEL above): the open fixer offers, highest priority first; per queue family exactly one
    fixer -- a new red of a family at work joins its fixer, a family with none is dispatched once (its other open
    offers taken with it) while a slot is free and dispatch can run. An offer that cannot be taken says why (its
    escalation names it)."""
    acts: list[str] = []
    offs = store.offers("fixer")
    if not offs:
        return acts
    offs.sort(key=lambda o: (o["priority"], KIND_ORDER.get(o["picture"].get("kind"), 1), o["opened_at"]))
    blocker = cached_dispatch_blocker()
    gone: set[str] = set()
    for off in offs:
        if off["id"] not in gone:
            dispatch_offer(store, o_r, repo, t, off, offs, gone, blocker, acts)
    return acts


def dispatch_offer(store: Any, o_r: str, repo: str, t: float, off: Any, offs: Any, gone: Any, blocker: Any, acts: Any) -> None:
    """One open offer: handed to its family's live owner, joined to its family's fixer, left waiting with its reason, or
    taken by a fixer of its own (the siblings of its family taken with it, into `gone`); what it did goes to `acts`."""
    pic = off["picture"]
    if repo_dispatch_off(store, red_repo_of(pic)):  # its repo's dispatch is off (the owner's): never dispatched
        store.offer_reason(off["id"], f"dispatch off for {red_repo_of(pic)} (owner)")
        return
    fam = red_queue_family(pic)
    owned = family_owned(fam) and off["tier"] not in ("revert", "author", "lander", "blame")
    if owned and offer_for_owner(store, off, pic, fam, t, acts):
        return
    holder, item = offer_family_holder(store, off, fam, t)
    if holder:
        join_family_fixer(store, off, pic, fam, holder, t, acts)
        return
    why = offer_waits_why(store, t, off, pic, item, blocker)
    if why:
        offer_says(store, off, why)
        return
    # ANOTHER WATCHED REPO'S RED: its fixer works in that repo's clone on this machine (red-repos.json `clone`)
    po = pic.get("repo") or o_r
    prepo = repo if po == o_r else red_repo_clone(po)
    why = offer_undispatchable_why(po, prepo, po != o_r)
    if why:
        offer_says(store, off, why)
        return
    spawn_offer(store, off, offs, gone, acts, pic, fam, owned, po, prepo)


def offer_for_owner(store: Any, off: Any, pic: Any, fam: str, t: float, acts: Any) -> bool:
    """True when the offer of an owned family is the owner's: handed to its live owner, or left waiting while the last
    wrapper (orphaned, revoked) is still ending -- the next starts after it."""
    live = owner_alive(store, fam, t)
    if live:
        hand_to_owner(store, off, pic, fam, live, acts)
        return True
    ending = owner_still_ending(store, fam, t)
    if ending:
        offer_says(store, off, ending)
    return bool(ending)


def offer_family_holder(store: Any, off: Any, fam: str, t: float) -> tuple[Any, dict[str, Any]]:
    """(the family's fixer or claim this offer joins, the red's item): None when it has none at work, the offer's own
    lease is it, or the red was released from it for no progress (red_joined_idle: a fixer of its own, never back)."""
    holder = red_family_holder(store, fam, t) if off["tier"] not in ("revert", "lander", "blame") else None
    item = store.item("red", off["item"].split(":", 1)[1]) or {"data": {}}
    if holder and holder["holder"] in (item["data"].get("family_released") or []):
        holder = None
    return (holder if holder and holder["name"] != off["lease"] else None), item


def offer_says(store: Any, off: Any, why: str) -> None:
    """The reason an offer waits, written on it when it changed (its escalation names it)."""
    if why != off.get("reason"):
        store.offer_reason(off["id"], why)


def hand_to_owner(store: Any, off: Any, pic: Any, fam: str, live: Any, acts: Any) -> None:
    """THE FAMILY'S OWNER IS ALIVE: the red is its next turn -- its lease taken for the owner and handed over."""
    took = store.take(off["id"], owner_holder(fam), OWNER_TTL, OWNER_MAX,
                      meta={"kind": "owned", "family": fam, "owner": owner_lease(fam), "host": socket_host(),
                            "worktree": lease_meta(live).get("worktree"), "gate": bool(pic.get("gate"))})
    if took and "epoch" in took:
        owner_hand(fam, off["lease"], took)
        red_family_tell(live, pic, fam)
        acts.append(f"{pic.get('workflow')} #{(pic.get('run') or {}).get('number')}: red {pic['key']} handed "
                    f"to its family {fam}'s owner")


def join_family_fixer(store: Any, off: Any, pic: Any, fam: str, holder: Any, t: float, acts: Any) -> None:
    """ONE FIXER PER FAMILY: a new red of a family whose fixer is at work is that fixer's -- under its lease; an owned
    family's too while its owner is not running (never a second session beside a live holder). A claim's red is a claim
    of the same worktree (renewed by its activity, red_renew_holders), a fixer's a sibling; the holder's note is about
    its own red, never the joined one's."""
    claim = LeaseKinds.is_claim(holder)
    took = store.take(off["id"], holder["holder"], *((RED_CLAIM_TTL, RED_CLAIM_MAX) if claim else
                                                     (RED_FIXER_TTL, RED_FIXER_MAX)),
                      meta={**lease_meta(holder), "family": fam, "outcome": None, "note": None, "notes": None,
                            "gate": bool(pic.get("gate")), "joined_at": t, "progressed": None,
                            **({} if claim else {"kind": "sibling", "primary": holder["name"]})})
    if took and "epoch" in took:
        red_family_tell(holder, pic, fam)
        acts.append(f"{pic.get('workflow')} #{(pic.get('run') or {}).get('number')}: red {pic['key']} joined "
                    f"its family {fam}'s fixer {holder['holder']}")


def offer_waits_why(store: Any, t: float, off: Any, pic: Any, item: Any, blocker: Any) -> Any:
    """Why the offer cannot be taken now -- its fixer crash-looping, offered to its author, its culprit's blame, the
    backoff, dispatch blocked, the cap -- or None when a fixer may start."""
    after = float(item["data"].get("dispatch_after") or 0)
    cap_why = offer_capacity_why(store, t, off, pic)
    crash_loop = crash_loop_hold(store.items("condition", ["open"]), off["item"])
    if crash_loop:  # ITS FIXER CRASH-LOOPS AT LAUNCH (crashloop.py): not launched again while its condition is open
        return crash_loop
    if off["tier"] == "author":
        return f"offered to its author ({tilde(str((pic.get('author') or {}).get('worktree') or '?'))})"
    if off["tier"] == "blame":
        return blame_waiting(pic.get("blame"))
    if after > t:
        return red_backoff_why(item["data"], after)
    return blocker or cap_why


def offer_capacity_why(store: Any, t: float, off: Any, pic: Any) -> str | None:
    """Why the cap holds the offer back, or None when it has room."""
    # NOTHING STOPS A GATE FIX, EVER (the owner, 2026-10-31): a gate red's fixer starts past the cap, and no fixer
    # waits on laptop pressure (that holds only other work: gov_fits); a gate fixer still counts toward the cap
    free, cap_why = (True, None) if pic.get("gate") else red_capacity(store, t - float(off.get("opened_at") or t), t)
    return None if free else cap_why


def offer_undispatchable_why(po: str, prepo: Any, other_repo: bool) -> str | None:
    """Why a red of repo `po` cannot be dispatched here -- another watched repo's red with no clone on this machine, or a
    clone claude refuses to launch in -- or None."""
    if other_repo and not prepo:
        return f"no clone of {po} on this machine (red-repos.json `clone`)"
    gap = claude_trust_gap(prepo)
    # never a launch claude refuses: said on its offer, dispatched once trusted -- the owner was asked when the repo was
    # registered (fixersession.py trust_asks), never once per offer
    return f"NOT DISPATCHABLE: {gap}" if gap else None


def spawn_offer(store: Any, off: Any, offs: Any, gone: Any, acts: Any, pic: Any, fam: str, owned: bool, po: str, prepo: Any) -> None:
    """Take the offer for a fixer of its own (an owner's turn for an owned family), the open offers of its family with it,
    and start the session; a start that failed releases every lease it took."""
    num = (pic.get("run") or {}).get("number")
    holder_id, ttl, max_ = offer_lease_terms(pic, fam, owned)
    meta = red_dispatch_meta(pic, fam, owned, off, num)
    took = store.take(off["id"], holder_id, ttl, max_, meta=meta)
    if not took or "held" in took or "gone" in took:
        return
    extra = []
    if off["tier"] not in ("revert", "resume", "lander"):
        extra = take_family_siblings(store, off, offs, gone, fam, (holder_id, ttl, max_, meta))
    ok, msg = (owner_spawn if owned else red_spawn)(store, po, prepo, *((fam,) if owned else ()), off, took, extra)
    if not ok:
        for x in [{"name": off["lease"], **took}] + extra:
            store.release(x["name"], x["epoch"], x["secret"], outcome=f"NOT STARTED: {str(msg)[:200]}")
    acts.append(msg)


def offer_lease_terms(pic: Any, fam: str, owned: bool) -> tuple[str, float, float]:
    """(the holder, ttl and max hold) a fixer's lease is taken with: the family owner's, or a fixer of this red's own."""
    if owned:
        return owner_holder(fam), OWNER_TTL, OWNER_MAX
    return f"laptop:fixer:red-{pic['key']}-{int(pic.get('attempts') or 0) + 1}", RED_FIXER_TTL, RED_FIXER_MAX


def take_family_siblings(store: Any, off: Any, offs: Any, gone: Any, fam: str, taking: Any) -> list[dict[str, Any]]:
    """The leases of the family's other open offers, taken for the same fixer (`taking`: holder, ttl, max, meta)."""
    holder_id, ttl, max_, meta = taking
    extra = []
    for sib in family_siblings(offs, off, gone, fam):
        st_ = store.take(sib["id"], holder_id, ttl, max_, meta=meta)
        if st_ and "epoch" in st_:
            extra.append({"name": sib["lease"], "epoch": st_["epoch"], "secret": st_["secret"], "picture": sib["picture"]})
            gone.add(sib["id"])
    return extra


def family_siblings(offs: Any, off: Any, gone: Any, fam: str) -> list[dict[str, Any]]:
    """The other open offers of the family that a fixer started for `off` takes with it."""
    return [sib for sib in offs
            if sib["id"] != off["id"] and sib["id"] not in gone
            and sib["tier"] not in ("author", "revert", "resume", "lander", "blame")
            and red_queue_family(sib["picture"]) == fam]

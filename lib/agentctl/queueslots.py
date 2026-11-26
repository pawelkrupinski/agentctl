# agentctl, part `queueslots`: check slots and a change's check as leases; the red-fix tree; a fix of an open red first.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import subprocess
from typing import Any
from contextlib import suppress
from agentctl.procrunner import process_runner
from agentctl.knobs import env_num
from agentctl.core import err, git
from agentctl.timefmt import parse_time, ts
from agentctl.waitingdir import QUEUE_POOL, save_entry, waiting_entries, waiting_state, wlog
from agentctl.lander import lander_fields
from agentctl.leases import LeaseHold, lease_live
from agentctl.land import tool_env
from agentctl.causes import checked_cause, reject_cause, reject_class
from agentctl.owned import Owned
from agentctl.worktrees import mark_internal
from agentctl.statemachines import (
    ENTRY_STATE, StaleVersion, TerminalState, change_event, joined_verdict, machine_state)
from agentctl.knobs import knob_check_lease_ttl


CHECK_LEASE_MAX = 3 * 3600


def tree_slot(st, d, i, max_hold = None):
    """THE lease of check tree i: whoever touches the tree -- a checker, the batch, a push -- holds it."""
    return LeaseHold(f"slot/check/{os.path.basename(d.rstrip('/'))}/{i}",
                     knob_check_lease_ttl(), max_hold or CHECK_LEASE_MAX,
                     store=st)


# A RED FIX NEVER WAITS FOR GREEN (the owner, 2026-11-16: "Red fix should never wait for green build (obv)"): every
# place a change can be held for main or for other changes exempts a red fix (its KIND, changekind.py): it takes
# THE RED-FIX TREE no ordinary change may take (08:58-09:15Z: Main #2515's fix sat "queued for 15 min, waiting on a
# check slot" behind three ordinary checks), it goes before an open batch, a known red never parks it on `waiting`,
# must_wait lets it go (the hook's rule), and the watchdog's WR escalates one that waits anyway.
RED_FIX_TREE = "redfix"  # <clone>-wt-check-redfix, under its own lease: only a red fix's checker takes it


# A FIX OF ANY OPEN RED GOES FIRST (2026-11-19, 08010c: `Fixes-Red: 6`, Alert backtest #6, red 5254bffda9d2 outside
# the gate): whether a change is a red fix -- and a GATE red's fix -- is its KIND (changekind.py classify_change), read
# against the gate's reds and every red the store has open (queue_facts). Where the queue ORDERS changes (a push before
# an open batch or a combined-tree verification, the red-fix tree, no batch membership) a fix of any open red counts
# (kind red_fix); where the GATE decides (waiting for green, a known red), only a fix of a gate red still does (gate_fix).


def check_tree_slot(st, repo, d, fix: bool = False):
    """(index, LeaseHold) of a free check tree slot, or None when all QUEUE_POOL are busy; a red fix (fix) tries the
    red-fix tree first."""
    for i in ([RED_FIX_TREE] if fix else []) + list(range(1, int(env_num("AGENTCTL_CHECK_TREES", QUEUE_POOL)) + 1)):
        h = tree_slot(st, d, i)
        if h.acquire():
            return i, h
    return None


def checker_lease(st, eid):
    return LeaseHold(f"check/{eid}", knob_check_lease_ttl(), CHECK_LEASE_MAX,
                     meta={"entry": eid}, store=st)


def checker_alive(st, eid):
    """The live checker lease of entry eid (meta: its pid), or None."""
    return lease_live(f"check/{eid}", st)


def check_tree(repo, i, sha):
    """The detached worktree <clone>-wt-check-<i> at sha (kept between checks: its build stays warm)."""
    _d, clone, _w = waiting_state(repo)
    tree = mark_internal(f"{clone}-wt-check-{i}")
    if not os.path.isdir(tree):
        git(repo, "worktree", "prune", check=False)
        git(repo, "worktree", "add", "-q", "--detach", mark_internal(tree), sha)
        seed = os.path.join(tree, "scripts", "sbt-warm")
        if os.access(seed, os.X_OK):  # a cold tree's first compile seeded from the shared build cache, not from scratch
            try:
                process_runner().run([seed, "--seed"], cwd=tree, capture_output=True, text=True, timeout=600, env=tool_env())
            except subprocess.TimeoutExpired:
                err(f"sbt-warm --seed in {tree} took over 10 min")
    else:
        git(tree, "reset", "-q", "--hard", check=False)
        git(tree, "clean", "-qfd", "-e", "target/", check=False)
        git(tree, "checkout", "-q", "--force", "--detach", sha)
    return tree


def entry_owned(eid: str) -> Owned:
    return Owned("check", eid, ["waiting", "check", eid])


# THE TICK DRIVES CHANGES (design §5.1, phase 1 step 6). A change item's state is written ONLY by set_change: the store
# item (a transition, with its event) and the entry's mirrored `status` (which the batch code still reads until step
# 8). The executor (`waiting check <id>`, holding check/<entry>) takes a change from queued to checking and, by its
# verdict, to checked, rejected (a test failure, a conflict) or back to queued (infra: never a rejection). The tick
# (tick_changes, every processor cycle) starts executors, re-queues a change whose checker lost its lease, and pushes a
# checked change under the landing lease: pushing -> landed, or back to checked (a push never rejects; after 3
# failures a condition on the git route). Main moving with an overlap is a new round (queued), never an attempt.
ENTRY_STATUS = {"queued": "queued", "checking": "checking", "checked": "checked", "pushing": "pushing",
                "waiting": "waiting", "landed": "landed", "rejected": "rejected", "superseded": "superseded",
                "cancelled": "cancelled"}
PUSH_ATTEMPTS_MAX = 3


def change_item_fields(e):
    """A change item's data at its creation, from its queue entry: where it is, its lander, the reds it claims to fix
    (`fixes`: the watchdog's WR reads them; a red fix never waits for green), and when it was submitted (`submitted_at`:
    its queue age -- an entry queued again after a conflict keeps its first one, entry_queued_since)."""
    since = entry_queued_since(e)
    created = entry_created_at(e)
    return {"worktree": e.get("worktree"), "head": e.get("head"), **lander_fields(e),
            **({"fixes": sorted(e["fixes"])} if e.get("fixes") else {}),
            **({"diagnoses": sorted(e["diagnoses"])} if e.get("diagnoses") else {}),
            **({"replays": e["replays"]} if e.get("replays") else {}),
            **({"submitted_at": since} if since else {}),
            **({"entry_created_at": created} if created else {})}


def entry_created_at(e: dict[str, Any]) -> float | None:
    """When this very entry was created (epoch), whatever place and queue age it inherited; None when it does not read."""
    try:
        return parse_time(e["created"]).timestamp() if e.get("created") else None
    except (ValueError, TypeError):
        return None


def entry_queued_since(e: dict[str, Any]) -> float | None:
    """When the change was first queued (epoch): `queued_since` (carried over a re-queue that kept its place), else
    its `created`; None when neither reads."""
    if e.get("queued_since"):
        return float(e["queued_since"])
    return entry_created_at(e)


def _landed_fields(fields):
    return {k: fields[k] for k in ("landed_sha", "pushed_at", "batch", "batch_id", "superseded_by", "decided_on")
            if fields.get(k) is not None}


def _item_data(cur, to, data, fields):
    """The data set_change puts on the change's item: the decided why (a fixer's land reads it there, await_queued),
    where and when it landed or what superseded it (an awaiter on any host reads it, await_entry, verdict_still_holds),
    and a hold of the state it leaves cleared (a new state's wait is said anew, note_hold: no stale reason)."""
    if fields.get("why"):
        data = {**(data or {}), "why": str(fields["why"])[:600]}
    landed = _landed_fields(fields)
    if landed:
        data = {**(data or {}), **landed}
    if to != cur["state"] and (cur.get("data") or {}).get("hold"):
        data = {**(data or {}), "hold": None}
    return data


def set_change(st: Any, d: str, e: dict[str, Any], to: str, data: dict[str, Any] | None = None,
               event: dict[str, Any] | None = None, *, cause: str, detail: str | None = None,
               **fields: Any) -> None:
    """THE writer of a change's state: its item (transition + event) and its entry's status, together. A waiting entry
    that queued changes joined passes a final state on to them (joined_verdicts). cause: why it moved, one of
    causes.CAUSES (a keyword every caller names: tests/test_causes.py); detail: a short qualifier."""
    event = {**(event or {}), "cause": checked_cause(cause), **({"detail": detail} if detail else {})}
    joined = joined_changes(d, e, st) if machine_state("change", to).terminal else []
    if joined and event:  # its author hears it under the queued id alone (joined_verdicts)
        event = {**event, "refs": {**(event.get("refs") or {}), "no_inbox": True}}
    cur = st.put_item("change", e["id"], ENTRY_STATE.get(e.get("status", ""), "queued"), change_item_fields(e))
    data = _item_data(cur, to, data, fields)
    try:
        if not machine_state("change", cur["state"]).terminal:
            st.transition("change", e["id"], to, cur["version"], data=data, event=event)
    except StaleVersion:  # another writer moved it: re-read once
        cur = st.item("change", e["id"])
        if not machine_state("change", cur["state"]).terminal:
            st.transition("change", e["id"], to, cur["version"], data=data, event=event)
    e.update(status=ENTRY_STATUS[to], **fields)
    save_entry(d, e)
    joined_verdicts(st, e, joined, to, event, data)


def joined_changes(d, w, st):
    """The queued changes that joined waiting entry w and are still open: entries from before ONE CHANGE, ONE ITEM
    (queue_join), when a queued change meeting a red gate became a waiting entry of its own, the queued one `joined`."""
    out = []
    for q in waiting_entries(d, open_only=False):
        if q.get("status") == "joined" and q.get("waiting_id") == w["id"] and q["id"] != w["id"]:
            it = st.item("change", q["id"])
            if it and not machine_state("change", it["state"]).terminal:
                out.append((q, it))
    return out


def _finish_joined(st: Any, w: Any, q: Any, it: Any, to: Any, refs: Any, data: Any, cause: str) -> Any:
    """Joined change q (item it) of waiting entry w moved to `to`, under q's id, its line the projection's, for the
    cause w moved."""
    line = joined_verdict(q, w if w.get("why") else {**w, "why": f"{w['id']}: {to}"}, to)
    sha = w.get("landed_sha") if to == "landed" else None
    extra = {k: refs[k] for k in refs if k not in ("verdict", "tail", "exit", "final", "command", "entry", "no_inbox")}
    ev = change_event(q, line, refs.get("tail") or (), refs.get("exit", 0 if to == "landed" else 1), **extra)
    with suppress(StaleVersion, TerminalState):
        st.transition("change", q["id"], to, it["version"],
                      data={**(data or {}), "why": line[:600], **({"tip": sha, "landed_sha": sha} if sha else {})},
                      event={**ev, "cause": cause})


def joined_verdicts(st, w, joined, to, event, data) -> None:
    """Waiting entry w's final state on each queued change that joined it, under the QUEUED id -- the one its `land`
    printed -- its line the projection's (joined_verdict), its author told there alone (review round 7:
    20261116-184811-movies-r2-ci-ac68b8's batch LANDED went to its waiting entry's id; its own item only followed later,
    through the projection, as a second LANDED)."""
    refs = (event or {}).get("refs") or {}
    for q, it in joined:
        _finish_joined(st, w, q, it, to, refs, data, (event or {}).get("cause") or "landed")


def queue_verdict(st, d, e, status, verdict, tail = (), code: int = 1, cause: str | None = None, **fields) -> None:
    """A change's final verdict: its state (set_change), whose event the projector makes its author's inbox entry.
    `decided_on`: the main it was decided on (else the base it was checked on, else the one it was queued on): a land
    replays the verdict only while main is still that (verdict_still_holds). cause: a landing's is `landed`, a
    reject's THE reject classifier's (causes.reject_cause: its class, infra | test | policy, the event's detail)."""
    fields["decided_on"] = fields.get("decided_on") or e.get("checked_base") or e.get("base")
    cause = cause or ("landed" if status == "landed" else reject_cause(verdict, tail) if status == "rejected" else None)
    detail = f"class={reject_class(cause)}" if status == "rejected" else None
    # its recheck log on its item too (stats' kept_verdicts reads it there): a batch's kept verdict is logged on the
    # entry alone
    set_change(st, d, e, {"joined": "waiting"}.get(status, status), event=change_event(e, verdict, tail, code),
               cause=checked_cause(cause), detail=detail,
               data={"rechecks": e["rechecks"]} if e.get("rechecks") else None, decided=ts(), why=verdict, **fields)
    wlog(d, f"{e['id']}: {verdict.splitlines()[0]}")

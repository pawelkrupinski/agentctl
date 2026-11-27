# agentctl, part `queueslots`: check slots and a change's check as leases; the red-fix tree; a fix of an open red first.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import subprocess
from typing import Any
from agentctl.procrunner import process_runner
from agentctl.statemachines import StaleVersion, change_event, change_state_of, machine_state
from agentctl.timefmt import ts
from agentctl.govknobs import QUEUE_POOL, knob_check_lease_ttl, knob_check_trees
from agentctl.core import err, git
from agentctl.causes import checked_cause, reject_cause, reject_class
from agentctl.waitingdir import RECORD_KEY, ChangeRecords, entry_record, waiting_state, wlog
from agentctl.worktrees import mark_internal
from agentctl.leases import LeaseHold, lease_live
from agentctl.land import tool_env
from agentctl.owned import Owned
from agentctl.waitingdir import change_item_fields


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
    for i in ([RED_FIX_TREE] if fix else []) + list(range(1, int(knob_check_trees()) + 1)):
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
# item (a transition, with its event) and the entry's mirrored `status` (which the queue's tick still reads until
# docs/design/one-change-model.md's step 4). The executor (`waiting check <id>`, holding check/<entry>) takes a change
# from queued to checking and, by its verdict, to checked, rejected (a test failure, a conflict) or back to queued (infra: never a rejection). The tick
# (tick_changes, every processor cycle) starts executors, re-queues a change whose checker lost its lease, and pushes a
# checked change under the landing lease: pushing -> landed, or back to checked (a push never rejects; after 3
# failures a condition on the git route). Main moving with an overlap is a new round (queued), never an attempt.
PUSH_ATTEMPTS_MAX = 3


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
    """THE writer of a change's state: ONE transition of its item carrying its record (state + data + event;
    docs/design/one-change-model.md). An item decided already (by a writer of the item alone: a cancel through the
    store, a stranded supersede) keeps its decision, and e takes it, its record merged (ChangeRecords.save). cause: why
    it moved, one of causes.CAUSES (a keyword every caller names: tests/test_causes.py); detail: a short qualifier."""
    cur = st.put_item("change", e["id"], change_state_of(e.get("status")) or "queued", change_item_fields(e))
    e.update(status=to, **fields)
    data = {**(_item_data(cur, to, data, fields) or {}), RECORD_KEY: entry_record(e)}
    if not _move_item(st, e["id"], cur, to, data, event, cause, detail):
        ChangeRecords(st, d).save(e)


def _move_item(st: Any, cid: str, cur: dict[str, Any], to: str, data: Any, event: dict[str, Any] | None, cause: str,
               detail: str | None) -> bool:
    """Change item cid (read as cur) moved to `to` for `cause` unless it is decided; a stale read is read again once.
    Whether it moved."""
    event = {**(event or {}), "cause": checked_cause(cause), **({"detail": detail} if detail else {})}
    if machine_state("change", cur["state"]).terminal:
        return False
    try:
        st.transition("change", cid, to, cur["version"], data=data, event=event)
        return True
    except StaleVersion:  # another writer moved it: re-read once
        cur = st.item("change", cid)
    if machine_state("change", cur["state"]).terminal:
        return False
    st.transition("change", cid, to, cur["version"], data=data, event=event)
    return True


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
    set_change(st, d, e, status, event=change_event(e, verdict, tail, code),
               cause=checked_cause(cause), detail=detail,
               data={"rechecks": e["rechecks"]} if e.get("rechecks") else None, decided=ts(), why=verdict, **fields)
    wlog(d, f"{e['id']}: {verdict.splitlines()[0]}")

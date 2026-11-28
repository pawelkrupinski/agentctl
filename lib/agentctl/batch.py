# agentctl, part `batch`: the batch machine: assembled at the tick, a pre-check, its steps from queued to pushed
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import subprocess
import sys
from contextlib import suppress
from typing import Any
from agentctl.context import Context
from agentctl.selfcode import TOOL_PATH
from agentctl.statemachines import StaleVersion, machine_state
from agentctl.timefmt import ts
from agentctl.queueknobs import BACKLOG_BATCH_MAX, knob_backlog_batch_max
from agentctl.govknobs import CHECK_LEASE_TTL
from agentctl.core import print_error, git, remote, spawn_detached
from agentctl.repos import precheck_hook
from agentctl.generated import without_generated
from agentctl.waitingdir import ChangeRecords, entry_final, wlog
from agentctl.storetoken import detached_env
from agentctl.batchitem import batch_move
from agentctl.queueslots import check_tree, check_tree_slot, set_change
from agentctl.queue import VERIFY_MODE, forces_full_recheck, range_paths, verify_supported
from agentctl.queuerecheck import read_overlap, recheck_reason
from agentctl.changekind import classify_change, facts_for, kind_text
from agentctl.waiting import batch_order, must_wait, must_wait_why
from agentctl.batchrun import assemble
from agentctl.batchselect import drift_serialize


# EVERY CHANGE IS A MEMBER (2026-11-20, the owner: "batching waiting changes ... structurally"): an identity change joins
# like any other -- before, it was always checked alone; for a few hours of 2026-11-20, one per batch. The batch's one
# full check runs the union of its members' areas, and G5's drift check (check_batch) of a tip holding more than one
# identity-model commit, with its split, tells which identity change moved takes.
BACKLOG_LIVE = ("checking", "bisecting", "pushing")  # a backlog (or push) batch's states that hold its members


def batch_kind(it):
    """`backlog batch` or `push batch`: how the member events name a batch item (by its depth)."""
    return "push batch" if ((it or {}).get("data") or {}).get("depth") == "verify" else "backlog batch"


def backlog_reconcile(d, st):
    """A backlog or push batch's members follow it: once it passed (pushing), each still `checking` is checked (a push
    batch's: verified, its own full check standing); a member it no longer carries (a prefix landing without it, the
    batch assembled again, held or gone) is queued again for the next batch (a push batch's: for its verify round, its
    full check kept). Returns what it did."""
    did = []
    for e in ChangeRecords(st, d).every():
        bid = e.get("backlog_batch")
        if not bid or entry_final(e.get("status")):
            continue
        it = st.item("batch", bid)
        if batch_carries(it, e):
            member_passes_with_batch(st, d, e, it)
        else:
            did.append(member_queued_again(st, d, e, it, bid))
    return did


def batch_carries(it: Any, e: Any) -> bool:
    """Whether the live batch item `it` (None: gone) still carries entry e among its members."""
    return bool(it) and it["state"] in BACKLOG_LIVE and e["id"] in ((it.get("data") or {}).get("members") or [])


def member_queued_again(st: Any, d: str, e: Any, it: Any, bid: str) -> str:
    """Entry e's batch no longer carries it: queued again for the next batch (a push that did not land is checked first);
    what it did, in words."""
    kind = batch_kind(it) if it else "batch"
    if e["status"] == "pushing":
        set_change(st, d, e, "checked", cause="batch", event={"text": f"{e['id']}: {kind} {bid}'s push did not land"})
    set_change(st, d, e, "queued", cause="batch", event={"text": f"{e['id']}: queued again: {kind} {bid} is "
                                              f"{it['state'] if it else 'gone'} without it"},
               backlog_batch=None, retry_after=0)
    return f"{e['id']}: queued again ({kind} {bid})"


def member_passes_with_batch(st: Any, d: str, e: Any, it: Any) -> None:
    """Member e of the live batch item `it`, once the batch is pushing: checked (a push batch's: verified on the batch's
    base with its own full check standing; a backlog batch's: its range check was the batch's)."""
    dd, bid = it.get("data") or {}, it["id"]
    if it["state"] != "pushing" or e["status"] != "checking":
        return
    if dd.get("depth") == "verify":
        set_change(st, d, e, "checked", cause="check-passed", event={"text": f"{e['id']}: verified {e['tip'][:10]} on {dd['base'][:10]} "
                                                   f"in push batch {bid} (fully checked on "
                                                   f"{(e.get('full_base') or '?')[:10]})"},
                   checked_base=dd["base"], checked_mode=VERIFY_MODE, checked_where=f"in push batch {bid}")
    else:
        set_change(st, d, e, "checked", cause="check-passed", event={"text": f"{e['id']}: checked in backlog batch {bid} ({e['tip'][:10]} "
                                                   f"on {dd['base'][:10]})"},
                   checked_base=dd["base"], full_base=dd["base"], full_tip=e["tip"], checked_mode="--range",
                   checked_where=f"in backlog batch {bid}")


# ONE ROUND, THE DEEPEST A MEMBER OWES (2026-11-23, batch #109: five of its seven changes were left out for a FULL
# round each -- main had changed what selects their checks -- and checked alone, one at a time, then verified one at a
# time behind each other: batch: 0 for hours). A push batch takes every change owing a round; when THE RULE sends one
# of them to a full round (against main plus the members before it), one is a queued change with no check of its own,
# or the assembly resolved a member's conflict, the batch's ONE round is the full landing check instead of the verify
# round -- nobody is left out, and every member lands in its one push.
def push_batch_full_round(tree: str, kept: list[dict[str, Any]], base: str) -> str | None:
    """Why the assembled push batch's one round must be the FULL landing check (member_full_round of each member
    against main plus the members before it), or None when the verify round suffices. Leaves the tree at its tip."""
    hook, prev, why = precheck_hook(tree), base, None
    for e in kept:
        why = member_full_round(tree, hook, e, prev, base)
        if why:
            break
        prev = e["tip"]
    git(tree, "checkout", "-q", "--force", "--detach", kept[-1]["tip"] if kept else base)
    return why


def member_full_round(tree: str, hook: str | None, e: dict[str, Any], prev: str, base: str) -> str | None:
    """Why push batch member e owes a FULL round on prev (main plus the members before it): never checked, its
    conflict resolved in the batch, or THE RULE (recheck_reason); None when its verify round suffices."""
    if e.get("status") == "queued" and not e.get("verify"):
        return f"{e['id']} has no check of its own"
    if e.get("autoresolved"):
        return f"{e['id']}'s conflict was resolved in the batch"
    git(tree, "checkout", "-q", "--force", "--detach", e["tip"])
    why = recheck_reason(tree, e.get("full_base") or e.get("checked_base") or base, prev, e["tip"],
                         range_paths(tree, prev, e["tip"]), full_tip=e.get("full_tip"))[1]
    if why is None and not verify_supported(hook):
        why = f"the hook has no {VERIFY_MODE} for the combined tree"
    return why and f"{e['id']}: {why}"


def batch_round_depth(tree: str, kept: list[dict[str, Any]], base: str, depth: str) -> tuple[str, str]:
    """(the batch's depth, what its full round is) once assembled: a push batch (`verify`) whose member owes a full
    round becomes `full` (ONE ROUND, THE DEEPEST A MEMBER OWES); a backlog batch's full round is its own."""
    why = push_batch_full_round(tree, kept, base) if depth == "verify" else None
    if why:
        return "full", f"a push batch, the full landing check: {why}"
    return depth, "a backlog: the full landing check"


def union_base(tree, members, base):
    """The push batch's verify range base: the oldest of its members' full-check bases (base when none is read)."""
    bases = sorted({e.get("full_base") or e.get("checked_base") or base for e in members})
    if len(bases) == 1:
        return bases[0]
    r = git(tree, "merge-base", "--octopus", *bases, check=False)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else base


_batch_said = [None]


def say_batch(d, lines):
    """The batch tick's lines: every one in processor.log, and in the reconciler's journal when they change (the
    journal had no batch line at all; a repeated `waiting for a check tree` is said once)."""
    for x in lines:
        wlog(d, f"tick: {x}")
    if lines and lines != _batch_said[0]:
        print_error("batch: " + "; ".join(lines))
    _batch_said[0] = lines or _batch_said[0]
    return lines


# BLOCKED BY WHAT MUST WAIT (2026-10-31 06:44-07:50Z). Main #2877 red; the one waiting change named it (Fixes-Red: 2877
# on one commit) but carried a production commit naming no red, so must_wait kept it for green. "A fix waits" sent the
# batch to assembling; assembling built nothing (every change must wait), said nothing, and its deadline set it
# assembling again every 5 min -- and an assembling batch goes first, so three checked changes that could land on red
# waited behind it for over an hour. Now red, a batch assembles only when something in it may land now (batch_ready);
# otherwise it is blocked-red, which holds no solo change, until the gate turns or a change that may go joins.
def batch_ready(repo, waiting, reds, st, *, ctx):
    """The changes a batch takes now, in batch order: red, only those that may land on red; after a drift-red batch at
    most one identity-model change (drift_serialize)."""
    order = batch_order(repo, waiting, reds, st, ctx=ctx)
    return drift_serialize(repo, [e for e in order if not reds or not must_wait(repo, e["commits"], reds, st, ctx=ctx)]
                           )[:waiting_batch_max()]


# A WAITING BATCH IS BOUNDED LIKE A BACKLOG BATCH (2026-11-20, the simulated 2026-11-19: with every production change
# joining `waiting` during a long red, the batch at green held 21-31 changes; one failure bisected across all of them and
# the p90 wait from arrival to LANDED doubled). It takes at most BACKLOG_BATCH_MAX waiting changes a round, in batch
# order -- its red pre-check the same ones --; the rest wait for the next round.
def waiting_batch_max():
    return int(knob_backlog_batch_max())


# THE BATCH IS THE MECHANISM (the owner, 2026-11-20: "batching waiting changes ... structurally, not just now"). While
# the gate is red each production change that must wait is still checked on its own, in parallel in the free check
# trees (its `--checks-only`), and joins `waiting`; the waiting batch -- blocked by the red -- then PRE-CHECKS every
# waiting change as ONE tree on the current main: assembled in batch order (one that no longer applies rejected alone,
# a generated file merged), checked once with `--checks-only` over the union of their areas (depth `red`), a failure
# bisected (the culprit rejected when the failure is its own; one failing only where main fails rejects nobody), and
# the verdict kept on the batch (`prechecked`: its members, base and tip) and on each member. It pre-checks again
# whenever its membership changes (main moving while red is judged once, at green). At green the batch, at most
# BACKLOG_BATCH_MAX changes, is assembled on the new main and lands in ONE push (green_batch_depth): its members exactly
# the pre-checked ones and main's move since read by none of their checks (MINIMAL RECHECKS, queuerecheck.py read_overlap) ->
# no check again; else the fast check of the combined tree, as before. Measured (the simulator over 2026-11-18..20's
# arrivals, 20 seeds a day, real round lengths): joining `waiting` unchecked instead, the batch's one tree doing every
# change's check, cut check time 8% but put the p90 wait at 274 ticks against 158 (the red's idle trees went unused);
# checked alone first, then pre-checked as one: p90 100. A red fix keeps its lane (2026-11-17..20: a red fix landed
# in a median 14 min, a batch member in 56).
def _precheck_stands(it: Any, order: Any) -> Any:
    """AGAIN ONLY WHEN ITS MEMBERS CHANGE: main moving while red (a tools-only change, the fix) is judged once, at green,
    by MINIMAL RECHECKS -- pre-checking again on every move cost the simulated 2026-11-18..20 ~20% more check time. A
    pre-check that failed as it is stands too: changes must come or go."""
    ids = [e["id"] for e in order]
    return (it["data"].get("prechecked") or {}).get("members") == ids \
        or (it["data"].get("precheck_failed") or [None])[1:] == ids


def red_precheck(repo, d, st, it, waiting, reds, *, ctx):
    """The blocked-red waiting batch's step: pre-check every waiting change as one tree when its membership or main
    changed since the last pre-check and a check tree is free (THE BATCH IS THE MECHANISM, above)."""
    order = [e for e in batch_order(repo, waiting, reds, st, ctx=ctx) if must_wait(repo, e["commits"], reds, st, ctx=ctx)][:waiting_batch_max()]
    if not order:
        return []
    if _precheck_stands(it, order):
        return []
    slot = check_tree_slot(st, repo, d)
    if not slot:
        return [f"batch {it['id']}: its red pre-check waits for a check tree"]
    i, h = slot
    try:
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
        base = git(repo, "rev-parse", f"{remote()}/main").strip()
        tree = check_tree(repo, i, base)
        kept = assemble(st, repo, d, tree, order, base)
        tip = git(tree, "rev-parse", "HEAD").strip()
    finally:
        h.release()
    if not kept:
        return [f"batch {it['id']}: nothing to pre-check"]
    batch_move(st, it, "checking", {"members": [e["id"] for e in kept], "tip": tip, "base": base, "tree": i,
                                    "cls": "check", "depth": "red"},
               text=f"batch {it['id']}: pre-checking {len(kept)} waiting change(s) as one at {tip[:10]} on {base[:10]} "
                    "while main is red")
    spawn_batch_executor(repo, d, it["id"])
    return [f"batch {it['id']}: red pre-check of {len(kept)} change(s) at {tip[:10]}"]


def _is_precheck_of(pre: Any, kept: Any) -> Any:
    """The recorded red pre-check (with its base) was of exactly these members."""
    return bool(pre) and pre.get("members") == [e["id"] for e in kept] and bool(pre.get("base"))


def green_batch_depth(tree, it, kept, base, tip):
    """(depth, why) of the waiting batch at green (THE BATCH IS THE MECHANISM): `kept` when its members are exactly its
    red pre-check's and no check of theirs reads what main changed since, else `fast` (the combined tree's fast check,
    as before)."""
    pre = it["data"].get("prechecked") or {}
    if not _is_precheck_of(pre, kept):
        return "fast", "not its red pre-check's members"
    if pre["base"] == base:
        return "kept", f"pre-checked as one on {base[:10]}, main unchanged since"
    landed, mine = range_paths(tree, pre["base"], base), range_paths(tree, base, tip)
    if any(forces_full_recheck(p) for p in landed) or without_generated(tree, sorted(set(landed) & set(mine))):
        return "fast", "main changed what it changes"
    why = read_overlap(tree, landed, mine)
    if why:
        return "fast", why
    n = len(git(tree, "rev-list", f"{pre['base']}..{base}", check=False).stdout.split())
    return "kept", (f"pre-checked as one on {pre['base'][:10]}; main moved by {n} commit{'' if n == 1 else 's'} since, "
                    "no check of it reading what changed: its verdict kept")


# A RED FIX IS NEVER A BATCH MEMBER, AT GREEN TOO (2026-11-20; the simulator gate's chaos-19-t1, and five more chaos seeds
# of a wider sweep: a fix of a red OUTSIDE the gate waited on `waiting` while the gate was red -- its other production
# commits name no gate red -- and at green the waiting batch took it with the rest, so it waited for their combined
# check). At green, before a new waiting batch forms, each waiting change that fixes an open red goes back to the queue,
# where it goes first, alone, in the red-fix tree (A FIX OF ANY OPEN RED GOES FIRST, changekind.py).
#
# WR SELF-HEALS (the owner, 2026-11-25, batch #124: a fix naming #2896 waited for the green of the open red #2898 for
# hours, WR said so, nobody acted): the same release runs while the gate is RED, for each waiting change the store's open
# red names (its episode: changekind.py queue_facts) whose commits must_wait no longer holds -- it goes back to the queue,
# first and alone, and a batch is left to the changes that may wait. One the rule still holds is told what holds it
# (`wait_rule` on its item: WR and the liveness line print it), the one escalation left.
def release_red_fixes(repo: Any, d: Any, st: Any, reds: Any, waiting: Any, t: float, *, ctx: Context) -> list[str]:
    facts = facts_for(reds, st)
    out = []
    for e in waiting:
        k = classify_change(e, facts, t, ctx=ctx)
        if not k["red_fix"]:
            continue
        why = must_wait_why(repo, e["commits"], reds, st, ctx=ctx) if reds else None
        if why:
            note_wait_rule(st, e, why)
            continue
        text = "main is green" if not reds else "the open red names it, so it never waits for green"
        set_change(st, d, e, "queued", cause="gate-red", data={"wait_rule": None},
                   event={"text": f"{e['id']}: {text} -- a fix of an open red ({kind_text(k)}) "
                                  "is checked alone and first, never a batch member", "severity": "info"},
                   waiting_released=ts())
        out.append(e["id"])
    return out


def note_wait_rule(st: Any, e: dict[str, Any], why: str) -> None:
    """The rule holding waiting change e, on its item (once per wording); a moved item is read again by the next tick."""
    it = st.item("change", e["id"])
    if it and (it.get("data") or {}).get("wait_rule") != why and not machine_state("change", it["state"]).terminal:
        with suppress(StaleVersion):
            st.update_item("change", e["id"], it["version"], {"wait_rule": why})


def batch_row_ids(bt: dict[str, Any] | None, rows: list[dict[str, Any]]) -> set[str]:
    """The change rows the batch carries: its members, and a change that joined one of them (the same worktree: a
    queued change that met a red gate before ONE CHANGE, ONE ITEM, queue_join, became a waiting entry of its own, so
    its item follows that entry)."""
    if not bt:
        return set()
    members = set((bt.get("data") or {}).get("members") or [])
    wts = {r["worktree"] for r in rows if r["id"] in members}
    return {r["id"] for r in rows if r["id"] in members or (r["state"] == "waiting" and r["worktree"] in wts)}


def spawn_batch_executor(repo, d, bid):
    with open(os.path.join(d, "processor.log"), "a") as logf:
        spawn_detached([sys.executable, TOOL_PATH, "waiting", "check-batch", bid, "--repo", repo],
                         stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True, cwd=repo,
                         env=detached_env(repo))


# THE DRIFT CHECK WAITS QUIETLY (2026-10-30): identity-drift-ci prints its header and then nothing while its CI run goes
# on, so the batch executor's leases (CHECK_LEASE_TTL, 10 min, renewed on output or CPU only) lapsed at 10 min every
# time (#1, #3, #4: "lease ... was lost: ending drift", no verdict), and the freed check tree went to a change. It runs
# quiet_ok: every poll renews, and DRIFT_STALL alone ends it.



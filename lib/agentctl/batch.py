# agentctl, part `batch`: the batch machine: assembled at the tick, a pre-check, its steps from queued to pushed
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import subprocess
import sys
from typing import Any
from agentctl.core import env_num, err, fmt_age, git, remote, spawn_detached
from agentctl.timefmt import ts
from agentctl.statefile import read_json_file, write_json_file
from agentctl.selfcode import TOOL_PATH
from agentctl.repos import precheck_hook
from agentctl.waitingdir import ENTRY_FINAL, save_entry, waiting_entries, wlog
from agentctl.storetoken import detached_env
from agentctl.leases import lease_live
from agentctl.land import landed
from agentctl.queueslots import CHECK_LEASE_TTL, check_tree, check_tree_slot, set_change
from agentctl.conditions import open_condition, run_probe
from agentctl.queue import VERIFY_MODE, forces_full_recheck, land_hold, range_paths, verify_supported
from agentctl.generated import without_generated
from agentctl.queuerecheck import queued_entries, read_overlap, recheck_logged, recheck_reason
from agentctl.changekind import classify_change, gate_facts, kind_text, queue_facts
from agentctl.waiting import batch_order, must_wait
from agentctl.redseen import gate_reds
from agentctl.recovery import batch_key, batch_move, current_batch
from agentctl.queuepush import (
    BACKLOG_BATCH_MAX, DEEP_BATCHES, backlog_candidates, backlog_min, check_tree_slot_free, drift_serialize,
    push_candidates)
from agentctl.batchitem import batch_fail, new_batch
from agentctl.batchpush import push_batch_item
from agentctl.batchrun import assemble, unread_note
from agentctl.statemachines import machine_state


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
    for e in waiting_entries(d, open_only=False):
        bid = e.get("backlog_batch")
        if not bid or e.get("status") in ENTRY_FINAL:
            continue
        it = st.item("batch", bid)
        dd = (it or {}).get("data") or {}
        kind = batch_kind(it) if it else "batch"
        if it and it["state"] in BACKLOG_LIVE and e["id"] in (dd.get("members") or []):
            if it["state"] == "pushing" and e["status"] == "checking" and dd.get("depth") == "verify":
                set_change(st, d, e, "checked", event={"text": f"{e['id']}: verified {e['tip'][:10]} on {dd['base'][:10]} "
                                                           f"in push batch {bid} (fully checked on "
                                                           f"{(e.get('full_base') or '?')[:10]})"},
                           checked_base=dd["base"], checked_mode=VERIFY_MODE, checked_where=f"in push batch {bid}")
            elif it["state"] == "pushing" and e["status"] == "checking":
                set_change(st, d, e, "checked", event={"text": f"{e['id']}: checked in backlog batch {bid} ({e['tip'][:10]} "
                                                           f"on {dd['base'][:10]})"},
                           checked_base=dd["base"], full_base=dd["base"], full_tip=e["tip"], checked_mode="--range",
                           checked_where=f"in backlog batch {bid}")
            continue
        if e["status"] == "pushing":
            set_change(st, d, e, "checked", event={"text": f"{e['id']}: {kind} {bid}'s push did not land"})
        set_change(st, d, e, "queued", event={"text": f"{e['id']}: queued again: {kind} {bid} is "
                                                  f"{it['state'] if it else 'gone'} without it"},
                   backlog_batch=None, retry_after=0)
        did.append(f"{e['id']}: queued again ({kind} {bid})")
    return did


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


# NO SILENT WAIT AT GREEN (2026-10-30). Changes waiting while the gate is green, with no hold and no batch checking,
# bisecting, pushing or held, are a stall: counted per tick (batch-progress.json in the waiting dir) and, past
# WAITING_GREEN_TICKS ticks, a STALLED condition. Its probe (batch_progress) reads the same count, so the tick that
# finds the batch moving again -- or nothing waiting, or the gate red -- is the one that clears it.
WAITING_GREEN_TICKS = 100  # ticks (~10 min) changes may wait at a green gate with no batch moving: then STALLED
BATCH_MOVING = ("checking", "bisecting", "pushing", "held")  # held: its own STALLED condition says so


def batch_progress_path(d: str) -> str:
    return os.path.join(d, "batch-progress.json")


def note_batch_progress(d, seen, t, st):
    """Count this tick: stalled (changes wait, gate green, no hold, no batch moving) or not. Returns the count."""
    waiting, it = seen.get("waiting") or [], seen.get("batch")
    stalled = (bool(waiting) and seen.get("reds") is not None and not seen["reds"] and not seen.get("held")
               and not (it and it["state"] in BATCH_MOVING))
    p = batch_progress_path(d)
    rec = read_json_file(p)
    if not stalled:
        if rec.get("ticks"):
            write_json_file(p, {"ticks": 0, "at": t})
        return 0
    n = int(rec.get("ticks") or 0) + 1
    since = float(rec.get("since") or t) if rec.get("ticks") else t
    why = (f"batch {it['id']} is {it['state']}" + (f" ({seen['last']})" if seen.get("last") else "")) if it else \
        "no batch was formed" + (f" ({seen['last']})" if seen.get("last") else "")
    write_json_file(p, {"ticks": n, "at": t, "since": since, "why": why, "waiting": [e["id"] for e in waiting]})
    limit = env_num("AGENTCTL_WAITING_GREEN_TICKS", WAITING_GREEN_TICKS)
    if n >= limit:
        ids = ", ".join(e["id"] for e in waiting)
        open_condition("STALLED", f"STALLED: {len(waiting)} change(s) waiting while the gate is green for {n} ticks "
                                  f"({fmt_age(t - since)}): {why}; waiting: {ids}",
                       run_probe("batch_progress", {"d": d}), [], key=f"waiting-green-{batch_key(d)}",
                       command="waiting", store=st)
    return n


def probe_batch_progress(a):
    """The batch tick no longer counts a stall at green (batch-progress.json under the limit)."""
    rec = read_json_file(batch_progress_path(a["d"]))
    n = int(rec.get("ticks") or 0)
    limit = env_num("AGENTCTL_WAITING_GREEN_TICKS", WAITING_GREEN_TICKS)
    return n < limit, (f"{n} stalled tick(s) at green: {rec.get('why')}" if n else "no change waits at green")


def tick_batch(repo, d, clock, st):
    """One tick of the repository's batch, then its stall count (note_batch_progress). Returns what it did."""
    seen = {}
    did = _tick_batch(repo, d, st, seen)
    try:
        seen["last"] = did[-1] if did else None
        if seen.get("waiting"):  # the batch as this tick left it (assembled -> checking is no stall)
            seen["batch"] = current_batch(st, d)
        note_batch_progress(d, seen, clock.time(), st)
    except (Exception, SystemExit) as e:  # never the batch's own step
        err(f"batch progress: {str(e)[:160]}")
    return did


_batch_said = [None]


def say_batch(d, lines):
    """The batch tick's lines: every one in processor.log, and in the reconciler's journal when they change (the
    journal had no batch line at all; a repeated `waiting for a check tree` is said once)."""
    for x in lines:
        wlog(d, f"tick: {x}")
    if lines and lines != _batch_said[0]:
        err("batch: " + "; ".join(lines))
    _batch_said[0] = lines or _batch_said[0]
    return lines


# BLOCKED BY WHAT MUST WAIT (2026-10-31 06:44-07:50Z). Main #2877 red; the one waiting change named it (Fixes-Red: 2877
# on one commit) but carried a production commit naming no red, so must_wait kept it for green. "A fix waits" sent the
# batch to assembling; assembling built nothing (every change must wait), said nothing, and its deadline set it
# assembling again every 5 min -- and an assembling batch goes first, so three checked changes that could land on red
# waited behind it for over an hour. Now red, a batch assembles only when something in it may land now (batch_ready);
# otherwise it is blocked-red, which holds no solo change, until the gate turns or a change that may go joins.
def batch_ready(repo, waiting, reds, st):
    """The changes a batch takes now, in batch order: red, only those that may land on red; after a drift-red batch at
    most one identity-model change (drift_serialize)."""
    order = batch_order(repo, waiting, reds, st)
    return drift_serialize(repo, [e for e in order if not reds or not must_wait(repo, e["commits"], reds, st)]
                           )[:waiting_batch_max()]


# A WAITING BATCH IS BOUNDED LIKE A BACKLOG BATCH (2026-11-20, the simulated 2026-11-19: with every production change
# joining `waiting` during a long red, the batch at green held 21-31 changes; one failure bisected across all of them and
# the p90 wait from arrival to LANDED doubled). It takes at most BACKLOG_BATCH_MAX waiting changes a round, in batch
# order -- its red pre-check the same ones --; the rest wait for the next round.
def waiting_batch_max():
    return int(env_num("AGENTCTL_BACKLOG_BATCH_MAX", BACKLOG_BATCH_MAX))


def _tick_batch(repo, d, st, seen):
    """A backlog batch's members brought in line with it (backlog_reconcile), then the batch's step. st: the tick's
    store, on the tick's clock (batch_step and the push read the time on it)."""
    return backlog_reconcile(d, st) + batch_step(repo, d, seen, st)


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
def red_precheck(repo, d, st, it, waiting, reds):
    """The blocked-red waiting batch's step: pre-check every waiting change as one tree when its membership or main
    changed since the last pre-check and a check tree is free (THE BATCH IS THE MECHANISM, above)."""
    order = [e for e in batch_order(repo, waiting, reds, st) if must_wait(repo, e["commits"], reds, st)][:waiting_batch_max()]
    if not order:
        return []
    # AGAIN ONLY WHEN ITS MEMBERS CHANGE: main moving while red (a tools-only change, the fix) is judged once, at green,
    # by MINIMAL RECHECKS -- pre-checking again on every move cost the simulated 2026-11-18..20 ~20% more check time
    pre = it["data"].get("prechecked") or {}
    if pre.get("members") == [e["id"] for e in order]:
        return []
    if (it["data"].get("precheck_failed") or [None])[1:] == [e["id"] for e in order]:  # failed as it is: changes must
        return []  # come or go
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


def green_batch_depth(tree, it, kept, base, tip):
    """(depth, why) of the waiting batch at green (THE BATCH IS THE MECHANISM): `kept` when its members are exactly its
    red pre-check's and no check of theirs reads what main changed since, else `fast` (the combined tree's fast check,
    as before)."""
    pre = it["data"].get("prechecked") or {}
    if not pre or pre.get("members") != [e["id"] for e in kept] or not pre.get("base"):
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
def release_red_fixes(d, st, reds, waiting, t):
    facts = queue_facts(reds, st)
    out = []
    for e in waiting:
        k = classify_change(e, facts, t)
        if not k["red_fix"]:
            continue
        set_change(st, d, e, "queued", event={"text": f"{e['id']}: main is green -- a fix of an open red ({kind_text(k)}) "
                                                  "is checked alone and first, never a batch member", "severity": "info"},
                   waiting_released=ts())
        out.append(e["id"])
    return out


def batch_step(repo, d, seen, st):
    t = st.clock()
    waiting = waiting_entries(d)
    it = current_batch(st, d)
    seen.update(waiting=waiting, batch=it)
    backlog = not waiting and not it and \
        sum(1 for e in queued_entries(d) if e.get("status") in ("queued", "checked")) >= backlog_min()
    if not waiting and not it and not backlog:
        return []
    if land_hold(st, t):  # the store's hold: honoured wherever `land hold` ran
        seen["held"] = True
        return ["held"]
    reds = gate_reds(repo)
    seen["reds"] = reds
    waiting = waiting_entries(d)
    released = release_red_fixes(d, st, reds, waiting, t) \
        if not reds and (not it or it["state"] in ("blocked-red", "assembling")) else []
    if released:
        waiting = waiting_entries(d)
    depth = (it or {}).get("data", {}).get("depth") or "fast"
    fix_waits = bool(reds) and any(classify_change(e, gate_facts(reds), t)["gate_fix"] for e in waiting)
    if not waiting and (not it or (depth in DEEP_BATCHES and it["state"] == "assembling")):
        # THE PUSH BATCH (checked changes, each owing a verify round) before THE BACKLOG BATCH (queued changes), no
        # waiting change; created only with a check tree free for it (an assembling batch waiting for one holds every
        # solo check behind it, and is held itself past its 5-min deadline)
        facts = queue_facts(reds, st)
        push = push_candidates(repo, d, reds, st, t, facts) if not it or depth == "verify" else []
        ready = push or (backlog_candidates(repo, d, reds, st, t, facts) if not it or depth == "full" else [])
        if not it:
            if not ready or not check_tree_slot_free(st, repo, d):
                return []
            it = new_batch(st, d, "assembling", depth="verify" if push else "full")
        elif not ready:  # the gate went red, or the changes went their own ways: no batch to assemble
            batch_move(st, it, "landed", {"members": []}, text=f"batch {it['id']}: no {batch_kind(it)} left to form")
            return [f"batch {it['id']}: no {batch_kind(it)} left to form"]
        depth = it["data"]["depth"]
    else:
        if not waiting and not it:
            return []
        unread_note(d, reds, waiting, st)
        ready = (batch_ready(repo, waiting, reds, st) if (not reds or fix_waits)
                 and (not it or it["state"] in ("blocked-red", "assembling")) else [])
    go = bool(ready)  # red: a fix waits AND something may land on red now (see BLOCKED BY WHAT MUST WAIT)
    if not it:
        it = new_batch(st, d, "assembling" if go else "blocked-red")
    spec = machine_state("batch", it["state"])
    if t > it["deadline"] and it["state"] != "checking" and it["state"] != "bisecting":
        it = batch_move(st, it, spec.on_deadline, text=f"batch {it['id']}: {it['state']} past its deadline")
        return [f"batch {it['id']}: deadline -> {it['state']}"]
    state = it["state"]
    if state == "blocked-red":
        if go:
            batch_move(st, it, "assembling")
            return [f"batch {it['id']}: assembling"]
        return red_precheck(repo, d, st, it, waiting, reds)
    if state == "assembling":
        if reds and not go:
            batch_move(st, it, "blocked-red")
            return [f"batch {it['id']}: blocked by the red gate"
                    + (" (its fix waits for green: a production commit names no red)" if fix_waits else "")]
        batch = ready
        if not batch:
            if not waiting:
                batch_move(st, it, "landed", {"members": []}, text=f"batch {it['id']}: nothing left to land")
            return []
        slot = check_tree_slot(st, repo, d)
        if not slot:
            return [f"batch {it['id']}: waiting for a check tree"]
        i, h = slot
        full = depth in DEEP_BATCHES
        tips = {e["id"]: e.get("tip") for e in batch}
        try:
            git(repo, "fetch", "--quiet", remote(), "main", check=False)
            base = git(repo, "rev-parse", f"{remote()}/main").strip()
            tree = check_tree(repo, i, base)
            kept = assemble(st, repo, d, tree, batch, base, alone=full, autoresolve=full)
            depth, full_round = batch_round_depth(tree, kept, base, depth)
            tip = git(tree, "rev-parse", "HEAD").strip()
            range_base = union_base(tree, kept, base) if depth == "verify" else None
        finally:
            h.release()
        if full and len(kept) < backlog_min():  # one left: it is checked (a push batch's: pushed) alone, as any change
            for e in kept:
                if e.get("status") == "checked" and tips.get(e["id"]):
                    e["tip"] = tips[e["id"]]
                    save_entry(d, e)
            batch_move(st, it, "landed", {"members": []}, text=f"batch {it['id']}: no {batch_kind(it)} left to form")
            return [f"batch {it['id']}: no {batch_kind(it)} left to form"]
        if not kept:
            if not waiting_entries(d):
                batch_move(st, it, "landed", {"members": []}, text=f"batch {it['id']}: nothing left to land")
            return [f"batch {it['id']}: nothing kept"]
        if not full:  # THE WAITING BATCH AT GREEN: pre-checked as one at red, its verdict kept, or checked as needed
            depth, why = green_batch_depth(tree, it, kept, base, tip)
            if depth == "kept":
                moved = (it["data"].get("prechecked") or {}).get("base") != base
                for e in kept if moved else ():  # MINIMAL RECHECKS' log, as a lone change's (land status, kept_verdicts)
                    e["rechecks"] = recheck_logged(e, "kept", why, base, t)
                    save_entry(d, e)
                batch_move(st, it, "checking", {"members": [e["id"] for e in kept], "tip": tip, "base": base, "tree": i,
                                                "cls": "check", "depth": "fast"}, text=f"batch {it['id']}: {why}")
                batch_move(st, st.item("batch", it["id"]), "pushing", {"kept": why},
                           text=f"batch {it['id']}: {tip[:10]} pushed on its red pre-check, no check again")
                return [f"batch {it['id']}: assembled {len(kept)} change(s), {why}"]
        batch_move(st, it, "checking", {"members": [e["id"] for e in kept], "tip": tip, "base": base, "tree": i,
                                        "cls": "red" if fix_waits else "check", "depth": depth,
                                        **({"range_base": range_base} if range_base else {})},
                   text=f"batch {it['id']}: checking {len(kept)} change(s) at {tip[:10]} on {base[:10]}"
                        + {"full": f" ({full_round})",
                           "verify": f" (a push batch: one verify round over {(range_base or base)[:10]}..{tip[:10]})"}
                        .get(depth, ""))
        for e in kept if full else ():  # its members are the batch's until it lands them or lets them go
            verify = {"verify": True, "rounds": e.get("rounds", 0) + (e.get("status") == "checked")} \
                if depth == "verify" else {}
            resolved = "; ".join(f"{m['file']} ({', '.join(m['hunks'])})" for m in e.get("autoresolved") or [])
            set_change(st, d, e, "checking", data={"autoresolved": resolved or None},
                       event={"text": f"{e['id']}: checking in {batch_kind(it)} {it['id']} "
                                      f"({len(kept)} change(s) at {tip[:10]})"
                                      + (f"; its conflict AUTO-RESOLVED in the batch, the batch's full check its "
                                         f"verification: {resolved}" if resolved else ""),
                              **({"audience": {"worktree": e.get("worktree")}} if resolved else {})},
                       backlog_batch=it["id"], since=ts(), round_since=ts(), **verify)
        spawn_batch_executor(repo, d, it["id"])
        return [f"batch {it['id']}: assembled {len(kept)} change(s), checking {tip[:10]}"]
    if state in ("checking", "bisecting"):
        if not lease_live(f"check/batch-{it['id']}", st):
            if t > it["deadline"]:
                return [batch_fail(st, d, it, "its check passed its deadline with no live checker")["state"]]
            spawn_batch_executor(repo, d, it["id"])
            return [f"batch {it['id']}: its executor started again"]
        return []
    if state == "pushing":
        return [push_batch_item(repo, d, st, it)]
    return []


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



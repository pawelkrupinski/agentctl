# agentctl, part `queuepush`: pushing a checked change: the oldest verification first, a red fix's verification kept.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import time
from agentctl.core import env_num, fmt_age, git, host_role, remote
from agentctl.timefmt import day_hhmmz, hhmmz
from agentctl.policy import policy_blocks
from agentctl.waitingdir import waiting_entries
from agentctl.queueslots import change_item_fields, check_tree_slot, checker_alive, queue_verdict, set_change
from agentctl.queue import requeue_infra
from agentctl.conflicts import conflict_cause, conflict_paths
from agentctl.queuerecheck import queued_entries, same_change_source, verdict_kept
from agentctl.identitychange import identity_model_commits
from agentctl.changekind import (
    change_hold_visible, classify_change, classify_queue, file_unheld_change, kind_order, queue_facts,
    record_change_kind)
from agentctl.waiting import supersede
from agentctl.redseen import gate_reds
from agentctl.holdgraph import OPEN_PARENT_STATES, push_due, stack_map, waiting_on_changes
from agentctl.queuebatch import (
    _behind_said, backlog_batchable, batch_round, break_hold_cycles, change_hold_stuck, hold_for_overlap, kind_of, main_conflict, note_hold,
    overlap_hold, restart_hold, spawn_executor, stuck_recovery)
from agentctl.queuediff import push_change
from agentctl.queueclose import regen_hold_overdue
from agentctl.recovery import current_batch
from agentctl.statemachines import ENTRY_STATE, StaleVersion


# THE OLDEST VERIFICATION PUSHES FIRST (2026-11-16, workermem: fully checked by 09:15, then its combined tree verified
# four times, each ~10 min, each on a main a YOUNGER change had pushed onto meanwhile, until a hook change forced a full
# round; 10 h unlanded). Main moving under a verification sends it round again, so a change verifies until no push
# lands during its verification -- with a steady stream of younger changes, never. While a change verifies (a round
# queued or checking with `verify`, not in an infra backoff), every younger checked change holds its push (a red's fix
# excepted) and the verification takes the next free check tree: it lands after ONE more round, and the younger ones
# verify after it.
def verifying(e, t, waits=frozenset()):
    """Whether entry e is in a combined-tree verification round that can run now: not in an infra backoff, nor -- its
    id in waits (holdgraph.py waiting_on_changes) -- held behind another change (A HELD VERIFICATION HOLDS NOTHING)."""
    return bool(e.get("verify")) and e.get("status") in ("queued", "checking") and not e.get("retry_after", 0) > t \
        and e["id"] not in waits


# A RED FIX'S VERIFICATION IS NEVER THROWN AWAY BY AN ORDINARY PUSH (2026-11-19: 08010c, `Fixes-Red: 6`, verified its
# combined tree 09:34-10:18Z; at 10:11Z ratchet-diff -- queued BEFORE it, so not held by the age rule -- pushed, main
# moved, and the fix verified again until 10:36Z, holding 100213 and 102400 behind it the whole time). A red fix goes
# first (changekind.py PRIORITY): while its verification runs, every ordinary checked change holds its push, older or
# younger. Tests: tests/test_backlog_batch.py PushBatchTest ..._never_pushes_under_a_red_fixs_verification.
def older_verifying(todo, e, t, kinds=None, waits=frozenset()):
    """The entry whose combined-tree verification runs now (verifying) and that e's push waits for: one queued before
    e, or a red fix (its kind in kinds, classify_queue) queued at any time; the oldest such, or None. A blocker e's
    cycle_ignore names (holdgraph.py A HOLD CYCLE IS BROKEN) holds it no more."""
    red = lambda o: bool(((kinds or {}).get(o["id"]) or {}).get("red_fix"))
    ignore = set(e.get("cycle_ignore") or [])
    return next((o for o in sorted(todo, key=lambda o: o["seq"])
                 if o["id"] != e["id"] and o["id"] not in ignore and (o["seq"] < e["seq"] or red(o))
                 and verifying(o, t, waits)), None)


def tick_changes(repo, d, clock, st):
    """One tick over the change items: re-queueing re-derived (only the newest entry per worktree and branch runs), an
    executor started for each queued or lease-less checking change (red fixes first, while a slot is free), a
    checking change past its deadline back to queued (infra), a queued one past its deadline escalated, and each
    checked change pushed. Returns what it did."""
    t = clock.time()
    try:
        reds = gate_reds(repo)
    except Exception:
        reds = set()
    facts = queue_facts(reds, st)  # A FIX OF ANY OPEN RED GOES FIRST (above): each change's KIND, recorded on its item
    entries = queued_entries(d)
    kinds = classify_queue(repo, d, entries, facts, t, st)
    todo = sorted(entries, key=lambda e: kind_order(kinds, e))  # THE PRIORITY ORDER (changekind.py PRIORITY)
    newest = {}
    for e in sorted(todo, key=lambda e: e["seq"]):  # entries queued before re-queueing replaced: the newest one runs
        for k, older in list(newest.items()):
            if same_change_source(older, e["worktree"], e.get("branch") or ""):
                supersede(st, d, older, e["id"], f"{e['worktree']} queued again; only the newest entry runs")
                del newest[k]
        newest[e["id"]] = e
    runs = [e for e in todo if e["id"] in newest]
    did = break_hold_cycles(st, d, runs)  # A HOLD CYCLE IS BROKEN (holdgraph.py)
    stacks = stack_map(todo, waiting_entries(d, open_only=False))  # A STACKED CHANGE WAITS FOR ITS PARENT
    # PUSHES FIRST, THEN THE HOLDS (queue simulator, 2026-11-18): the holds below were read before this tick's pushes,
    # and a checked change's push due holds every backlog batch back (backlog_candidates) -- so the tree a finished
    # check had just freed went to the oldest lone check, never to the batch: under load (every tree busy, a tree
    # freed only by a check ending) no backlog batch ever formed (seed 0 of the simulator's load profile: 40 changes,
    # 40 lone checks, 0 batches). The checked changes are pushed in a first pass; the holds are read again for the
    # second, which starts checks.
    for phase in ("push", "check"):
        held = backlog_held(repo, d, reds, st, t, facts)
        pheld = push_held(repo, d, reds, st, t, facts)
        # THE BATCH KEEPS ITS AGE-ORDER PLACE (review 2026-11-18): this tick runs before the batch tick, and the
        # changes held for a backlog or push batch take no tree, so a tree freed went to the next YOUNGER lone change
        # and the batch tick found none: with a steady stream of lone changes (identity changes, --no-batch) the batch
        # never formed. A lone change younger than the oldest held one never takes the LAST free tree: that is the
        # batch's (a red fix excepted).
        batch_seq = min((x["seq"] for x in todo if x["id"] in held | pheld), default=None)
        did += _tick_changes_pass(repo, d, st, clock, t, facts, kinds, phase, runs, todo, held, pheld, batch_seq,
                                  stacks)
    return did


def _tick_changes_pass(repo, d, st, clock, t, facts, kinds, phase, mine, todo, held, pheld, batch_seq, stacks):
    """One pass of tick_changes over the newest entries: `push` (the checked and interrupted pushes) or `check` (the
    rest: executors started, deadlines). facts: queue_facts; kinds: each entry's kind (classify_queue); stacks: each
    stacked entry's parent (holdgraph.py stack_map)."""
    did = []
    for e in mine:
        if e.get("backlog_batch"):  # a backlog batch's member: the batch checks and pushes it (backlog_reconcile)
            continue
        if e["status"] not in (("checked", "pushing") if phase == "push" else ("queued", "checking")):
            continue  # (a push this tick made it landed, waiting or rejected: no check for it)
        it = st.put_item("change", e["id"], ENTRY_STATE.get(e["status"], "queued"), change_item_fields(e))
        state = it["state"]
        if state == "pushing":  # a tick that died mid-push: verified next, never pushed twice blindly
            set_change(st, d, e, "checked", event={"text": f"{e['id']}: its push was interrupted: verified, then tried again"})
            state = "checked"
        kind = kinds.get(e["id"]) or classify_change(e, facts, t)
        record_change_kind(st, e, kind)  # on its item, now that it surely has one (a no-op when unchanged)
        fix = kind["red_fix"]
        if state == "checked":
            if e.get("retry_after", 0) > t:
                note_hold(st, e, "retry", f"its push is retried at {hhmmz(e['retry_after'])}", t)
                continue
            did += _push_checked(repo, d, st, clock, t, e, fix, todo, pheld, kinds)
            continue
        alive = checker_alive(e["id"])
        if state == "checking" and not alive and t > it["deadline"]:
            requeue_infra(st, d, e, "its check passed its deadline with no live checker", t)
            did.append(f"{e['id']}: re-queued (checking past its deadline)")
            continue
        if state == "queued":  # past its deadline: said, re-deadlined -- an escalation only when its hold is stuck
            lap_change_deadline(st, e, "queued", t)  # (A HELD WAIT IS NO ESCALATION, below)
        if state == "queued" and not alive:
            gone = main_conflict(repo, e)
            if gone:  # it no longer applies on main itself: said now, never after a wait for a tree or an older change
                # policy: no-longer-applies
                queue_verdict(st, d, e, "rejected", f"NOT LANDED: the change no longer applies on {remote()}/main "
                              f"{gone[0][:10]} (main moved since QUEUED{conflict_cause(repo, e.get('base'), gone[0], gone[1])}"
                              f"): rebase onto {remote()}/main and land again (it keeps its place in the queue)",
                              conflict_files=conflict_paths(gone[1]), decided_on=gone[0])
                did.append(f"{e['id']}: rejected (no longer applies on main)")
                continue
            lines, waits = queued_waiting_on_changes(repo, d, st, t, e, fix, todo, facts, stacks)
            did += lines
            if waits:
                continue
        if e.get("regen_hold") and regen_hold_overdue(st, d, e, t):
            continue
        if alive or e.get("retry_after", 0) > t:
            if not alive:  # said on its item: `land status` shows every wait (changekind.py, no held change silent)
                note_hold(st, e, *retry_hold(e), t)
            continue
        first = None if fix else batch_goes_first(st, d, ("assembling", "pushing"))
        if first:  # the next free check tree is the batch's: to assemble it, or to push from its own tree
            did += behind_batch(e, first, "check")
            note_hold(st, e, "batch", f"its check waits for batch {first['id']} ({first['state']})", t,
                      batch=first["id"])
            continue
        if not fix and e["id"] in pheld:  # THE PUSH BATCH verifies it with others
            if not _behind_said.get((e["id"], "push batch")):
                _behind_said[(e["id"], "push batch")] = True
                did.append(f"{e['id']}: its verify round waits for the next push batch")
            note_hold(st, e, "push-batch", "its verify round waits for the next push batch", t)
            continue
        if not fix and e["id"] in held:  # THE BACKLOG BATCH checks it with others
            if not _behind_said.get((e["id"], "backlog")):
                _behind_said[(e["id"], "backlog")] = True
                did.append(f"{e['id']}: its check waits for the next backlog batch")
            note_hold(st, e, "backlog-batch", "its check waits for the next backlog batch", t)
            continue
        slot = check_tree_slot(repo, d, fix=fix)
        if not slot:
            note_hold(st, e, "trees", "every check tree is busy (a check slot)", t)
            continue
        if not fix and batch_seq is not None and batch_seq < e["seq"] and not check_tree_slot_free(repo, d) \
                and policy_blocks("older-batch", st, e["id"], "the last free check tree kept for the older batch"):
            slot[1].release()  # AGE ORDER: the last free tree is the older changes' batch's
            if not _behind_said.get((e["id"], "older batch")):
                _behind_said[(e["id"], "older batch")] = True
                did.append(f"{e['id']}: its check waits: the last free tree goes to the older changes' batch")
            note_hold(st, e, "older-batch", "the last free check tree goes to the older changes' batch", t)
            continue
        slot[1].release()  # the executor takes it again itself
        note_hold(st, e, "starting", "its executor is starting its check", t)
        spawn_executor(repo, d, e["id"])
        did.append(f"{e['id']}: executor started")
    return did


def stacked_wait(st, t, e, parent):
    """Queued change e waits for its parent (holdgraph.py): its hold recorded, the line said once per parent."""
    why = (f"its check waits for {parent['id']} ({parent.get('status')}), which its branch is stacked on: it lands "
           "after it, checked once on top of it")
    note_hold(st, e, "stacked", why, t, blocker=parent["id"])
    said = _behind_said.get((e["id"], "stacked")) == parent["id"]
    _behind_said[(e["id"], "stacked")] = parent["id"]
    return [] if said else [f"{e['id']}: {why}"]


def queued_waiting_on_changes(repo, d, st, t, e, fix, todo, facts, stacks):
    """(the tick's lines, whether queued change e waits for its stack parent or an older change it overlaps), its hold
    recorded; a red fix waits for neither (told of a conflict all the same)."""
    parent = None if fix else stacks.get(e["id"])
    if parent is not None and parent.get("status") in OPEN_PARENT_STATES \
            and parent["id"] not in (e.get("cycle_ignore") or []):
        return stacked_wait(st, t, e, parent), True
    hold = overlap_hold(repo, d, e, todo, facts, t, stacks)
    if hold and fix:  # a red fix never waits: its author is told of a conflict, and it goes on
        return hold_for_overlap(st, d, e, *hold, waits=False, repo=repo), False
    if not hold:
        return [], False
    o, shared = hold[0], hold[1]
    did = hold_for_overlap(st, d, e, *hold, repo=repo)
    note_hold(st, e, "overlap", f"its check waits for {o['id']} ({o.get('status')}): both change "
              f"{', '.join(shared[:3])}" + (f" (+{len(shared) - 3} more)" if len(shared) > 3 else ""), t,
              blocker=o["id"], files=shared[:10])
    return did, True


def retry_hold(e):
    """(kind, why) of a queued change waiting for its retry_after: an infra re-queue's retry, or -- a REGEN NEEDED
    hold (regen_hold) -- its agent's regeneration, which no time brings (2026-11-20, phase8-units: `held: its check is
    retried at 12:03Z (an infra failure)`, read as a retry long overdue: it was the regen hold's end, the next day)."""
    hold = e.get("regen_hold") or {}
    if hold:
        wt = e.get("worktree") or ""
        gone = "" if not wt or os.path.isdir(wt) or host_role() == "server" else f"; its worktree {wt} is gone"
        return "regen", (f"its check waits for its agent's regeneration of {', '.join(hold.get('files') or [])}: "
                         f"`{hold.get('cmd')}` (run again at {day_hhmmz(e['retry_after'])} "
                         f"if unanswered; never rejected for it){gone}")
    why = re.sub(r"^infra:\s*", "", str(e.get("why") or "")).strip()
    return "retry", f"its check is retried at {hhmmz(e['retry_after'])} (an infra failure{': ' + why[:200] if why else ''})"


def _push_checked(repo, d, st, clock, t, e, fix, todo, pheld, kinds=None):
    """The change tick's step for checked entry e: pushed, unless its push is held -- before an open batch it is not
    in (THE BATCH GOES FIRST), for the next push batch, or behind an older change's combined-tree verification; a red
    fix (fix) is held by none of them. The hold is recorded on its item (`land status` prints it) and its deadline laps
    quietly while it is live; a checked change past its deadline that nothing holds escalates (lap_change_deadline)."""
    did = []
    first = None if fix else batch_goes_first(st, d)  # a red fix goes before the batch
    older = None if fix or first or e["id"] in pheld else older_verifying(todo, e, t, kinds, waiting_on_changes(st, todo))
    if first:  # a solo push now moves main under the batch, and its whole check is thrown away
        did += behind_batch(e, first, "push")
        note_hold(st, e, "batch", f"its push waits for batch {first['id']} ({first['state']}), which it is not in", t,
                  batch=first["id"])
    elif e["id"] in pheld:  # THE PUSH BATCH verifies and pushes it with others (never a red fix: push_candidates)
        if not _behind_said.get((e["id"], "push batch")):
            _behind_said[(e["id"], "push batch")] = True
            did.append(f"{e['id']}: its push waits for the next push batch")
        note_hold(st, e, "push-batch", "its push waits for the next push batch", t)
    elif older:
        did.append(f"{e['id']}: its push waits for {older['id']}'s combined-tree verification")
        note_hold(st, e, "verify", f"its push waits for {older['id']}'s combined-tree verification", t,
                  blocker=older["id"])
    if first or older or e["id"] in pheld:
        lap_change_deadline(st, e, "checked", t)
        return did
    # THE PUSH IS ITS HOLD WHILE IT RUNS (issue 20261119T123823-f7d6c9): clearing the hold before push_change -- which
    # may fetch, find main moved and re-queue the change for a verification -- left a checked change past its deadline
    # with nothing recorded for the seconds it ran, and the watchdog's pass filed it as unheld
    note_hold(st, e, "push", "its push is under way (re-checked first if main moved)", t)
    out = push_change(repo, d, e, clock, st)
    if out in ("checked", "busy"):  # raced, met the red gate, or the landing lock / push tree was busy: tried again
        note_hold(st, e, "push", "its push waits for the landing lock or a push tree" if out == "busy" else
                  "its last push did not land (a race or the red gate): tried again at the next tick", t)
    else:  # another answer, still checked: nothing holds it now (a new state's set_change cleared it already)
        note_hold(st, e, None, None, t)
    lap_change_deadline(st, e, "checked", t, f"its last push: {out}")
    return did + [f"{e['id']}: {out}"]


def lap_change_deadline(st, e, state, t, note=None):
    """A queued or checked change past its deadline: said and re-deadlined -- quietly while its hold is live, an
    escalation when it is stuck (change_hold_stuck: nothing holds it, its blocker is gone, held too long). note: what
    else the line says (a checked change's last push)."""
    it = st.item("change", e["id"])
    if not it or it["state"] != state or t <= it["deadline"]:
        return
    hold = change_hold_visible(st, it, e, t)
    stuck = change_hold_stuck(st, hold, it, t)
    if stuck and stuck_recovery(hold, stuck) == "rederive":
        # SELF-HEALING (2026-11-20, THE HOLD TABLE): a hold that describes a world that moved -- its blocker ended, or
        # its kind's bound passed -- is dropped, and the tick decides afresh (it records the hold again if one still
        # applies, its clock restarted); said quietly on the change's events, never a page
        # NEVER UNHELD BETWEEN TWO READINGS (2026-11-23, W1's "nothing holds it"): kept, its clock restarted
        restart_hold(st, e, t)
        try:
            it = st.item("change", e["id"]) or it
            st.transition("change", e["id"], state, it["version"],
                          event={"kind": "change.rederived", "severity": "info",
                                 "text": f"{e['id']}: {state} for {fmt_age(t - it['entered_at'])}; its hold "
                                         f"({hold.get('why')}) was stuck -- {stuck} -- so it is decided afresh"})
        except StaleVersion:
            pass
        return
    issue = None if hold else file_unheld_change(st, it, t, "agentctl (the change tick)")  # agentctl's bug: an issue,
    # A HELD LAP IS NO INBOX ENTRY (2026-11-19 08:39Z, 08:55Z: movies-fix-replay's "queued for 15 min / 30 min, held:
    # ..." reached the inbox every 15 min -- 93 of the day's 442 entries -- because the lap's event named the worktree
    # as its audience, and the projector writes every event with one). A live hold's lap is an event with no audience:
    # `land status` and the change's events say it; only a stuck or unheld lap goes to its author.
    quiet = bool(hold) and not stuck
    try:  # never an owner page (changekind.py, A CHANGE PAST ITS DEADLINE WITH NO RECORDED HOLD)
        it = st.item("change", e["id"]) or it
        st.transition("change", e["id"], state, it["version"],
                      event={"text": f"{e['id']}: {state} for {fmt_age(t - it['entered_at'])}, "
                                     + (f"held: {hold['why']}" if hold else "nothing holds it")
                                     + (f" -- STUCK: {stuck}" if stuck else "") + (f" ({note})" if note else "")
                                     + (f" -- filed as agentctl issue {issue}" if issue else ""),
                             **({} if quiet else {"audience": {"worktree": e.get("worktree")}}),
                             **({} if stuck and not issue else {"severity": "info"})})
    except StaleVersion:
        pass  # the next tick says it


# ---------------------------------------------------------------- the backlog batch (the owner, 2026-11-17)
# "BATCH WHENEVER THERE'S A BACKLOG, NOT ONLY ON RED": with 6-8 changes queued at green each paid a 30-40 min check of
# its own, mostly waiting for a ci-runner check slot. Now, at green with no batch open, BACKLOG_BATCH_MIN or more queued
# changes that no checker has taken and that may share a check (backlog_batchable: identity changes too, 2026-11-20;
# no `land --no-batch`, no red fix, no combined-tree verification
# round, none in an infra backoff or holding an unread remote run) are THE BATCH ABOVE with depth `full`: assembled in
# queue order (Fixes-Red first; a member that does not apply is left out and checked alone, batch_alone), its tip
# checked once with the FULL landing check (the hook's --range dry run; on ci-runner check/batch-<tip>, the same
# precommit-check round a change gets, area tests and heavy rows included -- no member had a check of its own), in
# ONE check tree. Pass: one push carrying that run (PREPUSH_REMOTE_RUN), every member LANDED with its own record and
# watch (batch_id, batch). Fail: bisected by prefix (no whole-batch re-check first: the culprit's own confirmation
# guards a flaky failure), the culprit rejected only when its failure is its own (judge_failure: a base red, a time
# cap or an infra failure sends it to a check of its own instead), the passing prefix pushed at once, the rest queued
# again (backlog_reconcile). At most BACKLOG_BATCH_MAX members, so a bisect is at most 4 extra rounds (3 prefixes and
# the confirmation). While such candidates wait for a tree, the change tick gives them none of its own (they wait for
# the batch); red fixes are checked alone beside it, and a red fix never waits for it.
BACKLOG_BATCH_MIN = 2  # queued changes that may share a check and make a backlog batch


BACKLOG_BATCH_MAX = 8  # a backlog batch's members at most: its bisect is 3 prefix checks + 1 confirmation


DEEP_BATCHES = ("full", "verify")  # the depths whose members are queue entries of their own: backlog and push batches


def backlog_min():
    return max(2, int(env_num("AGENTCTL_BACKLOG_BATCH_MIN", BACKLOG_BATCH_MIN)))


def backlog_candidates(repo, d, reds, st, t=None, facts=None):
    """The queued changes a backlog batch takes now, in queue order, at most BACKLOG_BATCH_MAX; [] while the gate is
    red, a change verifies its combined tree, or fewer than BACKLOG_BATCH_MIN may go. A fix of an open red (its kind
    under facts: queue_facts, read from the store when None) is checked alone."""
    if reds:
        return []
    t, facts, entries = batch_round(d, reds, st, t, facts)
    # THE OLDEST VERIFICATION PUSHES FIRST (queuepush.py) holds the batch too (review 2026-11-18): formed meanwhile, the
    # batch held the verified change's push for its whole check (THE BATCH GOES FIRST), then its push moved main under
    # it and it verified again -- behind the next backlog batch, and the next. No backlog batch while one verifies, or
    # while a checked change's push is due (its verification may have ended since the change tick).
    # A HELD VERIFICATION HOLDS NOTHING (holdgraph.py): nor a push that waits for one
    if any((verifying(e, t, waiting_on_changes(st, [e])) or push_due(st, e, t)) and not e.get("backlog_batch")
           for e in entries):  # a checked change's push is due at the next change tick
        return []
    ok = [e for e in entries if backlog_batchable(repo, d, e, t, facts)
          and not overlap_hold(repo, d, e, entries, facts, t)]
    out = sorted(ok, key=lambda e: e["seq"])[:int(env_num("AGENTCTL_BACKLOG_BATCH_MAX", BACKLOG_BATCH_MAX))]
    return out if len(out) >= backlog_min() else []


def backlog_held(repo, d, reds, st, t=None, facts=None):
    """The ids of the queued changes the change tick holds back for the next backlog batch: with no batch open, the
    candidates one would take now."""
    return set() if current_batch(st, d) else {c["id"] for c in backlog_candidates(repo, d, reds, st, t, facts)}


# ---------------------------------------------------------------- the push batch (the owner, 2026-11-17)
# "COMBINE THEM INSTEAD OF LANDING THEM ONE BY ONE" (W1: the oldest unlanded change over 2 h). A checked change pushed
# alone: on a main that moved since its full check it got a verify round of its own (THE RULE, queue.py), and THE
# OLDEST VERIFICATION PUSHES FIRST held every younger checked change meanwhile -- so N checked changes landed serially,
# each paying a check-runner wait plus a ~10-min verify round after the previous push moved main (ref-scrapers
# verifying while pr274 and postlanding held their pushes behind it, each to verify again after it pushed). Now, at
# green with no batch open and no verify round running, PUSH_BATCH_MIN or more changes that would each need a verify
# round on the current main (push_batchable: checked on an older main, or queued for a verify round; no red fix, no
# `land --no-batch`, no known red, none in a backoff) are THE BATCH ABOVE with depth `verify`, the backlog batch's
# machine: assembled oldest first on the current main, each member judged by THE RULE against main plus the members
# before it (push_batch_select: one that needs a FULL round is left out -- checked again alone now when what landed on
# main demands it, else pushed alone after the batch, push_alone), the union verified ONCE (the hook's --verify-only
# over <the oldest member's full base>..<tip>: every test config compiled, the lints, the fast specs and the unit
# tests of every module a member or what landed meanwhile touches; on ci-runner check/verify-<base>-batch-<tip>), and
# pushed ONCE (the hook skipping the plan the members' full checks passed, as a lone verified change's push does).
# Fail: bisected by prefix as the backlog batch is (the culprit confirmed, rejected only when the failure is its own),
# the passing prefix pushed, the rest queued again for their verify round. More than one identity-model commit in it:
# G5's drift check of its tip (check_batch); after a drift split, one identity change per batch (drift_serialize). A
# change checked while a push batch runs is no member: its push waits for the batch (batch_goes_first) and it joins
# the next one. At most BACKLOG_BATCH_MAX members.
PUSH_BATCH_MIN = 2  # changes owing a verify round that make a push batch


def push_batchable(repo, d, e, t, main, facts=None):
    """Whether entry e would need a verify round on main (THE PUSH BATCH above) and may share one: its KIND lets it (no
    red fix, no --no-batch, batch_alone, push_alone or known red: changekind.py) and it is checked on an older main, or
    queued for its verify round with no checker on it."""
    rem = e.get("remote") or {}
    if e.get("backlog_batch") or float(e.get("retry_after") or 0) > t:
        return False
    if kind_of(repo, e, facts, t)["push_batch"] is not None:
        return False
    if e.get("status") == "checked":  # one keeping its verdict on main (MINIMAL RECHECKS) owes no verify round
        return bool(e.get("checked_base")) and e["checked_base"] != main and not verdict_kept(repo, e, main)
    if e.get("status") != "queued" or not e.get("verify") or not e.get("full_base"):
        return False
    if rem.get("run") is not None and not rem.get("collected_at"):  # its own run: a checker adopts it
        return False
    return not checker_alive(e["id"])


def push_candidates(repo, d, reds, st, t=None, facts=None):
    """The changes a push batch takes now, oldest first, at most BACKLOG_BATCH_MAX; [] while the gate is red, while a
    change's own verify round runs (it pushes first), or when fewer than PUSH_BATCH_MIN may go. A fix of an open red
    (its kind under facts: queue_facts, read from the store when None) pushes alone."""
    if reds:
        return []
    t, facts, entries = batch_round(d, reds, st, t, facts)
    if any(e.get("status") == "checking" and e.get("verify") and not e.get("backlog_batch") for e in entries):
        return []
    # NO PUSH BATCH UNDER A RED FIX IN FLIGHT (2026-11-19 10:37:14Z: push batch #48 formed 13 s before 08010c, a red
    # fix just verified, pushed; its push moved main under the batch's verify round, which passed at 10:46 and was
    # thrown away -- "main moved during its check: assembled again"). A red fix goes first and pushes alone, so a push
    # batch formed meanwhile is verified for nothing: none forms until the open red fixes are decided.
    if any(not e.get("backlog_batch") and not float(e.get("retry_after") or 0) > t
           and classify_change(e, facts, t)["red_fix"] for e in entries):
        return []
    main = git(repo, "rev-parse", f"{remote()}/main", check=False).stdout.strip()
    out = [e for e in sorted(entries, key=lambda e: e["seq"])
           if push_batchable(repo, d, e, t, main, facts)]
    out = drift_serialize(repo, out)[:int(env_num("AGENTCTL_BACKLOG_BATCH_MAX", BACKLOG_BATCH_MAX))]
    return out if len(out) >= max(2, int(env_num("AGENTCTL_PUSH_BATCH_MIN", PUSH_BATCH_MIN))) else []


def push_held(repo, d, reds, st, t=None, facts=None):
    """The ids of the changes the change tick holds back for the next push batch: with no batch open, the candidates
    one would take now."""
    return set() if current_batch(st, d) else {c["id"] for c in push_candidates(repo, d, reds, st, t, facts)}


def check_tree_slot_free(repo, d):
    """Whether a check tree is free now (its lease taken and given back at once)."""
    slot = check_tree_slot(repo, d)
    if slot:
        slot[1].release()
    return bool(slot)


# THE BATCH GOES FIRST (2026-10-30). Batch #4 (the flips and filmweb changes) passed its 45-min check at 14:34Z, but a
# solo change (identity-solid) had pushed at 14:05Z, so its push found main moved and assembled it again; and the moment
# its executor freed check tree 3 the change tick (it runs before the batch tick) started a change's check in it, so
# the batch could neither push from its tree nor, assembling, get any tree: it waited behind every solo change, for
# ever. Now, while the batch is open with changes waiting, a checked change does not push (assembling, checking,
# bisecting, pushing: a push moves main under it) and no new change check takes a tree while the batch needs one
# (assembling, pushing). Its deadlines bound the wait: a held or blocked-red batch lets solo changes go.
BATCH_FIRST = ("assembling", "checking", "bisecting", "pushing")


def batch_goes_first(st, d, states=BATCH_FIRST):
    """The open batch, when solo changes wait for it (its state in `states`, changes waiting in it or a backlog batch),
    else None."""
    it = current_batch(st, d)
    if it and (it.get("data") or {}).get("depth") == "red":  # a red pre-check holds nothing: what lands on red moves
        return None  # main under it, and it pre-checks again (THE BATCH IS THE MECHANISM)
    backlog = it and (it.get("data") or {}).get("depth") in DEEP_BATCHES
    return it if it and it["state"] in states and (backlog or waiting_entries(d)) else None


def behind_batch(e, it, what):
    """The tick's line for a change held behind the batch: once per change, batch and kind."""
    k = (e["id"], what)
    if _behind_said.get(k) == it["id"]:
        return []
    _behind_said[k] = it["id"]
    return [f"{e['id']}: its {what} waits for batch {it['id']} ({it['state']})"]


def drift_serialize(repo, batch):
    """After a drift-red batch (an entry marked drift_split waits): at most ONE identity-model change per batch, the
    first in batch order; the rest of the identity changes wait for the next batch, every other change goes now."""
    if not any(e.get("drift_split") for e in batch):
        return batch
    out, ident = [], False
    for e in batch:
        if identity_model_commits(repo, e.get("commits") or []):
            if ident:
                continue
            ident = True
        out.append(e)
    return out

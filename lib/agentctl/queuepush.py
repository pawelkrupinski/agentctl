# agentctl, part `queuepush`: pushing a checked change: the oldest verification first, a red fix's verification kept.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import time
from typing import Any
from agentctl.context import Context
from agentctl.statemachines import change_state_of
from agentctl.timefmt import day_hhmmz, hhmmz
from agentctl.holdgraph import OPEN_PARENT_STATES, stack_map, waiting_on_changes
from agentctl.core import attempt, host_role, remote
from agentctl.policy import policy_blocks
from agentctl.waitingdir import ChangeRecords, change_item_fields
from agentctl.queuehold import break_hold_cycles, expire_idle_hold, idle_hold_expired, note_hold
from agentctl.batchitem import current_batch
from agentctl.conflicts import conflict_cause, conflict_paths
from agentctl.queueslots import check_tree_slot, checker_alive, queue_verdict, set_change
from agentctl.queueinfra import requeue_infra
from agentctl.queuerecheck import same_change_source
from agentctl.changekind import classify_change, classify_queue, facts_for, facts_tick, kind_order, record_change_kind
from agentctl.waiting import supersede
from agentctl.redseen import gate_reds
from agentctl.changelap import lap_change_deadline, regen_hold_overdue
from agentctl.overlapcheck import OverlapHold, hold_for_overlap, main_conflict, overlap_hold
from agentctl.entrychecks import spawn_executor
from agentctl.queuediff import push_change
from agentctl.batchselect import backlog_held, push_held


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


def tick_changes(repo: str, d: str, clock: Any, st: Any, *, ctx: Context) -> list[str]:
    """One tick over the change items (_tick_changes), the queue facts read once for it (changekind.py facts_tick)."""
    with facts_tick(ctx.caches.tick_facts):
        return _tick_changes(repo, d, clock, st, ctx=ctx)


def _tick_changes(repo: str, d: str, clock: Any, st: Any, *, ctx: Context) -> list[str]:
    """One tick over the change items: re-queueing re-derived (only the newest entry per worktree and branch runs), an
    executor started for each queued or lease-less checking change (red fixes first, while a slot is free), a
    checking change past its deadline back to queued (infra), a queued one past its deadline escalated, and each
    checked change pushed. Returns what it did."""
    t = clock.time()
    reds = attempt(lambda: gate_reds(repo, ctx=ctx), set(), catch=(Exception,))
    facts = facts_for(reds, st, ctx.caches.tick_facts)  # A FIX OF ANY OPEN RED GOES FIRST (above): each change's KIND, recorded on its item
    entries = ChangeRecords(st, d).queued()
    kinds = classify_queue(repo, d, entries, facts, t, st, ctx=ctx)
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
    stacks = stack_map(todo, ChangeRecords(st, d).every())  # A STACKED CHANGE WAITS FOR ITS PARENT
    # PUSHES FIRST, THEN THE HOLDS (queue simulator, 2026-11-18): the holds below were read before this tick's pushes,
    # and a checked change's push due holds every backlog batch back (backlog_candidates) -- so the tree a finished
    # check had just freed went to the oldest lone check, never to the batch: under load (every tree busy, a tree
    # freed only by a check ending) no backlog batch ever formed (seed 0 of the simulator's load profile: 40 changes,
    # 40 lone checks, 0 batches). The checked changes are pushed in a first pass; the holds are read again for the
    # second, which starts checks.
    for phase in ("push", "check"):
        held = backlog_held(repo, d, reds, st, t, facts, ctx=ctx)
        pheld = push_held(repo, d, reds, st, t, facts, ctx=ctx)
        # THE BATCH KEEPS ITS AGE-ORDER PLACE (review 2026-11-18): this tick runs before the batch tick, and the
        # changes held for a backlog or push batch take no tree, so a tree freed went to the next YOUNGER lone change
        # and the batch tick found none: with a steady stream of lone changes (identity changes, --no-batch) the batch
        # never formed. A lone change younger than the oldest held one never takes the LAST free tree: that is the
        # batch's (a red fix excepted).
        batch_seq = min((x["seq"] for x in todo if x["id"] in held | pheld), default=None)
        did += _tick_changes_pass(repo, d, st, clock, t, facts, kinds, phase, runs, todo, held, pheld, batch_seq,
                                  stacks, ctx=ctx)
    return did


def _tick_changes_pass(repo, d, st, clock, t, facts, kinds, phase, mine, todo, held, pheld, batch_seq, stacks, *, ctx):
    """One pass of tick_changes over the newest entries: `push` (the checked and interrupted pushes) or `check` (the
    rest: executors started, deadlines). facts: queue_facts; kinds: each entry's kind (classify_queue); stacks: each
    stacked entry's parent (holdgraph.py stack_map)."""
    run = ChangePass(repo, d, st, clock, t, facts, kinds, phase, todo, held, pheld, batch_seq, stacks, ctx=ctx)
    did = []
    for e in mine:
        did += run.entry(e)
    return did


def said_once(e: Any, key: Any, line: Any, did: Any, said: dict[Any, Any]) -> Any:
    """Say `line` (append it to did) once per entry and key: the hold it names is recorded each tick, said once."""
    if not said.get((e["id"], key)):
        said[(e["id"], key)] = True
        did.append(line)


class ChangePass:
    """One pass of tick_changes (`push` or `check`), a method per stage of one entry's walk. A stage that decides the
    entry's tick returns True (the entry goes no further); the lines it says go to `did`."""

    def __init__(self, repo: Any, d: Any, st: Any, clock: Any, t: Any, facts: Any, kinds: Any, phase: Any, todo: Any, held: Any, pheld: Any, batch_seq: Any, stacks: Any, *, ctx: Context) -> None:
        self.ctx = ctx
        self.repo, self.d, self.st, self.clock, self.t, self.facts, self.kinds = repo, d, st, clock, t, facts, kinds
        self.statuses = ("checked", "pushing") if phase == "push" else ("queued", "checking")
        self.todo, self.held, self.pheld, self.batch_seq, self.stacks = todo, held, pheld, batch_seq, stacks

    def entry(self, e: Any) -> Any:
        """The lines entry e's tick says."""
        if e.get("backlog_batch"):  # a backlog batch's member: the batch checks and pushes it (backlog_reconcile)
            return []
        if e["status"] not in self.statuses:
            return []  # (a push this tick made it landed, waiting or rejected: no check for it)
        st, t = self.st, self.t
        it = st.put_item("change", e["id"], change_state_of(e["status"]) or "queued", change_item_fields(e))
        state = it["state"]
        if state == "pushing":  # a tick that died mid-push: verified next, never pushed twice blindly
            set_change(st, self.d, e, "checked", cause="pushing", event={"text": f"{e['id']}: its push was interrupted: verified, then tried again"})
            state = "checked"
        kind = self.kinds.get(e["id"]) or classify_change(e, self.facts, t, ctx=self.ctx)
        record_change_kind(st, e, kind)  # on its item, now that it surely has one (a no-op when unchanged)
        fix = kind["red_fix"]
        if state == "checked":
            return self.checked(e, fix)
        did = []
        self.check(e, it, state, fix, did)
        return did

    def checked(self, e: Any, fix: Any) -> Any:
        """A checked entry: its push, unless it waits for its retry."""
        if e.get("retry_after", 0) > self.t:
            note_hold(self.st, e, "retry", f"its push is retried at {hhmmz(e['retry_after'])}", self.t)
            return []
        return _push_checked(self.repo, self.d, self.st, self.clock, self.t, e, fix, self.todo, self.pheld, self.kinds, ctx=self.ctx)

    def check(self, e: Any, it: Any, state: Any, fix: Any, did: Any) -> Any:
        """A queued or checking entry: lapsed, held, or its executor started."""
        alive = checker_alive(self.st, e["id"])
        if self.lapsed(e, it, state, alive, did) or self.queued_hold(e, state, alive, fix, did):
            return
        if self.retrying(e, alive) or self.batch_hold(e, fix, did):
            return
        self.start(e, fix, did)

    def lapsed(self, e: Any, it: Any, state: Any, alive: Any, did: Any) -> Any:
        """Checking past its deadline with no live checker: back to the queue (infra)."""
        if state == "checking" and not alive and self.t > it["deadline"]:
            requeue_infra(self.st, self.d, e, "its check passed its deadline with no live checker", self.t)
            did.append(f"{e['id']}: re-queued (checking past its deadline)")
            return True
        return False

    def queued_hold(self, e: Any, state: Any, alive: Any, fix: Any, did: Any) -> Any:
        """A queued entry: its deadline lapsed, rejected where it no longer applies on main, held for a parent or an
        overlapping older change."""
        if state != "queued":
            return False
        lap_change_deadline(self.st, e, "queued", self.t)  # past its deadline: said, re-deadlined (A HELD WAIT IS NO ESCALATION)
        if alive:
            return False
        gone = main_conflict(self.repo, e, self.ctx.caches.overlap_known)
        if gone:  # it no longer applies on main itself: said now, never after a wait for a tree or an older change
            self.reject_stale(e, gone)
            did.append(f"{e['id']}: rejected (no longer applies on main)")
            return True
        lines, waits = queued_waiting_on_changes(self.repo, self.d, self.st, self.t, e, fix, self.todo, self.facts,
                                                 self.stacks, ctx=self.ctx)
        did += lines
        return bool(waits)

    def reject_stale(self, e: Any, gone: Any) -> Any:
        # policy: no-longer-applies
        queue_verdict(self.st, self.d, e, "rejected", f"NOT LANDED: the change no longer applies on {remote()}/main "
                      f"{gone[0][:10]} (main moved since QUEUED{conflict_cause(self.repo, e.get('base'), gone[0], gone[1])}"
                      f"): rebase onto {remote()}/main and land again (it keeps its place in the queue)",
                      conflict_files=conflict_paths(gone[1]), decided_on=gone[0])

    def retrying(self, e: Any, alive: Any) -> Any:
        """A regeneration hold overdue, a live checker, or a retry still to come: nothing to start."""
        if e.get("regen_hold") and regen_hold_overdue(self.st, self.d, e, self.t):
            return True
        if alive or e.get("retry_after", 0) > self.t:
            if not alive:  # said on its item: `land status` shows every wait (changekind.py, no held change silent)
                note_hold(self.st, e, *retry_hold(e), self.t)
            return True
        return False

    def batch_hold(self, e: Any, fix: Any, did: Any) -> Any:
        """The next free check tree is a batch's: the assembling or pushing batch, or the push or backlog batch to come."""
        st, t = self.st, self.t
        first = None if fix else batch_goes_first(st, self.d, ("assembling", "pushing"))
        if first:  # the next free check tree is the batch's: to assemble it, or to push from its own tree
            did += behind_batch(e, first, "check", self.ctx.caches.behind_said)
            note_hold(st, e, "batch", f"its check waits for batch {first['id']} ({first['state']})", t, batch=first["id"])
            return True
        for ids, key, kind, line in ((self.pheld, "push batch", "push-batch", "its round waits for the next push batch"),
                                     (self.held, "backlog", "backlog-batch", "its check waits for the next backlog batch")):
            if not fix and e["id"] in ids:  # THE PUSH BATCH / THE BACKLOG BATCH checks it with others
                said_once(e, key, f"{e['id']}: {line}", did, self.ctx.caches.behind_said)
                note_hold(st, e, kind, line, t)
                return True
        return False

    def start(self, e: Any, fix: Any, did: Any) -> Any:
        """Its executor started, if a check tree is free (and not the last one, kept for an older batch)."""
        st = self.st
        slot = check_tree_slot(st, self.repo, self.d, fix=fix)
        if not slot:
            note_hold(st, e, "trees", "every check tree is busy (a check slot)", self.t)
            return
        if not fix and self.batch_seq is not None and self.batch_seq < e["seq"] \
                and not check_tree_slot_free(st, self.repo, self.d) \
                and policy_blocks("older-batch", st, e["id"], "the last free check tree kept for the older batch",
                                  said=self.ctx.caches.policy_said):
            slot[1].release()  # AGE ORDER: the last free tree is the older changes' batch's
            said_once(e, "older batch", f"{e['id']}: its check waits: the last free tree goes to the older changes' batch", did,
                      self.ctx.caches.behind_said)
            note_hold(st, e, "older-batch", "the last free check tree goes to the older changes' batch", self.t)
            return
        slot[1].release()  # the executor takes it again itself
        note_hold(st, e, "starting", "its executor is starting its check", self.t)
        spawn_executor(self.repo, self.d, e["id"])
        did.append(f"{e['id']}: executor started")


def stacked_wait(st, t, e, parent, said_to):
    """Queued change e waits for its parent (holdgraph.py): its hold recorded, the line said once per parent."""
    why = (f"its check waits for {parent['id']} ({parent.get('status')}), which its branch is stacked on: it lands "
           "after it, checked once on top of it")
    note_hold(st, e, "stacked", why, t, blocker=parent["id"])
    said = said_to.get((e["id"], "stacked")) == parent["id"]
    said_to[(e["id"], "stacked")] = parent["id"]
    return [] if said else [f"{e['id']}: {why}"]


def queued_waiting_on_changes(repo, d, st, t, e, fix, todo, facts, stacks, *, ctx):
    """(the tick's lines, whether queued change e waits for its stack parent or an older change it overlaps), its hold
    recorded; a red fix waits for neither (told of a conflict all the same)."""
    parent = None if fix else stacks.get(e["id"])
    if parent is not None and parent.get("status") in OPEN_PARENT_STATES \
            and parent["id"] not in (e.get("cycle_ignore") or []):
        return stacked_wait(st, t, e, parent, ctx.caches.behind_said), True
    hold, did = live_overlap_hold(repo, d, st, t, e, fix, todo, facts, stacks, ctx=ctx)
    if hold and fix:  # a red fix never waits: its author is told of a conflict, and it goes on
        return hold_for_overlap(st, d, e, *hold, ctx.caches.behind_said, waits=False, repo=repo), False
    if not hold:
        return did, False
    o, shared = hold[0], hold[1]
    did += hold_for_overlap(st, d, e, *hold, ctx.caches.behind_said, repo=repo)
    note_hold(st, e, "overlap", f"its check waits for {o['id']} ({o.get('status')}): both change "
              f"{', '.join(shared[:3])}" + (f" (+{len(shared) - 3} more)" if len(shared) > 3 else ""), t,
              blocker=o["id"], files=shared[:10])
    return did, True


def live_overlap_hold(repo: str, d: str, st: Any, t: float, e: dict[str, Any], fix: object, todo: list[dict[str, Any]],
                      facts: Any, stacks: dict[str, dict[str, Any]], *, ctx: Context) -> tuple[OverlapHold | None, list[str]]:
    """(the overlap hold queued change e waits under, the lines said): every blocker whose hold on it expired (AN IDLE
    HOLDER'S HOLD EXPIRES, queuehold.py) let go of first -- one at a time, each for good, so the walk ends."""
    hold, did = overlap_hold(st, repo, d, e, todo, facts, t, stacks, ctx=ctx), []
    while hold and not fix and idle_hold_expired(st, e, hold[0], t):
        did.append(expire_idle_hold(st, d, e, hold[0], t))
        hold = overlap_hold(st, repo, d, e, todo, facts, t, stacks, ctx=ctx)
    return hold, did


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


def _note_push_hold(st: Any, e: Any, first: Any, older: Any, pheld: Any, t: Any, said: dict[Any, Any]) -> Any:
    """The hold a checked entry's push waits under (the batch it is not in, the push batch, an older change's
    verification), recorded on its item; the lines to say, if any."""
    did = []
    if first:  # a solo push now moves main under the batch, and its whole check is thrown away
        did += behind_batch(e, first, "push", said)
        note_hold(st, e, "batch", f"its push waits for batch {first['id']} ({first['state']}), which it is not in", t,
                  batch=first["id"])
    elif e["id"] in pheld:  # THE PUSH BATCH verifies and pushes it with others (never a red fix: push_candidates)
        said_once(e, "push batch", f"{e['id']}: its push waits for the next push batch", did, said)
        note_hold(st, e, "push-batch", "its push waits for the next push batch", t)
    elif older:
        did.append(f"{e['id']}: its push waits for {older['id']}'s combined-tree verification")
        note_hold(st, e, "verify", f"its push waits for {older['id']}'s combined-tree verification", t,
                  blocker=older["id"])
    return did


def _push_checked(repo: Any, d: Any, st: Any, clock: Any, t: Any, e: Any, fix: Any, todo: Any, pheld: Any, kinds: Any = None, *, ctx: Context) -> Any:
    """The change tick's step for checked entry e: pushed, unless its push is held -- before an open batch it is not
    in (THE BATCH GOES FIRST), for the next push batch, or behind an older change's combined-tree verification; a red
    fix (fix) is held by none of them. The hold is recorded on its item (`land status` prints it) and its deadline laps
    quietly while it is live; a checked change past its deadline that nothing holds escalates (lap_change_deadline)."""
    did = []
    first = None if fix else batch_goes_first(st, d)  # a red fix goes before the batch
    older = None if fix or first or e["id"] in pheld else older_verifying(todo, e, t, kinds, waiting_on_changes(st, todo))
    did += _note_push_hold(st, e, first, older, pheld, t, ctx.caches.behind_said)
    if first or older or e["id"] in pheld:
        lap_change_deadline(st, e, "checked", t)
        return did
    # THE PUSH IS ITS HOLD WHILE IT RUNS (issue 20261119T123823-f7d6c9): clearing the hold before push_change -- which
    # may fetch, find main moved and re-queue the change for a verification -- left a checked change past its deadline
    # with nothing recorded for the seconds it ran, and the watchdog's pass filed it as unheld
    note_hold(st, e, "push", "its push is under way (re-checked first if main moved)", t)
    out = push_change(repo, d, e, clock, st, ctx=ctx)
    if out in ("checked", "busy"):  # raced, met the red gate, or the landing lock / push tree was busy: tried again
        note_hold(st, e, "push", "its push waits for the landing lock or a push tree" if out == "busy" else
                  "its last push did not land (a race or the red gate): tried again at the next tick", t)
    else:  # another answer, still checked: nothing holds it now (a new state's set_change cleared it already)
        note_hold(st, e, None, None, t)
    lap_change_deadline(st, e, "checked", t, f"its last push: {out}")
    return did + [f"{e['id']}: {out}"]


DEEP_BATCHES = ("full", "verify")  # the depths whose members are queue entries of their own: backlog and push batches


def check_tree_slot_free(st, repo, d):
    """Whether a check tree is free now (its lease taken and given back at once)."""
    slot = check_tree_slot(st, repo, d)
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
    return it if it and it["state"] in states and (backlog or ChangeRecords(st, d).waiting()) else None


def behind_batch(e, it, what, said):
    """The tick's line for a change held behind the batch: once per change, batch and kind."""
    k = (e["id"], what)
    if said.get(k) == it["id"]:
        return []
    said[k] = it["id"]
    return [f"{e['id']}: its {what} waits for batch {it['id']} ({it['state']})"]



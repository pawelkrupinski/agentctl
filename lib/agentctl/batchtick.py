# agentctl, part `batchtick`: one tick of the repository's batch: the open batch's plan, its assembly in a check tree, and the states it acts on
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, NamedTuple
from agentctl.statemachines import machine_state
from agentctl.timefmt import ts
from agentctl.core import err, git, remote
from agentctl.waitingdir import ChangeRecords
from agentctl.batchitem import batch_fail, batch_move, current_batch, new_batch
from agentctl.leases import lease_live
from agentctl.queueslots import check_tree, check_tree_slot, set_change
from agentctl.queuerecheck import recheck_logged
from agentctl.redland import may_land_on_red
from agentctl.landhold import land_hold
from agentctl.changekind import facts_for, facts_tick
from agentctl.batchrun import assemble, unread_note
from agentctl.redseen import gate_reds
from agentctl.queuebatchsel import backlog_candidates, backlog_min, push_candidates
from agentctl.batch import (
    backlog_reconcile, batch_kind, batch_ready, batch_round_depth, green_batch_depth, red_precheck,
    release_red_fixes, spawn_batch_executor, union_base)
from agentctl.queuepush import DEEP_BATCHES, check_tree_slot_free
from agentctl.batchpush import push_batch_item


def tick_batch(repo: str, d: str, clock: Any, st: Any) -> list[str]:
    """One tick of the repository's batch. Returns what it did. A queue that stops moving is named by queuestall."""
    with facts_tick():  # the queue facts read once for the tick (changekind.py)
        return _tick_batch(repo, d, st)


def _tick_batch(repo: str, d: str, st: Any) -> list[str]:
    """A backlog batch's members brought in line with it (backlog_reconcile), then the batch's step. st: the tick's
    store, on the tick's clock (batch_step and the push read the time on it)."""
    return backlog_reconcile(d, st) + batch_step(repo, d, st)


class BatchTick(NamedTuple):
    """What one pass of batch_step read and decided before it acts on the open batch `it`'s state."""
    repo: str
    d: str
    st: Any
    t: float
    it: dict[str, Any]
    waiting: list[dict[str, Any]]
    reds: set[str]
    ready: list[dict[str, Any]]
    depth: str
    fix_waits: bool


class Assembled(NamedTuple):
    """A batch's members assembled in check tree `i` on `base`: the tree, what was kept, its tip, and the round it gets."""
    i: Any
    tree: str
    base: str
    kept: list[dict[str, Any]]
    tips: dict[str, Any]
    depth: str
    full: bool
    full_round: str
    tip: str
    range_base: str | None


def batch_step(repo: str, d: str, st: Any) -> list[str]:
    t = st.clock()
    waiting = ChangeRecords(st, d).waiting()
    it = current_batch(st, d)
    backlog = not waiting and not it and \
        sum(1 for e in ChangeRecords(st, d).queued() if e.get("status") in ("queued", "checked")) >= backlog_min()
    if not waiting and not it and not backlog:
        return []
    if land_hold(st, t):  # the store's hold: honoured wherever `land hold` ran
        return ["held"]
    reds = gate_reds(repo)
    tick, acts = batch_plan(repo, d, st, t, it, reds)
    return acts if tick is None else batch_state_step(tick)


def batch_state_step(tick: BatchTick) -> list[str]:
    """What the open batch's state asks for now: its deadline, and then by state -- blocked by the red gate, assembling,
    checking, pushing."""
    repo, d, st, t, it, reds = tick.repo, tick.d, tick.st, tick.t, tick.it, tick.reds
    spec = machine_state("batch", it["state"])
    if t > it["deadline"] and it["state"] != "checking" and it["state"] != "bisecting":
        it = batch_move(st, it, spec.on_deadline, text=f"batch {it['id']}: {it['state']} past its deadline", cause="timeout")
        return [f"batch {it['id']}: deadline -> {it['state']}"]
    state = it["state"]
    if state == "blocked-red":
        if tick.ready:
            batch_move(st, it, "assembling")
            return [f"batch {it['id']}: assembling"]
        return red_precheck(repo, d, st, it, tick.waiting, reds)
    if state == "assembling":
        return batch_assemble(tick)
    if state in ("checking", "bisecting"):
        return batch_executor_alive(tick)
    if state == "pushing":
        return [push_batch_item(repo, d, st, it)]
    return []


def batch_plan(repo: str, d: str, st: Any, t: float, it: Any, reds: Any) -> tuple[BatchTick | None, list[str]]:
    """(the tick, []) once the batch to act on is chosen -- the open one, or a new one made for what may go now; (None,
    acts) when nothing is to be done: the acts say what the pass did instead."""
    waiting = waiting_after_release(repo, d, st, t, it, reds)
    depth = (it or {}).get("data", {}).get("depth") or "fast"
    fix_waits = gate_fix_waits(repo, st, reds, waiting)
    if batch_forming(it, depth, waiting):
        it, ready, depth, acts = batch_formed(repo, d, st, t, it, reds, depth)
        if it is None:
            return None, acts
    else:
        unread_note(d, reds, waiting, st)
        ready = batch_ready_now(repo, st, it, waiting, reds, fix_waits)
    if not it:  # red: a fix waits AND something may land on red now (see BLOCKED BY WHAT MUST WAIT)
        it = new_batch(st, d, "assembling" if ready else "blocked-red")
    return BatchTick(repo, d, st, t, it, waiting, reds, ready, depth, fix_waits), []


def batch_forming(it: Any, depth: Any, waiting: Any) -> bool:
    """Whether this pass forms a batch (none waiting, none open -- or an open deep one still assembling) rather than
    taking the waiting entries."""
    return not waiting and (not it or (depth in DEEP_BATCHES and it["state"] == "assembling"))


def gate_fix_waits(repo: str, st: Any, reds: Any, waiting: Any) -> bool:
    """Whether the gate is red and a waiting change names its red (THE RED-LANDING RULE: one that may land on it is
    released to the queue; one that waits beside a production commit naming no red stays)."""
    facts = facts_for(reds, st)
    return bool(reds) and any(may_land_on_red(facts, e, repo, store=st).names_gate_red for e in waiting)


def waiting_after_release(repo: str, d: str, st: Any, t: float, it: Any, reds: Any) -> list[dict[str, Any]]:
    """The waiting entries, read again when a red fix was released: at green, or while red when the open red names it
    (release_red_fixes: WR self-heals)."""
    waiting = ChangeRecords(st, d).waiting()
    if (not it or it["state"] in ("blocked-red", "assembling")) and release_red_fixes(repo, d, st, reds, waiting, t):
        return ChangeRecords(st, d).waiting()
    return waiting


def batch_ready_now(repo: str, st: Any, it: Any, waiting: Any, reds: Any, fix_waits: bool) -> Any:
    """The waiting entries a batch may take now: none while a red gate holds them (unless a fix waits for green), none
    for a batch already checking."""
    if (reds and not fix_waits) or (it and it["state"] not in ("blocked-red", "assembling")):
        return []
    return batch_ready(repo, waiting, reds, st)


def batch_formed(repo: str, d: str, st: Any, t: float, it: Any, reds: Any, depth: Any) -> tuple[Any, list[dict[str, Any]], str, list[str]]:
    """(the batch, its ready members, its depth, acts) for a pass with no waiting change: THE PUSH BATCH (checked changes,
    each owing a verify round) before THE BACKLOG BATCH (queued changes). A new batch is made only with a check tree free
    for it (an assembling batch waiting for one holds every solo check behind it, and is held itself past its 5-min
    deadline); the batch is None when there is nothing to form."""
    push, ready = batch_candidates(repo, d, st, t, it, reds, depth)
    if not it:
        if not ready or not check_tree_slot_free(st, repo, d):
            return None, ready, depth, []
        it = new_batch(st, d, "assembling", depth="verify" if push else "full")
    elif not ready:  # the gate went red, or the changes went their own ways: no batch to assemble
        batch_move(st, it, "landed", {"members": []}, text=f"batch {it['id']}: no {batch_kind(it)} left to form")
        return None, ready, depth, [f"batch {it['id']}: no {batch_kind(it)} left to form"]
    return it, ready, it["data"]["depth"], []


def batch_candidates(repo: str, d: str, st: Any, t: float, it: Any, reds: Any, depth: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(the push batch's candidates, what is ready): the checked changes owing a verify round, else the queued ones a
    backlog batch would take -- each asked only when the batch is new or of that depth."""
    facts = facts_for(reds, st)
    push = push_candidates(repo, d, reds, st, t, facts) if not it or depth == "verify" else []
    return push, push or (backlog_candidates(repo, d, reds, st, t, facts) if not it or depth == "full" else [])


def batch_assemble(tick: BatchTick) -> list[str]:
    """The assembling batch: blocked by the red gate, emptied, or assembled in a free check tree and sent to be checked
    (or, with its red pre-check kept, straight to its push)."""
    it, st, d, repo = tick.it, tick.st, tick.d, tick.repo
    if tick.reds and not tick.ready:
        batch_move(st, it, "blocked-red", cause="gate-red")
        return [f"batch {it['id']}: blocked by the red gate"
                + (" (its fix waits for green: a production commit names no red)" if tick.fix_waits else "")]
    if not tick.ready:
        if not tick.waiting:
            batch_move(st, it, "landed", {"members": []}, text=f"batch {it['id']}: nothing left to land")
        return []
    slot = check_tree_slot(st, repo, d)
    if not slot:
        return [f"batch {it['id']}: waiting for a check tree"]
    return batch_assembled(tick, assemble_in_tree(tick, slot))


def batch_assembled(tick: BatchTick, asm: Any) -> list[str]:
    """The assembled batch: its one member left alone, nothing kept, its red pre-check kept at green, or sent to be checked."""
    it, d, kept = tick.it, tick.d, asm.kept
    if asm.full and len(kept) < backlog_min():  # one left: it is checked (a push batch's: pushed) alone, as any change
        return batch_left_alone(tick, asm)
    if not kept:
        if not ChangeRecords(tick.st, d).waiting():
            batch_move(tick.st, it, "landed", {"members": []}, text=f"batch {it['id']}: nothing left to land")
        return [f"batch {it['id']}: nothing kept"]
    if not asm.full:  # THE WAITING BATCH AT GREEN: pre-checked as one at red, its verdict kept, or checked as needed
        depth, why = green_batch_depth(asm.tree, it, kept, asm.base, asm.tip)
        if depth == "kept":
            return batch_kept_on_precheck(tick, asm, why)
        asm = asm._replace(depth=depth)
    return batch_checking_started(tick, asm)


def assemble_in_tree(tick: BatchTick, slot: Any) -> Assembled:
    """The batch's members assembled on main in the check tree the slot holds (the slot given back)."""
    i, h = slot
    full = tick.depth in DEEP_BATCHES
    tips = {e["id"]: e.get("tip") for e in tick.ready}
    try:
        git(tick.repo, "fetch", "--quiet", remote(), "main", check=False)
        base = git(tick.repo, "rev-parse", f"{remote()}/main").strip()
        tree = check_tree(tick.repo, i, base)
        kept = assemble(tick.st, tick.repo, tick.d, tree, tick.ready, base, alone=full, autoresolve=full)
        depth, full_round = batch_round_depth(tree, kept, base, tick.depth)
        tip = git(tree, "rev-parse", "HEAD").strip()
        range_base = union_base(tree, kept, base) if depth == "verify" else None
    finally:
        h.release()
    return Assembled(i, tree, base, kept, tips, depth, full, full_round, tip, range_base)


def batch_left_alone(tick: BatchTick, asm: Any) -> list[str]:
    """Only one member is left: it is checked (a push batch's: pushed) alone, as any change, its own tip kept."""
    for e in asm.kept:
        if e.get("status") == "checked" and asm.tips.get(e["id"]):
            e["tip"] = asm.tips[e["id"]]
            ChangeRecords(tick.st, tick.d).save(e)
    it = tick.it
    batch_move(tick.st, it, "landed", {"members": []}, text=f"batch {it['id']}: no {batch_kind(it)} left to form")
    return [f"batch {it['id']}: no {batch_kind(it)} left to form"]


def batch_kept_on_precheck(tick: BatchTick, asm: Any, why: str) -> list[str]:
    """The waiting batch's red pre-check stands at green: assembled, checking and pushing in one pass, no check again."""
    it, st, kept = tick.it, tick.st, asm.kept
    moved = (it["data"].get("prechecked") or {}).get("base") != asm.base
    for e in kept if moved else ():  # MINIMAL RECHECKS' log, as a lone change's (land status, kept_verdicts)
        e["rechecks"] = recheck_logged(e, "kept", why, asm.base, tick.t)
        ChangeRecords(tick.st, tick.d).save(e)
    batch_move(st, it, "checking", {"members": [e["id"] for e in kept], "tip": asm.tip, "base": asm.base, "tree": asm.i,
                                    "cls": "check", "depth": "fast"}, text=f"batch {it['id']}: {why}")
    batch_move(st, st.item("batch", it["id"]), "pushing", {"kept": why},
               text=f"batch {it['id']}: {asm.tip[:10]} pushed on its red pre-check, no check again")
    return [f"batch {it['id']}: assembled {len(kept)} change(s), {why}"]


def batch_checking_started(tick: BatchTick, asm: Any) -> list[str]:
    """The assembled batch goes to be checked: its members are the batch's until it lands them or lets them go, and its
    executor starts."""
    it, st, d, kept, tip, base, depth = tick.it, tick.st, tick.d, asm.kept, asm.tip, asm.base, asm.depth
    batch_move(st, it, "checking", {"members": [e["id"] for e in kept], "tip": tip, "base": base, "tree": asm.i,
                                    "cls": "red" if tick.fix_waits else "check", "depth": depth,
                                    **({"range_base": asm.range_base} if asm.range_base else {})},
               text=f"batch {it['id']}: checking {len(kept)} change(s) at {tip[:10]} on {base[:10]}"
                    + {"full": f" ({asm.full_round})",
                       "verify": f" (a push batch: one verify round over {(asm.range_base or base)[:10]}..{tip[:10]})"}
                    .get(depth, ""))
    for e in kept if asm.full else ():
        member_checking(st, d, it, e, len(kept), tip, depth)
    spawn_batch_executor(tick.repo, d, it["id"])
    return [f"batch {it['id']}: assembled {len(kept)} change(s), checking {tip[:10]}"]


def member_checking(st: Any, d: str, it: Any, e: Any, n: Any, tip: str, depth: Any) -> None:
    """Entry e is checking in batch `it` (`n` members at `tip`); a conflict the assembly resolved is told to its author."""
    verify = {"verify": True, "rounds": e.get("rounds", 0) + (e.get("status") == "checked")} if depth == "verify" else {}
    resolved = "; ".join(f"{m['file']} ({', '.join(m['hunks'])})" for m in e.get("autoresolved") or [])
    set_change(st, d, e, "checking", cause="batch", data={"autoresolved": resolved or None},
               event={"text": f"{e['id']}: checking in {batch_kind(it)} {it['id']} "
                              f"({n} change(s) at {tip[:10]})"
                              + (f"; its conflict AUTO-RESOLVED in the batch, the batch's full check its "
                                 f"verification: {resolved}" if resolved else ""),
                      **({"audience": {"worktree": e.get("worktree")}} if resolved else {})},
               backlog_batch=it["id"], since=ts(), round_since=ts(), **verify)


def batch_executor_alive(tick: BatchTick) -> list[str]:
    """A checking or bisecting batch: left to its executor while it holds its lease; started again when it does not (or,
    past the batch's deadline, failed)."""
    it = tick.it
    if lease_live(f"check/batch-{it['id']}", tick.st):
        return []
    if tick.t > it["deadline"]:
        return [batch_fail(tick.st, tick.d, it, "its check passed its deadline with no live checker")["state"]]
    spawn_batch_executor(tick.repo, tick.d, it["id"])
    return [f"batch {it['id']}: its executor started again"]

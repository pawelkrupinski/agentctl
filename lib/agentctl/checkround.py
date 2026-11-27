# agentctl, part `checkround`: one check round of a queued change: run its hook checks, mark it checking and checked
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
from agentctl.context import Context
from agentctl.clock import Clock
from agentctl.timefmt import ts
from agentctl.repos import precheck_hook, waiting_supported
from agentctl.clones import clone_shared
from agentctl.waitingdir import ChangeRecords, waiting_state, wlog
from agentctl.queueslots import check_tree_slot, checker_lease, entry_owned, set_change
from agentctl.queue import VERIFY_MODE
from agentctl.redland import may_land_on_red
from agentctl.changekind import facts_for
from agentctl.waiting import must_wait, waiting_commits
from agentctl.gov import gov_class_of
from agentctl.redseen import gate_reds
from agentctl.queuecheck import (
    adopted_run, check_mode, commits_here, fetched_main, judge_check_failure, requeued_for_infra, round_ended,
    round_head, tip_refused)
from agentctl.entrychecks import run_entry_checks


def check_entry(repo: str, eid: str, clock: Clock, store: Any, *, ctx: Context) -> int:
    """THE EXECUTOR of one change's checks (`waiting check <id>`, detached): holding check/<entry> and a check tree slot
    (leases), it takes the change to checking and leaves it checked (with the tip and base it passed on), rejected (a
    test failure, a conflict) or queued again (infra). It never pushes: the tick does (push_change)."""
    d, _c, _w = waiting_state(repo)
    lease = checker_lease(store, eid)
    if not lease.acquire():  # another checker holds it (and progresses)
        return 0
    own = entry_owned(eid)
    if not own.acquire():
        lease.release()
        return 0
    try:
        e = ChangeRecords(store, d).get(eid)
        if not e or e.get("status") not in ("queued", "checking") or e.get("backlog_batch"):  # a backlog batch's
            return 0
        fix = may_land_on_red(facts_for(gate_reds(repo, ctx=ctx), store), e, repo, store=store, ctx=ctx).red_fix  # its own tree
        slot = check_tree_slot(store, repo, d, fix=fix)
        if not slot:  # all trees busy: it stays queued, the tick starts it again when one frees
            return 0
        i, slot_h = slot
        try:
            with clone_shared(repo):  # a clone swap (repair_clone) waits for the check
                return _check_entry(store, repo, d, e, i, clock, own, ctx=ctx)
        finally:
            slot_h.release()
    finally:
        own.release()
        lease.release()


def _check_entry(st: Any, repo: str, d: str, e: dict[str, Any], i: int, clock: Clock, own: Any = None, *, ctx: Context) -> int:
    """One change's check round: main fetched, the change rebased onto it (or its remote run adopted), refused for its
    blame or an unreplayed fix, the hook's checks run, and the change marked checked. 0: the round is over (checked,
    requeued, landed already), 1: the change is rejected."""
    mark_checking(st, d, e)
    base = round_base(st, repo, d, e, clock)
    if base is None:
        return 0
    rem = e.get("remote") or {}
    adopt = adopted_run(repo, e, rem, base)
    tree, head, via, base, conflicts = round_head(st, d, e, repo, i, base, rem, adopt)
    ended = round_ended(st, d, e, tree, base, head, via, conflicts)
    if ended is not None:
        return ended
    e["tip"] = head
    ChangeRecords(st, d).save(e)
    commits, reds = waiting_commits(tree, base, head), gate_reds(repo, ctx=ctx) if waiting_supported(repo) else set()
    if tip_refused(st, repo, d, e, tree, commits, base):
        return 1
    hook = precheck_hook(repo)
    mode = check_mode(d, e, tree, base, head, rem if adopt else {}, bool(reds) and must_wait(repo, commits, reds, st, ctx=ctx), hook)
    done, where, known_red = run_hook_checks(st, repo, d, e, tree, base, head, mode, gov_class_of(repo, commits, reds, ctx=ctx),
                                             os.path.join(d, f"check-{e['id']}"), clock, own, ctx=ctx) if hook \
        else (None, "no hook", None)
    if done is not None:
        return done
    mark_checked(st, d, e, i, base, head, mode, where, known_red)
    return 0


def run_hook_checks(st: Any, repo: str, d: str, e: dict[str, Any], tree: Any, base: str, head: str, mode: str,
                    cls: Any, log_path: str, clock: Clock, own: Any, *, ctx: Context) -> tuple[int | None, str, Any]:
    """The hook's checks over the change: (the round's exit code when it ended here, else None; where they ran; the
    red they failed only as its base fails, if any). An infra failure (stalled, network, laptop, time cap) requeues."""
    ok, out, where = run_entry_checks(repo, d, e, tree, base, head, mode, cls, log_path, clock, st, own)
    if requeued_for_infra(st, repo, d, e, ok, out, where, clock):
        return 0, where, None
    if ok:
        return None, where, None
    return judge_check_failure(st, repo, d, e, tree, base, out, where, clock.time(), ctx=ctx)


def mark_checked(st: Any, d: str, e: dict[str, Any], i: int, base: str, head: str, mode: str, where: str,
                 known_red: Any) -> None:
    """The change marked checked: on its own base, or verified (the earlier full check carried to this main)."""
    verify = mode == VERIFY_MODE
    full_base = (e.get("full_base") or e.get("checked_base") or base) if verify else base
    full_tip = e.get("full_tip") if verify else head
    areas = e.pop("verify_areas", None) if verify else None
    verified = (f"verified {head[:10]} on {base[:10]} (fully checked on {full_base[:10]}"
                + (f"; with the area tests: sbt modules both sides touch, {', '.join(areas)}" if areas else "") + ")") \
        if verify else f"checked {head[:10]} on {base[:10]}"
    set_change(st, d, e, "checked", cause="check-passed", data={"tip": head, "base": base, "mode": mode, "where": where},
               event={"text": f"{e['id']}: {verified} ({where})"
                      + (" -- failing only as its base fails" if known_red else "")},
               tip=head, checked_base=base, full_base=full_base, full_tip=full_tip, checked_mode=mode, checked_where=where, tree=i,
               infra_reason=None, infra_same=0, known_red=known_red, verify=False, push_alone=None,
               verified_areas=areas)
    wlog(d, f"{e['id']}: {verified} ({where})")


def mark_checking(st: Any, d: str, e: dict[str, Any]) -> None:
    if e.get("status") != "checking":
        set_change(st, d, e, "checking", cause="check-running", event={"text": f"{e['id']}: checking"}, since=ts())


def round_base(st: Any, repo: str, d: str, e: dict[str, Any], clock: Clock) -> str | None:
    """The main the round checks on, with the change's commits here; None when either failed (the change requeued)."""
    base = fetched_main(st, repo, d, e, clock)
    return base if base is not None and commits_here(st, repo, d, e, clock) else None

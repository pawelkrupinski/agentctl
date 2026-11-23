# agentctl, part `batchpush`: a green batch's push: its tip onto main, each member's verdict
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from agentctl.core import git, git_ok, remote
from agentctl.timefmt import ts
from agentctl.repos import precheck_hook
from agentctl.waitingdir import waiting_state, wlog
from agentctl.gating import mark_gate_dirty
from agentctl.leases import lock_note
from agentctl.land import RACE, REDGATE, land_lock_if_free, landed, push_argv, push_bounds, run_watched, tool_env
from agentctl.owned import Owned, lock_held
from agentctl.queueslots import queue_verdict, set_change, tree_slot
from agentctl.queue import hook_query, range_paths
from agentctl.waiting import tail_of
from agentctl.landwatch import start_watch
from agentctl.recovery import batch_move
from agentctl.queuepush import DEEP_BATCHES
from agentctl.batchitem import batch_fail, batch_members


def push_batch_item(repo, d, st, it):
    """The tick's push of a passing batch, under the landing lease: main moved -> assembled again; else the tip pushed
    (the batch's fast pass stands for the push: PREPUSH_REUSE_FAST) and verified on main -> every change LANDED."""
    members = batch_members(d, it)
    tip, base, i = it["data"]["tip"], it["data"]["base"], it["data"]["tree"]
    if not members:
        batch_move(st, it, "assembling", text=f"batch {it['id']}: its changes are gone")
        return f"batch {it['id']}: nothing to push"
    slot = tree_slot(d, i)  # its tree: never touched under a checker that took the slot since the batch's check
    if not slot.acquire():
        return f"batch {it['id']}: its check tree is busy"
    try:
        return _push_batch_item(repo, d, st, it, members, tip, base, i)
    finally:
        slot.release()


def _push_batch_item(repo, d, st, it, members, tip, base, i):
    _d, clone, _w = waiting_state(repo)
    tree = f"{clone}-wt-check-{i}"
    with land_lock_if_free(repo) as held:
        if not held:
            return f"batch {it['id']}: the landing lease is busy"
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
        if git(repo, "rev-parse", f"{remote()}/main").strip() != base:
            batch_move(st, it, "assembling", text=f"batch {it['id']}: main moved during its check: assembled again")
            return f"batch {it['id']}: main moved"
        # A DECIDED MEMBER IS NEVER PUSHED (queue simulator, 2026-11-18, seed 121 of the chaos profile): a member
        # superseded or cancelled while the batch checked dropped out of batch_members, but its commits were still in
        # the assembled tip -- and landed with the others. The tip is assembled again without it.
        gone = [m for m in it["data"].get("members") or [] if m not in {e["id"] for e in members}]
        if gone:
            batch_move(st, it, "assembling", text=f"batch {it['id']}: {', '.join(gone)} left it (decided) during its "
                                                  "check: assembled again without")
            return f"batch {it['id']}: {len(gone)} member(s) gone"
        lock_note(f"push of the batch {tip[:10]}")
        if not os.path.isdir(tree) or not git_ok(tree, "cat-file", "-e", f"{tip}^{{commit}}"):
            batch_move(st, it, "assembling", text=f"batch {it['id']}: its tree is gone: assembled again")
            return f"batch {it['id']}: tree gone"
        git(tree, "checkout", "-q", "--force", "--detach", tip, check=False)
        pushed_at = st.clock()
        depth = it["data"].get("depth")
        if depth == "full":  # its full check's run stands for the push, once GitHub confirms it
            run = (it["data"].get("runs") or {}).get(tip)
            env = dict(tool_env(), **({"PREPUSH_REMOTE_RUN": str(run)} if run else {}))
        elif depth == "verify":  # each member fully checked on an older main, the union verified: the red gate only
            skip = hook_query(precheck_hook(tree), tree, "--plan", range_paths(tree, base, tip))
            if skip is None:
                batch_fail(st, d, it, "the hook's plan for its push could not be read", len(members))
                return f"batch {it['id']}: its plan is unreadable"
            env = dict(tool_env(), PREPUSH_SKIP=" ".join(skip))
        elif it["data"].get("kept"):  # pre-checked as one at red, nothing main changed read by it: the red gate only
            skip = hook_query(precheck_hook(tree), tree, "--plan", range_paths(tree, base, tip))
            if skip is None:
                batch_move(st, it, "assembling", {"kept": None, "prechecked": None},
                           text=f"batch {it['id']}: the hook's plan for its push could not be read: checked again")
                return f"batch {it['id']}: its plan is unreadable"
            env = dict(tool_env(), PREPUSH_SKIP=" ".join(skip))
        else:
            env = dict(tool_env(), PREPUSH_REUSE_FAST="1")
        for e in members if depth in DEEP_BATCHES else ():
            if e.get("status") == "checked":
                set_change(d, e, "pushing", event={"text": f"{e['id']}: pushing in batch {it['id']} ({tip[:10]})"})
        rc, out = run_watched(push_argv(remote(), tip), tree, env, os.path.join(d, f"push-{tip[:10]}"), "push",
                              **push_bounds())
        with open(os.path.join(d, "checks.log"), "a") as f:
            f.write(f"=== push {ts()} {tip[:10]} rc {rc}\n{out}\n")
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
        if not landed(repo, tip, f"refs/remotes/{remote()}/main"):
            if rc is not None and REDGATE.search(out or ""):
                batch_move(st, it, "assembling", text=f"batch {it['id']}: the gate went red before the push")
                return f"batch {it['id']}: red"
            if rc is not None and RACE.search(out or ""):
                batch_move(st, it, "assembling", text=f"batch {it['id']}: raced: assembled again")
                return f"batch {it['id']}: raced"
            why = "its push stalled" if rc is None else f"its push failed: {tail_of(out or '', 3)[:160]}"
            batch_fail(st, d, it, why, len(members))
            return f"batch {it['id']}: {why}"
    mark_gate_dirty()
    for e in members:
        kind = "push batch" if it["data"].get("depth") == "verify" else "batch"
        queue_verdict(d, e, "landed", f"LANDED {e['tip']} ({kind} {it['id']}: {len(members)} change(s) in one push)",
                      code=0, landed_sha=e["tip"], pushed_at=pushed_at, batch=len(members), batch_id=it["id"])
        if not lock_held(Owned("await", e["id"]).lockpath):  # a `land --follow` waiting on it watches it itself
            start_watch(st, repo, e["tip"], pushed_at, worktree=e["worktree"],
                        close=e["worktree"] if os.path.isdir(e["worktree"]) else None, session=e.get("session"))
    batch_move(st, st.item("batch", it["id"]), "landed", text=f"LANDED {tip} ({len(members)} change(s) in one push)")
    wlog(d, f"LANDED {tip} ({len(members)} change(s) in one push)")
    return f"batch {it['id']}: landed {tip[:10]}"

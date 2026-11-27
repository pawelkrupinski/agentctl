# agentctl, part `batchpush`: a green batch's push: its tip onto main, each member's verdict
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any, Optional
from agentctl.timefmt import ts
from agentctl.core import git, remote, commit_known
from agentctl.repos import precheck_hook
from agentctl.onmain import landed_state
from agentctl.leases import lock_note
from agentctl.gating import mark_gate_dirty
from agentctl.waitingdir import waiting_state, wlog
from agentctl.landwatch import start_watch
from agentctl.owned import Owned
from agentctl.land import RACE, REDGATE, land_lock_if_free, push_argv, push_bounds, run_watched, tool_env
from agentctl.queueslots import queue_verdict, set_change, tree_slot
from agentctl.queue import hook_query, range_paths
from agentctl.waiting import tail_of
from agentctl.batchitem import batch_fail, batch_members, batch_move
from agentctl.queuepush import DEEP_BATCHES


def push_batch_item(repo, d, st, it):
    """The tick's push of a passing batch, under the landing lease: main moved -> assembled again; else the tip pushed
    (the batch's fast pass stands for the push: PREPUSH_REUSE_FAST) and verified on main -> every change LANDED."""
    members = batch_members(st, d, it)
    tip, base, i = it["data"]["tip"], it["data"]["base"], it["data"]["tree"]
    if not members:
        batch_move(st, it, "assembling", text=f"batch {it['id']}: its changes are gone")
        return f"batch {it['id']}: nothing to push"
    slot = tree_slot(st, d, i)  # its tree: never touched under a checker that took the slot since the batch's check
    if not slot.acquire():
        return f"batch {it['id']}: its check tree is busy"
    try:
        return BatchPush(repo, d, st, it, members, tip, base, i).run()
    finally:
        slot.release()


class BatchPush:
    """One push of a passing batch, under the landing lease: the checks that the batch is still pushable (main did not move,
    no member was decided, its tree is there), the push in the environment its depth earns, and each member's verdict."""

    def __init__(self, repo: str, d: str, st: Any, it: dict[str, Any], members: list[dict[str, Any]], tip: str, base: str,
                 i: Any) -> None:
        self.repo, self.d, self.st, self.it, self.members, self.tip, self.base = repo, d, st, it, members, tip, base
        _d, clone, _w = waiting_state(repo)
        self.tree = f"{clone}-wt-check-{i}"
        self.bid = it["id"]
        self.pushed_at: Any = None
        self.env: Any = None

    def say(self, to: str, text: str, **data: Any) -> None:
        said = f"batch {self.bid}: {text}"
        if data:
            batch_move(self.st, self.it, to, data, text=said)
        else:
            batch_move(self.st, self.it, to, text=said)

    def run(self) -> str:
        with land_lock_if_free(self.repo, self.st) as held:
            if not held:
                return f"batch {self.bid}: the landing lease is busy"
            stale = self.stale() or self.prepare()
            if stale:
                return stale
            refused = self.push()
            if refused:
                return refused
        mark_gate_dirty()
        self.land_members()
        return f"batch {self.bid}: landed {self.tip[:10]}"

    def stale(self) -> Optional[str]:
        """Why the batch is no longer pushable (assembled again), else None."""
        git(self.repo, "fetch", "--quiet", remote(), "main", check=False)
        if git(self.repo, "rev-parse", f"{remote()}/main").strip() != self.base:
            self.say("assembling", "main moved during its check: assembled again")
            return f"batch {self.bid}: main moved"
        # A DECIDED MEMBER IS NEVER PUSHED (queue simulator, 2026-11-18, seed 121 of the chaos profile): a member
        # superseded or cancelled while the batch checked dropped out of batch_members, but its commits were still in
        # the assembled tip -- and landed with the others. The tip is assembled again without it.
        gone = [m for m in self.it["data"].get("members") or [] if m not in {e["id"] for e in self.members}]
        if gone:
            self.say("assembling", f"{', '.join(gone)} left it (decided) during its check: assembled again without")
            return f"batch {self.bid}: {len(gone)} member(s) gone"
        lock_note(f"push of the batch {self.tip[:10]}")
        if not os.path.isdir(self.tree) or not commit_known(self.tree, self.tip):
            self.say("assembling", "its tree is gone: assembled again")
            return f"batch {self.bid}: tree gone"
        return None

    def prepare(self) -> Optional[str]:
        """The tree at the tip, the push's environment and the members marked pushing; why not, else None."""
        git(self.tree, "checkout", "-q", "--force", "--detach", self.tip, check=False)
        self.pushed_at = self.st.clock()
        self.env = self.push_env()
        if isinstance(self.env, str):
            return self.env
        depth = self.it["data"].get("depth")
        for e in self.members if depth in DEEP_BATCHES else ():
            if e.get("status") == "checked":
                set_change(self.st, self.d, e, "pushing", cause="pushing",
                           event={"text": f"{e['id']}: pushing in batch {self.bid} ({self.tip[:10]})"})
        return None

    def plan_skip(self) -> Any:
        """The checks the hook's plan says the push may skip, or None when the plan could not be read."""
        return hook_query(precheck_hook(self.tree), self.tree, "--plan", range_paths(self.tree, self.base, self.tip))

    def push_env(self) -> Any:
        """The push's environment by the batch's depth, or the tick's line when its plan is unreadable."""
        data, depth = self.it["data"], self.it["data"].get("depth")
        if depth == "full":  # its full check's run stands for the push, once GitHub confirms it
            run = (data.get("runs") or {}).get(self.tip)
            return dict(tool_env(), **({"PREPUSH_REMOTE_RUN": str(run)} if run else {}))
        if depth == "verify" or data.get("kept"):
            return self.skip_env(depth)
        return dict(tool_env(), PREPUSH_REUSE_FAST="1")

    def skip_env(self, depth: Any) -> Any:
        """The environment of a push the hook's plan lets skip checks (verify: each member fully checked on an older main,
        the union verified; kept: pre-checked as one at red, nothing main changed read by it: the red gate only), or the
        tick's line when the plan is unreadable."""
        skip = self.plan_skip()
        if skip is not None:
            return dict(tool_env(), PREPUSH_SKIP=" ".join(skip))
        if depth == "verify":
            batch_fail(self.st, self.d, self.it, "the hook's plan for its push could not be read", len(self.members))
        else:
            self.say("assembling", "the hook's plan for its push could not be read: checked again",
                     kept=None, prechecked=None)
        return f"batch {self.bid}: its plan is unreadable"

    def push(self) -> Optional[str]:
        """Push the tip; why it did not land, else None."""
        rc, out = run_watched(push_argv(remote(), self.tip), self.tree, self.env,
                              os.path.join(self.d, f"push-{self.tip[:10]}"), "push", **push_bounds())
        with open(os.path.join(self.d, "checks.log"), "a") as f:
            f.write(f"=== push {ts()} {self.tip[:10]} rc {rc}\n{out}\n")
        git(self.repo, "fetch", "--quiet", remote(), "main", check=False)
        if landed_state(self.repo, f"refs/remotes/{remote()}/main", self.tip).landed:  # unknown: the push's rc decides below
            return None
        if rc is not None and REDGATE.search(out or ""):
            self.say("assembling", "the gate went red before the push")
            return f"batch {self.bid}: red"
        if rc is not None and RACE.search(out or ""):
            self.say("assembling", "raced: assembled again")
            return f"batch {self.bid}: raced"
        why = "its push stalled" if rc is None else f"its push failed: {tail_of(out or '', 3)[:160]}"
        batch_fail(self.st, self.d, self.it, why, len(self.members))
        return f"batch {self.bid}: {why}"

    def land_members(self) -> None:
        """Every member LANDED with its own record, and a watch on its tip (unless its `land --follow` waits on it)."""
        st, it, tip, n = self.st, self.it, self.tip, len(self.members)
        for e in self.members:
            kind = "push batch" if it["data"].get("depth") == "verify" else "batch"
            queue_verdict(st, self.d, e, "landed", f"LANDED {e['tip']} ({kind} {self.bid}: {n} change(s) in one push)",
                          code=0, landed_sha=e["tip"], pushed_at=self.pushed_at, batch=n, batch_id=self.bid)
            if not Owned("await", e["id"]).live():  # a `land --follow` waiting on it watches it itself
                start_watch(st, self.repo, e["tip"], self.pushed_at, worktree=e["worktree"],
                            close=e["worktree"] if os.path.isdir(e["worktree"]) else None, session=e.get("session"))
        batch_move(st, st.item("batch", self.bid), "landed", text=f"LANDED {tip} ({n} change(s) in one push)")
        wlog(self.d, f"LANDED {tip} ({n} change(s) in one push)")

# agentctl, part `queuediff`: a checked change's push under the landing lock: brought onto main as it is now (its verdict kept, a new round, or rejected), joined to `waiting` behind a red, or pushed and verified
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any, List, Optional, Tuple
from agentctl.statefile import read_record
from agentctl.core import git, git_ok, remote
from agentctl.netretry import TRANSIENT_PUSH
from agentctl.repos import precheck_hook, waiting_supported
from agentctl.waitingdir import waiting_state, wlog
from agentctl.gating import mark_gate_dirty
from agentctl.clones import fetch_main
from agentctl.landstate import landed_state
from agentctl.land import RACE, REDGATE, land_lock_if_free, push_argv, push_bounds, run_watched, tool_env
from agentctl.queueslots import PUSH_ATTEMPTS_MAX, check_tree, queue_verdict, set_change, tree_slot
from agentctl.conditions import open_condition, run_probe
from agentctl.queue import (
    VERIFY_MODE, hook_query, module_overlap, range_paths, verify_areas_supported, verify_supported)
from agentctl.generated import regen_owed, without_generated
from agentctl.conflicts import conflict_cause, conflict_paths, union_safe
from agentctl.queuerecheck import own_diff_changed, read_overlap, recheck_logged, recheck_reason
from agentctl.redfixes import fixes_of
from agentctl.changekind import facts_for
from agentctl.redland import may_land_on_red
from agentctl.waiting import already_landed, apply_entry, must_wait, tail_of, waiting_commits
from agentctl.landwatch import start_watch
from agentctl.redseen import gate_reds
from agentctl.queuebatch import queue_join
from agentctl.queuefailure import Entry, keys_text


def push_change(repo: str, d: str, e: Entry, clock: Any, st: Any) -> Optional[str]:
    """The tick's push of one checked change, under the landing lease: rebased onto a moved main (re-checked when what
    landed overlaps: a new round, queued), joined to `waiting` while main is red and it must wait, else pushed with
    the push's bounds and verified. pushing -> landed, or back to checked (a push never rejects); after
    PUSH_ATTEMPTS_MAX failures a condition on the git route, and it waits for it."""
    # Its own tree first (its tip and build are there), never checked out under a checker that took the slot since;
    # held, the PUSH TREE: a checker holds its slot for its remote run's whole wait (up to CHECK_LEASE_MAX), and a
    # push waiting for it sat `busy` 30 min past its deadline (2026-11-06, three changes, every tree held).
    for i in (e.get("tree") or 1, PUSH_TREE):
        slot = tree_slot(st, d, i)
        if slot.acquire():
            try:
                return _push_change(repo, d, e, i, clock, st)
            finally:
                slot.release()
    return "busy"


PUSH_TREE = "push"  # <clone>-wt-check-push, under its own lease; no checker ever takes it (check_tree_slot: 1..N,
# and a red fix's RED_FIX_TREE)


def _push_change(repo: str, d: str, e: Entry, i: Any, clock: Any, st: Any) -> Optional[str]:
    hook = precheck_hook(repo)
    tree = check_tree_for_push(repo, e, i)
    with land_lock_if_free(repo, st) as held:
        if not held:
            return "busy"
        status = (read_record(os.path.join(d, "entries", f"{e['id']}.json")) or {}).get("status")
        if status != "checked":  # superseded / cancelled since its checks: no push
            return status
        fetch_main(repo)  # a corrupt clone is marked for repair; a failed fetch pushes onto the last known main
        moved = git(repo, "rev-parse", f"{remote()}/main").strip()
        push = ChangePush(repo, d, e, tree, clock, st, hook)
        return push.onto(moved) or push.wait_or_push()


class ChangePush:
    """One checked change's push under the landing lock: what it is pushed as (base, head, where it was checked, the
    checks its hook may skip), brought onto main as it is now, then joined to `waiting` or pushed and verified."""

    def __init__(self, repo: str, d: str, e: Entry, tree: str, clock: Any, st: Any, hook: Any) -> None:
        self.repo, self.d, self.e, self.tree, self.clock, self.st, self.hook = repo, d, e, tree, clock, st, hook
        self.base, self.head, self.where = e["checked_base"], e["tip"], e.get("checked_where") or ""
        self.skip: Any = None
        self.full_base = e.get("full_base") or self.base
        self.log_path = os.path.join(d, f"check-{e['id']}")

    def onto(self, moved: str) -> Optional[str]:
        """The change brought onto main at `moved`: None to push it, else the outcome (landed, queued, rejected)."""
        if moved != self.base:
            return self._rebased(moved)
        if self.e.get("checked_mode") == VERIFY_MODE:
            return self._combined_tree_verified()
        return None

    def _rebased(self, moved: str) -> Optional[str]:
        conflicts: list[str] = []  # no regen command under the landing lock: a generated file's conflict is a new round's
        new_head, via = rebase_in_tree(self.d, self.tree, self.e, moved, regen=False, conflicts=conflicts)
        if via:
            already_landed(self.st, self.d, self.e, via if via != "an equivalent commit" else None)
            return "landed"
        owed = generated_round(self.tree, conflicts, self.base, moved, new_head)
        if owed:
            return self._regenerating_round(owed)
        if not new_head:
            # policy: no-longer-applies
            queue_verdict(self.st, self.d, self.e, "rejected", f"NOT LANDED: the change no longer applies on "
                          f"{remote()}/main {moved[:10]} (main moved during its checks"
                          f"{conflict_cause(self.tree, self.base, moved, conflicts)}): rebase onto {remote()}/main and "
                          "land again (it keeps its place in the queue)",
                          conflict_files=conflict_paths(conflicts), decided_on=moved)
            return "rejected"
        return self._recheck(moved, new_head)

    def _regenerating_round(self, owed: tuple[str, list[str]]) -> str:
        e = self.e
        what = f"main moved {owed[0]} ({', '.join(owed[1][:3])}): a new round, which regenerates them"
        set_change(self.st, self.d, e, "queued", cause="main-moved", data={"rounds": e.get("rounds", 0) + 1},
                   event={"text": f"{e['id']}: {what}"}, rounds=e.get("rounds", 0) + 1, retry_after=0, verify=True)
        wlog(self.d, f"{e['id']}: {what}: queued for a new round")
        return "queued"

    def _recheck(self, moved: str, new_head: str) -> Optional[str]:
        """What main's move asks of the rebased change: its verdict kept, a new round, or (no hook) nothing."""
        tree_hook = precheck_hook(self.tree) if self.hook else None
        moved_by, why = self._recheck_reason(tree_hook, moved, new_head)
        kept, verify_why = self._minimal_recheck(tree_hook, moved, new_head) if why is None and tree_hook else (None, None)
        if kept is not None:
            self._verdict_kept(moved, new_head, moved_by, kept)
            return None
        if why is not None or tree_hook:  # a new round, not an attempt: the full checks, or the combined tree's
            return self._new_round(moved, new_head, moved_by, why, verify_why)
        note = f"{moved_by}, no file overlap: pushed without re-check"  # no hook: nothing to check it with
        wlog(self.d, f"{self.e['id']}: {note}")
        self.where = f"{self.where}; {note}"
        self._moved_to(moved, new_head)
        return None

    def _recheck_reason(self, tree_hook: Any, moved: str, new_head: str) -> tuple[str, Optional[str]]:
        """(how far main moved, why the change needs its full checks again or None)."""
        n, why = recheck_reason(self.tree, self.full_base, moved, new_head, range_paths(self.tree, moved, new_head),
                                full_tip=self.e.get("full_tip"))
        if why is None and tree_hook and not verify_supported(tree_hook):
            why = f"no file overlap, but the hook has no {VERIFY_MODE} for the combined tree"
        return f"main moved by {n} commit{'' if n == 1 else 's'}", why

    def _minimal_recheck(self, tree_hook: Any, moved: str, new_head: str) -> tuple[Any, Optional[str]]:
        """MINIMAL RECHECKS: a round only when its checks read what main changed. (the hook's plan, kept; or why a
        round)."""
        tree = self.tree
        verify_why = read_overlap(tree, range_paths(tree, self.base, moved), range_paths(tree, moved, new_head)) \
            or ("the rebase changed its own diff" if own_diff_changed(tree, self.base, self.head, moved, new_head)
                else None)
        kept = None if verify_why else hook_query(tree_hook, tree, "--plan", range_paths(tree, moved, new_head))
        if not verify_why and kept is None:
            verify_why = "the hook's plan could not be read"
        return kept, verify_why

    def _verdict_kept(self, moved: str, new_head: str, moved_by: str, kept: Any) -> None:
        note = f"{moved_by} since its checks on {self.base[:10]}, no check of it reading what changed: its verdict kept"
        wlog(self.d, f"{self.e['id']}: {note}")
        self.e["rechecks"] = recheck_logged(self.e, "kept", note, moved, self.clock.time())
        self.where, self.skip = f"{self.where}; {note}", kept
        self._moved_to(moved, new_head)

    def _moved_to(self, moved: str, new_head: str) -> None:
        self.e.pop("remote_run", None)
        self.base, self.head, self.e["tip"] = moved, new_head, new_head

    def _new_round(self, moved: str, new_head: str, moved_by: str, why: Optional[str], verify_why: Optional[str]) -> str:
        e, verify = self.e, why is None
        what = self._combined_tree_words(moved, new_head, verify_why) if verify else f"{why}: checked again"
        log = recheck_logged(e, "verify" if verify else "full", f"{moved_by}: {verify_why or why}", moved, self.clock.time())
        set_change(self.st, self.d, e, "queued", cause="main-moved", data={"rounds": e.get("rounds", 0) + 1, "rechecks": log},
                   event={"text": f"{e['id']}: {moved_by} since its checks, {what}"},
                   rounds=e.get("rounds", 0) + 1, retry_after=0, verify=verify, rechecks=log)
        wlog(self.d, f"{e['id']}: {moved_by} during its checks, {what}: queued for a new round")
        return "queued"

    def _combined_tree_words(self, moved: str, new_head: str, verify_why: Optional[str]) -> str:
        tree = self.tree
        areas = module_overlap(range_paths(tree, self.full_base, moved), range_paths(tree, moved, new_head)) \
            if verify_areas_supported(tree) else []
        return ("no file overlap: its combined tree verified (every test config compiled, the touched modules' unit "
                "tests" + (f", the area tests: sbt modules both sides touch, {', '.join(areas)}" if areas else "") + ")"
                + (f", as {verify_why}" if verify_why else ""))

    def _combined_tree_verified(self) -> Optional[str]:
        """The combined tree passed: the change's own checks stand (the hook skips them), unless its plan is unread."""
        e = self.e
        self.skip = hook_query(precheck_hook(self.tree), self.tree, "--plan", range_paths(self.tree, self.base, self.head))
        if self.skip is None:
            set_change(self.st, self.d, e, "queued", cause="main-moved",
                       event={"text": f"{e['id']}: the hook's plan could not be read: checked again"},
                       retry_after=0, verify=False)
            return "queued"
        areas = e.get("verified_areas")
        self.where = (f"{self.where}; main moved since its full check on {self.full_base[:10]}: no file overlap, the "
                      "combined tree verified"
                      + (f" with the area tests (sbt modules both sides touch: {', '.join(areas)})" if areas else ""))
        e.pop("remote_run", None)
        return None

    def wait_or_push(self) -> str:
        """Joined to `waiting` behind main's red when it must wait, else pushed."""
        commits = waiting_commits(self.tree, self.base, self.head)
        reds = gate_reds(self.repo) if waiting_supported(self.repo) else set()
        # a gate red's fix failing only as main fails lands: no wait
        landing = may_land_on_red(facts_for(reds, self.st), self.e, self.repo, commits, self.st)
        known = self.e.get("known_red")
        # policy: known-red-tools
        kr = None if landing.gate_fix or landing.tools_only_known_red else known
        if kr or (reds and must_wait(self.repo, commits, reds, self.st)):
            queue_join(self.st, self.repo, self.d, self.e, self.tree, self.head, known_red_words(kr))
            return "joined"
        fixes_of(self.repo, commits, self.st)  # its Fixes-Red trailers checked (fix_check caches each verdict and tells
        # the author of one that counts for nothing)
        return self._push(landing.tools_only_known_red)

    def _push(self, tools_only_known_red: bool = False) -> str:
        e, head = self.e, self.head
        past = " (tools-only, failing only as main's gate fails: not held behind that red)" if tools_only_known_red else ""
        set_change(self.st, self.d, e, "pushing", cause="tools-only-known-red" if tools_only_known_red else "pushing",
                   data={"rechecks": e["rechecks"]} if e.get("rechecks") else None,
                   event={"text": f"{e['id']}: pushing {head[:10]}{past}"})
        pushed_at = self.clock.time()
        env = self._push_env()
        rc, out = run_watched(push_argv(remote(), head), self.tree, env, self.log_path, "push", **push_bounds())
        git(self.repo, "fetch", "--quiet", remote(), "main", check=False)
        if landed_state(self.repo, f"refs/remotes/{remote()}/main", head).landed:  # verify: on main, whatever rc said (unknown: the rc decides)
            return self._landed(pushed_at)
        if rc is not None and (RACE.search(out or "") or REDGATE.search(out or "")):
            set_change(self.st, self.d, e, "checked", cause="pushing",
                       event={"text": f"{e['id']}: the push raced or met a red gate: tried again"})
            return "checked"
        return self._push_failed(rc, out)

    def _push_env(self) -> dict[str, str]:
        env: dict[str, str] = tool_env()
        if self.e.get("remote_run"):  # the hook takes ci-runner's pass once GitHub confirms it (remote checks)
            env["PREPUSH_REMOTE_RUN"] = str(self.e["remote_run"])
        if self.skip:  # fully checked on an older base, the combined tree verified: the hook judges the red gate only
            env["PREPUSH_SKIP"] = " ".join(self.skip)
        return env

    def _landed(self, pushed_at: float) -> str:
        e, head = self.e, self.head
        mark_gate_dirty()
        queue_verdict(self.st, self.d, e, "landed", f"LANDED {head} (queued {e['id']}, checked {self.where})", code=0,
                      landed_sha=head, pushed_at=pushed_at)
        close = None if e.get("keep_worktree") else e["worktree"]
        start_watch(self.st, self.repo, head, pushed_at, worktree=e["worktree"],
                    close=close if close and os.path.isdir(close) else None, session=e.get("session"))
        return "landed"

    def _push_failed(self, rc: Optional[int], out: Optional[str]) -> str:
        """Back to checked, tried again later each time; past PUSH_ATTEMPTS_MAX a STALLED condition on the git route."""
        e = self.e
        n = e.get("push_attempts", 0) + 1
        why = "the push stalled" if rc is None else ((TRANSIENT_PUSH.search(out or "") or [None])[0]
                                                     or f"the push failed (exit {rc})")
        set_change(self.st, self.d, e, "checked", cause="pushing", data={"push_attempts": n},
                   event={"text": f"{e['id']}: {why}: push attempt {n}",
                          "severity": "escalate" if n >= PUSH_ATTEMPTS_MAX else "info",
                          "audience": {"worktree": e.get("worktree")}},
                   push_attempts=n, retry_after=self.clock.time() + min(60 * 2 ** n, 1800))
        wlog(self.d, f"{e['id']}: {why} (push attempt {n}); its tail: {tail_of(out or '')[:300]}")
        if n >= PUSH_ATTEMPTS_MAX:
            open_condition("STALLED", f"STALLED: the push of {e['id']} failed {n} times ({why}); it is tried again "
                                      f"when git can read {remote()}/main", run_probe("git_fetch", {"repo": self.repo}),
                           [], key=f"push-failing-{e['id']}", command="land", cwd=e.get("worktree"),
                           tail=(out or "").splitlines()[-15:], store=self.st)
        return "checked"


def known_red_words(kr: Any) -> Optional[str]:
    """Why a change failing only where main fails waits (not held against it), or None."""
    if not kr:
        return None
    return (f"its checks failed only where {remote()}/main fails too ({keys_text(kr['keys'])}: "
            f"{'; '.join(kr['where'])}): not held against it; it lands with the batch once main is green "
            "or the red's fix lands")


def check_tree_for_push(repo: str, e: Entry, i: Any) -> str:
    """Tree i (the one the change was checked in, or the push tree) at the change's tip, made when it is missing."""
    _d, clone, _w = waiting_state(repo)
    tree = f"{clone}-wt-check-{i}"
    if not os.path.isdir(tree) or not git_ok(tree, "cat-file", "-e", f"{e['tip']}^{{commit}}"):
        tree = check_tree(repo, i, e["tip"])
    git(tree, "checkout", "-q", "--force", "--detach", e["tip"], check=False)
    return tree


def generated_round(tree: str, conflicts: List[str], base: str, moved: str,
                    new_head: Optional[str]) -> Optional[Tuple[str, List[str]]]:
    """(how, files) when a push's rebase (no regen command under the landing lock) leaves generated files only a new
    round's regeneration decides -- it conflicts only in generated files (or prose), or git merged a snapshot or
    derived file both sides changed (generated.py regen_owed: A DERIVED FILE HOLDS NOTHING) --, else None."""
    if not new_head and conflicts and not [f for f in without_generated(tree, conflicts) if not union_safe(f)]:
        return "by what conflicts with it only in generated files", conflicts
    owed = regen_owed(tree, base, moved, new_head)
    return ("under regenerated files it changes too", owed) if owed else None


def rebase_in_tree(d: str, tree: str, e: Entry, base: str, regen: bool = True,
                   conflicts: Optional[list[str]] = None) -> tuple[Optional[str], Optional[str]]:
    """The entry's commits on base, in the tree: (head, None); (None, None) on a conflict (aborted; its files appended
    to `conflicts`); (None, via) when every commit is on base already (dropped: LANDED via that commit, never an empty
    push). regen False: a conflict in a generated file is left a conflict (waiting.py conflict_resolve)."""
    git(tree, "checkout", "-q", "--force", "--detach", base)
    how, x = apply_entry(d, tree, e, base, regen=regen)
    if how == "conflict":
        if conflicts is not None:
            conflicts.extend(x or [])
        return None, None
    if how in ("landed", "duplicate"):
        return None, x or "an equivalent commit"
    return git(tree, "rev-parse", "HEAD").strip(), None

# agentctl, part `cleanuppass`: one `agentctl cleanup` pass: repos fetched, merged worktrees and finished measure branches removed
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import sys
from typing import Any
from agentctl.clock import stopwatch
from agentctl.core import PROTECTED_BRANCHES, REPOS, err, git, remote, scratch, slug
from agentctl.forge import forge_branch_sha, forge_delete_branch, gh_json
from agentctl.procs import process_cwds
from agentctl.worktrees import merged_reason, worktrees
from agentctl.deletions import log_deletion
from agentctl.cleanup import bounded_fetch, cleanup_left, plan_measure, plan_worktrees, still_in_use
from agentctl.tickknobs import knob_cleanup_remove_reserve


def cmd_cleanup(a: Any) -> int:
    return CleanupPass(a).run()


class CleanupPass:
    """One `agentctl cleanup`: each repo fetched, its merged worktrees (and their branches) removed, its finished
    measure branches deleted; dry unless executed. Its state: the exit code, each repo's time, what the deadline left,
    and a stalled remote (one wait, not one per repo)."""

    def __init__(self, a: Any) -> None:
        self.a = a
        self.dry = a.dry_run or (sys.stdin.isatty() and not a.execute)
        self.watch = stopwatch(getattr(a, "deadline", None))
        self.reserve = knob_cleanup_remove_reserve()
        self.cwds = process_cwds()
        self.rc = 0
        self.took: list[str] = []
        self.stopped: list[str] = []
        self.net_down: str | None = None

    def run(self) -> int:
        print(f"agentctl cleanup ({'DRY RUN' if self.dry else 'EXECUTE'}); log: "
              f"{os.path.join(scratch(), 'agentctl-deleted.log')}")
        for repo in self.a.repo or REPOS:
            self._repo(os.path.abspath(os.path.expanduser(repo)))
        if self.stopped:
            print(f"cleanup: stopped at its {self.a.deadline:g} s deadline: {len(self.stopped)} left for the next pass "
                  f"({', '.join(self.stopped[:3])}{', ...' if len(self.stopped) > 3 else ''})")
        print(f"cleanup: {self.watch.elapsed():.0f} s ({'; '.join(self.took) or 'no repo'})")
        return self.rc

    def _repo(self, repo: str) -> None:
        name = os.path.basename(repo)
        if not os.path.isdir(repo):
            return
        if cleanup_left(self.watch) <= 0:
            self.stopped.append(f"repo {name}")
            return
        if self.net_down:
            err(f"{name}: not fetched -- {self.net_down}; skipping repo")
            self.rc = 1
            return
        t_repo = stopwatch()
        main = self._fetched_main(repo, name)
        if main is None:
            return
        print(f"\n== {name} ({remote()}/main {main[:10]})")
        for row in plan_worktrees(repo, main, self.a.min_age_hours, self.cwds, self.a.include_caches, self.watch):
            self._worktree(repo, name, main, *row)
        self.took.append(f"{name} {t_repo.elapsed():.0f} s")
        if not self.a.no_remote:
            self._measure_branches(repo, name)

    def _fetched_main(self, repo: str, name: str) -> str | None:
        """origin/main after the fetch (unless --no-fetch); None, told, when it failed: the repo is skipped."""
        try:
            if not self.a.no_fetch:
                f = bounded_fetch(repo, refs=("main",))
                if f is not True:
                    if str(f).startswith("timed out"):  # A STALLED REMOTE STALLS THEM ALL: one wait, not one per repo
                        self.net_down = f"{name}'s fetch {f}"
                    raise RuntimeError(f"fetch: {f}")
            return git(repo, "rev-parse", f"refs/remotes/{remote()}/main").strip()
        except RuntimeError as e:
            err(f"{name}: {e}; skipping repo")
            self.rc = 1
            return None

    def _worktree(self, repo: str, name: str, main: str, action: str, path: str, branch: Any, sha: str,
                  why: str) -> None:
        label = f"worktree {path} [{branch or 'detached'}] {sha[:10]}"
        if self._deadline_cut(action, path) or self._kept(action, label, why):
            return
        print(f"  {'would delete' if self.dry else 'delete'} {label}: {why}")
        if not self.dry and self._remove_worktree(repo, name, main, path, sha, why) and branch:
            self._delete_branch(repo, name, branch, sha, why)

    def _deadline_cut(self, action: str, path: str) -> bool:
        """The deadline stops the pass (or leaves no time to remove one): the path is left for the next one."""
        cut = action == "stop" or (action == "delete" and not self.dry and cleanup_left(self.watch) < self.reserve)
        if cut:
            self.stopped.append(path)  # never a removal begun that the deadline could cut in half
        return cut

    def _kept(self, action: str, label: str, why: str) -> bool:
        if action == "skip" and self.a.verbose:
            print(f"  keep   {label}: {why}")
        return action == "skip"

    def _remove_worktree(self, repo: str, name: str, main: str, path: str, sha: str, why: str) -> bool:
        """Re-verified right before acting; `git worktree remove` (no --force) refuses dirty trees itself. A run over
        many repos takes minutes (2026-11-22: 30, killed by upkeep's bound), so the plan's lock and cwd reads are
        taken again here: a tree a process entered or locked since planning is kept. True when it was removed."""
        if git(repo, "-C", path, "rev-parse", "HEAD").strip() != sha or not merged_reason(repo, sha, main):
            print("    changed since planning; kept")
            return False
        busy_now = still_in_use(repo, path)
        if busy_now:
            print(f"    {busy_now} since planning; kept")
            return False
        r = git(repo, "worktree", "remove", path, check=False)
        if r.returncode != 0:
            print(f"    worktree remove refused: {r.stderr.strip()}")
            self.rc = 1
            return False
        log_deletion("worktree", name, path, sha, why)
        return True

    def _delete_branch(self, repo: str, name: str, branch: str, sha: str, why: str) -> None:
        """The removed worktree's branch deleted, unless protected or checked out in another worktree."""
        if branch in PROTECTED_BRANCHES:
            return
        if f"refs/heads/{branch}" in {w.get("branch") for w in worktrees(repo)}:
            print(f"    branch {branch} checked out elsewhere; kept")
            return
        r = git(repo, "update-ref", "-d", f"refs/heads/{branch}", sha, check=False)
        if r.returncode == 0:
            log_deletion("branch", name, branch, sha, why)
            print(f"    deleted branch {branch} ({sha[:10]})")
        else:
            print(f"    branch {branch} not deleted: {r.stderr.strip()}")
            self.rc = 1

    def _measure_branches(self, repo: str, name: str) -> None:
        o_r = slug(repo)
        try:
            # listing: one measure branch's runs; deleted only when every listed run finished
            mplan = plan_measure(o_r, lambda b: gh_json(o_r, "run", "list", "--branch", b, "--limit", "20", "--json",
                                                        "databaseId,status"))
        except Exception as e:
            err(f"{name}: measure branches not checked: {e}")
            self.rc = 1
            return
        for action, bname, sha, why in mplan:
            label = f"remote {o_r}:{bname} {sha[:10]}"
            if self._kept(action, label, why):
                continue
            print(f"  {'would delete' if self.dry else 'delete'} {label}: {why}")
            if not self.dry:
                self._delete_remote_branch(o_r, bname, sha, why)

    def _delete_remote_branch(self, o_r: str, bname: str, sha: str, why: str) -> None:
        try:
            if forge_branch_sha(o_r, bname) != sha:
                print("    moved since planning; kept")
                return
            forge_delete_branch(o_r, bname)
            log_deletion("remote-branch", o_r, bname, sha, why)
        except Exception as e:
            print(f"    delete failed: {e}")
            self.rc = 1

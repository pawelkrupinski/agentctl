# agentctl, part `worktreelife`: a worktree's life: made fresh off main, the guard that puts it back on every exit, and retired when its fixer stops -- its uncommitted diff, untracked files and unmerged commits archived under the state dir, then the tree removed and its branch deleted (only after the archive is written)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import shutil
from typing import Any, NamedTuple, Optional
from agentctl.timefmt import ts
from agentctl.core import PROTECTED_BRANCHES, err, git, git_ok, remote
from agentctl.paths import state_root
from agentctl.deletions import log_deletion
from agentctl.waitingdir import waiting_state


class TreeState(NamedTuple):
    """What a linked worktree holds: its clone, HEAD, branch, `git status` text and whether it has commits past main."""
    root: str
    head: str
    branch: str
    status: str
    unmerged: bool


def read_tree(wt: str) -> Optional[TreeState]:
    """wt's state, None when it is not a linked checkout of a clone."""
    common = git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False).stdout.strip()
    base = f"refs/remotes/{remote()}/main"
    return TreeState(
        os.path.dirname(common), git(wt, "rev-parse", "HEAD", check=False).stdout.strip(),
        git(wt, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip(),
        git(wt, "status", "--porcelain", "--untracked-files=all", check=False).stdout,
        bool(git_ok(wt, "rev-parse", "--verify", "-q", base) and git(wt, "rev-list", "-1", f"{base}..HEAD", check=False).stdout.strip(),
             )) if common.endswith("/.git") else None


def archive_name(wt: str, stamp: str) -> str:
    return f"{os.path.basename(wt)}-{stamp.replace(':', '')}"


def write_archive(wt: str, dest: str, state: TreeState, why: str) -> None:
    """wt's state under dest: status.txt, uncommitted.diff, commits.patch (the commits past main), untracked/ files."""
    os.makedirs(os.path.join(dest, "untracked"))
    base = f"refs/remotes/{remote()}/main"
    for name, text in (("status.txt", f"{why}\n{state.status}"),
                       ("uncommitted.diff", git(wt, "diff", "HEAD", "--binary", check=False).stdout),
                       ("commits.patch", git(wt, "diff", "--binary", f"{base}...HEAD", check=False).stdout)):
        with open(os.path.join(dest, name), "w") as f:
            f.write(text)
    for name in git(wt, "ls-files", "--others", "--exclude-standard", check=False).stdout.splitlines():
        os.makedirs(os.path.dirname(os.path.join(dest, "untracked", name)), exist_ok=True)
        shutil.copy2(os.path.join(wt, name), os.path.join(dest, "untracked", name))


def remove_tree(wt: str, state: TreeState, why: str) -> Optional[str]:
    """Remove wt and delete its branch (the deletions logged); git's refusal, else None."""
    r = git(state.root, "worktree", "remove", "--force", wt, check=False)
    if r.returncode:
        return str(r.stderr).strip()[:160]
    log_deletion("worktree", os.path.basename(state.root), wt, state.head, f"retired: {why}")
    if state.branch not in PROTECTED_BRANCHES and git(state.root, "update-ref", "-d", f"refs/heads/{state.branch}", state.head, check=False).returncode == 0:
        log_deletion("branch", os.path.basename(state.root), state.branch, state.head, f"retired: {why}")
    return None


def retire_worktree(wt: str, why: str) -> str:
    """A stopped fixer's worktree: archived under <state>/fixer-archive when it holds uncommitted work or unmerged commits,
    then removed with its branch. One line saying what; a tree that cannot be archived or removed is kept."""
    state = read_tree(wt) if os.path.isdir(wt) else None
    if state is None:
        return f"worktree {wt} not retired: not a linked checkout (or already gone)"
    dest = os.path.join(state_root("fixer-archive"), archive_name(wt, ts()))
    archived = ""
    if state.status.strip() or state.unmerged:
        try:
            write_archive(wt, dest, state, why)
        except OSError as e:
            return f"worktree {wt} kept: archive failed ({e})"
        archived = f"; archived to {dest}"
    refused = remove_tree(wt, state, why)
    return f"worktree {wt} kept: git worktree remove refused: {refused}{archived}" if refused else f"worktree {wt} removed{archived}"


def fresh_worktree(repo: str, kind: str, num: Any) -> tuple[str | None, str | None]:
    """A fresh <clone>-wt-<kind>-<run>[-n] off <remote>/main (its branch named the same): (path, None) or (None, why).
    A blame probe's (kind blame/...) and a red fixer's (kind red) worktree alike."""
    _d, clone, _w = waiting_state(repo)
    wt, branch, n = f"{clone}-wt-{kind}-{num}", f"{kind}-{num}", 1
    while os.path.exists(wt) or git_ok(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"):
        n += 1
        wt, branch = f"{clone}-wt-{kind}-{num}-{n}", f"{kind}-{num}-{n}"
    git(repo, "worktree", "prune", check=False)
    git(repo, "fetch", "--quiet", remote(), "main", check=False)
    r = git(repo, "worktree", "add", "-q", "-b", branch, wt, f"{remote()}/main", check=False)
    if r.returncode != 0:
        return None, f"git worktree add failed ({r.stderr.strip()[:160]})"
    return wt, None


# EVERY EXIT PUTS THE WORKTREE BACK (2026-11-20, the owner: "fix agentctl issues structurally"; 11 closed issues were a
# path that left state behind: a NOT LANDED with its rebase in progress, a stash never restored, a hook left running, a
# lease held 20 min after its land was gone). Two guards, one for each way out:
# - exit_guard(): SIGTERM, SIGHUP and SIGINT raise Interrupted inside the block (a BaseException), so every `finally`
#   and context exit above it runs -- the stash restored, the landing lease released, a child group ended
#   (run_watched) -- where Python's default would have died mid-step with none of them;
# - WorktreeGuard(repo): what a command started in a worktree and left unfinished -- a rebase, cherry-pick, revert or
#   merge in progress -- is aborted on every exit (an error, a signal, a timeout, a return), and the branch it was on is
#   checked out again if it left HEAD detached; an operation already in progress when it began is the agent's, never
#   touched. Used by land (its rebase, its stash, its lease), the blame and fallback reverts and the check tree's picks.
GIT_IN_PROGRESS = (("rebase-merge", "rebase"), ("rebase-apply", "rebase"), ("CHERRY_PICK_HEAD", "cherry-pick"),
                   ("REVERT_HEAD", "revert"), ("MERGE_HEAD", "merge"))


def git_in_progress(repo):
    """The git operations in progress in worktree repo: {name} of rebase / cherry-pick / revert / merge."""
    args = [x for marker, _n in GIT_IN_PROGRESS for x in ("--git-path", marker)]
    paths = git(repo, "rev-parse", "--path-format=absolute", *args, check=False).stdout.splitlines()
    return {name for (_m, name), p in zip(GIT_IN_PROGRESS, paths) if p.strip() and os.path.exists(p.strip())}


class WorktreeGuard:
    """`with WorktreeGuard(repo):` -- on every exit, the operations started inside and left in progress are aborted and
    a branch left for a detached HEAD is checked out again (EVERY EXIT PUTS THE WORKTREE BACK, above)."""

    def __init__(self, repo):
        self.repo = repo

    def __enter__(self):
        self.before = git_in_progress(self.repo)
        self.branch = git(self.repo, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip() or None
        return self

    def __exit__(self, *exc):
        try:
            for op in sorted(git_in_progress(self.repo) - self.before):
                r = git(self.repo, op, "--abort", check=False)
                if r.returncode and op == "rebase":  # a rebase-apply left by `git am`
                    git(self.repo, "am", "--abort", check=False)
                err(f"agentctl: aborted the {op} it left in progress in {self.repo}")
            now = git(self.repo, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip() or None
            if self.branch and now is None and not git_in_progress(self.repo):
                git(self.repo, "checkout", "-q", self.branch, check=False)
        except Exception as e:  # a guard never hides the exit it guards
            err(f"agentctl: could not put {self.repo} back ({str(e)[:160]})")
        return False

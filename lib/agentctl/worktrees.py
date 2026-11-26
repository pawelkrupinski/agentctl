# agentctl, part `worktrees`: agent worktrees: whose one is (claimant, worktree_of), the trees agentctl made for itself, a run's fresh worktree, the clone's shared exclude file.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any, Optional
from agentctl.core import HOME, git, git_ok, remote, state_root
from agentctl.landstate import landed_state
from agentctl.timefmt import ts
from agentctl.statefile import write_json, note_failure
from agentctl.procs import safe_cwd
from agentctl.waitingdir import waiting_state
from agentctl.fixerrecords import agent_name
from agentctl.allowedpaths import allowed_path
from agentctl.core import err
from agentctl.storeknobs import knob_internal_trees_dir


def internal_trees_dir(*parts: str) -> str:
    d = knob_internal_trees_dir() or state_root("internal-trees")
    return os.path.join(d, *parts)


def mark_internal(path: str) -> Any:
    """Tag a worktree agentctl made for itself (check-N, baseline, recheck, waiting, batch, a finished red fixer's):
    a registry entry, so owner and fixer detection exclude it by the tag, not by what the tree is called."""
    if not path:
        return path
    try:
        import hashlib
        p = os.path.realpath(path)
        os.makedirs(internal_trees_dir(), exist_ok=True)
        write_json(internal_trees_dir(hashlib.sha1(p.encode()).hexdigest() + ".json"), {"path": p, "at": ts()})
    except OSError as e:
        note_failure("worktree registry not written", e)
    return path


def claimant(cwd: str | None = None) -> str:
    """The agent name (agent_name: a dispatched fixer's, by its session's record), else the Claude session, else the
    worktree."""
    name = agent_name()
    if name:
        return name
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return f"session {os.environ['CLAUDE_CODE_SESSION_ID'][:8]}"
    return os.path.basename(worktree_of(cwd or safe_cwd()) or safe_cwd())


def worktree_of(path: str | None) -> str | None:
    if not path or not os.path.isdir(path):
        return None
    r = git(path, "rev-parse", "--show-toplevel", check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def worktree_exclude(wt: str, names: Any) -> None:
    """Keep agentctl's notes in worktree wt out of every commit and `git status`: each name in the clone's shared
    info/exclude (once). Raises OSError."""
    common = git(wt, "rev-parse", "--git-common-dir", check=False).stdout.strip()
    excl = os.path.join(wt if not os.path.isabs(common) else "", common, "info", "exclude")
    os.makedirs(os.path.dirname(excl), exist_ok=True)
    have = open(excl).read() if os.path.exists(excl) else ""
    add = [p for p in names if p not in have.split()]
    if add:
        with open(excl, "a") as f:
            f.write("\n".join(add) + "\n")


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


def worktrees(repo: str) -> list[dict[str, Any]]:
    wts: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}
    for line in git(repo, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if cur:
                wts.append(cur)
            cur = {}
            continue
        k, _, v = line.partition(" ")
        cur[k] = v if v else True
    return [w for w in wts if not isinstance(w.get("worktree"), str) or allowed_path(w["worktree"])]


def gitdir_activity(repo: str, wt_path: str) -> Optional[float]:
    gd = git(repo, "-C", wt_path, "rev-parse", "--absolute-git-dir", check=False)
    if gd.returncode != 0:
        return None
    d = gd.stdout.strip()
    mt = [os.stat(os.path.join(d, f)).st_mtime for f in ("HEAD", "index", "logs/HEAD", "ORIG_HEAD") if os.path.exists(os.path.join(d, f))]
    return max(mt) if mt else None


def merged_reason(repo: str, head: str, main: str) -> Optional[str]:
    state = landed_state(repo, main, head)
    if state.how == "ancestor":
        return f"ancestor of {remote()}/main"
    if state.how == "patch-equivalent":
        return f"all {len(state.commits)} commit(s) patch-equivalent on {remote()}/main (by patch-id or changed lines)"
    return None  # not landed, or unknown (git cannot compare): no proof of a merge


CACHE_TREES = os.path.join(HOME, ".cache") + "/"  # tool-owned trees (e.g. kinowo-identity measure-local): reused, not litter


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

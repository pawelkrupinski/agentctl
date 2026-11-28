# agentctl, part `syncworktrees`: the sync's checkouts follow main: the root checkout fast-forwarded, the landed change's worktree closed
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
from agentctl.core import git, remote
from agentctl.gitrepo import is_ancestor
from agentctl.cleanup import close_worktree
from agentctl.syncsteps import any_exists, is_dir


# THE ROOT CHECKOUT FOLLOWS MAIN (issue 20261119T140508-66d831: ~/projects/movies sat at fb326c38b while main had
# 995f01ed's hook fix, so the main session's .claude hooks kept raising a false 'WAITING ON YOU'). Nobody works in the
# root checkout (movies CLAUDE.md), so the sync fast-forwards it to <remote>/main each cycle -- only on `main`, clean
# (tracked files), no rebase, merge or cherry-pick in progress, never anything but a fast-forward.
def _root_checkout_ff_safe(repo: Any) -> Any:
    """repo is a root checkout (no linked worktree: some agent's) on main, clean, with no rebase, merge or pick going."""
    common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False).stdout.strip()
    gitdir = git(repo, "rev-parse", "--path-format=absolute", "--git-dir", check=False).stdout.strip()
    if not common or os.path.realpath(common) != os.path.realpath(gitdir):
        return False
    if git(repo, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip() != "main":
        return False
    if any_exists([os.path.join(gitdir, x) for x in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD",
                                                      "REVERT_HEAD")]):
        return False
    return not git(repo, "status", "--porcelain", "--untracked-files=no", check=False).stdout.strip()


def root_checkout_ff(repo: str) -> list[str]:
    """Fast-forward clone repo's own checkout (its root, not a worktree) to <remote>/main when that is safe; its lines."""
    if not repo or not is_dir(repo) or not _root_checkout_ff_safe(repo):
        return []
    ref = f"refs/remotes/{remote()}/main"
    head = git(repo, "rev-parse", "HEAD", check=False).stdout.strip()
    main = git(repo, "rev-parse", "-q", "--verify", ref, check=False).stdout.strip()
    if not main or main == head or not is_ancestor(repo, head, main):
        return []
    r = git(repo, "merge", "--ff-only", "-q", ref, check=False)
    if r.returncode:
        return [f"root checkout {repo}: could not fast-forward to {main[:10]}: {(r.stderr or r.stdout).strip()[:160]}"]
    return [f"root checkout {repo}: fast-forwarded {head[:10]} -> {main[:10]}"]


def close_landed_worktrees(st: Any) -> list[str]:
    """The sync's half of WATCH OK while the reconciler runs on ci-runner: the worktree a change was landed from is on
    this laptop, so each `watch.ok` event's `close` worktree is closed here (close_worktree: only when proven merged and
    clean). Its own cursor (meta closer_seq). Returns what it said."""
    out = []
    for ev in st.events(after=int(st.meta_get("closer_seq", 0) or 0)):
        wt = (ev.get("refs") or {}).get("close")
        if ev["kind"] == "watch.ok" and wt and is_dir(wt):
            out.append(close_worktree(wt))  # raising: the cursor stays before it, tried again next cycle
        st.meta_set("closer_seq", ev["seq"])
    return out

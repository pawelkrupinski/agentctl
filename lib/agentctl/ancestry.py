# agentctl, part `ancestry`: commit ancestry: whether a commit contains another, here or after fetching main
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from agentctl.clock import epoch_now
from agentctl.core import git, remote, commit_known, is_ancestor


def contains_local(repo: str, sha: str, head: str | None) -> bool | None:
    """True / False when both commits are here, else None."""
    if not head:
        return None
    if head == sha:
        return True
    if not commit_known(repo, head):
        return None
    return is_ancestor(repo, sha, head)


def last_fetch(repo: str) -> float:
    """When the clone last fetched (its FETCH_HEAD's mtime), 0 when never or unreadable."""
    path = git(repo, "rev-parse", "--git-path", "FETCH_HEAD", check=False).stdout.strip()
    try:
        return os.path.getmtime(os.path.join(repo, path)) if path else 0.0
    except OSError:  # never fetched: fetch now
        return 0.0


def contains(repo: str, sha: str, head: str | None, refetch_after: float) -> bool:
    """Whether a run's head commit contains sha, fetching main when head is unknown here -- at most once per head per
    refetch_after seconds (review 2026-11-26: a head that never arrives, force-pushed away, fetched main on every
    land-watch tick). The clone's own FETCH_HEAD says when it last fetched: no process state to keep."""
    if not head:
        return False
    if head == sha:
        return True
    if not commit_known(repo, head) and epoch_now() - last_fetch(repo) >= refetch_after:
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
    return is_ancestor(repo, sha, head)

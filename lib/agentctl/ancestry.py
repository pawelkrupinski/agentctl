# agentctl, part `ancestry`: commit ancestry: whether a commit contains another, here or after fetching main
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.core import git, git_ok, remote


def contains_local(repo: str, sha: str, head: str | None) -> bool | None:
    """True / False when both commits are here, else None."""
    if not head:
        return None
    if head == sha:
        return True
    if not git_ok(repo, "cat-file", "-e", f"{head}^{{commit}}"):
        return None
    return git_ok(repo, "merge-base", "--is-ancestor", sha, head)


def contains(repo: str, sha: str, head: str | None) -> bool:
    """Whether a run's head commit contains sha (fetching main once when head is unknown here)."""
    if not head:
        return False
    if head == sha:
        return True
    if not git_ok(repo, "cat-file", "-e", f"{head}^{{commit}}"):
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
    return git_ok(repo, "merge-base", "--is-ancestor", sha, head)

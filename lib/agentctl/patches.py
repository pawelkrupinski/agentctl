# agentctl, part `patches`: a commit's patch identity: its patch-id, whether it is empty (or deliberately so), whether an equivalent patch is upstream already
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Optional
from agentctl.core import content_twin, git, run_cmd


# NEVER AN EMPTY COMMIT (2026-10-27). An agent queued a commit, queued it again, and both entries ran: the later one's
# commit had already landed through the earlier one, its rebase (a cherry-pick with --keep-redundant-commits) left an
# EMPTY commit, and that was pushed -- four times on movies main (317fba9b3, 1fddd8951, 1d554f051, 693e1be78/82f4d9f55).
# Now every rebuild of an entry drops a commit that is patch-equivalent to its new base, or empty after the pick; an
# entry left with nothing is LANDED (already on main via <commit>); re-queueing supersedes the older entry; and the
# squash guard refuses any push carrying an empty commit unless it is a deliberate one with an `Allow-Empty:` trailer.
ALLOW_EMPTY_TRAILER = "Allow-Empty"


def deliberate_empty(repo: str, c: str) -> bool:
    """A commit made empty on purpose: `git commit --allow-empty` with an `Allow-Empty: <why>` trailer."""
    return bool(git(repo, "log", "-1", f"--format=%(trailers:key={ALLOW_EMPTY_TRAILER},valueonly)", c,
                    check=False).stdout.strip())


def is_empty_commit(repo, c):
    """A non-merge commit whose tree is its parent's (a root commit is never empty here)."""
    parents = git(repo, "rev-list", "--parents", "-n", "1", c).split()[1:]
    if len(parents) != 1:
        return False
    return git(repo, "rev-parse", f"{c}^{{tree}}").strip() == git(repo, "rev-parse", f"{parents[0]}^{{tree}}").strip()


def patch_id(repo: str, c: str) -> Optional[str]:
    diff = git(repo, "show", "--format=", "--no-color", "--no-ext-diff", c, check=False).stdout
    if not diff.strip():
        return None
    r = run_cmd(["git", "patch-id", "--stable"], cwd=repo, input=diff)
    return (r.stdout.split() or [None])[0]


def equivalent_on(repo: str, c: str, upstream: str, limit: int = 400) -> Optional[str]:
    """The commit of `upstream` (since its merge-base with c) that carries c's patch, or None (`git cherry`'s test,
    naming the commit), else its content twin (content_twin: same subject, author, date and changed lines)."""
    pid = patch_id(repo, c)
    if not pid:
        return None
    mb = git(repo, "merge-base", c, upstream, check=False).stdout.strip()
    rng = f"{mb}..{upstream}" if mb else upstream
    for u in git(repo, "rev-list", "--no-merges", "-n", str(limit), rng, check=False).stdout.split():
        if patch_id(repo, u) == pid:
            return u
    return content_twin(repo, c, upstream)


def upstream_patch_ids(repo: str, since_commit: str, upstream: str, limit: int = 400) -> dict[str, str]:
    """{patch-id: commit} of `upstream`'s non-merge commits since its merge-base with `since_commit`, in one pass."""
    mb = git(repo, "merge-base", since_commit, upstream, check=False).stdout.strip()
    rng = f"{mb}..{upstream}" if mb else upstream
    log = git(repo, "log", "-p", "--no-merges", "--no-color", "--no-ext-diff", "-n", str(limit), "--format=commit %H",
              rng, check=False).stdout
    if not log.strip():
        return {}
    r = run_cmd(["git", "patch-id", "--stable"], cwd=repo, input=log)
    return {pid: c for pid, c in (l.split() for l in r.stdout.splitlines() if len(l.split()) == 2)}

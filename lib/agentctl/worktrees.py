# agentctl, part `worktrees`: agent worktrees: whose one is (claimant, worktree_of), the trees agentctl made for itself, a run's fresh worktree, the clone's shared exclude file.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
from agentctl.core import git, git_ok, remote, state_root
from agentctl.timefmt import ts
from agentctl.statefile import write_json
from agentctl.procs import safe_cwd
from agentctl.waitingdir import waiting_state
from agentctl.fixerrecords import agent_name


def internal_trees_dir(*parts: str) -> str:
    d = os.environ.get("AGENTCTL_INTERNAL_TREES_DIR") or state_root("internal-trees")
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
    except OSError:
        pass
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

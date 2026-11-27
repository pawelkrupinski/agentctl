# agentctl, part `worktrees`: agent worktrees: whose one is (claimant, worktree_of), the trees agentctl made for itself, the clone's shared exclude file.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any, Optional
from agentctl.statefile import note_failure, write_json
from agentctl.timefmt import ts
from agentctl.storeknobs import knob_internal_trees_dir
from agentctl.core import HOME, git, remote
from agentctl.allowedpaths import allowed_path
from agentctl.procs import safe_cwd
from agentctl.paths import claude_session_id, state_root
from agentctl.fixerrecords import agent_name
from agentctl.onmain import landed_state


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
    sid = claude_session_id()
    if sid:
        return f"session {sid[:8]}"
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



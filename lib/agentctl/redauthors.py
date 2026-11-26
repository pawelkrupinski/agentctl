# agentctl, part `redauthors`: a red's authors: the commits it holds, who landed them, whether their sessions are still at work
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import wall_epoch
import os
import re
from typing import Any
from agentctl.core import attempt, git, git_ok
from agentctl.waitingdir import waiting_entries, waiting_state
from agentctl.allowedpaths import allowed_path
from agentctl.procs import lsof_cwds, proc_alive, ps
from agentctl.inboxentry import inbox_entries
from agentctl.worktrees import gitdir_activity
from agentctl.dispatches import worktree_holding
from agentctl.jvmreap import AGENT_WORK
from agentctl.blameknobs import knob_author_active_min


def _green_older_than(runs: Any, rid: Any) -> Any:
    """The first completed green run of runs, with a head commit, whose id is below rid, or None."""
    return next((r for r in runs if r.get("status") == "completed" and r.get("conclusion") == "success"
                 and (r.get("databaseId") or 0) < rid and r.get("headSha")), None)


def _green_before(repo: Any, u: Any) -> Any:
    """The head sha of the first completed green run of u's workflow older than u's run, when the clone has its commit."""
    green = _green_older_than(u.get("runs") or [], u["run"].get("databaseId") or 0)
    return green["headSha"] if green and git_ok(repo, "cat-file", "-e", f"{green['headSha']}^{{commit}}") else None


def red_commits(repo, u, limit=30):
    """The commits of the red run since the workflow's last green run on main (newest first); just its head when the
    range cannot be read here."""
    red = u["run"].get("headSha")
    if not repo or not red or not git_ok(repo, "cat-file", "-e", f"{red}^{{commit}}"):
        return [red] if red else []
    green = _green_before(repo, u)
    if green:
        r = git(repo, "rev-list", f"--max-count={limit}", red, f"^{green}", check=False)
    else:
        r = git(repo, "rev-list", "--max-count=1", red, check=False)
    return [s for s in r.stdout.split() if s] if r.returncode == 0 else [red]


def landed_heads(repo):
    """{pushed head sha: {worktree, session}}: the changes that landed, as their queue entries recorded them at
    submission (audit #15: the author comes from the change item, never from an inherited environment)."""
    out = {}
    try:
        d, _c, _w = waiting_state(repo)
        for e in waiting_entries(d, open_only=False):
            if e.get("landed_sha"):
                out[e["landed_sha"]] = {"worktree": e.get("worktree"), "session": e.get("session")}
    except (Exception, SystemExit):
        pass
    return out


def _agent_pids() -> Any:
    """The pids of agent processes (claude, agentctl land/watch/wait, the hook) other than this one."""
    return [pid for pid, _pp, _e, cmd in ps() if pid != os.getpid()
            and (AGENT_WORK.search(cmd or "") or re.search(r"agentctl (watch|wait)\b", cmd or ""))]


def agent_pids_in(wts):
    """{worktree: [pid]} of the agent processes (claude, agentctl land/watch, the hook) whose cwd is in one of wts."""
    want = [os.path.realpath(w) for w in wts if w]
    if not want:
        return {}
    pids = _agent_pids()
    out = {}
    for pid, cwd in lsof_cwds(pids).items() if pids else ():
        c = os.path.realpath(cwd)
        for w in want:
            if c == w or c.startswith(w + "/"):
                out.setdefault(w, []).append(pid)
    return out


def worktree_activity(repo, wt, entries=()):
    """The worktree's last sign of work: git (index, HEAD log, last commit) or an inbox entry from it."""
    beats = [gitdir_activity(repo, wt) or 0.0]
    r = git(wt, "log", "-1", "--format=%ct", check=False)
    if r.returncode == 0 and r.stdout.strip().isdigit():
        beats.append(float(r.stdout.strip()))
    beats += [float(e.get("at") or 0) for e in entries if os.path.realpath(e.get("cwd") or "/") == os.path.realpath(wt)]
    return max(beats)


def _landed_head(heads: Any, c: Any) -> Any:
    """The landed head (by exact or abbreviated sha) of commit c, or None."""
    return heads.get(c) or next((v for k, v in heads.items() if c.startswith(k) or k.startswith(c)), None)


def _commit_owners(repo: Any, commits: Any) -> Any:
    """{commit: the landed head (or the holding worktree) that owns it}: a change's commits (oldest first) end at its
    landed head; the ones not landed belong to the worktree holding them."""
    heads = landed_heads(repo)
    owner: dict[Any, Any] = {}
    pending: list[Any] = []
    for c in reversed(commits):  # oldest first: a change's commits end at its landed head
        pending.append(c)
        h = _landed_head(heads, c)
        if h:
            for p in pending:
                owner[p] = h
            pending = []
    for c in pending:
        wt = worktree_holding(repo, c)
        if wt:
            owner[c] = {"worktree": wt}
    return owner


def _by_worktree(commits: Any, owner: Any) -> Any:
    """{real worktree path: {worktree, shas (newest first), pids, session}} of the commits whose owner's worktree is an
    allowed, existing directory."""
    by_wt: dict[Any, Any] = {}
    for c in commits:  # newest first
        h = owner.get(c)
        if not h or not h.get("worktree") or not allowed_path(h["worktree"]) or not os.path.isdir(h["worktree"]):
            continue
        a = by_wt.setdefault(os.path.realpath(h["worktree"]), {"worktree": h["worktree"], "shas": [], "pids": set(),
                                                               "session": h.get("session")})
        a["shas"].append(c)
    return by_wt


def _author_liveness(repo: Any, by_wt: Any, t: Any) -> Any:
    """by_wt's authors with their pids, activity and liveness (an agent process alive and active lately) filled in."""
    entries = attempt(inbox_entries, [], catch=(OSError,))
    running = agent_pids_in(list(by_wt))
    active = knob_author_active_min() * 60
    out: list[Any] = []
    for k, a in by_wt.items():
        a["pids"] = sorted(set(a["pids"]) | set(running.get(k, [])))
        a["activity"] = worktree_activity(repo, a["worktree"], entries)
        alive = [p for p in a["pids"] if proc_alive(p)]
        a["live_pid"] = alive[0] if alive else None
        a["live"] = bool(alive) and t - a["activity"] <= active
        out.append(a)
    return out


def red_authors(repo, u, now_=None):
    """[{worktree, session, shas, pids, live, activity}] for the commits of the red run, the newest commit's author
    first. Live: an agent process works in that worktree now and it was active lately."""
    t = now_ if now_ is not None else wall_epoch()  # against git times and mtimes
    commits = red_commits(repo, u)
    if not repo or not commits:
        return []
    return _author_liveness(repo, _by_worktree(commits, _commit_owners(repo, commits)), t)

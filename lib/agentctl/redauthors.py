# agentctl, part `redauthors`: a red's authors: the commits it holds, who landed them, whether their sessions are still at work
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import time
from agentctl.core import env_num, git, git_ok
from agentctl.waitingdir import waiting_entries, waiting_state
from agentctl.allowedpaths import allowed_path
from agentctl.procs import lsof_cwds, proc_alive, ps
from agentctl.inboxentry import inbox_entries
from agentctl.worktrees import gitdir_activity
from agentctl.dispatches import worktree_holding
from agentctl.jvmreap import AGENT_WORK


AUTHOR_ACTIVE_MIN = 15   # live = a pid alive and activity within this ($AGENTCTL_AUTHOR_ACTIVE_MIN)


def red_commits(repo, u, limit=30):
    """The commits of the red run since the workflow's last green run on main (newest first); just its head when the
    range cannot be read here."""
    red = u["run"].get("headSha")
    if not repo or not red or not git_ok(repo, "cat-file", "-e", f"{red}^{{commit}}"):
        return [red] if red else []
    rid = u["run"].get("databaseId") or 0
    green = next((r for r in u.get("runs") or [] if r.get("status") == "completed" and r.get("conclusion") == "success"
                  and (r.get("databaseId") or 0) < rid and r.get("headSha")), None)
    if green and git_ok(repo, "cat-file", "-e", f"{green['headSha']}^{{commit}}"):
        r = git(repo, "rev-list", f"--max-count={limit}", red, f"^{green['headSha']}", check=False)
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


def agent_pids_in(wts):
    """{worktree: [pid]} of the agent processes (claude, agentctl land/watch, the hook) whose cwd is in one of wts."""
    want = [os.path.realpath(w) for w in wts if w]
    if not want:
        return {}
    pids = [pid for pid, _pp, _e, cmd in ps() if pid != os.getpid()
            and (AGENT_WORK.search(cmd or "") or re.search(r"agentctl (watch|wait)\b", cmd or ""))]
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


def red_authors(repo, u, now_=None):
    """[{worktree, session, shas, pids, live, activity}] for the commits of the red run, the newest commit's author
    first. Live: an agent process works in that worktree now and it was active lately."""
    t = now_ if now_ is not None else time.time()
    commits = red_commits(repo, u)
    if not repo or not commits:
        return []
    heads = landed_heads(repo)
    owner, pending = {}, []
    for c in reversed(commits):  # oldest first: a change's commits end at its landed head
        pending.append(c)
        h = heads.get(c) or next((v for k, v in heads.items() if c.startswith(k) or k.startswith(c)), None)
        if h:
            for p in pending:
                owner[p] = h
            pending = []
    for c in pending:
        wt = worktree_holding(repo, c)
        if wt:
            owner[c] = {"worktree": wt}
    by_wt = {}
    for c in commits:  # newest first
        h = owner.get(c)
        if not h or not h.get("worktree") or not allowed_path(h["worktree"]) or not os.path.isdir(h["worktree"]):
            continue
        a = by_wt.setdefault(os.path.realpath(h["worktree"]), {"worktree": h["worktree"], "shas": [], "pids": set(),
                                                               "session": h.get("session")})
        a["shas"].append(c)
    try:
        entries = inbox_entries()
    except OSError:
        entries = []
    running = agent_pids_in(list(by_wt))
    active = env_num("AGENTCTL_AUTHOR_ACTIVE_MIN", AUTHOR_ACTIVE_MIN) * 60
    out = []
    for k, a in by_wt.items():
        a["pids"] = sorted(set(a["pids"]) | set(running.get(k, [])))
        a["activity"] = worktree_activity(repo, a["worktree"], entries)
        alive = [p for p in a["pids"] if proc_alive(p)]
        a["live_pid"] = alive[0] if alive else None
        a["live"] = bool(alive) and t - a["activity"] <= active
        out.append(a)
    return out

# agentctl, part `cleanup`: cleanup and the lifecycle.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
import contextlib
import datetime as dt
import io
import os
import re
import signal
import subprocess
import time
from agentctl.procrunner import process_runner
from agentctl.core import (
    GIT_NET_SSH, HOME, PROTECTED_BRANCHES, cherry_landed, env_num, err, git, git_ok, remote, run_cmd, slug,
    state_root)
from agentctl.timefmt import now, ts
from agentctl.forge import forge_branches
from agentctl.procs import PROC_ROOT, process_cwds, ps
from agentctl.worktrees import CACHE_TREES, gitdir_activity, merged_reason, worktrees
from agentctl.maint import cmd_orphans
from agentctl.deletions import SWEEP_KEEP_REMOTE, delete_remote_branch, log_deletion
from agentctl.dispatches import orphan_branches
from agentctl.jvmreap import SBT_STALL_MIN


# A CLEANUP IS BOUNDED PER TREE AND AS A WHOLE (2026-11-22: the upkeep's `cleanup --execute --no-remote` ran past its
# 1800 s bound at 03:58Z and was killed in the middle of a `git worktree remove`). Each tree's own git reads get
# CLEANUP_ITEM_TIMEOUT -- a tree that cannot be decided in that time is kept, said, and looked at next pass -- the
# fetch is the unattended bounded_fetch (180 s, not git()'s 600 s), and the whole pass stops by itself at its
# --deadline: no tree is looked at past it, and no removal starts with less than CLEANUP_REMOVE_RESERVE left.
CLEANUP_ITEM_TIMEOUT = 120  # $AGENTCTL_CLEANUP_ITEM_TIMEOUT
CLEANUP_REMOVE_RESERVE = 300  # $AGENTCTL_CLEANUP_REMOVE_RESERVE


def cleanup_left(until):
    """Seconds left before the cleanup's deadline (monotonic `until`); inf without one."""
    return float("inf") if until is None else until - time.monotonic()


def plan_worktrees(repo, main, min_age_h, cwds, include_caches=False, until=None):
    """[(action, wt, branch, sha, reason)] where action is 'delete' or 'skip' -- 'stop' for every tree not looked at
    once the deadline (`until`, monotonic) passed."""
    plan = []
    wts = worktrees(repo)
    item = env_num("AGENTCTL_CLEANUP_ITEM_TIMEOUT", CLEANUP_ITEM_TIMEOUT)
    for w in wts[1:]:  # the first entry is the main checkout
        path, sha = w["worktree"], w.get("HEAD", "")
        branch = w.get("branch", "").removeprefix("refs/heads/") or None
        def skip(why):
            plan.append(("skip", path, branch, sha, why))
        if cleanup_left(until) <= 0:
            plan.append(("stop", path, branch, sha, "not looked at: the cleanup's deadline passed")); continue
        if "locked" in w:
            skip("locked"); continue
        if branch in PROTECTED_BRANCHES:
            skip(f"on {branch}"); continue
        if path.startswith(CACHE_TREES) and not include_caches:
            skip("tool cache tree (--include-caches to consider)"); continue
        if "prunable" in w or not os.path.isdir(path):
            skip("directory missing (prunable; not touched)"); continue
        busy = [p for p, c in cwds.items() if c == path or c.startswith(path + "/")]
        if busy:
            skip(f"in use by pid {busy[:3]}"); continue
        act = gitdir_activity(repo, path)
        if act and time.time() - act < min_age_h * 3600:
            skip(f"active {int((time.time() - act) / 60)} min ago"); continue
        t0 = time.monotonic()
        dirty = git(repo, "-C", path, "status", "--porcelain", "--untracked-files=all", check=False, timeout=item)
        if dirty.returncode == 124:
            skip(f"undecided: git status took over {item:g} s (looked at again next pass)"); continue
        if dirty.returncode != 0:
            skip("git status failed"); continue
        if dirty.stdout.strip():
            skip(f"dirty ({len(dirty.stdout.splitlines())} changed/untracked)"); continue
        if cleanup_left(until) <= 0:
            plan.append(("stop", path, branch, sha, "not looked at: the cleanup's deadline passed")); continue
        why = merged_reason(repo, sha, main)
        if not why:
            n = git(repo, "rev-list", "--count", f"{main}..{sha}", check=False, timeout=item).stdout.strip() or "?"
            skip(f"unmerged ({n} commit(s) not on {remote()}/main)"); continue
        if time.monotonic() - t0 > 30:
            err(f"cleanup: {path} took {time.monotonic() - t0:.0f} s to decide")
        plan.append(("delete", path, branch, sha, why))
    return plan


def still_in_use(repo, path):
    """Why worktree `path` must be kept after all, read now (not at plan time), or None."""
    w = next((w for w in worktrees(repo) if w["worktree"] == path), None)
    if w is None:
        return "gone"
    if "locked" in w:
        return "locked"
    busy = [p for p, c in process_cwds().items() if c == path or c.startswith(path + "/")]
    if busy:
        return f"in use by pid {busy[:3]}"
    return None  # (no gitdir-activity re-read: the plan's own `git status` refreshed the index's mtime)


def plan_measure(o_r, runs_for, now_=None):
    """Remote measure/* branches: [(action, name, sha, reason)]. runs_for(name) -> list of run dicts."""
    now_ = now_ or now()
    return [measure_decision(name, sha, when, runs_for(name), now_) for name, sha, when in forge_branches(o_r, "measure/")]


def measure_decision(name, sha, commit_time, runs, now_):
    if not name.startswith("measure/"):
        return ("skip", name, sha, "not a measure/ branch")
    active = [r for r in runs if r.get("status") != "completed"]
    if active:
        return ("skip", name, sha, f"run {active[0].get('databaseId')} {active[0].get('status')}")
    if runs:
        return ("delete", name, sha, f"{len(runs)} run(s) finished")
    age_h = (now_ - commit_time).total_seconds() / 3600
    if age_h > 24:
        return ("delete", name, sha, f"no runs, {age_h:.0f}h old")
    return ("skip", name, sha, f"no runs yet, {age_h:.1f}h old")


# ---------------------------------------------------------------- the lifecycle closes itself
# The owner (2026-10-26): after WATCH OK, land removes the worktree it ran from and its branch -- only when proven merged
# (git cherry shows nothing of its own left, the tree clean) and never the root checkout or a tree another process is
# in; and a daily `agentctl sweep` reports what is unmerged and idle, deleting only remote branches proven merged.
SWEEP_IDLE_DAYS = 3
SWEEP_DIR = None  # a test's override; else sweep_dir(): under the state root, resolved at each use


def sweep_dir():
    return SWEEP_DIR or state_root("sweep")
SWEEP_REPOS = [os.path.join(HOME, "projects", "movies")]


def own_ancestry():
    """This process and its parents (the agent's shell is in the worktree it lands from)."""
    parents = {p: pp for p, pp, _e, _c in ps()}
    chain, pid = set(), os.getpid()
    while pid and pid not in chain:
        chain.add(pid)
        pid = parents.get(pid)
    return chain


def close_worktree(wt):
    """Remove the worktree land ran from and its local branch, when proven merged. Returns one line saying what."""
    wt = os.path.realpath(wt)
    try:
        trees = worktrees(wt)
    except RuntimeError as e:
        return f"worktree {wt} kept: {e}"
    root = os.path.realpath(trees[0]["worktree"])
    if wt == root:
        return f"worktree {wt} kept: the root checkout is never closed"
    me = next((t for t in trees if os.path.realpath(t["worktree"]) == wt), None)
    if me is None:
        return f"worktree {wt} kept: not a worktree of {root}"
    if "locked" in me:
        return f"worktree {wt} kept: locked"
    branch = (me.get("branch") or "").removeprefix("refs/heads/") or None
    if branch in PROTECTED_BRANCHES:
        return f"worktree {wt} kept: on {branch}"
    mine = own_ancestry()
    busy = [p for p, c in process_cwds().items() if p not in mine and (c == wt or c.startswith(wt + "/"))]
    if busy:
        return f"worktree {wt} kept: in use by pid {busy[:3]}"
    dirty = git(wt, "status", "--porcelain", "--untracked-files=all", check=False)
    if dirty.returncode or dirty.stdout.strip():
        return f"worktree {wt} kept: the tree is not clean ({len(dirty.stdout.splitlines())} changed/untracked)"
    git(wt, "fetch", "--quiet", remote(), "main", check=False)
    main = f"refs/remotes/{remote()}/main"
    head = git(wt, "rev-parse", "HEAD").strip()
    _commits, ahead = cherry_landed(wt, main, head)
    if ahead is None:
        return f"worktree {wt} kept: git cherry could not compare it with {remote()}/main"
    if ahead:
        return f"worktree {wt} kept: {len(ahead)} commit(s) not on {remote()}/main"
    why = merged_reason(root, head, main) or "nothing of its own left"
    r = git(root, "worktree", "remove", wt, check=False)
    if r.returncode:
        return f"worktree {wt} kept: git worktree remove refused: {r.stderr.strip()[:200]}"
    log_deletion("worktree", os.path.basename(root), wt, head, f"closed after WATCH OK: {why}")
    msg = f"closed worktree {wt}"
    if branch and f"refs/heads/{branch}" not in {t.get("branch") for t in worktrees(root)}:
        if git(root, "update-ref", "-d", f"refs/heads/{branch}", head, check=False).returncode == 0:
            log_deletion("branch", os.path.basename(root), branch, head, f"closed after WATCH OK: {why}")
            msg += f" and branch {branch}"
    return msg + f" ({why})"


# FREEING A LANDED WORKTREE (2026-11-18: `land --until-done` LANDED 94a33a968 but printed no DONE -- the agent's own
# kept sbt server, `scripts/sbt-warm`, still worked in the worktree, and the agent removed it by hand). Before the
# removal, free_worktree stops what is the CALLER's in that worktree and nothing else: the checkout's own sbt server
# (`scripts/sbt-warm --stop`, which only ever stops that checkout's), then every process whose cwd is inside it and
# whose environment carries the caller's CLAUDE_CODE_SESSION_ID (SIGTERM, by pid -- never a pattern). It waits up to
# FREE_WAIT s for the tree to be free; whatever still holds it (another session's, or one without a session) is
# returned, never signalled.
FREE_WAIT = 30  # s --until-done waits for a landed worktree to be free of processes before it keeps it
NEVER_SIGNAL = re.compile(r"(^|/)(claude|login|tmux|ssh)(\s|$)")  # an agent session or a terminal: reported, never stopped


def worktree_holders(wt):
    """{pid: command} of the processes working inside wt, this process and its parents excepted."""
    wt = os.path.realpath(wt)
    mine = own_ancestry()
    cmds = {p: c for p, _pp, _e, c in ps()}
    return {p: cmds.get(p, "?") for p, c in process_cwds().items()
            if p not in mine and (c == wt or c.startswith(wt + "/"))}


def process_session(pid):
    """The CLAUDE_CODE_SESSION_ID in pid's environment, or None (none, or not readable)."""
    if os.path.isdir(os.path.join(PROC_ROOT, "self")):
        try:
            with open(os.path.join(PROC_ROOT, str(pid), "environ"), "rb") as f:
                env = f.read().decode(errors="replace").split("\0")
        except OSError:
            return None
        return next((e.split("=", 1)[1] for e in env if e.startswith("CLAUDE_CODE_SESSION_ID=")), None)
    m = re.search(r"(?:^|\s)CLAUDE_CODE_SESSION_ID=(\S+)",
                  run_cmd(["ps", "-E", "-ww", "-o", "command=", "-p", str(pid)]).stdout)
    return m.group(1) if m else None


def free_worktree(wt, out=print):
    """Stop the caller's processes in worktree wt (FREEING A LANDED WORKTREE above), then wait for it to be free.
    Returns {pid: command} of what still holds it (empty: free)."""
    held = worktree_holders(wt)
    if not held:
        return held
    warm = os.path.join(wt, "scripts", "sbt-warm")
    if os.access(warm, os.X_OK):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        r = run_cmd([warm, "--stop"], timeout=90, cwd=wt, env=env)
        out(f"scripts/sbt-warm --stop in {wt}: exit {r.returncode}", flush=True)
        held = worktree_holders(wt)
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    for pid, cmd in sorted(held.items()):
        if sid and not NEVER_SIGNAL.search(cmd) and process_session(pid) == sid:
            try:
                os.kill(pid, signal.SIGTERM)
                out(f"stopped pid {pid} of this session: {cmd[:120]}", flush=True)
            except OSError:
                pass
    deadline = time.time() + env_num("AGENTCTL_FREE_WAIT", FREE_WAIT)
    while held and time.time() < deadline:
        time.sleep(0.5)
        held = worktree_holders(wt)
    return held


FETCH_TIMEOUT = 180  # an unattended fetch's bound ($AGENTCTL_FETCH_TIMEOUT)


def bounded_fetch(repo, *extra, refs=()):
    """`git fetch <remote>` for an unattended run (maintain's sweep): bounded, ssh keepalives on, never prompting. True,
    or why not (2026-10-27: the remote's ssh accepted the connection and never answered, and the first maintain pass hung
    in the sweep's fetch). Every remote here is https, where the ssh keepalives do nothing: a transfer under 1 KB/s for
    30 s is given up (http.lowSpeed*), so a lossy uplink (2026-11-22 03:28Z, 40% loss) never waits out the whole bound."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_SSH_COMMAND=GIT_NET_SSH)
    try:
        r = process_runner().run(["git", "-C", repo, "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=30", "fetch", "--quiet", *extra, remote(), *refs], capture_output=True, text=True, env=env,
                           timeout=env_num("AGENTCTL_FETCH_TIMEOUT", FETCH_TIMEOUT), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"timed out after {int(env_num('AGENTCTL_FETCH_TIMEOUT', FETCH_TIMEOUT))} s"
    return True if r.returncode == 0 else (r.stderr.strip().splitlines() or ["exit %d" % r.returncode])[-1][:160]


def sweep_repo(repo, idle_days, dry, out, store):
    name = os.path.basename(repo)
    now_ = time.time()
    fetched = bounded_fetch(repo, "--prune")
    main = f"refs/remotes/{remote()}/main"
    if fetched is not True:
        out.append(f"== {name}: fetch failed ({fetched}): from the last fetched state")
    out.append(f"== {name} ({remote()}/main {git(repo, 'rev-parse', main).strip()[:10]})")
    idle_wt, checked_out = [], set()
    for w in worktrees(repo)[1:]:
        path, sha = w["worktree"], w.get("HEAD", "")
        branch = (w.get("branch") or "").removeprefix("refs/heads/")
        checked_out.add(branch)
        if not os.path.isdir(path) or path.startswith(CACHE_TREES) or merged_reason(repo, sha, main):
            continue
        tip = int(git(repo, "log", "-1", "--format=%ct", sha, check=False).stdout.strip() or 0)
        last = max(gitdir_activity(repo, path) or 0, tip)
        days = (now_ - last) / 86400
        if days > idle_days:
            n = len(git(repo, "rev-list", f"{main}..{sha}").split())
            idle_wt.append(f"  UNMERGED worktree {path} [{branch or 'detached'}]: {n} commit(s), idle {int(days)} d")
    out += idle_wt or [f"  no unmerged worktree idle more than {idle_days} d"]
    lonely = []
    for line in git(repo, "for-each-ref", "--format=%(refname:short) %(objectname) %(committerdate:unix)",
                    "refs/heads").splitlines():
        b, sha, t = line.split()
        if b in PROTECTED_BRANCHES or b in checked_out or merged_reason(repo, sha, main):
            continue
        days = (now_ - int(t)) / 86400
        if days > idle_days:
            lonely.append(f"  UNMERGED branch {b} (no worktree): tip {sha[:10]}, idle {int(days)} d")
    out += lonely
    o_r = None
    for line in git(repo, "ls-remote", "--heads", remote()).splitlines():
        sha, ref = line.split()
        b = ref.removeprefix("refs/heads/")
        if b in SWEEP_KEEP_REMOTE or b.startswith("measure/"):
            continue  # measure/* is `agentctl cleanup`'s, by its runs
        if not git_ok(repo, "cat-file", "-e", f"{sha}^{{commit}}"):
            git(repo, "fetch", "--quiet", remote(), b, check=False)
        why = git_ok(repo, "cat-file", "-e", f"{sha}^{{commit}}") and merged_reason(repo, sha, main)
        if not why:
            t = int(git(repo, "log", "-1", "--format=%ct", sha, check=False).stdout.strip() or now_)
            out.append(f"  unmerged remote branch {b}: tip {sha[:10]}, {int((now_ - t) / 86400)} d old (listed, kept)")
            continue
        if dry:
            out.append(f"  would delete remote branch {b} {sha[:10]}: {why}")
            continue
        try:
            o_r = o_r or slug(repo)
            delete_remote_branch(repo, o_r, b, sha)
            log_deletion("remote-branch", o_r, b, sha, f"sweep: {why}")
            out.append(f"  deleted remote branch {b} {sha[:10]}: {why}")
        except Exception as e:
            out.append(f"  remote branch {b} not deleted: {e}")
    out.append("== branches whose owner is gone (worktree removed or pid dead) and whose runs all finished")
    try:
        if not orphan_branches(repo, o_r or slug(repo), dry, out, store):
            out.append("  none")
    except (Exception, SystemExit) as e:
        out.append(f"  could not list them ({str(e)[:160]})")


def cmd_sweep(a, store):
    repos = [os.path.abspath(os.path.expanduser(r)) for r in (a.repo or SWEEP_REPOS)]
    out = [f"agentctl sweep {ts()}{' (DRY RUN)' if a.dry_run else ''}: unmerged and idle > {a.idle_days} d is listed, "
           "never deleted; remote branches proven merged into main are deleted (standing OK)"]
    rc = 0
    for repo in repos:
        try:
            sweep_repo(repo, a.idle_days, a.dry_run, out, store)
        except RuntimeError as e:
            out.append(f"== {os.path.basename(repo)}: {e}")
            rc = 1
    reaped = io.StringIO()
    if os.environ.get("AGENTCTL_SBT_REAP") != "0":  # the tests never reap this machine's servers
        with contextlib.redirect_stdout(reaped):
            cmd_orphans(argparse.Namespace(kill=not a.dry_run, sbt_only=True))
    out.append("== sbt servers (a gone worktree, an orphaned serving command, a running command with no CPU for "
               f"{SBT_STALL_MIN} min){'' if not a.dry_run else ': not reaped (DRY RUN)'}")
    out += ["  " + l for l in reaped.getvalue().splitlines()]
    text = "\n".join(out)
    print(text)
    if not a.no_report:
        os.makedirs(sweep_dir(), exist_ok=True)
        with open(os.path.join(sweep_dir(), dt.date.today().isoformat() + ".txt"), "w") as f:
            f.write(text + "\n")
    return rc

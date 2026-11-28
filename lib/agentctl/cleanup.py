# agentctl, part `cleanup`: cleanup and the lifecycle.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from collections.abc import Sequence
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
from agentctl.clock import Stopwatch, epoch_now, stopwatch, wall_epoch
from agentctl.timefmt import now, ts
from agentctl.tickknobs import knob_cleanup_item_timeout, knob_fetch_timeout
from agentctl.queueknobs import knob_free_wait
from agentctl.govknobs import SBT_STALL_MIN, knob_sbt_reap
from agentctl.core import GIT_NET_SSH, HOME, PROTECTED_BRANCHES, err, git, remote, run_cmd
from agentctl.repos import slug
from agentctl.procs import PROC_ROOT, process_cwds, ps
from agentctl.forge import forge_branches
from agentctl.deletions import SWEEP_KEEP_REMOTE, delete_remote_branch, log_deletion
from agentctl.maint import cmd_orphans
from agentctl.onmain import landed_state
from agentctl.worktrees import CACHE_TREES, gitdir_activity, merged_reason, worktrees
from agentctl.runowners import orphan_branches
from agentctl.gitrepo import commit_known, commit_time
from agentctl.paths import claude_session_id, state_root


def cleanup_left(until: Stopwatch | None) -> float:
    """Seconds left before the cleanup's deadline (a Stopwatch bounded by it); inf without one."""
    return float("inf") if until is None else until.left()


DEADLINE_NOTE = "not looked at: the cleanup's deadline passed"


class TreeReview:
    """One cleanup pass's decision about each linked worktree, a method per stage: the tree's flags, whether it is in
    use, its own git reads (status, merged). The first stage with an answer decides."""

    def __init__(self, repo: str, main: str, min_age_h: float, cwds: dict[int, str], include_caches: bool,
                 until: Stopwatch | None) -> None:
        self.repo, self.main, self.min_age_h, self.cwds = repo, main, min_age_h, cwds
        self.include_caches, self.until = include_caches, until
        self.item = knob_cleanup_item_timeout()

    def verdict(self, w: dict[str, Any]) -> tuple[str, str, str | None, str, str]:
        """(action, wt, branch, sha, reason) of linked worktree entry w."""
        path, sha = w["worktree"], w.get("HEAD", "")
        branch = w.get("branch", "").removeprefix("refs/heads/") or None
        if cleanup_left(self.until) <= 0:
            return ("stop", path, branch, sha, DEADLINE_NOTE)
        why = self.flagged(w, path, branch) or self.in_use(path)
        if why:
            return ("skip", path, branch, sha, why)
        action, why = self.by_git(path, sha)
        return (action, path, branch, sha, why)

    def flagged(self, w: dict[str, Any], path: str, branch: str | None) -> str | None:
        """Why the tree is not touched by what it is (locked, protected, a tool cache, missing), or None."""
        if "locked" in w:
            return "locked"
        if branch in PROTECTED_BRANCHES:
            return f"on {branch}"
        if path.startswith(CACHE_TREES) and not self.include_caches:
            return "tool cache tree (--include-caches to consider)"
        if "prunable" in w or not os.path.isdir(path):
            return "directory missing (prunable; not touched)"
        return None

    def in_use(self, path: str) -> str | None:
        """Why the tree is not touched by what uses it (a process in it, recent git activity), or None."""
        busy = [p for p, c in self.cwds.items() if c == path or c.startswith(path + "/")]
        if busy:
            return f"in use by pid {busy[:3]}"
        act = gitdir_activity(self.repo, path)
        if act and wall_epoch() - act < self.min_age_h * 3600:
            return f"active {int((wall_epoch() - act) / 60)} min ago"
        return None

    def unclean(self, path: str) -> str | None:
        """Why `git status` says the tree is not clean (or could not say), or None."""
        dirty = git(self.repo, "-C", path, "status", "--porcelain", "--untracked-files=all", check=False, timeout=self.item)
        if dirty.returncode == 124:
            return f"undecided: git status took over {self.item:g} s (looked at again next pass)"
        if dirty.returncode != 0:
            return "git status failed"
        if dirty.stdout.strip():
            return f"dirty ({len(dirty.stdout.splitlines())} changed/untracked)"
        return None

    def by_git(self, path: str, sha: str) -> tuple[str, str]:
        """(action, reason) from the tree's own git reads: dirty, then merged into main."""
        t0 = stopwatch()
        why = self.unclean(path)
        if why:
            return "skip", why
        if cleanup_left(self.until) <= 0:
            return "stop", DEADLINE_NOTE
        why = merged_reason(self.repo, sha, self.main)
        if not why:
            n = git(self.repo, "rev-list", "--count", f"{self.main}..{sha}", check=False, timeout=self.item).stdout.strip() or "?"
            return "skip", f"unmerged ({n} commit(s) not on {remote()}/main)"
        if t0.elapsed() > 30:
            err(f"cleanup: {path} took {t0.elapsed():.0f} s to decide")
        return "delete", why


def plan_worktrees(repo: str, main: str, min_age_h: float, cwds: dict[int, str], include_caches: bool = False,
                   until: Stopwatch | None = None) -> list[tuple[str, str, str | None, str, str]]:
    """[(action, wt, branch, sha, reason)] where action is 'delete' or 'skip' -- 'stop' for every tree not looked at
    once the deadline (`until`, a Stopwatch) passed."""
    wts = worktrees(repo)
    review = TreeReview(repo, main, min_age_h, cwds, include_caches, until)
    return [review.verdict(w) for w in wts[1:]]  # the first entry is the main checkout


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


def own_ancestry() -> set[int]:
    """This process and its parents (the agent's shell is in the worktree it lands from)."""
    parents = {p: pp for p, pp, _e, _c in ps()}
    chain, pid = set(), os.getpid()
    while pid and pid not in chain:
        chain.add(pid)
        pid = parents.get(pid)
    return chain


class WorktreeClosing:
    """Closing the worktree land ran from, a method per stage; each returns why the worktree is KEPT, or None to go on."""

    def __init__(self, wt: str) -> None:
        self.wt = os.path.realpath(wt)
        self.root = self.head = self.main = self.message = ""
        self.branch: str | None = None

    def placed(self) -> str | None:
        """It is a linked worktree of a repository, unlocked, on no protected branch."""
        try:
            trees = worktrees(self.wt)
        except RuntimeError as e:
            return str(e)
        self.root = os.path.realpath(trees[0]["worktree"])
        if self.wt == self.root:
            return "the root checkout is never closed"
        me = next((t for t in trees if os.path.realpath(t["worktree"]) == self.wt), None)
        if me is None:
            return f"not a worktree of {self.root}"
        if "locked" in me:
            return "locked"
        self.branch = (me.get("branch") or "").removeprefix("refs/heads/") or None
        return f"on {self.branch}" if self.branch in PROTECTED_BRANCHES else None

    def unused(self) -> str | None:
        """No other process is in it and its tree is clean."""
        mine, wt = own_ancestry(), self.wt
        busy = [p for p, c in process_cwds().items() if p not in mine and (c == wt or c.startswith(wt + "/"))]
        if busy:
            return f"in use by pid {busy[:3]}"
        dirty = git(wt, "status", "--porcelain", "--untracked-files=all", check=False)
        if dirty.returncode or dirty.stdout.strip():
            return f"the tree is not clean ({len(dirty.stdout.splitlines())} changed/untracked)"
        return None

    def landed(self) -> str | None:
        """Every commit of its own is on origin's main (git cherry)."""
        git(self.wt, "fetch", "--quiet", remote(), "main", check=False)
        self.main = f"refs/remotes/{remote()}/main"
        self.head = git(self.wt, "rev-parse", "HEAD").strip()
        state = landed_state(self.wt, self.main, self.head)
        if state.unknown:  # kept: nothing proves it landed
            return f"git cherry could not compare it with {remote()}/main"
        return None if state.landed else f"{len(state.left)} commit(s) not on {remote()}/main"

    def removed(self) -> str | None:
        """Remove the worktree and its branch (the message says what), unless git refuses."""
        why = merged_reason(self.root, self.head, self.main) or "nothing of its own left"
        r = git(self.root, "worktree", "remove", self.wt, check=False)
        if r.returncode:
            return f"git worktree remove refused: {r.stderr.strip()[:200]}"
        log_deletion("worktree", os.path.basename(self.root), self.wt, self.head, f"closed after WATCH OK: {why}")
        msg = f"closed worktree {self.wt}"
        branch = self.branch
        if branch and f"refs/heads/{branch}" not in {t.get("branch") for t in worktrees(self.root)}:
            if git(self.root, "update-ref", "-d", f"refs/heads/{branch}", self.head, check=False).returncode == 0:
                log_deletion("branch", os.path.basename(self.root), branch, self.head, f"closed after WATCH OK: {why}")
                msg += f" and branch {branch}"
        self.message = msg + f" ({why})"
        return None


def close_worktree(wt: str) -> str:
    """Remove the worktree land ran from and its local branch, when proven merged. Returns one line saying what."""
    closing = WorktreeClosing(wt)
    kept = closing.placed() or closing.unused() or closing.landed() or closing.removed()
    return f"worktree {closing.wt} kept: {kept}" if kept else closing.message


NEVER_SIGNAL = re.compile(r"(^|/)(claude|login|tmux|ssh)(\s|$)")  # an agent session or a terminal: reported, never stopped


def worktree_holders(wt: str) -> dict[int, str]:
    """{pid: command} of the processes working inside wt, this process and its parents excepted."""
    wt = os.path.realpath(wt)
    mine = own_ancestry()
    cmds = {p: c for p, _pp, _e, c in ps()}
    return {p: cmds.get(p, "?") for p, c in process_cwds().items()
            if p not in mine and (c == wt or c.startswith(wt + "/"))}


def process_session(pid: int) -> str | None:
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


def stop_session_holders(held: dict[int, str], out: Any) -> None:
    """SIGTERM the holders {pid: command} that belong to this Claude session (never a NEVER_SIGNAL one)."""
    sid = claude_session_id()
    for pid, cmd in sorted(held.items()):
        if sid and not NEVER_SIGNAL.search(cmd) and process_session(pid) == sid:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
                out(f"stopped pid {pid} of this session: {cmd[:120]}", flush=True)


def free_worktree(wt: str, out: Any = print) -> dict[int, str]:
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
    stop_session_holders(held, out)
    deadline = wall_epoch() + knob_free_wait()
    while held and wall_epoch() < deadline:
        time.sleep(0.5)  # wall: waits on and times real processes and threads
        held = worktree_holders(wt)
    return held


def bounded_fetch(repo: str, *extra: str, refs: Sequence[str] = ()) -> bool | str:
    """`git fetch <remote>` for an unattended run (maintain's sweep): bounded, ssh keepalives on, never prompting. True,
    or why not (2026-10-27: the remote's ssh accepted the connection and never answered, and the first maintain pass hung
    in the sweep's fetch). Every remote here is https, where the ssh keepalives do nothing: a transfer under 1 KB/s for
    30 s is given up (http.lowSpeed*), so a lossy uplink (2026-11-22 03:28Z, 40% loss) never waits out the whole bound."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_SSH_COMMAND=GIT_NET_SSH)
    try:
        r = process_runner().run(["git", "-C", repo, "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=30", "fetch", "--quiet", *extra, remote(), *refs], capture_output=True, text=True, env=env,
                           timeout=knob_fetch_timeout(), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"timed out after {int(knob_fetch_timeout())} s"
    return True if r.returncode == 0 else (r.stderr.strip().splitlines() or ["exit %d" % r.returncode])[-1][:160]


class RepoSweep:
    """The sweep of one repository, a method per section of its report (lines appended to `out`)."""

    def __init__(self, repo: str, idle_days: float, dry: bool, out: list[str], store: Any) -> None:
        self.repo, self.idle_days, self.dry, self.out, self.store = repo, idle_days, dry, out, store
        self.name, self.now_ = os.path.basename(repo), wall_epoch()  # ages git times and mtimes: the real world's
        self.main, self.o_r = "", ""
        self.checked_out: set[str] = set()

    def header(self) -> None:
        fetched = bounded_fetch(self.repo, "--prune")
        self.main = f"refs/remotes/{remote()}/main"
        if fetched is not True:
            self.out.append(f"== {self.name}: fetch failed ({fetched}): from the last fetched state")
        self.out.append(f"== {self.name} ({remote()}/main {git(self.repo, 'rev-parse', self.main).strip()[:10]})")

    def idle_worktrees(self) -> None:
        idle = [line for line in map(self.idle_worktree, worktrees(self.repo)[1:]) if line]
        self.out += idle or [f"  no unmerged worktree idle more than {self.idle_days} d"]

    def idle_worktree(self, w: dict[str, Any]) -> str | None:
        """The report line of a linked worktree unmerged and idle too long, or None."""
        path, sha = w["worktree"], w.get("HEAD", "")
        branch = (w.get("branch") or "").removeprefix("refs/heads/")
        self.checked_out.add(branch)
        if not os.path.isdir(path) or path.startswith(CACHE_TREES) or merged_reason(self.repo, sha, self.main):
            return None
        tip = int(commit_time(self.repo, sha) or 0)
        days = (self.now_ - max(gitdir_activity(self.repo, path) or 0, tip)) / 86400
        if days <= self.idle_days:
            return None
        n = len(git(self.repo, "rev-list", f"{self.main}..{sha}").split())
        return f"  UNMERGED worktree {path} [{branch or 'detached'}]: {n} commit(s), idle {int(days)} d"

    def lonely_branches(self) -> None:
        for line in git(self.repo, "for-each-ref", "--format=%(refname:short) %(objectname) %(committerdate:unix)",
                        "refs/heads").splitlines():
            b, sha, t = line.split()
            if b in PROTECTED_BRANCHES or b in self.checked_out or merged_reason(self.repo, sha, self.main):
                continue
            days = (self.now_ - int(t)) / 86400
            if days > self.idle_days:
                self.out.append(f"  UNMERGED branch {b} (no worktree): tip {sha[:10]}, idle {int(days)} d")

    def remote_branches(self) -> None:
        for line in git(self.repo, "ls-remote", "--heads", remote()).splitlines():
            sha, ref = line.split()
            b = ref.removeprefix("refs/heads/")
            if b in SWEEP_KEEP_REMOTE or b.startswith("measure/"):
                continue  # measure/* is `agentctl cleanup`'s, by its runs
            self.remote_branch(b, sha)

    def remote_branch(self, b: str, sha: str) -> None:
        repo, out = self.repo, self.out
        if not commit_known(repo, sha):
            git(repo, "fetch", "--quiet", remote(), b, check=False)
        why = commit_known(repo, sha) and merged_reason(repo, sha, self.main)
        if not why:
            t = int(commit_time(repo, sha) or self.now_)
            out.append(f"  unmerged remote branch {b}: tip {sha[:10]}, {int((self.now_ - t) / 86400)} d old (listed, kept)")
        elif self.dry:
            out.append(f"  would delete remote branch {b} {sha[:10]}: {why}")
        else:
            self.delete_remote(b, sha, why)

    def delete_remote(self, b: str, sha: str, why: str) -> None:
        try:
            self.o_r = self.o_r or slug(self.repo)
            delete_remote_branch(self.repo, self.o_r, b, sha)
            log_deletion("remote-branch", self.o_r, b, sha, f"sweep: {why}")
            self.out.append(f"  deleted remote branch {b} {sha[:10]}: {why}")
        except Exception as e:
            self.out.append(f"  remote branch {b} not deleted: {e}")

    def orphans(self) -> None:
        self.out.append("== branches whose owner is gone (worktree removed or pid dead) and whose runs all finished")
        try:
            if not orphan_branches(self.repo, self.o_r or slug(self.repo), self.dry, self.out, self.store):
                self.out.append("  none")
        except (Exception, SystemExit) as e:
            self.out.append(f"  could not list them ({str(e)[:160]})")


def sweep_repo(repo: str, idle_days: float, dry: bool, out: list[str], store: Any) -> None:
    sweep = RepoSweep(repo, idle_days, dry, out, store)
    sweep.header()
    sweep.idle_worktrees()
    sweep.lonely_branches()
    sweep.remote_branches()
    sweep.orphans()


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
    if knob_sbt_reap() != "0":  # the tests never reap this machine's servers
        with contextlib.redirect_stdout(reaped):
            cmd_orphans(argparse.Namespace(kill=not a.dry_run, sbt_only=True))
    out.append("== sbt servers (a gone worktree, an orphaned serving command, a running command with no CPU for "
               f"{SBT_STALL_MIN} min){'' if not a.dry_run else ': not reaped (DRY RUN)'}")
    out += ["  " + l for l in reaped.getvalue().splitlines()]
    text = "\n".join(out)
    print(text)
    if not a.no_report:
        os.makedirs(sweep_dir(), exist_ok=True)
        with open(os.path.join(sweep_dir(), dt.date.fromtimestamp(epoch_now()).isoformat() + ".txt"), "w") as f:
            f.write(text + "\n")
    return rc

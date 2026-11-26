# agentctl, part `land`: land.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import signal
import subprocess
import time
import sys
import contextlib
import json
from typing import Any, Iterator
from agentctl.procrunner import process_runner
from agentctl.selfcode import TOOL_PATH
from agentctl.clock import Clock
from agentctl.queueknobs import knob_land_stall_min
from agentctl.fixerknobs import knob_stall_cpu_seconds
from agentctl.leasetable import LeaseLost
from agentctl.core import HOME, SHORT, err, git, process_cache, reds_dir, slug, spawn_detached
from agentctl.forge import forge_env
from agentctl.procs import ps_rows
from agentctl.store import StoreUnavailable
from agentctl.leases import LeaseHold, _landing, lock_beat
from agentctl.queueknobs import (
    knob_land_lease_max, knob_land_lease_ttl, knob_land_lock_timeout, knob_land_wait_report_min,
    knob_push_stall_secs, knob_push_wall_min)


# ---------------------------------------------------------------- land / env
SDKMAN_BINS = [f"{HOME}/.sdkman/candidates/sbt/current/bin", f"{HOME}/.sdkman/candidates/java/current/bin"]
RACE = re.compile(r"non-fast-forward|cannot lock ref|fetch first|failed to update ref|stale info|incorrect old value|"
                  r"reference already exists|remote rejected.*(lock|update)", re.I)
# The hook REFUSING for red (its "is RED on main" warnings also print when tools-only commits land while red: not a refusal)
REDGATE = re.compile(r"main is red: only a fix|carries no Fixes-Red")
TUNNEL_TROUBLE = re.compile(r"GitOps manifests STALE|Connection to localhost closed|"
                            r"Could not read from remote repository|Connection refused", re.I)
LAND_RETRIES = 3
LAND_PRECHECK_ROUNDS = 2  # main moved under the lock this often: the next check runs under it, so a lander finishes


def _descendants(rows: Any, leader: Any) -> Any:
    """The pids of process `leader` and every descendant of it, whatever group each runs in."""
    kids = {}
    for row in rows:
        kids.setdefault(row.ppid, []).append(row.pid)
    tree, todo = set(), [leader]
    while todo:
        cur = todo.pop()
        if cur not in tree:
            tree.add(cur)
            todo += kids.get(cur, [])
    return tree


def group_cpu_seconds(pgid: Any) -> Any:
    """The CPU seconds the step's work has used: every live process of process group `pgid` AND every descendant of its
    leader, whatever group it runs in (ps's cumulative TIME), or 0. ISSUE 20261108T232313-7384d0: the movies hook's
    run-script-tests runs each self-test in a process group of its own, so a push whose hook ran scripttests for 19
    silent minutes read "no output and no CPU" and was ended twice."""
    rows = ps_rows(("pid", "ppid", "pgid", "time", "stat"))
    tree = _descendants(rows, int(pgid))
    mine = [x for x in rows if x.pgid == int(pgid) or x.pid in tree]
    # A RUNNABLE PROCESS IS PROGRESS (as movies' stall-watch.sh counts it): under a load of 75 on 18 cores a busy child
    # is granted ~0.25 CPU-s a second, under the judge's 1 s a window, yet it is working, not stalled. Each sample with
    # one of the tree running or waiting for a CPU (state R) is credited STALL_CPU_SECONDS.
    # A process new since the last sample is no progress (a hook's helper loop forks one every second).
    seen = _tree_seen.get(pgid, set())
    if any(x.stat.startswith("R") and x.pid in seen for x in mine):
        _runnable_credit[pgid] = _runnable_credit.get(pgid, 0.0) + knob_stall_cpu_seconds()
    _tree_seen[pgid] = {x.pid for x in mine}
    # ps's d-hh:mm:ss: a day is 86400 s (the old split read it as 60 s)
    return sum(x.cpu_seconds for x in mine) + _runnable_credit.get(pgid, 0.0)


_runnable_credit, _tree_seen = process_cache({}), process_cache({})


def stall_clock():
    """The stall judge's clock (tests drive it, with group_cpu_seconds, so no verdict depends on the machine's load)."""
    return time.monotonic()  # wall: waits on and times real processes and threads


WATCHED_GROUPS = set()  # process groups run_watched started and that may still run: the watchdog ends them first


def run_watched(argv, cwd, env, log_path, step, stall = None, sampler = None, cpu = None, clock = None, wall = None,
                quiet_ok: bool = False, hook_stall = None, hooks = None):
    """Run argv (its own process group, stdin closed), its output into a file beside log_path. (returncode, output);
    returncode None when it STALLED: no output and no CPU in its process group for `stall` seconds, after which the
    group is ended. Waits with Popen.wait, never a sleep. `cpu` (pgid -> CPU seconds) and `clock` are the samples the
    verdict reads: group_cpu_seconds and stall_clock unless given. quiet_ok: a step that legitimately waits quietly
    (a remote run polled without output) renews the leases this process holds at every poll, so its own `stall`
    bound ends it, not the leases' TTL. hook_stall (a push's): while a pre-push hook runs in the group (`hooks`, pgid
    -> bool: group_runs_hook unless given), it is judged as quiet_ok with hook_stall as its bound -- the hook bounds
    itself -- and `stall` judges the transport around it. A stalled run's output ends with its STALL_LINE (why)."""
    cpu_of, clock = cpu or group_cpu_seconds, clock or stall_clock
    hooks = hooks or group_runs_hook
    stall = stall if stall is not None else knob_land_stall_min() * 60
    out_path = f"{log_path}.{re.sub(r'[^A-Za-z0-9_-]', '_', step)}.out"
    with open(out_path, "w") as out:
        p = process_runner().popen(argv, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
    WATCHED_GROUPS.add(p.pid)
    try:
        stalled = _watch_group(p, out_path, step, stall, sampler, cpu_of, clock, wall, quiet_ok,
                               hook_stall, hooks)
    except BaseException:  # a store error, a signal: never a child group left running, untracked
        _end_group(p)
        WATCHED_GROUPS.discard(p.pid)
        raise
    WATCHED_GROUPS.discard(p.pid)
    with open(out_path, errors="replace") as f:
        output = f.read()
    with contextlib.suppress(OSError):
        os.unlink(out_path)
    if stalled:
        output += f"{'' if not output or output.endswith(chr(10)) else chr(10)}{STALL_LINE}{stalled}\n"
    return (None if stalled else p.returncode), output


STALL_LINE = "agentctl: ended, "  # run_watched's last output line of a run it ended: STALL_LINE + why


def stall_reason(output):
    """Why run_watched ended a run (its output's STALL_LINE), or None."""
    for line in reversed((output or "").splitlines()):
        if line.startswith(STALL_LINE):
            return line[len(STALL_LINE):].strip()
    return None


HOOK_COMMAND = re.compile(r"(^|[\s/])hooks/pre-push(\s|$)")


def group_runs_hook(pgid) -> bool:
    """Whether a live process of group `pgid` is a git pre-push hook (`.../hooks/pre-push <remote> <url>`)."""
    return any(r.pgid == int(pgid) and HOOK_COMMAND.search(r.args) for r in ps_rows(("pgid", "command")))


def _group_gone_on(p: Any, sig: Any) -> Any:
    """Signal the step's group; True when the step is over already (no such group, or no leader left to signal)."""
    try:
        os.killpg(p.pid, sig)
    except ProcessLookupError:
        return True
    except PermissionError:
        if p.poll() is not None:
            return True
        try:
            p.send_signal(sig)
        except ProcessLookupError:
            return True
    return False


def _end_group(p) -> None:
    """SIGTERM, then SIGKILL, the step's process group. macOS answers EPERM for a group whose members have all exited
    but are not reaped yet (2026-11-22: a quiet step ending as its watch ended it, at load 290): the step is over then;
    a live leader it refuses is signalled alone."""
    for sig, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        if _group_gone_on(p, sig):
            return
        try:
            p.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def beat_mark(step, size, cpu, polls: int = 0, quiet_ok: bool = False):
    """The progress mark a poll renews the leases with: output bytes and whole CPU seconds -- and, for a quiet_ok step,
    the poll count (every poll is progress: its own stall bound judges it)."""
    return f"{step}:{size}:{int(cpu or 0)}" + (f":{polls}" if quiet_ok else "")


def watch_lease_loss(step: Any, size: Any, cpu: Any, polls: Any, quiet_ok: Any) -> Any:
    """Why the landing lease was lost at this poll (it renews on real progress only: output bytes, whole CPU seconds),
    else None. A store that did not answer is no verdict: renewed at the next poll (a lease lapsed meanwhile is lost)."""
    try:
        lock_beat(beat_mark(step, size, cpu, polls, quiet_ok))
    except LeaseLost as ex:
        return f"{ex} (no progress within its TTL)"
    except StoreUnavailable as ex:
        err(f"{step}: the store did not answer the lease renewal ({str(ex)[:120]})")
    return None


class GroupProgress:
    """What a watched group last did: its output size and CPU, and when either last moved."""

    def __init__(self, started: Any) -> None:
        self.size, self.cpu, self.changed = -1, None, started

    def stalled(self, size: Any, cpu: Any, now: Any, need: Any, hook_toggled: Any, bound: Any) -> bool:
        """Progress: output grew, or the group used a real second of CPU since the last progress (the hook idle in `wait`
        with only its watchdog polling used ~0.08 s in 50 min, 2026-10-27: that is not progress); a hook ending is
        progress too (the transport's bound starts there). Else whether it has gone `bound` s without."""
        if size != self.size or self.cpu is None or cpu - self.cpu >= need or hook_toggled:
            self.size, self.cpu, self.changed = size, cpu, now
            return False
        return now - self.changed > bound


def poll_verdict(lease_why: Any, stalled: Any, bound: Any, in_hook: Any, waited: Any, wall: Any) -> Any:
    """The first reason to end the group at this poll: its lease lost, no progress within `bound`, past its wall bound."""
    if lease_why:
        return lease_why
    if stalled:
        return f"no output and no CPU for {bound:g} s" + (" while its pre-push hook ran" if in_hook else "")
    if wall is not None and waited > wall:  # busy or not: the wall bound (a push's)
        return f"past its wall bound of {wall:g} s"
    return None


def _exited_within(p, seconds) -> bool:
    """Whether the process exited within `seconds`."""
    try:
        p.wait(timeout=seconds)
        return True
    except subprocess.TimeoutExpired:
        return False


def _in_hook(hook_stall, hooks, pid):
    """Whether the group is in the pre-push hook (only judged when a hook bound is set)."""
    return hook_stall is not None and hooks(pid)


def _judged_end(p, why, step):
    """Ends the group on the verdict `why` (and returns it) -- unless it exited while it was judged (a late wakeup under
    load): its own end, None."""
    if p.poll() is not None:
        return None
    err(f"{why}: ending {step}")
    _end_group(p)
    return why


def _watch_group(p, out_path, step, stall, sampler, cpu_of, clock, wall, quiet_ok: bool = False, hook_stall = None, hooks = None):
    """run_watched's wait: why it ended the group (it stalled, lost a lease, passed its wall bound), or None when the
    group exited by itself."""
    seen, polls, in_hook = GroupProgress(clock()), 0, False
    poll = max(0.2, min(15.0, stall / 4))
    need = knob_stall_cpu_seconds()
    t_start = clock()  # the wall bound reads the same clock: tests drive it, so no verdict depends on the load
    while not _exited_within(p, poll):
        if sampler:
            sampler()
        size, cpu = os.path.getsize(out_path), cpu_of(p.pid)
        was_hook, in_hook = in_hook, _in_hook(hook_stall, hooks, p.pid)
        polls += 1
        why = watch_lease_loss(step, size, cpu, polls, quiet_ok or in_hook)
        now = clock()
        bound = hook_stall if in_hook else stall
        stalled = seen.stalled(size, cpu, now, need, was_hook != in_hook, bound)
        why = poll_verdict(why, stalled, bound, in_hook, now - t_start, wall)
        if why:
            return _judged_end(p, why, step)
    return None


def spawn_land(wt, env, log):
    """A detached `agentctl land` of wt (the normal hook; its verdicts reach the inbox). Its pid."""
    with open(log, "a") as logf:
        return spawn_detached([sys.executable, TOOL_PATH, "land"], cwd=wt, stdin=subprocess.DEVNULL, stdout=logf,
                              stderr=logf, start_new_session=True, env=env).pid


def tool_env():
    """os.environ with sdkman's sbt and java first on PATH (the movies hook's sbt check needs them) -- moved to the
    front when PATH already holds them further back (a fixer's environment: another java ahead of sdkman's)."""
    env = dict(os.environ)
    bins = [b for b in SDKMAN_BINS if os.path.isdir(b)]
    env["PATH"] = ":".join(bins + [p for p in env.get("PATH", "").split(":") if p not in bins])
    java_home = f"{HOME}/.sdkman/candidates/java/current"
    if not env.get("JAVA_HOME") and os.path.isdir(java_home):
        env["JAVA_HOME"] = java_home
    # the movies hook's known-red baseline: `<dir>/<main sha>`, written by baseline_failures (a hook without the mode
    # ignores it)
    env.setdefault("PREPUSH_BASELINE_DIR", reds_dir("baseline-out"))
    env["KINOWO_FORGE"] = "github"  # the movies hook's gate and tools read GitHub (scripts/forge-gh)
    try:  # as the lander App on ci-runner
        env = forge_env(SHORT["movies"], env)
    except (Exception, SystemExit) as e:
        err(f"tool env: no GitHub App token for movies ({str(e)[:120]}); the hook reads without one")
    return env


def push_bounds():
    """A push's bounds: `stall` for its transport; while its pre-push hook runs, the hook's own -- the check outside the
    lock's (LAND_STALL_MIN), its leases renewed at every poll (run_watched hook_stall); `wall` over all of it."""
    return {"stall": knob_push_stall_secs(),
            "wall": knob_push_wall_min() * 60,
            "hook_stall": knob_land_stall_min() * 60}


def push_argv(remote, src, dst: str = "main"):
    argv = ["git", "push", remote, f"{src}:refs/heads/{dst}"]
    assert not any(a in ("--no-verify", "-n", "--force", "-f") or a.startswith("--force") for a in argv)
    return argv


def land_lease_name(repo: str) -> str:
    """One lease per REPOSITORY (its GitHub slug): every clone and worktree of it, on every host -- keyed by the
    local clone's path, the laptop's `land --follow` and ci-runner's pushes took different leases on one store. A
    checkout without the remote: its git common dir."""
    try:
        key = slug(repo)
    except (SystemExit, RuntimeError):
        key = os.path.realpath(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    return "land/" + re.sub(r"[^A-Za-z0-9._-]", "_", key.strip("/"))


def land_lease_hold(repo: str, store: Any) -> LeaseHold:
    return LeaseHold(land_lease_name(repo), knob_land_lease_ttl(),
                     knob_land_lease_max(), meta={"repo": repo},
                     store=store)


@contextlib.contextmanager
def land_lock(repo: str, clock: Clock, store: Any) -> Iterator[bool]:
    """Hold the repository's landing lease, waiting for it (LAND_LOCK_TIMEOUT s at most) with a line saying who holds
    it and doing what, repeated every LAND_WAIT_REPORT_MIN minutes. Yields True when held, False when the wait timed
    out."""
    h = land_lease_hold(repo, store)
    if not (h.acquire() or waited_for_lease(h, clock)):
        yield False
        return
    with holding_land_lease(h):
        yield True


@contextlib.contextmanager
def land_lock_if_free(repo: str, store: Any) -> Iterator[bool]:
    """The landing lease when it is free now, else False: the tick's pushes, where busy now is tried again next tick
    (never a tick held for hours)."""
    h = land_lease_hold(repo, store)
    if not h.acquire():
        yield False
        return
    with holding_land_lease(h):
        yield True


def waited_for_lease(h: LeaseHold, clock: Clock) -> bool:
    """Wait for the held lease h, saying who holds it; True once acquired, False after LAND_LOCK_TIMEOUT s."""
    def said(r: dict[str, Any]) -> str:
        meta = json.loads(r.get("meta") or "{}")
        return (f"{r['holder']} (pid {meta.get('pid')}), {meta.get('note') or 'working'}; "
                f"expires in {int(r['expires_at'] - clock.time())} s")
    err(f"waiting for the landing lease {h.name}, held by {said(h.held)}")
    deadline = clock.monotonic() + knob_land_lock_timeout()
    every = knob_land_wait_report_min() * 60
    report_at = clock.monotonic() + every
    while not h.acquire():
        if clock.monotonic() > deadline:
            return False
        if clock.monotonic() > report_at:
            err(f"still waiting for the landing lease: {said(h.held)}")
            report_at = clock.monotonic() + every
        clock.sleep(2)
    err("got the landing lease")
    return True


@contextlib.contextmanager
def holding_land_lease(h: LeaseHold) -> Iterator[None]:
    _landing[0] = h
    try:
        yield
    finally:
        _landing[0] = None
        h.release()


# THE KNOWN-RED BASELINE (the owner, 2026-10-27: "pre-push fails on plain origin/main" is itself a claimed red, and
# while it is open a change is judged against main's known-failing checks). When a change's dry run fails, the same
# checks run on plain origin/main's tree: a commit with main's tree on top of the change (`git commit-tree
# main^{tree} -p <head>`), checked with `--checks-only <head>..<that>` -- the change's paths select the checks, main's
# tree is checked. Checks failing there the same way (the same normalized failure lines) are main's own red: recorded
# in reds/local.json, shown by ci-why as `Local checks`, claimable (`red claim local`) and dispatched like any red, and
# land says which failures are the change's own. The hook's own output there is kept as `reds/baseline-out/<main sha>`
# (first line `pre-push baseline <main sha>`), the file movies' pre-push reads through $PREPUSH_BASELINE_DIR (tool_env):
# land's dry run then runs the hook once more, which passes a check failing only as it fails on main (its pass stored,
# so the push reuses it). No file for main: the hook blocks every failure (fail-closed).
HOOK_STATUS = re.compile(r"pre-push (\S+) (ok|FAILED) \(\d+s\)")

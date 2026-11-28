# agentctl, part `core`: the process-cache registry, err and attempt, and the git runner and process helpers every part
# uses (split 2026-11-27: the commit-graph questions to gitrepo, the state paths to paths, slug to repos).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and procrunner's only.

import os
import shlex
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Sequence, TypeVar
from agentctl.procrunner import process_runner
from agentctl.psparse import parse_ps
from agentctl.storeknobs import knob_host, knob_remote, knob_role

# THE PROCESS-WIDE CACHES, IN ONE REGISTRY (simplification-2026-10-31 item 10): every module-level dict, list or set a
# part keeps between calls is made with `process_cache(...)`, so the test harness clears them all between tests
# (tests/test_agentctl.py Base) -- 33 of them leaked from one test into the next. test_caches holds the rule.
PROCESS_CACHES = []


T = TypeVar("T")


def process_cache(empty: T) -> T:
    """empty, registered as one of the process's caches (PROCESS_CACHES: cleared together, the tests between cases)."""
    PROCESS_CACHES.append(empty)
    return empty

HOME = os.path.expanduser("~")


REPOS = [f"{HOME}/bitcashier/{r}" for r in ("infra", "core", "dashboard", "invoice-page", "version-dashboard")] + [
    f"{HOME}/projects/movies"
]
SHORT = {
    "movies": "pawelkrupinski/movies",
    **{r: f"bitcashier/{r}" for r in ("infra", "core", "dashboard", "invoice-page", "version-dashboard")},
}


def remote() -> str:
    """THE PUSH AND FETCH REMOTE of every clone agentctl lands, queues, waits, fixes and batches through:
    $AGENTCTL_REMOTE, else `origin`. Read at EVERY call: a test or a config change applies at once."""
    return knob_remote()
PROTECTED_BRANCHES = {"main", "master"}
ZERO = "0" * 40


# ---------------------------------------------------------------- helpers
def print_error(msg: object) -> None:
    print(f"agentctl: {msg}", file=sys.stderr, flush=True)


D = TypeVar("D")


def attempt(fn: Callable[[], T], default: D, *, catch: tuple[type[BaseException], ...],
            what: str | None = None) -> T | D:
    """fn(), or default when it raises one of `catch` -- exactly the types the call site names, nothing wider. With
    `what`, the failure is told first: `what (<the error, 160 characters at most>)` on stderr."""
    try:
        return fn()
    except catch as e:
        if what is not None:
            print_error(f"{what} ({str(e)[:160]})")
        return default


GIT_NET_SSH = "ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"


# agentctl's own commits and rewrites (land's rebase, a blame or fallback revert, a queue cherry-pick) never sign:
# main's commits are unsigned, and a global commit.gpgsign=true behind an empty ssh-agent fails them (issue
# 20261115T112749-d27361).
NO_SIGN = ("-c", "commit.gpgsign=false")


def git(repo: str, *args: str, check: bool = True, input: str | None = None, timeout: float = 600) -> Any:
    """git -C repo args, bounded: its stdout when check (raising on a failure), else the CompletedProcess."""
    env = None
    if args and args[0] in ("fetch", "ls-remote"):  # never hang on a remote whose ssh accepts and does not answer
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        env.setdefault("GIT_SSH_COMMAND", GIT_NET_SSH)
    try:
        r = process_runner().run(["git", "-C", repo, *args], capture_output=True, text=True, errors="replace", input=input, env=env,
                           timeout=timeout)  # every git call is bounded: a stuck lock or network never holds a step
    except subprocess.TimeoutExpired:
        if check:
            raise RuntimeError(f"git {' '.join(args)} in {repo}: timed out after {timeout:g} s")
        return subprocess.CompletedProcess(args, 124, "", f"timed out after {timeout:g} s")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {repo}: {r.stderr.strip()}")
    return r.stdout if check else r


def run_cmd(args: Sequence[str], timeout: float = 60, text: bool = True, **kw: Any) -> subprocess.CompletedProcess[Any]:
    """THE subprocess runner for a short local command (ps, lsof, pgrep, launchctl, a probe, a quick git): output
    captured, stdin closed, and BOUNDED -- a hung command reads as exit 124 with empty output, never a hung caller."""
    kw.setdefault("stdin", None if "input" in kw else subprocess.DEVNULL)
    try:
        return process_runner().run(args, capture_output=True, text=text, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        empty = "" if text else b""
        return subprocess.CompletedProcess(args, 124, empty, empty or f"timed out after {timeout} s")


def probe_output(cmd: str | None) -> str:
    """The stdout of a seam's command (the tests' $AGENTCTL_*_PROBE, a fixed ps line), run as ARGV: shlex-split, never
    through a shell (review 2026-11-26: shell=True made any such environment variable a shell command the governor
    ran -- `;`, `$(...)` and pipes included). "" for no command."""
    argv = shlex.split(cmd or "")
    return str(run_cmd(argv).stdout) if argv else ""


def git_ok(repo: str, *args: str) -> bool:
    try:
        return process_runner().run(["git", "-C", repo, *args], capture_output=True, timeout=120).returncode == 0
    except subprocess.TimeoutExpired:
        return False


# DETACHED CHILDREN ARE REAPED (2026-10-29: the processor, pid 77145, carried ~17 <defunct> children, some 6 h old --
# executors, fixers' wrappers and lands it started with Popen and never waited for). Every detached start goes through
# spawn_detached, which keeps the Popen; reap_children() -- each tick -- polls them, and also waits for a zombie child
# this process no longer knows (one started before an exec onto new code: the pid survives, the Popen objects do not)
# once it has been a zombie at two looks in a row, so a child a subprocess.run in another thread is about to wait for is
# never taken from it.
def spawn_detached(argv: list[str], **kw: Any) -> "subprocess.Popen[Any]":
    """agentctl's own background process (a processor, executor, watch, fixer wrapper, owner): marked
    AGENTCTL_DETACHED, so it never runs `secrets` (interactive_foreground) -- its token comes in its environment or from
    the processor's socket."""
    kw["env"] = {**(kw.get("env") if kw.get("env") is not None else os.environ), "AGENTCTL_DETACHED": "1"}
    runner = process_runner()
    p = runner.popen(argv, **kw)
    runner.detached.append(p)  # the runner that started it keeps it until a reap waits for it
    return p


def zombie_children(pid=None):
    """The pids of this process's children that are zombies (ps; [] when it cannot tell)."""
    me = pid or os.getpid()
    try:
        out = process_runner().run(["ps", "-ax", "-o", "pid=,ppid=,stat="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [r.pid for r in parse_ps(out, ("pid", "ppid", "stat")) if r.ppid == me and r.stat.startswith("Z")]


def reap_children(zombies_seen: set[int], detached: "list[subprocess.Popen[Any]]") -> int:
    """Poll the detached children (a finished one is waited for and forgotten), then wait for each zombie child seen at
    the last look too (`zombies_seen`: the pids that look saw, the caller's memo). Returns how many were reaped."""
    n = 0
    for p in list(detached):
        if p.poll() is not None:
            detached.remove(p)
            n += 1
    known = {p.pid for p in detached}
    now_z = set(zombie_children()) - known
    for pid in now_z & zombies_seen:
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                n += 1
        except ChildProcessError:
            pass
    zombies_seen.clear()
    zombies_seen.update(now_z)
    return n


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def on_ci_runner() -> bool:
    """The reconciler's own host (infra's agentctl.nix sets AGENTCTL_HOST=ci-runner), not a test's server role."""
    return knob_host() == "ci-runner"


def host_role() -> str:
    """"server" on ci-runner (AGENTCTL_HOST=ci-runner, or AGENTCTL_ROLE=server), else "sync". There is no third role."""
    if knob_role() == "server" or knob_host() == "ci-runner":
        return "server"
    return "sync"


def branch_of(wt: str | None) -> str:
    """The worktree's checked-out branch ('' when detached or gone)."""
    if not os.path.isdir(wt or ""):
        return ""
    return str(git(wt or "", "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout).strip()


def run_bounded(name, fn, secs, threads):
    """Run fn in a daemon thread and wait at most secs: a hung network call can never stall the processor's loop. A step
    still running from an earlier cycle is not started again (False); True when fn finished in time. threads: the
    process's bounded threads by name (Caches.bounded), where an earlier cycle's is found."""
    import threading
    t = threads.get(name)
    if t and t.is_alive():
        return False
    t = threading.Thread(target=fn, name=name, daemon=True)
    threads[name] = t

    t.start()
    t.join(secs)
    return not t.is_alive()


_tokens: dict[str, str] = process_cache({})

# agentctl, module `procs`: the process table (ps and its elapsed times), a process's start time, and whether a pid is still that process.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import subprocess
from typing import Any
from agentctl.psparse import PsRow, parse_ps
from agentctl.core import pid_alive, run_cmd
from agentctl.allowedpaths import allowed_path


def process_start(pid: int) -> str:
    """The process's start time as ps prints it: with the pid, it names one process even after the pid is reused."""
    # LC_ALL=C: one format whoever asks (2026-10-27: launchd's processor wrote "Tue Oct 27 …", a session's en_GB ps read
    # "Tue 27 Oct …", and a live red fixer looked dead)
    r = run_cmd(["ps", "-o", "lstart=", "-p", str(pid)], env=dict(os.environ, LC_ALL="C"))
    return str(r.stdout).strip()


def proc_alive(pid: Any, start: Any = None) -> bool:
    if not pid or not pid_alive(int(pid)):
        return False
    r = run_cmd(["ps", "-o", "stat=", "-p", str(int(pid))])
    if r.stdout.strip().startswith("Z"):  # a defunct process its parent never reaped: ended
        return False
    # the same words in any order: records written before process_start ran ps under LC_ALL=C hold a locale's order
    return not start or sorted(process_start(int(pid)).split()) == sorted(str(start).split())


def record_alive(rec: Any) -> bool:
    """The process a record names (its `pid`, and `process_start` when it has one) is running: the ONE reading of a
    fixer, owner, dispatch or probe record's liveness."""
    return proc_alive(rec.get("pid"), rec.get("process_start"))


# ---------------------------------------------------------------- orphans
def ps_rows(cols: tuple[str, ...], pids: tuple[int, ...] = (), wide: bool = False) -> list[PsRow]:
    """THE PROCESS TABLE: every process (or the `pids` asked for) with the ps `cols` (psparse.COLUMNS), parsed once.
    `wide` stops ps cutting a command line at the terminal's width. No rows when ps cannot run."""
    argv = ["ps", *(["-ww"] if wide else []), "-o", ",".join(f"{c}=" for c in cols),
            *(["-p", ",".join(map(str, pids))] if pids else ["-A"])]
    try:
        return parse_ps(run_cmd(argv).stdout, cols)
    except (OSError, subprocess.SubprocessError):
        return []


def ps() -> list[tuple[int, int, str, str]]:
    return [(r.pid, r.ppid, r.etime, r.args) for r in ps_rows(("pid", "ppid", "etime", "command"))]


def safe_cwd() -> str:
    """The cwd, or what it was: a watch's WATCH OK closes the worktree it runs in, and os.getcwd() then raises (the
    verdict was lost with it)."""
    try:
        return os.getcwd()
    except OSError:
        return os.environ.get("PWD") or "(removed)"


def socket_host() -> str:
    import socket
    return socket.gethostname().split(".")[0]


class Terminated(SystemExit):
    """A signal's exit (Owned's handler): never swallowed by a wait loop that rides out a token or tunnel outage."""


PROC_ROOT = "/proc"  # Linux: every process's cwd is <PROC_ROOT>/<pid>/cwd


def _proc_cwds(pids: Any) -> dict[int, str]:
    res: dict[int, str] = {}
    for pid in pids:
        try:
            cwd = os.readlink(os.path.join(PROC_ROOT, str(pid), "cwd"))
        except (FileNotFoundError, PermissionError, ProcessLookupError):  # gone, or not ours to read
            continue
        if allowed_path(cwd):
            res[pid] = cwd
    return res


def lsof_cwds(pids: Any) -> dict[int, str]:
    """{pid: cwd} keeping only allowed paths: on Linux from /proc/<pid>/cwd (review 2026-11-18: a Linux host with no
    lsof -- python:3.14's Debian image -- crashed `cleanup` and `orphans` with FileNotFoundError), elsewhere through ONE
    `lsof -a -p <pids> -d cwd` (always pid-scoped)."""
    pids = [int(x) for x in pids]
    if os.path.isdir(os.path.join(PROC_ROOT, "self")):
        return _proc_cwds(pids)
    res = {}
    for i in range(0, len(pids), 200):
        r = run_cmd(["lsof", "-nP", "-a", "-p", ",".join(map(str, pids[i:i + 200])), "-d", "cwd", "-Fpn"])
        pid = None
        for line in r.stdout.splitlines():
            if line.startswith("p"):
                pid = int(line[1:])
            elif line.startswith("n") and pid and allowed_path(line[1:]):
                res[pid] = line[1:]
    return res


SYSTEM_CMD = ("/System/", "/Applications/", "/usr/libexec/", "/Library/", "/usr/sbin/", "/sbin/", "/usr/bin/login")


def cwd_candidates() -> list[int]:
    """Pids worth asking for a cwd: everything but GUI apps and system daemons (Finder, Preview...). NEVER every process:
    lsof walking all cwds touches ~/Desktop, ~/Documents... and macOS charges those to our Python (TCC prompts)."""
    return [pid for pid, _pp, _e, cmd in ps() if not cmd.startswith(SYSTEM_CMD) and pid != os.getpid()]


def process_cwds() -> dict[int, str]:
    return lsof_cwds(cwd_candidates())

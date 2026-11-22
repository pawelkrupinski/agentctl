# agentctl, module `procs`: the process table (ps and its elapsed times), a process's start time, and whether a pid is still that process.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
from agentctl.core import pid_alive, run_cmd


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


# ---------------------------------------------------------------- orphans
def ps() -> list[tuple[int, int, str, str]]:
    r = run_cmd(["ps", "-axo", "pid=,ppid=,etime=,command="])
    out = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            out.append((int(parts[0]), int(parts[1]), parts[2], parts[3]))
    return out


def tosec(t: Any) -> float:
    """ps's [[dd-]hh:]mm:ss[.frac] (etime) or unbounded mm:ss (cputime) -> seconds."""
    dd = 0
    t = str(t)
    if "-" in t:
        d_, t = t.split("-", 1)
        dd = int(d_ or 0)
    s = 0.0
    for part in t.split(":"):
        try:
            s = s * 60 + float(part)
        except ValueError:
            return 0.0
    return s + dd * 86400


def safe_cwd() -> str:
    """The cwd, or what it was: a watch's WATCH OK closes the worktree it runs in, and os.getcwd() then raises (the
    verdict was lost with it)."""
    try:
        return os.getcwd()
    except OSError:
        return os.environ.get("PWD") or "(removed)"

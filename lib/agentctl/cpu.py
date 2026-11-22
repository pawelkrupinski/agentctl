# agentctl, part `cpu`: the whole machine's CPU, as the governor and laptop_pressure read it (gov.py uses it).

import os
import time
from typing import Any, Callable, Optional
from agentctl.core import env_num, state_root
from agentctl.statefile import read_record, write_json


GOV_SATURATED = 0.95    # x the cores: the machine's measured CPU at or over it is starved, whatever is charged
LAPTOP_LOAD = 2.0             # holds other work while the 1-min load is over this x the cores

# THE WHOLE MACHINE'S CPU (2026-11-22: top 0.24% idle -- 44% user, 56% sys -- at load 131-150 on 18 cores, while ps's
# %CPU summed to 6.1 cores and the governor saw no pressure). ps's %CPU is a decaying per-process average: it misses
# kernel time and the fork/exec churn of short-lived processes (git, test fake repos, run-script-tests trees,
# syspolicyd scanning each new executable). The busy cores are the machine's own CPU ticks -- user + sys + nice over
# a delta (host_statistics on macOS, /proc/stat on Linux) -- times the cores.
def cpu_ticks() -> Optional[tuple[int, int]]:
    """(busy ticks, all ticks) of the whole machine since boot, or None where unreadable."""
    try:
        if os.path.exists("/proc/stat"):
            with open("/proc/stat") as f:
                v = [int(x) for x in f.readline().split()[1:]]
            idle = v[3] + (v[4] if len(v) > 4 else 0)  # idle + iowait
            return sum(v[:8]) - idle, sum(v[:8])
        import ctypes, ctypes.util
        lib = ctypes.CDLL(ctypes.util.find_library("System"))
        lib.mach_host_self.restype = ctypes.c_uint
        info, count = (ctypes.c_uint * 4)(), ctypes.c_uint(4)
        if lib.host_statistics(lib.mach_host_self(), 3, ctypes.byref(info), ctypes.byref(count)) != 0:  # CPU_LOAD
            return None
        user, system, idle, nice = info
        return user + system + nice, user + system + idle + nice
    except (OSError, ValueError, AttributeError, IndexError):
        return None


CPU_SAMPLE_GAP = 0.25  # s between two tick samples when no recent one is kept
CPU_SAMPLE_KEEP = 120  # s a kept sample serves as the delta's start


def cpu_sample_path() -> str:
    """The kept tick sample: in the governor's directory (gov.py's gov_dir, $AGENTCTL_GOV_DIR), read here as it reads
    it -- cpu.py comes before the governor that uses it."""
    d = os.environ.get("AGENTCTL_GOV_DIR") or state_root("gov")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "cpu-ticks.json")


def _tick_start(path: str, read: Callable[[], Optional[tuple[int, int]]], sleep: Callable[[float], Any],
                now_: float) -> Any:
    """The delta's first sample: the one kept at `path` when it is 0.5-CPU_SAMPLE_KEEP s old, else one read now and
    CPU_SAMPLE_GAP waited."""
    prev = read_record(path) or {}
    if prev.get("ticks") and 0.5 <= now_ - float(prev.get("at") or 0) <= CPU_SAMPLE_KEEP:
        return prev["ticks"]
    start = read()
    sleep(CPU_SAMPLE_GAP)
    return start


def machine_busy_cores(n: int, read: Optional[Callable[[], Optional[tuple[int, int]]]] = None,
                       sleep: Optional[Callable[[float], Any]] = None, state: Optional[str] = None) -> Optional[float]:
    """The cores the whole machine is busy on (user + sys + nice), from a tick delta: against the sample kept in
    <gov>/cpu-ticks.json when it is 0.5-120 s old, else two samples CPU_SAMPLE_GAP apart. None where unreadable."""
    read, path, now_ = read or cpu_ticks, state or cpu_sample_path(), time.time()
    start = _tick_start(path, read, sleep or time.sleep, now_)
    end = read()
    if not start or not end or end[1] <= start[1]:
        return None
    try:
        write_json(path, {"at": now_, "ticks": list(end)})
    except OSError:
        pass
    return round(float(n * (end[0] - start[0]) / (end[1] - start[1])), 2)


def run_queue(passed: Optional[float] = None) -> Optional[float]:
    """The 1-min load average: the run queue, beside the busy cores (a saturated CPU is pressure only when it is
    oversubscribed too). None where unreadable, or when a caller passed its own load (pressure_reading's seam)."""
    if passed is not None:
        return None
    try:
        return os.getloadavg()[0]
    except OSError:
        return None


def cpu_pressure(r: dict[str, Any]) -> list[str]:
    """The CPU's part of laptop_pressure: busy cores past LAPTOP_LOAD x the cores (a reading charged past them), or the
    whole machine's CPU saturated (GOV_SATURATED, user + sys) with a run queue over LAPTOP_LOAD x the cores -- never
    a long run queue alone (46 threads waiting on 18 cores while cores sat idle, 2026-11-19)."""
    factor, n, busy, runq = env_num("AGENTCTL_LAPTOP_LOAD", LAPTOP_LOAD), r.get("cores"), r.get("load1"), r.get("runq")
    if busy is None or not n:
        return []
    if busy > factor * n:
        return [f"{busy:.1f} cores busy of {n} (holds over {factor * n:g})"]
    if busy >= GOV_SATURATED * n and runq is not None and runq > factor * n:
        return [f"CPU saturated: {busy:.1f} of {n} cores busy (user + sys), run queue {runq:.0f} (holds at "
                f"{GOV_SATURATED:.0%} busy with a run queue over {factor * n:g})"]
    return []

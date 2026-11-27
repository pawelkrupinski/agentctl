# agentctl, part `cpu`: the whole machine's CPU, as the governor and laptop_pressure read it (gov.py uses it).

from agentctl.clock import epoch_now
import os
import time
from typing import Any, Callable, Optional
from agentctl.govpool import gov_dir_path
from agentctl.statefile import read_record, write_json, note_failure
from agentctl.govknobs import knob_laptop_load


RUNQ_STARVED = 3.0     # x the cores: the run queue, 1-min and 5-min both over it, is starved however few cores read busy
GOV_SATURATED = 0.95    # x the cores: the machine's measured CPU at or over it is starved, whatever is charged

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
    """The kept tick sample: in the governor's directory (govpool.gov_dir_path)."""
    d = gov_dir_path()
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
    read, path, now_ = read or cpu_ticks, state or cpu_sample_path(), epoch_now()
    start = _tick_start(path, read, sleep or time.sleep, now_)  # wall: waits on and times real processes and threads
    end = read()
    if not start or not end or end[1] <= start[1]:
        return None
    try:
        write_json(path, {"at": now_, "ticks": list(end)})
    except OSError as e:
        note_failure("cpu state not written", e)
    return round(float(n * (end[0] - start[0]) / (end[1] - start[1])), 2)


def runq_oversubscribed(runq: Any, runq5: Any, n: Any) -> bool:
    """The run queue sustained over RUNQ_STARVED x the cores: the 1-min and the 5-min load both (a burst is no starving)."""
    return bool(n and runq is not None and runq5 is not None and min(runq, runq5) > RUNQ_STARVED * n)


def cpu_pressure(r: dict[str, Any]) -> list[str]:
    """The CPU's part of laptop_pressure: busy cores past LAPTOP_LOAD x the cores (a reading charged past them), or the
    whole machine's CPU saturated (GOV_SATURATED, user + sys) with a run queue over LAPTOP_LOAD x the cores -- never
    a long run queue alone (46 threads waiting on 18 cores while cores sat idle, 2026-11-19)."""
    factor, n, busy, runq = knob_laptop_load(), r.get("cores"), r.get("load1"), r.get("runq")
    if busy is None or not n:
        return []
    if busy > factor * n:
        return [f"{busy:.1f} cores busy of {n} (holds over {factor * n:g})"]
    if runq_oversubscribed(runq, r.get("runq5"), n):  # 91/118 on 18 cores at 13.6 busy (2026-11-27): bursts of short processes
        return [f"run queue {runq:.0f} (5-min {r['runq5']:.0f}) on {n} cores, {busy:.1f} busy: oversubscribed "
                f"(holds over {RUNQ_STARVED:g}x the cores sustained)"]
    if busy >= GOV_SATURATED * n and runq is not None and runq > factor * n:
        return [f"CPU saturated: {busy:.1f} of {n} cores busy (user + sys), run queue {runq:.0f} (holds at "
                f"{GOV_SATURATED:.0%} busy with a run queue over {factor * n:g})"]
    return []

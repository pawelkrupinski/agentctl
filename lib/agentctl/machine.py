# agentctl, part `machine`: the machine agentctl runs on: its memory, load, cores and free disk (or the fixed machine a test pins)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
import re
from typing import Any, Optional
from agentctl.statefile import read_text_or
from agentctl.core import HOME, probe_output, run_cmd
from agentctl.govknobs import knob_disk_path


def read_meminfo(path: str = "/proc/meminfo") -> dict[str, int]:
    """{field: kB} of Linux's /proc/meminfo ({} elsewhere)."""
    return {m.group(1): int(m.group(2)) for m in re.finditer(r"^(\w+):\s+(\d+)", read_text_or(path, ""), re.M)}


# A FIXED MACHINE ($AGENTCTL_MACHINE, JSON): ram_gb, free_gb, pressure, swap_used_mb, swap_total_mb, load1, cores,
# free_pct, level -- read in place of this host's (sysctl, vm_stat, /proc/meminfo, the load, the CPU count). The tests'
# one seam: tests/harness.py pins an idle 12-core 36 GB machine, so no test depends on the runner it runs on
# (2026-10-31: GitHub's 4-core hosted runner, with its own load and RAM, failed a dozen governor, check-cap and land
# tests that pass on the laptop and on ci-runner). A test of a reader passes its own input or sets its own probe.
def machine_fixed() -> Optional[dict[str, Any]]:
    raw = os.environ.get("AGENTCTL_MACHINE")
    if not raw:
        return None
    try:
        fixed: dict[str, Any] = json.loads(raw)
        return fixed
    except ValueError:
        return None


def machine_load1() -> float:
    m = machine_fixed()
    return float(m["load1"]) if m and m.get("load1") is not None else os.getloadavg()[0]


def run_queues() -> Optional[tuple[float, float]]:
    """The 1- and 5-min load averages (the run queue, sustained), or the fixed machine's `load1` / `load5` (default:
    its load1). None where unreadable."""
    m = machine_fixed()
    if m and m.get("load1") is not None:
        return float(m["load1"]), float(m.get("load5", m["load1"]))
    try:
        la = os.getloadavg()
    except OSError:
        return None
    return la[0], la[1]


def run_queue(passed: Optional[float] = None) -> Optional[float]:
    """The 1-min load average: the run queue, beside the busy cores (a saturated CPU is pressure only when it is
    oversubscribed too). None where unreadable, or when a caller passed its own load (pressure_reading's seam)."""
    if passed is not None:
        return None
    try:
        return machine_load1()
    except OSError:
        return None


def machine_cores() -> int:
    m = machine_fixed()
    return int(m["cores"]) if m and m.get("cores") else (os.cpu_count() or 1)


def _read_macos_memory(out: dict[str, Any]) -> None:
    """Fill `out` from macOS's sysctl and vm_stat, each key left None when unreadable."""
    r = run_cmd(["sysctl", "-n", "hw.memsize"], timeout=10)
    if r.stdout.strip().isdigit():
        out["ram_gb"] = int(r.stdout.strip()) / 2 ** 30
    vm = run_cmd(["vm_stat"], timeout=10).stdout
    page = re.search(r"page size of (\d+) bytes", vm)
    if page:
        pages = sum(int(m.group(2)) for m in re.finditer(r"Pages (free|inactive|speculative):\s+(\d+)", vm))
        out["free_gb"] = pages * int(page.group(1)) / 2 ** 30
    lvl = run_cmd(["sysctl", "-n", "kern.memorystatus_level"], timeout=10).stdout.strip()  # memory_pressure's free %
    if lvl.isdigit():
        out["pressure"] = 100 - int(lvl)
    sw = run_cmd(["sysctl", "-n", "vm.swapusage"], timeout=10).stdout
    tot, used = re.search(r"total = ([\d.]+)M", sw), re.search(r"used = ([\d.]+)M", sw)
    if tot and used:
        out["swap_total_mb"], out["swap_used_mb"] = float(tot.group(1)), float(used.group(1))


def mem_stats() -> dict[str, Any]:
    """THE machine's memory, macOS or Linux alike: {ram_gb, free_gb, pressure (% of RAM not available), swap_used_mb,
    swap_total_mb}, each None when unreadable. macOS: sysctl, vm_stat, memory_pressure; Linux (ci-runner, where a check
    may fall back to running locally): /proc/meminfo (MemAvailable). Before, Linux read none of it: the budget assumed
    36 GB and free memory, pressure and swap were unknown, so the governor admitted blind."""
    out = dict.fromkeys(("ram_gb", "free_gb", "pressure", "swap_used_mb", "swap_total_mb"))
    fixed = machine_fixed()
    if fixed is not None:
        return {k: fixed.get(k) for k in out}
    mi = read_meminfo()
    if mi.get("MemTotal"):
        out["ram_gb"] = mi["MemTotal"] / 2 ** 20
        if "MemAvailable" in mi:
            out["free_gb"] = mi["MemAvailable"] / 2 ** 20
            out["pressure"] = round(100 * (1 - mi["MemAvailable"] / mi["MemTotal"]))
        if "SwapTotal" in mi and "SwapFree" in mi:
            out["swap_total_mb"] = mi["SwapTotal"] / 1024
            out["swap_used_mb"] = (mi["SwapTotal"] - mi["SwapFree"]) / 1024
        return out
    _read_macos_memory(out)
    return out


def ram_gb() -> float:
    """GB of RAM: mem_stats', else the C library's page count (review 2026-11-26: a hard-coded 36 GB, a silently wrong
    budget on any other machine)."""
    return mem_stats()["ram_gb"] or os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2 ** 30


def free_gb() -> Optional[float]:
    """GB of RAM a new heap can have now (free + inactive + speculative pages); $AGENTCTL_GOV_FREE_PROBE's output when
    set (the tests' seam); None when unreadable."""
    probe = os.environ.get("AGENTCTL_GOV_FREE_PROBE")
    if probe:
        try:
            return float(probe_output(probe).strip())
        except ValueError:
            return None
    free: Optional[float] = mem_stats()["free_gb"]
    return free


def disk_free_gb(path: Optional[str] = None) -> float:
    """GB free on path ($AGENTCTL_DISK_PATH, else HOME); the fixed machine's disk_free_gb for the default path."""
    import shutil
    fixed = machine_fixed()
    if path in (None, "", HOME) and not knob_disk_path() and fixed and \
            fixed.get("disk_free_gb") is not None:  # the default path (maybe_upkeep names HOME itself)
        return float(fixed["disk_free_gb"])
    return shutil.disk_usage(path or knob_disk_path() or HOME).free / 1e9


def machine_sample() -> tuple[float, float, Any, Any]:
    """(load1, cpu %, memory pressure %, swap used MB) of this machine, cheaply; None where unreadable."""
    load1 = machine_load1()
    cpu = min(100.0, 100.0 * load1 / machine_cores())
    m = mem_stats()
    return load1, cpu, m["pressure"], m["swap_used_mb"]

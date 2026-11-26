# agentctl, part `pressure`: the laptop's memory pressure and busy cores: sysctl's level, swap growth, ps' busy cores, the pressure report
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import os
import re
import sys
from typing import Any
from agentctl.configfile import env_num
from agentctl.core import attempt, run_cmd
from agentctl.procs import ps_rows
from agentctl.psparse import PsRow, parse_ps
from agentctl.statefile import read_record, write_json
from agentctl.cpu import cpu_pressure, machine_busy_cores
from agentctl.machine import machine_cores, machine_fixed, read_meminfo, run_queue
from agentctl.govtickets import gov_dir
from agentctl.configfile import (
    LAPTOP_SWAP_WINDOW, knob_laptop_free_pct, knob_laptop_pressure_level, knob_laptop_swap_growth_mb,
    knob_laptop_swap_window)
from agentctl.configfile import knob_gov_busy_probe, knob_gov_load_probe, knob_gov_ps_probe, knob_laptop_pressure


GOV_IDLE_CLAUDE = 10.0  # %CPU under which a Claude session is idle (not work)


def gov_busy_cores():
    """The cores the machine is busy on now: its own CPU ticks (cpu.py machine_busy_cores: user + sys), else ps's %CPU
    summed (ps_busy_cores). Seams: $AGENTCTL_GOV_BUSY_PROBE (its output: the number), the tests' $AGENTCTL_GOV_LOAD_PROBE (read as
    busy), $AGENTCTL_GOV_PS_PROBE (ps's `pcpu args` rows)."""
    for cmd in (knob_gov_busy_probe(), knob_gov_load_probe()):
        if cmd:
            return attempt(lambda: float(run_cmd(cmd, shell=True).stdout.strip() or 0), 0.0, catch=(ValueError,))
    probe = knob_gov_ps_probe()
    fixed = machine_fixed() if not probe else None
    if fixed is not None:  # the tests' fixed machine (AGENTCTL_MACHINE): its busy cores, else its load
        return float(fixed.get("busy", fixed.get("load1")) or 0)
    whole = None if probe else machine_busy_cores(machine_cores())
    return whole if whole is not None else ps_busy_cores(probe)


def ps_busy_cores(probe=None):
    """ps's %CPU summed (the machine's own ticks unreadable, or the tests' $AGENTCTL_GOV_PS_PROBE rows), less the
    governor's waiters and idle Claude sessions."""
    cols = ("pcpu", "args")
    rows = parse_ps(run_cmd(probe, shell=True).stdout, cols) if probe else ps_rows(cols)
    return sum(ps_row_percent(row) for row in rows) / 100.0


def ps_row_percent(row: PsRow) -> float:
    """The %CPU a `ps` row counts: 0 for a governor waiter or an idle Claude session."""
    if "agentctl gov admit" in row.args:
        return 0.0
    if os.path.basename(row.args.split()[0]) == "claude" and row.pcpu < GOV_IDLE_CLAUDE:
        return 0.0
    return row.pcpu


PRESSURE_LEVELS = {1: "normal", 2: "warn", 4: "critical"}


def pressure_sysctl():
    """macOS: free %, pressure level and vm.swapusage, one sysctl call ("" elsewhere)."""
    if sys.platform != "darwin":
        return ""
    return run_cmd(["sysctl", "-n", "kern.memorystatus_level", "kern.memorystatus_vm_pressure_level", "vm.swapusage"],
                   timeout=10).stdout


def swap_growth(mb, now_):
    """(MB swap grew since the oldest sample within LAPTOP_SWAP_WINDOW, seconds that spans); this sample is kept."""
    if mb is None:
        return None, 0
    window = knob_laptop_swap_window()
    path = os.path.join(gov_dir(), "swap-samples.json")
    samples = [s for s in (read_record(path) or []) if now_ - window <= s[0] <= now_]
    samples.append([now_, mb])
    try:
        write_json(path, samples)
    except OSError:
        pass
    first = samples[0]
    return mb - first[1], now_ - first[0]


def _meminfo_reading(mi: dict[str, Any]) -> dict[str, Any]:
    """The memory fields (free_pct, swap_mb) a Linux /proc/meminfo dict gives, those it has."""
    got: dict[str, Any] = {}
    if "MemAvailable" in mi:
        got["free_pct"] = round(100 * mi["MemAvailable"] / mi["MemTotal"])
    if "SwapTotal" in mi and "SwapFree" in mi:
        got["swap_mb"] = (mi["SwapTotal"] - mi["SwapFree"]) / 1024
    return got


def _sysctl_reading(text: str) -> dict[str, Any]:
    """The memory fields (free_pct, level, swap_mb) macOS's pressure_sysctl text gives, those it has."""
    lines = text.splitlines()
    got: dict[str, Any] = {}
    if len(lines) >= 2 and lines[0].strip().isdigit() and lines[1].strip().isdigit():
        got["free_pct"], got["level"] = int(lines[0]), int(lines[1])
    used = re.search(r"used = ([\d.]+)M", "\n".join(lines))
    if used:
        got["swap_mb"] = float(used.group(1))
    return got


def _memory_reading(sysctl_out: Any, meminfo: Any) -> dict[str, Any]:
    """The memory fields of this machine: the fixed probe's (the tests'), else /proc/meminfo, else sysctl."""
    fixed = machine_fixed() if meminfo is None and sysctl_out is None else None
    if fixed is not None:
        return {"free_pct": fixed.get("free_pct"), "level": fixed.get("level"), "swap_mb": fixed.get("swap_used_mb")}
    mi = read_meminfo() if meminfo is None else meminfo
    if mi.get("MemTotal"):
        return _meminfo_reading(mi)
    return _sysctl_reading(pressure_sysctl() if sysctl_out is None else sysctl_out)


def pressure_reading(sysctl_out=None, meminfo=None, load1=None, cores=None, now_=None):
    """{free_pct, level, load1, cores, swap_mb, swap_growth_mb, swap_span}: this machine now, None where unreadable.
    The readers are the tests' seams: sysctl_out (pressure_sysctl's text), meminfo (read_meminfo's dict)."""
    now_ = epoch_now() if now_ is None else now_
    # its CPU is the cores processes use (gov_busy_cores), never the load average, which counts threads waiting (46 on
    # 18 cores while 5.2 were busy, 2026-11-19); `load1` passed in (the tests' seam) stands for it
    r = {"free_pct": None, "level": None, "swap_mb": None, "load1": gov_busy_cores() if load1 is None else load1,
         "cores": cores or machine_cores(), "runq": run_queue(passed=load1)}
    r.update(_memory_reading(sysctl_out, meminfo))
    r["swap_growth_mb"], r["swap_span"] = swap_growth(r["swap_mb"], now_)
    return r


def _memory_pressure(r: Any) -> Any:
    """The reasons of a reading's free memory and kernel pressure level, as lines."""
    why = []
    free_pct = knob_laptop_free_pct()
    if r.get("free_pct") is not None and r["free_pct"] < free_pct:
        why.append(f"memory {r['free_pct']}% free (holds under {free_pct:g}%)")
    if r.get("level") is not None and r["level"] >= knob_laptop_pressure_level():
        why.append(f"kernel memory pressure {PRESSURE_LEVELS.get(r['level'], '?')} (level {r['level']})")
    return why


def _swap_pressure(r: Any) -> Any:
    """The reason of a reading's swap growth, as lines."""
    why = []
    bar = knob_laptop_swap_growth_mb()
    if r.get("swap_growth_mb") is not None and r["swap_growth_mb"] >= bar:
        why.append(f"swap growing +{r['swap_growth_mb'] / 1024:.1f} GB in {r['swap_span'] / 60:.0f} min (holds at "
                   f"+{bar / 1024:.1f} GB in {env_num('AGENTCTL_LAPTOP_SWAP_WINDOW', LAPTOP_SWAP_WINDOW) / 60:.0f} min)")
    return why


def laptop_pressure(r: Any = None, load: Any = True) -> Any:
    """Why this machine is under pressure now, with its numbers ("; "-joined), or None. load=False leaves the load out
    (the governor holds each class on its own load factor). $AGENTCTL_LAPTOP_PRESSURE set (the tests'): its value is
    the answer, `-` or empty for none."""
    if r is None and knob_laptop_pressure() is not None:
        return (knob_laptop_pressure() or "").strip("-") or None  # `-` or empty: none
    r = r or pressure_reading()
    why = _memory_pressure(r) + (cpu_pressure(r) if load else []) + _swap_pressure(r)
    return "; ".join(why) or None


def pressure_summary(r=None):
    """Every number of the reading, for a status line."""
    r = r or pressure_reading()
    q = lambda v, f="{}": "?" if v is None else f.format(v)
    lvl = f"{r['level']} ({PRESSURE_LEVELS.get(r['level'], '?')})" if r.get("level") is not None else "?"
    swap = "swap ?" if r.get("swap_mb") is None else (
        f"swap {r['swap_mb'] / 1024:.1f} GB ({round((r.get('swap_growth_mb') or 0) / 1024, 1) + 0.0:+.1f} GB in "
        f"{(r.get('swap_span') or 0) / 60:.0f} min)")
    return f"memory {q(r.get('free_pct'))}% free, level {lvl}, load {q(r.get('load1'), '{:.1f}')}/{r['cores']} cores, {swap}"


def pressure_report():
    """(why or None, summary) of this machine now: the tests' $AGENTCTL_LAPTOP_PRESSURE stands for both."""
    if knob_laptop_pressure() is not None:
        why = laptop_pressure()
        return why, why or "none (test)"
    r = pressure_reading()
    return laptop_pressure(r), pressure_summary(r)

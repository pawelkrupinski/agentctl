# agentctl, part `pressure`: the laptop's memory pressure and busy cores: sysctl's level, swap growth, ps' busy cores, the pressure report
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import sys
import time
from agentctl.core import env_num, run_cmd
from agentctl.statefile import read_record, write_json
from agentctl.cpu import cpu_pressure, machine_busy_cores, run_queue
from agentctl.machine import machine_cores, machine_fixed, read_meminfo
from agentctl.govtickets import gov_dir


GOV_IDLE_CLAUDE = 10.0  # %CPU under which a Claude session is idle (not work)


def gov_busy_cores():
    """The cores the machine is busy on now: its own CPU ticks (cpu.py machine_busy_cores: user + sys), else ps's %CPU
    summed (ps_busy_cores). Seams: $AGENTCTL_GOV_BUSY_PROBE (its output: the number), the tests' $AGENTCTL_GOV_LOAD_PROBE (read as
    busy), $AGENTCTL_GOV_PS_PROBE (ps's `pcpu args` rows)."""
    for k in ("AGENTCTL_GOV_BUSY_PROBE", "AGENTCTL_GOV_LOAD_PROBE"):
        if os.environ.get(k):
            try:
                return float(run_cmd(os.environ[k], shell=True).stdout.strip() or 0)
            except ValueError:
                return 0.0
    probe = os.environ.get("AGENTCTL_GOV_PS_PROBE")
    fixed = machine_fixed() if not probe else None
    if fixed is not None:  # the tests' fixed machine (AGENTCTL_MACHINE): its busy cores, else its load
        return float(fixed.get("busy", fixed.get("load1")) or 0)
    whole = None if probe else machine_busy_cores(machine_cores())
    return whole if whole is not None else ps_busy_cores(probe)


def ps_busy_cores(probe=None):
    """ps's %CPU summed (the machine's own ticks unreadable, or the tests' $AGENTCTL_GOV_PS_PROBE rows), less the
    governor's waiters and idle Claude sessions."""
    out = run_cmd(probe, shell=True).stdout if probe else run_cmd(["ps", "-A", "-o", "pcpu=,args="]).stdout
    total = 0.0
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pct = float(parts[0])
        except ValueError:
            continue
        args = parts[1]
        if "agentctl gov admit" in args:
            continue
        if os.path.basename(args.split()[0]) == "claude" and pct < GOV_IDLE_CLAUDE:
            continue
        total += pct
    return total / 100.0


# THE LAPTOP'S PRESSURE, read as it is NOW. Two movies reds sat `waiting: laptop pressure (swap 17.7 of 19.0 GB used)`
# for want of a fixer (2026-10-31) while memory_pressure said 58% free at load 5: macOS keeps pages swapped out hours
# ago counted as swap USED until they are touched, so swap's level -- as a share (7c3c342 dropped that) or in GB -- is
# no signal. The signals are macOS's free % (kern.memorystatus_level: memory_pressure's "free percentage", one cheap
# sysctl; Linux: MemAvailable of MemTotal), the kernel's own pressure level (kern.memorystatus_vm_pressure_level: 1
# normal, 2 warn, 4 critical), the 1-min load, and swap only while it GROWS (over the last LAPTOP_SWAP_WINDOW, from
# samples kept in <gov>/swap-samples.json). Under pressure the governor holds every class but red (gov_fits): NOTHING
# STOPS A GATE FIX, EVER (the owner, 2026-10-31) -- the red dispatcher never reads it, and a red fix's ticket never
# waits. `agentctl gov status` and `agentctl red status` show the reading with its numbers.
LAPTOP_FREE_PCT = 20          # holds other work while macOS's memory free % is under this


LAPTOP_PRESSURE_LEVEL = 2     # holds other work at this kernel pressure level or over (1 normal, 2 warn, 4 critical)


LAPTOP_SWAP_GROWTH_MB = 1024  # holds other work while swap grew this much (MB) within LAPTOP_SWAP_WINDOW


LAPTOP_SWAP_WINDOW = 600      # seconds of swap samples the growth is read over


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
    window = env_num("AGENTCTL_LAPTOP_SWAP_WINDOW", LAPTOP_SWAP_WINDOW)
    path = os.path.join(gov_dir(), "swap-samples.json")
    samples = [s for s in (read_record(path) or []) if now_ - window <= s[0] <= now_]
    samples.append([now_, mb])
    try:
        write_json(path, samples)
    except OSError:
        pass
    first = samples[0]
    return mb - first[1], now_ - first[0]


def pressure_reading(sysctl_out=None, meminfo=None, load1=None, cores=None, now_=None):
    """{free_pct, level, load1, cores, swap_mb, swap_growth_mb, swap_span}: this machine now, None where unreadable.
    The readers are the tests' seams: sysctl_out (pressure_sysctl's text), meminfo (read_meminfo's dict)."""
    now_ = time.time() if now_ is None else now_
    # its CPU is the cores processes use (gov_busy_cores), never the load average, which counts threads waiting (46 on
    # 18 cores while 5.2 were busy, 2026-11-19); `load1` passed in (the tests' seam) stands for it
    r = {"free_pct": None, "level": None, "swap_mb": None, "load1": gov_busy_cores() if load1 is None else load1,
         "cores": cores or machine_cores(), "runq": run_queue(passed=load1)}
    fixed = machine_fixed() if meminfo is None and sysctl_out is None else None
    mi = {} if fixed is not None else read_meminfo() if meminfo is None else meminfo
    if fixed is not None:
        r["free_pct"], r["level"], r["swap_mb"] = fixed.get("free_pct"), fixed.get("level"), fixed.get("swap_used_mb")
    elif mi.get("MemTotal"):
        if "MemAvailable" in mi:
            r["free_pct"] = round(100 * mi["MemAvailable"] / mi["MemTotal"])
        if "SwapTotal" in mi and "SwapFree" in mi:
            r["swap_mb"] = (mi["SwapTotal"] - mi["SwapFree"]) / 1024
    else:
        lines = (pressure_sysctl() if sysctl_out is None else sysctl_out).splitlines()
        if len(lines) >= 2 and lines[0].strip().isdigit() and lines[1].strip().isdigit():
            r["free_pct"], r["level"] = int(lines[0]), int(lines[1])
        used = re.search(r"used = ([\d.]+)M", "\n".join(lines))
        if used:
            r["swap_mb"] = float(used.group(1))
    r["swap_growth_mb"], r["swap_span"] = swap_growth(r["swap_mb"], now_)
    return r


def laptop_pressure(r=None, load=True):
    """Why this machine is under pressure now, with its numbers ("; "-joined), or None. load=False leaves the load out
    (the governor holds each class on its own load factor). $AGENTCTL_LAPTOP_PRESSURE set (the tests'): its value is
    the answer, `-` or empty for none."""
    if r is None and "AGENTCTL_LAPTOP_PRESSURE" in os.environ:
        return os.environ["AGENTCTL_LAPTOP_PRESSURE"].strip("-") or None  # `-` or empty: none
    r = r or pressure_reading()
    why = []
    free_pct = env_num("AGENTCTL_LAPTOP_FREE_PCT", LAPTOP_FREE_PCT)
    if r.get("free_pct") is not None and r["free_pct"] < free_pct:
        why.append(f"memory {r['free_pct']}% free (holds under {free_pct:g}%)")
    if r.get("level") is not None and r["level"] >= env_num("AGENTCTL_LAPTOP_PRESSURE_LEVEL", LAPTOP_PRESSURE_LEVEL):
        why.append(f"kernel memory pressure {PRESSURE_LEVELS.get(r['level'], '?')} (level {r['level']})")
    why += cpu_pressure(r) if load else []
    bar = env_num("AGENTCTL_LAPTOP_SWAP_GROWTH_MB", LAPTOP_SWAP_GROWTH_MB)
    if r.get("swap_growth_mb") is not None and r["swap_growth_mb"] >= bar:
        why.append(f"swap growing +{r['swap_growth_mb'] / 1024:.1f} GB in {r['swap_span'] / 60:.0f} min (holds at "
                   f"+{bar / 1024:.1f} GB in {env_num('AGENTCTL_LAPTOP_SWAP_WINDOW', LAPTOP_SWAP_WINDOW) / 60:.0f} min)")
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
    if "AGENTCTL_LAPTOP_PRESSURE" in os.environ:
        why = laptop_pressure()
        return why, why or "none (test)"
    r = pressure_reading()
    return laptop_pressure(r), pressure_summary(r)

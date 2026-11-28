# agentctl, part `pressure`: the laptop's memory pressure and busy cores: sysctl's level, swap growth, ps' busy cores, the pressure report
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import os
from typing import Any, Optional
from agentctl.psparse import PsRow
from agentctl.statefile import read_record, write_json, note_failure
from agentctl.cpu import cpu_pressure
from agentctl.machine import MachineProbe
from agentctl.govtickets import gov_dir
from agentctl.govknobs import LAPTOP_SWAP_WINDOW, knob_laptop_free_pct, knob_laptop_pressure_level, knob_laptop_swap_growth_mb, knob_laptop_swap_window
from agentctl.govknobs import knob_laptop_pressure


GOV_IDLE_CLAUDE = 10.0  # %CPU under which a Claude session is idle (not work)


def gov_busy_cores(machine: MachineProbe) -> float:
    """The cores the machine is busy on now: its own CPU ticks (cpu.py machine_busy_cores: user + sys), else ps's %CPU
    summed (ps_busy_cores)."""
    whole = machine.tick_busy_cores()
    return whole if whole is not None else ps_busy_cores(machine.process_rows(("pcpu", "args")))


def ps_busy_cores(rows: list[PsRow]) -> float:
    """ps's %CPU summed (the machine's own ticks unreadable), less the governor's waiters and idle Claude sessions."""
    return sum(ps_row_percent(row) for row in rows) / 100.0


def ps_row_percent(row: PsRow) -> float:
    """The %CPU a `ps` row counts: 0 for a governor waiter or an idle Claude session."""
    if "agentctl gov admit" in row.args:
        return 0.0
    if os.path.basename(row.args.split()[0]) == "claude" and row.pcpu < GOV_IDLE_CLAUDE:
        return 0.0
    return row.pcpu


PRESSURE_LEVELS = {1: "normal", 2: "warn", 4: "critical"}


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
    except OSError as e:
        note_failure("pressure sample not written", e)
    first = samples[0]
    return mb - first[1], now_ - first[0]


def pressure_reading(machine: MachineProbe, now_: Optional[float] = None) -> dict[str, Any]:
    """{free_pct, level, load1, cores, runq, runq5, swap_mb, swap_growth_mb, swap_span}: this machine now, None where
    unreadable."""
    now_ = epoch_now() if now_ is None else now_
    # its CPU is the cores processes use (gov_busy_cores), never the load average, which counts threads waiting (46 on
    # 18 cores while 5.2 were busy, 2026-11-19)
    queues = machine.load_averages()
    r: dict[str, Any] = {"free_pct": None, "level": None, "swap_mb": None, "load1": gov_busy_cores(machine),
                         "cores": machine.cores(), "runq": queues[0] if queues else None,
                         "runq5": queues[1] if queues else None}  # the run queue sustained: the 5-min load (cpu_pressure)
    r.update({k: v for k, v in machine.pressure_memory().items() if k in ("free_pct", "level", "swap_mb")})
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
                   f"+{bar / 1024:.1f} GB in {knob_laptop_swap_window() / 60:.0f} min)")
    return why


def laptop_pressure(r: Optional[dict[str, Any]], load: bool = True) -> Optional[str]:
    """Why this machine is under pressure now (reading `r`), with its numbers ("; "-joined), or None. load=False leaves
    the load out (the governor holds each class on its own load factor). $AGENTCTL_LAPTOP_PRESSURE set (the tests'),
    with no reading: its value is the answer, `-` or empty for none."""
    if r is None:
        return (knob_laptop_pressure() or "").strip("-") or None  # `-` or empty: none
    why = _memory_pressure(r) + (cpu_pressure(r) if load else []) + _swap_pressure(r)
    return "; ".join(why) or None


def pressure_summary(r):
    """Every number of the reading, for a status line."""
    q = lambda v, f="{}": "?" if v is None else f.format(v)
    lvl = f"{r['level']} ({PRESSURE_LEVELS.get(r['level'], '?')})" if r.get("level") is not None else "?"
    swap = "swap ?" if r.get("swap_mb") is None else (
        f"swap {r['swap_mb'] / 1024:.1f} GB ({round((r.get('swap_growth_mb') or 0) / 1024, 1) + 0.0:+.1f} GB in "
        f"{(r.get('swap_span') or 0) / 60:.0f} min)")
    queue = "" if r.get("runq") is None else f", load {r['runq']:.0f}/{r['cores']} (run queue, 5-min {'?' if r.get('runq5') is None else format(r['runq5'], '.0f')})"
    return f"memory {q(r.get('free_pct'))}% free, level {lvl}, busy {q(r.get('load1'), '{:.1f}')}/{r['cores']} cores{queue}, {swap}"


def pressure_report(machine: MachineProbe):
    """(why or None, summary) of this machine now: the tests' $AGENTCTL_LAPTOP_PRESSURE stands for both."""
    if knob_laptop_pressure() is not None:
        why = laptop_pressure(None)
        return why, why or "none (test)"
    r = pressure_reading(machine)
    return laptop_pressure(r), pressure_summary(r)

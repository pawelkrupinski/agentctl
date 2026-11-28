# agentctl, part `checkslot`: a check slot: its lease (Slot, check_slot), a check's record, and the slot cap adjusted to the machine's memory pressure
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.govpool import WIDTH_ENV
from agentctl.statefile import write_json
from agentctl.clock import epoch_now
from agentctl.timefmt import ts
from agentctl.govknobs import knob_gov_admit_max, knob_max_checks
from agentctl.core import print_error
from agentctl.inboxentry import inbox_write
from agentctl.govbudget import (
    CHECKS_DEFAULT_CAP, GOV_MEM_GB, cap_state, check_runs, checks_cap, checks_dir, gov_max_checks, slot_holders)
from agentctl.govusage import GOV_JOB_CORES
from agentctl.gov import gov_admit
from agentctl.percentile import pct
import contextlib
import json
import os
import time
from typing import Any


class Slot:
    def __init__(self, label, cls="check", *, machine):
        self.machine = machine
        self.label, self.cls, self.ticket, self.waited, self.t0 = label, cls, None, 0.0, None
        self.samples, self.running, self.cap, self._last_sample = [], 0, 0, 0.0
        self.rc, self.out = None, ""  # the check's result: set by done(); None while it runs

    def sample(self, force=False):
        if force or time.monotonic() - self._last_sample >= 30:  # wall: waits on and times real processes and threads
            self._last_sample = time.monotonic()  # wall: waits on and times real processes and threads
            try:
                self.samples.append(self.machine.sample())
            except (OSError, ValueError) as e:  # a reading the machine would not give: the check goes on without it
                print_error(f"could not sample the machine for a check: {e}")

    def done(self, rc, out):
        self.rc, self.out = rc, out

    def env(self, env):
        """The hook's environment: its sbt JVM is admitted at this check's priority, and its sbt-warm calls take no cores
        ticket of their own (KINOWO_GOV_CPU_HELD): this check's ticket is charged the hook's cores already. So is every
        parallel runner in it: THE GOVERNED FAN-OUT (govpool.py, movies' scripts/gov-parallel) takes no ticket under
        $AGENTCTL_GOV_WIDTH and runs at most the cores this ticket is charged."""
        cores = int(float((self.ticket.rec if self.ticket else {}).get("cores") or GOV_JOB_CORES["check"]))
        return dict(env, KINOWO_GOV_CLASS=self.cls, KINOWO_GOV_CPU_HELD="1", **{WIDTH_ENV: str(max(1, cores))})


@contextlib.contextmanager
def check_slot(repo, label, cls="check", *, machine):
    """Hold a check ticket of the governor for a pre-commit check run (waiting, with a line saying why, until it is
    admitted). Records the run in runs.jsonl and adjusts the cap afterwards."""
    slot = Slot(label, cls, machine=machine)
    slot.ticket = gov_admit(cls, GOV_MEM_GB["check"], label, kind="check",
                            max_wait=knob_gov_admit_max(), machine=machine)
    slot.waited, slot.cap = slot.ticket.waited, checks_cap(machine.ram_gb())
    slot.running = len(slot_holders())
    slot.t0, slot.rc = time.monotonic(), None  # wall: waits on and times real processes and threads
    slot.sample(force=True)
    try:
        yield slot
    finally:
        slot.sample(force=True)
        record_check(slot, time.monotonic() - slot.t0)  # wall: waits on and times real processes and threads
        slot.ticket.release()
        try:
            maybe_adjust_cap(machine.ram_gb())
        except (OSError, ValueError, KeyError) as e:  # the cap's files unreadable: kept as it was
            print_error(f"could not adjust the check cap: {e}")


def record_check(slot, duration):
    def mx(i):
        vals = [x[i] for x in slot.samples if x and x[i] is not None]
        return max(vals) if vals else None
    rec = {"at": epoch_now(), "label": slot.label, "duration": round(duration, 1), "waited": round(slot.waited, 1),
           "cap": slot.cap, "running": slot.running, "rc": slot.rc, "load1": mx(0), "cpu": mx(1), "pressure": mx(2),
           "swap_mb": mx(3), "swap_mb_start": slot.samples[0][3] if slot.samples and slot.samples[0] else None}
    with open(os.path.join(checks_dir(), "runs.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")


SLOT_WAIT_COMMON = 0.3        # share of the day's runs that waited over a minute for a slot


PRESSURE_LOW, PRESSURE_HIGH = 50, 75


SWAP_GROWTH_HIGH_MB = 512


def adjust_cap(runs: Any, cap: Any, max_cap: Any, last_change: Any, now: Any) -> Any:
    """(new cap, reason) from the last day's check runs, or (cap, None). At most one change a day. Lower by 1 (to 1)
    when memory pressure or swap rises, or when p90 duration at the current level is over 1.5x the p90 at the level
    below; raise by 1 (to max_cap) when slot waits are common and memory pressure and swap stay low."""
    if now - (last_change or 0) < 86400:
        return cap, None
    day = [r for r in runs if r.get("at", 0) >= now - 86400]
    if not day:
        return cap, None
    return CapReview(runs, day, cap, max_cap).verdict()


class CapReview:
    """The last day's check runs (`day`, of all the `runs`) read for the cap: strain, a slowdown, slot waits."""

    def __init__(self, runs: Any, day: Any, cap: Any, max_cap: Any) -> None:
        self.runs, self.day, self.cap, self.max_cap = runs, day, cap, max_cap
        self.pressures = [r["pressure"] for r in day if r.get("pressure") is not None]
        self.growth = [(r["swap_mb"] or 0) - (r.get("swap_mb_start") or 0) for r in day if r.get("swap_mb") is not None]

    def verdict(self) -> Any:
        cap = self.cap
        if self.strained():
            return (cap - 1, self.strain_reason()) if cap > 1 else (cap, None)
        slower = self.slowdown()
        if slower:
            return cap - 1, slower
        waits = self.slot_waits()
        return (cap + 1, waits) if waits else (cap, None)

    def strained(self) -> Any:
        """Memory pressure or swap growth at their high marks during a check."""
        return bool((self.pressures and max(self.pressures) >= PRESSURE_HIGH) or (self.growth and max(self.growth) >= SWAP_GROWTH_HIGH_MB))

    def strain_reason(self) -> Any:
        return (f"memory pressure up to {max(self.pressures) if self.pressures else '?'}% or swap growing up to "
                f"{int(max(self.growth)) if self.growth else 0} MB during a check")

    def slowdown(self) -> Any:
        """Why the cap is too high: p90 duration with `cap` running is over 1.5x the p90 with one fewer; else None."""
        cap = self.cap
        at = [r["duration"] for r in self.runs if r.get("running") == cap]
        below = [r["duration"] for r in self.runs if r.get("running") == cap - 1]
        if cap > 1 and len(at) >= 5 and len(below) >= 5 and pct(at, 0.9) > 1.5 * pct(below, 0.9):
            return f"p90 check duration with {cap} running is {pct(at, 0.9):.0f}s, over 1.5x the {pct(below, 0.9):.0f}s with {cap - 1}"
        return None

    def calm(self) -> Any:
        """Memory pressure and swap growth both low."""
        return (not self.pressures or max(self.pressures) < PRESSURE_LOW) and (not self.growth or max(self.growth) < 64)

    def slot_waits(self) -> Any:
        """Why the cap is too low: slot waits are common while the machine stays calm; else None."""
        day = self.day
        waited = [r for r in day if r.get("waited", 0) > 60]
        if self.cap < self.max_cap and len(day) >= 5 and len(waited) / len(day) >= SLOT_WAIT_COMMON and self.calm():
            return (f"{len(waited)} of {len(day)} checks today waited over a minute for a slot, memory pressure "
                    f"under {PRESSURE_LOW}% and swap steady")
        return None


def maybe_adjust_cap(ram_gb: float, now=None):
    if knob_max_checks():
        return None
    now = epoch_now() if now is None else now
    st = cap_state()
    cap = int(st.get("cap", CHECKS_DEFAULT_CAP))
    new, why = adjust_cap(check_runs(7, now), cap, gov_max_checks(ram_gb), st.get("changed_at"), now)
    if why is None or new == cap:
        return None
    st = {"cap": new, "changed_at": now, "reason": why, "previous": cap}
    write_json(os.path.join(checks_dir(), "cap.json"), st, indent=None)
    line = f"CHECK CAP {cap} -> {new}: {why}"
    with open(os.path.join(checks_dir(), "adjust.log"), "a") as f:
        f.write(f"{ts()} {line}\n")
    inbox_write("checks", line, [line], [], 0)
    return line

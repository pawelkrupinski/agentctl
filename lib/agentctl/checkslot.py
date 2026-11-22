# agentctl, part `checkslot`: a check slot: its lease (Slot, check_slot), a check's record, and the slot cap adjusted to the machine's memory pressure
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import contextlib
import json
import os
import time
from agentctl.core import env_num, err, pct
from agentctl.timefmt import ts
from agentctl.statefile import write_json
from agentctl.inboxentry import inbox_write
from agentctl.machine import machine_sample
from agentctl.govbudget import (
    CHECKS_DEFAULT_CAP, GOV_MEM_GB, cap_state, check_runs, checks_cap, checks_dir, gov_max_checks, slot_holders)
from agentctl.gov import GOV_ADMIT_MAX, gov_admit


class Slot:
    def __init__(self, label, cls="check"):
        self.label, self.cls, self.ticket, self.waited, self.t0 = label, cls, None, 0.0, None
        self.samples, self.running, self.cap, self._last_sample = [], 0, 0, 0.0

    def sample(self, force=False):
        if force or time.monotonic() - self._last_sample >= 30:
            self._last_sample = time.monotonic()
            try:
                self.samples.append(machine_sample())
            except Exception:
                pass

    def done(self, rc, out):
        self.rc, self.out = rc, out

    def env(self, env):
        """The hook's environment: its sbt JVM is admitted at this check's priority, and its sbt-warm calls take no cores
        ticket of their own (KINOWO_GOV_CPU_HELD): this check's ticket is charged the hook's cores already."""
        return dict(env, KINOWO_GOV_CLASS=self.cls, KINOWO_GOV_CPU_HELD="1")


@contextlib.contextmanager
def check_slot(repo, label, cls="check"):
    """Hold a check ticket of the governor for a pre-commit check run (waiting, with a line saying why, until it is
    admitted). Records the run in runs.jsonl and adjusts the cap afterwards."""
    slot = Slot(label, cls)
    slot.ticket = gov_admit(cls, GOV_MEM_GB["check"], label, kind="check",
                            max_wait=env_num("AGENTCTL_GOV_ADMIT_MAX", GOV_ADMIT_MAX))
    slot.waited, slot.cap = slot.ticket.waited, checks_cap()
    slot.running = len(slot_holders())
    slot.t0, slot.rc = time.monotonic(), None
    slot.sample(force=True)
    try:
        yield slot
    finally:
        slot.sample(force=True)
        record_check(slot, time.monotonic() - slot.t0)
        slot.ticket.release()
        try:
            maybe_adjust_cap()
        except Exception as e:
            err(f"could not adjust the check cap: {e}")


def record_check(slot, duration):
    def mx(i):
        vals = [x[i] for x in slot.samples if x and x[i] is not None]
        return max(vals) if vals else None
    rec = {"at": time.time(), "label": slot.label, "duration": round(duration, 1), "waited": round(slot.waited, 1),
           "cap": slot.cap, "running": slot.running, "rc": slot.rc, "load1": mx(0), "cpu": mx(1), "pressure": mx(2),
           "swap_mb": mx(3), "swap_mb_start": slot.samples[0][3] if slot.samples and slot.samples[0] else None}
    with open(os.path.join(checks_dir(), "runs.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")


SLOT_WAIT_COMMON = 0.3        # share of the day's runs that waited over a minute for a slot


PRESSURE_LOW, PRESSURE_HIGH = 50, 75


SWAP_GROWTH_HIGH_MB = 512


def adjust_cap(runs, cap, max_cap, last_change, now):
    """(new cap, reason) from the last day's check runs, or (cap, None). At most one change a day. Lower by 1 (to 1)
    when memory pressure or swap rises, or when p90 duration at the current level is over 1.5x the p90 at the level
    below; raise by 1 (to max_cap) when slot waits are common and memory pressure and swap stay low."""
    if now - (last_change or 0) < 86400:
        return cap, None
    day = [r for r in runs if r.get("at", 0) >= now - 86400]
    if not day:
        return cap, None
    pressures = [r["pressure"] for r in day if r.get("pressure") is not None]
    growth = [(r["swap_mb"] or 0) - (r.get("swap_mb_start") or 0) for r in day if r.get("swap_mb") is not None]
    if (pressures and max(pressures) >= PRESSURE_HIGH) or (growth and max(growth) >= SWAP_GROWTH_HIGH_MB):
        if cap > 1:
            return cap - 1, (f"memory pressure up to {max(pressures) if pressures else '?'}% or swap growing up to "
                             f"{int(max(growth)) if growth else 0} MB during a check")
        return cap, None
    at = [r["duration"] for r in runs if r.get("running") == cap]
    below = [r["duration"] for r in runs if r.get("running") == cap - 1]
    if cap > 1 and len(at) >= 5 and len(below) >= 5 and pct(at, 0.9) > 1.5 * pct(below, 0.9):
        return cap - 1, f"p90 check duration with {cap} running is {pct(at, 0.9):.0f}s, over 1.5x the {pct(below, 0.9):.0f}s with {cap - 1}"
    waited = [r for r in day if r.get("waited", 0) > 60]
    if (cap < max_cap and len(day) >= 5 and len(waited) / len(day) >= SLOT_WAIT_COMMON
            and (not pressures or max(pressures) < PRESSURE_LOW) and (not growth or max(growth) < 64)):
        return cap + 1, (f"{len(waited)} of {len(day)} checks today waited over a minute for a slot, memory pressure "
                         f"under {PRESSURE_LOW}% and swap steady")
    return cap, None


def maybe_adjust_cap(now=None):
    if os.environ.get("AGENTCTL_MAX_CHECKS"):
        return None
    now = now or time.time()
    st = cap_state()
    cap = int(st.get("cap", CHECKS_DEFAULT_CAP))
    new, why = adjust_cap(check_runs(7, now), cap, gov_max_checks(), st.get("changed_at"), now)
    if why is None or new == cap:
        return None
    st = {"cap": new, "changed_at": now, "reason": why, "previous": cap}
    write_json(os.path.join(checks_dir(), "cap.json"), st, indent=None)
    line = f"CHECK CAP {cap} -> {new}: {why}"
    with open(os.path.join(checks_dir(), "adjust.log"), "a") as f:
        f.write(f"{ts()} {line}\n")
    inbox_write("checks", line, [line], [], 0)
    return line

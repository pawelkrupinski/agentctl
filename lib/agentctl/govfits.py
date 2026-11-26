# agentctl, part `govfits`: the governor's admission test: the capacity (starved, ok, spare) and the ordered refusals a class of work meets before it may start
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from contextlib import suppress
from typing import Any
from agentctl.statefile import read_record, write_json
from agentctl.clock import wall_epoch
from agentctl.knobs import env_num
from agentctl.cpu import GOV_SATURATED
from agentctl.machine import machine_cores
from agentctl.govtickets import gov_dir, gov_log, gov_tickets
from agentctl.govbudget import checks_cap, gov_budget
from agentctl.pressure import gov_busy_cores
from agentctl.knobs import GOV_CPU_FACTOR, knob_gov_cpu_factor


SUITE_CAP = 1  # agentctl's own test-suite runs at once ($AGENTCTL_SUITE_CAP): parallel agents queue, never pile up
GOV_RAMP = 120          # seconds a new ticket's JVM is still growing into its expected memory
GOV_RESERVE_GB = 1.0
GOV_FREE_NEED_GB = 2.5  # free memory a new JVM needs NOW (beyond the reserve); its heap grows into the budget's share later
GOV_PRESSURE_DEFAULT = 75  # memory pressure (100 - free %) at which every class but red waits


# THE CAPACITY (the owner, 2026-11-19: "if cpu is starved then pause until we have enough resources and resume when we
# have too much"): BUSY is max(the live tickets' charged cores, the cores processes use now -- ps's %CPU, less the
# governor's own waiters and idle Claude sessions; never the load average, which read 46 on 18 cores while processes
# used 5.2). Over GOV_STARVED x the cores the laptop is STARVED: every class but red pauses, its waiters blocking with no
# CPU; under GOV_RESUME x it is ok again (between the two it keeps its state: no thrashing at one line); under GOV_SPARE x
# it has SPARE capacity. `agentctl gov capacity` prints it in one line.
GOV_STARVED = 1.0       # x the cores: over it, starved ($AGENTCTL_GOV_STARVED)
GOV_RESUME = 0.8        # x the cores: under it, admitting again ($AGENTCTL_GOV_RESUME)
GOV_SPARE = 0.5         # x the cores: under it, spare ($AGENTCTL_GOV_SPARE)


def gov_committed(tickets):
    return sum(float(t.get("mem") or 0) for t in tickets)


def gov_charged(tickets):
    """The cores the live tickets are charged (a ticket from before cores were charged: none)."""
    return sum(float(t.get("cores") or 0) for t in tickets)


def gov_ramp(tickets, now=None):
    now = now or wall_epoch()
    return sum(float(t.get("mem") or 0) for t in tickets if now - t.get("at", 0) < GOV_RAMP)


def gov_starved(busy: float, measured: float, n: int) -> bool:
    """Charged or busy past GOV_STARVED x the cores, or the whole machine's CPU saturated (GOV_SATURATED: a tick
    measure never reads past the cores)."""
    return busy > env_num("AGENTCTL_GOV_STARVED", GOV_STARVED) * n or measured >= GOV_SATURATED * n


def _capacity_state(busy: Any, measured: Any, n: Any, prev: Any) -> Any:
    """starved | spare | ok for busy of n cores; between GOV_RESUME and GOV_STARVED the previous state is kept."""
    if gov_starved(busy, measured, n):
        return "starved"
    if busy < env_num("AGENTCTL_GOV_SPARE", GOV_SPARE) * n:
        return "spare"
    if busy < env_num("AGENTCTL_GOV_RESUME", GOV_RESUME) * n:
        return "ok"
    return "starved" if prev == "starved" else "ok"


def gov_capacity(tickets=None, m=None):
    """{state: starved | ok | spare, busy, charged, measured, cores, load1}: the laptop's CPU now, with hysteresis (the
    last state kept in <gov>/capacity.json between GOV_RESUME and GOV_STARVED)."""
    tickets = gov_tickets() if tickets is None else tickets
    n = (m or {}).get("cores") or machine_cores()
    charged, measured = gov_charged(tickets), gov_busy_cores()
    busy = max(charged, measured)
    path = os.path.join(gov_dir(), "capacity.json")
    prev = (read_record(path) or {}).get("state", "ok")
    state = _capacity_state(busy, measured, n, prev)
    if state != prev:
        with suppress(OSError):
            write_json(path, {"state": state, "busy": busy, "at": wall_epoch()})
            gov_log(f"capacity {prev} -> {state}: {busy:.1f} of {n} cores busy ({charged:g} charged, {measured:.1f} "
                    "measured)")
    return {"state": state, "busy": busy, "charged": charged, "measured": measured, "cores": n,
            "load1": (m or {}).get("load1")}


def _suite_refusal(cls: Any, mem: Any, tickets: Any, m: Any, kind: Any, cores: Any) -> Any:
    """agentctl's own test suite (`agentctl suite`): at most SUITE_CAP at once."""
    if cls != "suite":
        return None
    suites = sum(1 for t in tickets if t.get("class") == "suite")
    if suites >= env_num("AGENTCTL_SUITE_CAP", SUITE_CAP):
        return False, f"{suites}/{env_num('AGENTCTL_SUITE_CAP', SUITE_CAP):g} agentctl suite run(s) going (the suite cap)", False
    return None


def _check_cap_refusal(cls: Any, mem: Any, tickets: Any, m: Any, kind: Any, cores: Any) -> Any:
    """Checks (kind check): at most checks_cap() at once."""
    if kind != "check":
        return None
    checks = sum(1 for t in tickets if t.get("kind") == "check")
    if checks >= checks_cap():
        return False, f"{checks}/{checks_cap()} checks running (the check cap)", False
    return None


def _budget_refusal(cls: Any, mem: Any, tickets: Any, m: Any, kind: Any, cores: Any) -> Any:
    """The tickets' memory plus this work's over the budget."""
    budget = gov_budget()
    committed = gov_committed(tickets)
    if committed + mem > budget:
        return False, f"{committed:.0f}+{mem:.0f} GB over the {budget:.0f} GB budget", True
    return None


def _free_memory_refusal(cls: Any, mem: Any, tickets: Any, m: Any, kind: Any, cores: Any) -> Any:
    """Less free memory than the work needs now (beyond the reserve and what the new tickets are still growing into)."""
    if m.get("free") is None:
        return None
    room = m["free"] - env_num("AGENTCTL_GOV_RESERVE_GB", GOV_RESERVE_GB) - gov_ramp(tickets)
    need = min(mem, env_num("AGENTCTL_GOV_FREE_NEED_GB", GOV_FREE_NEED_GB))
    if need > room:
        return False, (f"{m['free']:.1f} GB free, {gov_ramp(tickets):.0f} GB still growing: {max(room, 0):.1f} GB "
                       f"for the {need:g} GB it needs now"), True
    return None


def _pressure_refusal(cls: Any, mem: Any, tickets: Any, m: Any, kind: Any, cores: Any) -> Any:
    """Memory pressure at GOV_PRESSURE_DEFAULT, or the laptop's own pressure reading."""
    stop = GOV_PRESSURE_DEFAULT
    if m.get("pressure") is not None and m["pressure"] >= stop:
        return False, f"memory pressure {m['pressure']:.0f}% (waits at {stop}%)", True
    if m.get("laptop"):
        return False, f"laptop pressure: {m['laptop']}", True
    return None


def _cpu_refusal(cls: Any, mem: Any, tickets: Any, m: Any, kind: Any, cores: Any) -> Any:
    """The cores: PAUSED while starved (gov_capacity: resumes by itself, no one acts), else held while the cores it asks
    for would pass the load factor."""
    cap = gov_capacity(tickets, m)
    if cap["state"] == "starved":
        return False, (f"starved: {cap['busy']:.1f} of {cap['cores']} cores busy (pauses over "
                       f"{env_num('AGENTCTL_GOV_STARVED', GOV_STARVED):g}x, resumes under "
                       f"{env_num('AGENTCTL_GOV_RESUME', GOV_RESUME):g}x)"), False
    if cores and cap["charged"] > 0:  # nothing charged: the head goes whatever it asks (never starved by its own size)
        limit = knob_gov_cpu_factor() * cap["cores"]
        if cap["busy"] + cores > limit:
            return False, (f"cores: {cap['charged']:g} charged ({cap['measured']:.1f} measured, {cap['busy']:.1f} busy) "
                           f"+ {cores:g} over {limit:g} ({env_num('AGENTCTL_GOV_CPU_FACTOR', GOV_CPU_FACTOR):g}x "
                           f"{cap['cores']} cores)"), False
    return None


# the admission checks, in the order they judge: the first refusal is the answer
GOV_REFUSALS = (_suite_refusal, _check_cap_refusal, _budget_refusal, _free_memory_refusal, _pressure_refusal, _cpu_refusal)


def gov_fits(cls: Any, mem: Any, tickets: Any, m: Any, kind: Any = "job", cores: Any = 0.0) -> Any:
    """(True, None, False) when work of class cls expecting `mem` GB and `cores` cores may start now; else (False, why,
    on_memory). Red: always."""
    if cls == "red":  # nothing stops a gate fix, ever
        return True, None, False
    for refusal in GOV_REFUSALS:
        verdict = refusal(cls, mem, tickets, m, kind, cores)
        if verdict:
            return verdict
    return True, None, False

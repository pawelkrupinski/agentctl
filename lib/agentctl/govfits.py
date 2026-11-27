# agentctl, part `govfits`: the governor's admission test: the capacity (starved, ok, spare) and the ordered refusals a class of work meets before it may start
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from contextlib import suppress
from typing import Any
from agentctl.statefile import read_record, write_json
from agentctl.clock import wall_epoch
from agentctl.cpu import GOV_SATURATED, RUNQ_STARVED, runq_oversubscribed
from agentctl.machine import machine_cores
from agentctl.govtickets import gov_dir, gov_log, gov_tickets
from agentctl.govbudget import checks_cap, gov_budget
from agentctl.pressure import gov_busy_cores
from agentctl.govknobs import RED_STARVED_CAP, knob_gov_cpu_factor, knob_laptop_load
from agentctl.govknobs import (
    knob_gov_pool_share, knob_gov_free_need_gb, knob_gov_reserve_gb, knob_gov_resume, knob_gov_spare, knob_gov_starved, knob_suite_cap)


GOV_RAMP = 120          # seconds a new ticket's JVM is still growing into its expected memory
GOV_PRESSURE_DEFAULT = 75  # memory pressure (100 - free %) at which every class but red waits


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
    return busy > knob_gov_starved() * n or measured >= GOV_SATURATED * n


def gov_queue_starved(queues: Any, n: Any, prev: Any) -> bool:
    """The run queue (1-min, 5-min) sustained over RUNQ_STARVED x the cores, and once starved on it, until the 1-min
    falls under LAPTOP_LOAD x the cores (2026-11-27: 91/118/120 on 18 cores while 13.6 read busy, 8 tickets admitted)."""
    if not queues:
        return False
    return runq_oversubscribed(queues[0], queues[1], n) or (prev == "starved" and queues[0] > knob_laptop_load() * n)


def _capacity_state(busy: Any, measured: Any, n: Any, prev: Any, queues: Any = None) -> Any:
    """starved | spare | ok for busy of n cores; between GOV_RESUME and GOV_STARVED the previous state is kept."""
    if gov_starved(busy, measured, n) or gov_queue_starved(queues, n, prev):
        return "starved"
    if busy < knob_gov_spare() * n:
        return "spare"
    if busy < knob_gov_resume() * n:
        return "ok"
    return "starved" if prev == "starved" else "ok"


def gov_capacity(tickets: list[dict[str, Any]] | None = None, m: dict[str, Any] | None = None) -> dict[str, Any]:
    """{state: starved | ok | spare, busy, charged, measured, cores, load1}: the laptop's CPU now, with hysteresis (the
    last state kept in <gov>/capacity.json between GOV_RESUME and GOV_STARVED)."""
    tickets = gov_tickets() if tickets is None else tickets
    n = (m or {}).get("cores") or machine_cores()
    charged, measured = gov_charged(tickets), gov_busy_cores()
    busy = max(charged, measured)
    path = os.path.join(gov_dir(), "capacity.json")
    prev = (read_record(path) or {}).get("state", "ok")
    queues = (m or {}).get("queues")  # the run queue the caller read (gov_machine): no ambient read here
    state = _capacity_state(busy, measured, n, prev, queues)
    if state != prev:
        with suppress(OSError):
            write_json(path, {"state": state, "busy": busy, "at": wall_epoch()})
            gov_log(f"capacity {prev} -> {state}: {busy:.1f} of {n} cores busy ({charged:g} charged, {measured:.1f} "
                    "measured)")
    return {"state": state, "busy": busy, "charged": charged, "measured": measured, "cores": n,
            "queues": queues, "load1": (m or {}).get("load1")}


def _suite_refusal(cls: Any, mem: Any, tickets: Any, m: Any, kind: Any, cores: Any) -> Any:
    """agentctl's own test suite (`agentctl suite`): at most SUITE_CAP at once."""
    if cls != "suite":
        return None
    suites = sum(1 for t in tickets if t.get("class") == "suite")
    if suites >= knob_suite_cap():
        return False, f"{suites}/{knob_suite_cap():g} agentctl suite run(s) going (the suite cap)", False
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
    room = m["free"] - knob_gov_reserve_gb() - gov_ramp(tickets)
    need = min(mem, knob_gov_free_need_gb())
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
    queues = cap.get("queues") or (None, None)
    if cap["state"] == "starved" and runq_oversubscribed(queues[0], queues[1], cap["cores"]):
        return False, (f"starved: run queue {queues[0]:.0f} (5-min {queues[1]:.0f}) on {cap['cores']} cores, "
                       f"{cap['busy']:.1f} busy (pauses over {RUNQ_STARVED:g}x sustained)"), False
    if cap["state"] == "starved":
        return False, (f"starved: {cap['busy']:.1f} of {cap['cores']} cores busy (pauses over "
                       f"{knob_gov_starved():g}x, resumes under "
                       f"{knob_gov_resume():g}x)"), False
    if cores and cap["charged"] > 0:  # nothing charged: the head goes whatever it asks (never starved by its own size)
        limit = knob_gov_cpu_factor() * cap["cores"]
        if cap["busy"] + cores > limit:
            return False, (f"cores: {cap['charged']:g} charged ({cap['measured']:.1f} measured, {cap['busy']:.1f} busy) "
                           f"+ {cores:g} over {limit:g} ({knob_gov_cpu_factor():g}x "
                           f"{cap['cores']} cores)"), False
    return None


def gov_grant(cores: float, floor: float | None, cap: dict[str, Any]) -> float:
    """THE GRANTED WIDTH (govknobs.py): the cores an admitted pool asking `cores` with floor `floor` is granted and
    charged under the capacity `cap` (gov_capacity) -- what is free now (GOV_CPU_FACTOR x the cores less the busy
    ones), at most GOV_POOL_SHARE of the machine, never under its floor. A fixed ask (no floor) gets what it asked."""
    if floor is None or not cores:
        return cores
    free = int(knob_gov_cpu_factor() * cap["cores"] - cap["busy"])
    share = int(knob_gov_pool_share() * cap["cores"])
    return float(max(min(floor, cores), min(cores, free, max(share, 1))))


# the admission checks, in the order they judge: the first refusal is the answer
GOV_REFUSALS = (_suite_refusal, _check_cap_refusal, _budget_refusal, _free_memory_refusal, _pressure_refusal, _cpu_refusal)


def _red_verdict(tickets: Any, m: Any) -> Any:
    """Red goes whatever the memory, but while the CPU is starved (gov_capacity) at most RED_STARVED_CAP red fixes run."""
    reds = sum(1 for t in tickets if t.get("class") == "red")
    if reds >= RED_STARVED_CAP and gov_capacity(tickets, m)["state"] == "starved":
        return False, f"starved: {reds} red fix(es) already running (at most {RED_STARVED_CAP} while starved)", False
    return True, None, False


def gov_fits(cls: Any, mem: Any, tickets: Any, m: Any, kind: Any = "job", cores: Any = 0.0) -> Any:
    """(True, None, False) when work of class cls expecting `mem` GB and `cores` cores may start now; else (False, why,
    on_memory). Red: always."""
    if cls == "red":  # a gate fix waits for no budget or pressure; only a starved CPU bounds how many run at once
        return _red_verdict(tickets, m)
    for refusal in GOV_REFUSALS:
        verdict = refusal(cls, mem, tickets, m, kind, cores)
        if verdict:
            return verdict
    return True, None, False

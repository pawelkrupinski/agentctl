# agentctl, part `govcmd`: agentctl gov and agentctl checks: the governor's and the check slots' command lines.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import argparse
import datetime as dt
import json
import os
import time
from typing import Any
from contextlib import suppress
from agentctl.core import attempt, env_num, pct
from agentctl.machine import machine_cores, machine_load1
from agentctl.govtickets import gov_dir, gov_tickets
from agentctl.govbudget import cap_state, check_runs, checks_cap, gov_budget, slot_holders
from agentctl.gov import GOV_CLASSES, gov_admit, gov_evict, gov_machine, gov_waiters, idle_servers
from agentctl.govfits import GOV_CPU_FACTOR, gov_capacity, gov_charged, gov_committed, gov_ramp
from agentctl.pressure import pressure_report
from agentctl.govusage import GOV_USAGE_MIN_WALL, gov_job_cores, gov_measured_cores, gov_record_usage, gov_usage
from agentctl.govroute import gov_replay, gov_route


# THE GOVERNOR'S FEATURES, for a caller in another repository (movies' scripts/jvm-gate.sh governor_bin, bitcashier's
# bin/gov-sbt): a stable line, never a grep of agentctl's source (2026-11-22 to -25: jvm-gate.sh grepped gov.py for
# `def cmd_gov` and `def gov_route`; a module split moved both, and movies took no ticket for three days).
GOV_FEATURES = ("admit", "cores", "route", "width")


def admit_rows() -> list[Any]:
    """The admissions of <gov>/admits.jsonl, in order: a torn line skipped, an unreadable file read as far as it went."""
    rows: list[Any] = []
    with suppress(OSError), open(os.path.join(gov_dir(), "admits.jsonl")) as f:
        for line in f:
            with suppress(ValueError):
                rows.append(json.loads(line))
    return rows

def gov_probe(a: Any) -> int:
    print("agentctl governor: " + " ".join(GOV_FEATURES))
    return 0


def gov_admit_cmd(a: Any) -> int:
    t = gov_admit(a.cls, a.mem, a.label or "", kind=a.kind, fd=a.fd, ticket=a.ticket, wait=not a.try_,
                  force=a.force, pid=a.pid, cwd=a.cwd, cores=a.cores, job=a.job)
    return 0 if t else 1


def gov_usage_cmd(a: Any) -> int:
    rec = gov_record_usage(a.job, a.wall, a.cpu, a.load1, a.label or "", a.meter)
    print(f"{a.job}: {rec['cores']:g} cores over {a.wall:g} s" if rec else f"{a.job}: under {GOV_USAGE_MIN_WALL} s, not recorded")
    return 0


def gov_cores_cmd(a: Any) -> int:
    print(f"{gov_job_cores(a.job, a.cls):g}")
    return 0


def gov_route_cmd(a: Any) -> int:
    where, why = gov_route(a.job, a.cls, a.mem)
    print(f"{where}: {why}")
    return 0 if where == "ci" else 1


def _live_cores(jobs: Any) -> str:
    return ", ".join(f"{label} {cores:g}" for label, cores in jobs) or "-"


def gov_replay_cmd(a: Any) -> int:
    """`gov replay --at T`: the admissions around T, as admitted and under the cores rule."""
    at = dt.datetime.fromisoformat(a.at.replace("Z", "+00:00")).timestamp()
    rows = [r for r in admit_rows() if abs(float(r.get("at", r.get("end", 0))) - at) <= 12 * 3600]
    cap = env_num("AGENTCTL_GOV_CPU_FACTOR", GOV_CPU_FACTOR) * (a.cores or machine_cores())
    res = gov_replay(rows, cap, at, a.hold * 60)
    print(f"replay at {a.at} (cap {cap:g} cores; a ticket with no end record held {a.hold:g} min):")
    print(f"  as admitted:    {len(res['old'])} live, {sum(c for _, c in res['old']):g} cores: {_live_cores(res['old'])}")
    print(f"  the cores rule: {len(res['new'])} live, {sum(c for _, c in res['new']):g} cores: {_live_cores(res['new'])}; "
          f"{len(res['waiting'])} waiting: {_live_cores(res['waiting'])}")
    print(f"  peak cores live: {res['old_peak']:g} as admitted, {res['new_peak']:g} under the rule; p50 wait "
          f"{res['old_wait_p50'] / 60:.0f} min as admitted, {res['new_wait_p50'] / 60:.0f} min under the rule")
    return 0


def gov_evict_cmd(a: Any) -> int:
    if a.dry_run:
        for idle, root, _p in idle_servers():
            print(f"would evict {root} (unused {int(idle // 60)} min)")
        return 0
    root = gov_evict("by hand (agentctl gov evict)")
    print(f"evicted {root}" if root else "no idle sbt server to evict")
    return 0


def gov_capacity_cmd(a: Any) -> int:
    cap, tickets = gov_capacity(), gov_tickets()
    print(f"{cap['state']}: {cap['busy']:.1f} of {cap['cores']} cores busy ({cap['charged']:g} charged, "
          f"{cap['measured']:.1f} measured, load {machine_load1():.1f}); {len(tickets)} admitted, "
          f"{len(gov_waiters())} waiting")
    return 0


def gov_status_cmd(a: Any) -> int:
    return gov_status()


def _admit_waits(days: int) -> None:
    """The stats table of the admissions of the last `days` days: a row per class that admitted any."""
    cutoff = epoch_now() - days * 86400
    rows = [r for r in admit_rows() if r.get("at", 0) >= cutoff]
    print(f"{'class':>7} {'admits':>6} {'p50 wait':>9} {'p90 wait':>9} {'waited>1m':>9}")
    for cls in GOV_CLASSES:
        rs = [r for r in rows if r.get("class") == cls]
        if rs:
            w = [r.get("waited", 0) for r in rs]
            print(f"{cls:>7} {len(rs):>6} {pct(w, .5):>8.0f}s {pct(w, .9):>8.0f}s {sum(1 for x in w if x > 60):>9}")


def _measured_cores(days: int) -> None:
    """A line per job with usage records: its runs and the cores it is charged."""
    usage = gov_usage(days)
    for job in sorted({r.get("job") for r in usage if r.get("job")}):
        m_ = gov_measured_cores(job, usage)
        print(f"cores {job}: {sum(1 for r in usage if r.get('job') == job)} runs, charged "
              f"{gov_job_cores(job):g} ({'measured p75' if m_ is not None else 'default'})")


def gov_stats_cmd(a: Any) -> int:
    rc = cmd_checks(argparse.Namespace(ccmd="stats", days=a.days))
    _admit_waits(a.days)
    _measured_cores(a.days)
    ev = attempt(lambda: sum(1 for l in open(os.path.join(gov_dir(), "gov.log")) if " evicted " in l), 0, catch=(OSError,))
    print(f"evictions (all time): {ev}")
    return rc


GOV_COMMANDS = {"probe": gov_probe, "admit": gov_admit_cmd, "usage": gov_usage_cmd, "cores": gov_cores_cmd,
                "route": gov_route_cmd, "replay": gov_replay_cmd, "evict": gov_evict_cmd, "capacity": gov_capacity_cmd,
                "status": gov_status_cmd, "stats": gov_stats_cmd}


def cmd_gov(a: Any) -> int:
    handler = GOV_COMMANDS.get(a.gcmd)
    return handler(a) if handler else 2


def ticket_line(t: dict[str, Any]) -> str:
    """One live ticket: its class, memory, cores, job, label, pid, since and checkout."""
    return (f"  {t.get('class'):<7} {t.get('mem'):>4} GB {float(t.get('cores') or 0):>4g} cores  "
            f"{t.get('job') or t.get('kind'):<9} {t.get('label')}  pid {t.get('pid')}  since {t.get('since')}  "
            f"{t.get('cwd')}")


def gov_status() -> int:
    """`agentctl gov status`: the totals, every live ticket and waiter, the cores outside any ticket, the evictable
    servers and the pressure."""
    tickets, m = gov_tickets(), gov_machine()
    cap = env_num("AGENTCTL_GOV_CPU_FACTOR", GOV_CPU_FACTOR) * m["cores"]
    print(f"governor: {len(tickets)} admitted, {gov_committed(tickets):.1f}/{gov_budget():.0f} GB committed, "
          f"{gov_charged(tickets):g}/{cap:g} cores charged, {gov_ramp(tickets):.0f} GB still growing; free {m['free'] if m['free'] is None else round(m['free'], 1)} "
          f"GB, pressure {m['pressure']}%, load {m['load1']:.1f}/{m['cores']} cores; check cap {checks_cap()}")
    for t in tickets:
        print(ticket_line(t))
    capacity = gov_capacity(tickets, m)
    outside = capacity["measured"] - capacity["charged"]
    if outside >= 1:  # what no ticket accounts for: a runner outside the governed fan-out, or a ticket undercharged
        print(f"  outside any ticket: {outside:.1f} of {capacity['measured']:.1f} busy cores")
    for w in gov_waiters():
        print(f"  WAITING {w.get('class'):<7} {w.get('mem'):>4} GB {float(w.get('cores') or 0):>4g} cores  "
              f"{w.get('label')}  pid {w.get('pid')}")
    idle = idle_servers()
    if idle:
        print("evictable: " + ", ".join(f"{os.path.basename(r)} ({int(i // 60)} min)" for i, r, _p in idle))
    why, summary = pressure_report()
    print(f"pressure: {why} -- holds all but red fixes [{summary}]" if why else f"pressure: none [{summary}]")
    return 0


def _checks_level_line(level: Any, rs: Any) -> Any:
    """The stats table's row for the check runs that saw `level` checks running."""
    dur, wait = [r["duration"] for r in rs], [r["waited"] for r in rs]
    mem = [r["pressure"] for r in rs if r.get("pressure") is not None]
    swp = [r["swap_mb"] for r in rs if r.get("swap_mb") is not None]
    return (f"{level:>7} {len(rs):>5} {pct(dur, .5):>7.0f}s {pct(dur, .9):>7.0f}s {pct(wait, .5):>8.0f}s "
            f"{pct(wait, .9):>8.0f}s {max(mem) if mem else '-':>8} {int(max(swp)) if swp else '-':>8}")


def _checks_stats(a: Any) -> Any:
    runs = check_runs(a.days)
    cap = checks_cap()
    st = cap_state()
    print(f"check slots: cap {cap}" + (" ($AGENTCTL_MAX_CHECKS)" if os.environ.get("AGENTCTL_MAX_CHECKS") else
                                      f" (auto; last change: {st.get('reason', 'none')})")
          + f"; running now: {len(slot_holders(cap))}; {len(runs)} check run(s) in {a.days} d")
    print(f"{'running':>7} {'runs':>5} {'p50 dur':>8} {'p90 dur':>8} {'p50 wait':>9} {'p90 wait':>9} {'max mem%':>8} {'max swap':>8}")
    for level in sorted({r.get("running", 0) for r in runs}):
        print(_checks_level_line(level, [r for r in runs if r.get("running", 0) == level]))
    return 0


def cmd_checks(a):
    if a.ccmd == "stats":
        return _checks_stats(a)
    if a.ccmd == "status":
        cap = checks_cap()
        holders = slot_holders(cap)
        print(f"checks: {len(holders)}/{cap} running (the governor's check cap; `agentctl gov status` for the rest)"
              + "".join(f"\n  {h}" for h in holders))
        return 0
    return 2

# agentctl, part `gov`: the governor: ONE admission control for the laptop's memory and cores: its capacity, its waiters, a ticket admitted or refused, idle sbt servers evicted
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import time
from typing import Any
from contextlib import suppress
from agentctl.procrunner import process_runner
from agentctl.core import attempt, env_num, err, pid_alive, process_cache, run_cmd
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json
from agentctl.procs import proc_alive, process_start
from agentctl.cpu import GOV_SATURATED
from agentctl.owned import lock_held
from agentctl.machine import free_gb, machine_cores, machine_sample
from agentctl.govtickets import (
    GOV_POLL, gov_bump, gov_change_mark, gov_dir, gov_log, gov_log_end, gov_tickets, gov_wait_change)
from agentctl.jvmreap import sbt_warm_state
from agentctl.govbudget import GOV_MEM_GB, checks_cap, gov_budget
from agentctl.store import LeaseLost
from agentctl.leases import lock_beat
from agentctl.redfixes import fixes_of
from agentctl.changekind import classify_change, queue_facts
from agentctl.pressure import gov_busy_cores, laptop_pressure, pressure_reading
from agentctl.govusage import gov_job_cores


# ---------------------------------------------------------------- the governor: ONE admission control for memory and CPU
# The owner (2026-10-27): "one governor for laptop memory and CPU". Three limiters guarded the 36 GB / 14-core Mac each on
# its own -- movies' scripts/jvm-gate.sh (4 JVM slots), the check slots' cap (below) and sbt-warm's kept servers (each
# holding a JVM slot, idle or not) -- so an idle warm server held the slot a red fix's check waited for, and none knew
# what the others had admitted. Now one governor admits every heavy unit of work:
#
# - A TICKET is an flock on <gov>/t-<id>.lock (freed by the kernel when its holder ends, however it ends) with its record
#   beside it, t-<id>.json: class, expected GB, label, pid, cwd, since. agentctl's own checks hold one in-process; movies'
#   scripts/jvm-gate.sh opens descriptor 7 on a ticket file and runs `agentctl gov admit --fd 7`, which locks that shared
#   open file: the shell, and the JVM or the kept server's keeper it hands descriptor 7 to, hold the ticket.
# - CLASSES, in priority order: red (a change carrying Fixes-Red for a red run; a red fixer's worktree), check (a
#   pre-commit check), agent (an agent's own sbt), measure (identity-measure*). Waiters queue in that order, first come
#   first served within a class, and only the head of the queue is admitted: a lower class never overtakes a higher one.
# - MEMORY: the expected GB must fit under the BUDGET (the sum of the live tickets' expected GB; $AGENTCTL_GOV_BUDGET_GB,
#   default 60% of RAM), and what it needs NOW ($AGENTCTL_GOV_FREE_NEED_GB, 2.5, or less) must fit into FREE memory
#   (free + inactive + speculative pages) less a reserve ($AGENTCTL_GOV_RESERVE_GB, 1) less what the tickets admitted in
#   the last 2 min will still grow into (a JVM takes its heap over its first minutes); and memory pressure (100 -
#   memory_pressure's free %) must be under 75%, and the laptop not under pressure (laptop_pressure, below).
# - RED NEVER WAITS (the owner, 2026-10-31: "nothing should stop gate fixes ever"): a red fix's ticket is admitted at
#   once, whatever the budget, free memory, pressure or load.
# - CPU (the owner, 2026-11-19): pause when starved, resume with room -- THE CAPACITY, below (gov_capacity).
# - CORES (the owner, 2026-11-18): memory alone over-admitted CPU-bound JVMs -- 8 admitted on 40 of 77 GB drove the
#   18-core laptop to a load of 100 at 05:45Z. So a ticket also carries the CORES its job is expected to use
#   (GOV_JOB_CORES, or the job's measured p75 in <gov>/usage.jsonl once it has GOV_USAGE_MIN uncontended runs:
#   gov_job_cores), and work is admitted only while the capacity's busy cores + its own <= the cores x
#   $AGENTCTL_GOV_CPU_FACTOR (1.0). A ticket of no cores (a kept server's keeper: its memory, idle; each call through it
#   takes a cores ticket of its own) never waits on cores, nor one with nothing charged. Red never waits.
# - EVICTION: when the head waiter does not fit on memory, the governor stops the idle kept sbt server unused longest
#   (sbt-warm's registry: no call running in it -- its busy lock taken exclusively for the stop, so none starts under it --
#   unused for a minute, not serving, never the root checkout's), then looks again. Idle warm servers go first.
# - CHECKS keep their cap (`checks`, below): measured and auto-tuned as before, now one of the governor's limits.
# Every admission is a line of <gov>/admits.jsonl (class, GB, wait, what it waited on); every eviction a line of gov.log.
GOV_CLASSES = {"red": 0, "check": 1, "agent": 2, "measure": 3, "suite": 4}
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
GOV_CPU_FACTOR = 1.0    # charged cores (or the load) + a job's own may reach this x the cores ($AGENTCTL_GOV_CPU_FACTOR)
GOV_EVICT_IDLE = 60     # a kept server unused this long (seconds) may be evicted
GOV_REPORT = 600


def gov_machine():
    """{free, pressure, load1, cores, laptop}: live memory and CPU, and why the laptop is under pressure (laptop_pressure
    without the load: each class has its own) ($AGENTCTL_GOV_PRESSURE_PROBE / _LOAD_PROBE: the tests')."""
    load1, _cpu, pressure, _swap = machine_sample()
    if os.environ.get("AGENTCTL_GOV_PRESSURE_PROBE"):
        pressure = float(run_cmd(os.environ["AGENTCTL_GOV_PRESSURE_PROBE"], shell=True).stdout.strip() or 0)
    if os.environ.get("AGENTCTL_GOV_LOAD_PROBE"):
        load1 = float(run_cmd(os.environ["AGENTCTL_GOV_LOAD_PROBE"], shell=True).stdout.strip() or 0)
    try:
        laptop = laptop_pressure(None if "AGENTCTL_LAPTOP_PRESSURE" in os.environ else pressure_reading(), load=False)
    except Exception:
        laptop = None
    return {"free": free_gb(), "pressure": pressure, "load1": load1, "cores": machine_cores(), "laptop": laptop}


def gov_committed(tickets):
    return sum(float(t.get("mem") or 0) for t in tickets)


def gov_charged(tickets):
    """The cores the live tickets are charged (a ticket from before cores were charged: none)."""
    return sum(float(t.get("cores") or 0) for t in tickets)


def gov_ramp(tickets, now=None):
    now = now or time.time()
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
            write_json(path, {"state": state, "busy": busy, "at": time.time()})
            gov_log(f"capacity {prev} -> {state}: {busy:.1f} of {n} cores busy ({charged:g} charged, {measured:.1f} "
                    "measured)")
    return {"state": state, "busy": busy, "charged": charged, "measured": measured, "cores": n,
            "load1": (m or {}).get("load1")}


def gov_fits(cls, mem, tickets, m, kind="job", cores=0.0):
    """(True, None) when work of class cls expecting `mem` GB and `cores` cores may start now; else (False, why,
    on_memory). Red: always."""
    if cls == "red":  # nothing stops a gate fix, ever
        return True, None, False
    if cls == "suite":  # agentctl's own test suite (`agentctl suite`): at most SUITE_CAP at once
        suites = sum(1 for t in tickets if t.get("class") == "suite")
        if suites >= env_num("AGENTCTL_SUITE_CAP", SUITE_CAP):
            return False, f"{suites}/{env_num('AGENTCTL_SUITE_CAP', SUITE_CAP):g} agentctl suite run(s) going (the suite cap)", False
    if kind == "check" and cls != "red":
        checks = sum(1 for t in tickets if t.get("kind") == "check")
        if checks >= checks_cap():
            return False, f"{checks}/{checks_cap()} checks running (the check cap)", False
    budget = gov_budget()
    committed = gov_committed(tickets)
    if committed + mem > budget:
        return False, f"{committed:.0f}+{mem:.0f} GB over the {budget:.0f} GB budget", True
    if m.get("free") is not None:
        room = m["free"] - env_num("AGENTCTL_GOV_RESERVE_GB", GOV_RESERVE_GB) - gov_ramp(tickets)
        need = min(mem, env_num("AGENTCTL_GOV_FREE_NEED_GB", GOV_FREE_NEED_GB))
        if need > room:
            return False, (f"{m['free']:.1f} GB free, {gov_ramp(tickets):.0f} GB still growing: {max(room, 0):.1f} GB "
                           f"for the {need:g} GB it needs now"), True
    stop = GOV_PRESSURE_DEFAULT
    if m.get("pressure") is not None and m["pressure"] >= stop:
        return False, f"memory pressure {m['pressure']:.0f}% (waits at {stop}%)", True
    if m.get("laptop"):
        return False, f"laptop pressure: {m['laptop']}", True
    cap = gov_capacity(tickets, m)
    if cap["state"] == "starved":  # PAUSED until the cores free up (gov_capacity): resumes by itself, no one acts
        return False, (f"starved: {cap['busy']:.1f} of {cap['cores']} cores busy (pauses over "
                       f"{env_num('AGENTCTL_GOV_STARVED', GOV_STARVED):g}x, resumes under "
                       f"{env_num('AGENTCTL_GOV_RESUME', GOV_RESUME):g}x)"), False
    if cores and cap["charged"] > 0:  # nothing charged: the head goes whatever it asks (never starved by its own size)
        limit = env_num("AGENTCTL_GOV_CPU_FACTOR", GOV_CPU_FACTOR) * cap["cores"]
        if cap["busy"] + cores > limit:
            return False, (f"cores: {cap['charged']:g} charged ({cap['measured']:.1f} measured, {cap['busy']:.1f} busy) "
                           f"+ {cores:g} over {limit:g} ({env_num('AGENTCTL_GOV_CPU_FACTOR', GOV_CPU_FACTOR):g}x "
                           f"{cap['cores']} cores)"), False
    return True, None, False


# WAITING IS CHEAP (2026-11-19 15:35Z: ~13 sbt-warm waiters each re-read every waiter's liveness -- two `ps` per waiter
# -- and the machine's memory -- four more processes -- every GOV_POLL: ~170 short processes a second, a load of ~60 on 18
# cores that held them all on the load factor, a loop feeding itself). Now a waiter behind the head re-reads the queue
# only when the CHANGE MARK moves (gov_bump: a waiter or ticket leaving) or its backoff (GOV_POLL doubling to
# GOV_IDLE_MAX) runs out, a stat of one file meanwhile; and a waiter's liveness is its pid (a signal 0, no process) with
# its start time read once per GOV_ALIVE_TTL.
GOV_IDLE_MAX = 30.0     # s a waiter behind the head waits at most between two reads of the queue
GOV_ALIVE_TTL = 60.0    # s a waiter's start time, once read, vouches for its pid
_gov_alive = process_cache({})


def gov_waiter_alive(rec):
    pid, start = int(rec.get("pid", 0) or 0), rec.get("start")
    if not pid or not pid_alive(pid):
        return False
    k = (pid, str(start))
    at = _gov_alive.get(k)
    if at is not None and time.time() - at < GOV_ALIVE_TTL:
        return True
    ok = proc_alive(pid, start)  # a reused pid is not the waiter
    if ok:
        _gov_alive[k] = time.time()
    return ok


def gov_waiters():
    """The live waiters, in admission order: (class priority, arrival)."""
    out = []
    for p in glob.glob(os.path.join(gov_dir(), "w-*.json")):
        rec = read_record(p)
        if not rec or not gov_waiter_alive(rec):
            try:
                os.unlink(p)
                gov_bump()
            except OSError:
                pass
            continue
        rec["path"] = p
        out.append(rec)
    return sorted(out, key=lambda r: (GOV_CLASSES.get(r.get("class"), 9), r.get("at", 0)))


def evictable_root(root_file: str, prefix: str) -> str | None:
    """The checkout of the kept server whose files start with `prefix`, when it is one the governor may evict: a live
    server in a non-root-checkout directory, neither serving nor in a call."""
    try:
        with open(root_file) as rf, open(prefix + ".pid") as pf:
            root, pid = rf.read().strip(), int(pf.read().strip())
    except (OSError, ValueError):
        return None
    if not os.path.isdir(root) or os.path.isdir(os.path.join(root, ".git")) or not pid_alive(pid):
        return None
    return None if os.path.exists(prefix + ".serving") or lock_held(prefix + ".busy") else root


def idle_servers(min_idle=None):
    """The kept sbt servers the governor may evict, unused longest first: [(idle seconds, root, key prefix)]. Never the
    root checkout's (the owner's own), a serving one, or one a call is running in (its busy lock held)."""
    min_idle = GOV_EVICT_IDLE if min_idle is None else min_idle
    out = []
    for f in glob.glob(os.path.join(sbt_warm_state(), "*.root")):
        prefix = f[:-5]
        root = evictable_root(f, prefix)
        idle = attempt(lambda: time.time() - os.path.getmtime(prefix + ".used"), None, catch=(OSError,)) if root else None
        if idle is not None and idle >= min_idle:
            out.append((idle, root, prefix))
    return sorted(out, reverse=True)


def gov_evict(why):
    """Stop the idle kept server unused longest (holding its busy lock, so no call starts under the stop). The evicted
    checkout, or None."""
    for idle, root, prefix in idle_servers():
        f = open(prefix + ".busy", "a+")
        try:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue  # a call started meanwhile
            script = os.path.join(root, "scripts", "sbt-warm")
            if not os.access(script, os.X_OK):
                continue
            try:
                process_runner().run([script, "--stop", "--root"], cwd=root, capture_output=True, text=True, timeout=180)
            except subprocess.TimeoutExpired:
                gov_log(f"evict {root}: sbt-warm --stop took over 3 min")
            gov_log(f"evicted the idle sbt server of {root} (unused {int(idle // 60)} min) for: {why}")
            return root
        finally:
            f.close()
    return None


class GovTicket:
    def __init__(self, path, rec, f=None):
        self.path, self.rec, self.f = path, rec, f
        self.waited, self.why = rec.get("waited", 0.0), rec.get("waited_on")

    def release(self):
        if self.f is None and not os.path.exists(self.path):
            return
        for p in (self.path[:-5] + ".json", self.path):
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            gov_log_end(os.path.basename(self.path))
        except OSError:
            pass
        if self.f:
            self.f.close()
            self.f = None
        gov_bump()


class GovTimeout(RuntimeError):
    """The governor did not admit the work within its bound (audit #12: an unbounded wait hid a stuck queue entry)."""


GOV_ADMIT_MAX = 2 * 3600  # a check waits this long for the governor at most ($AGENTCTL_GOV_ADMIT_MAX)


class GovWait:
    """One unit of work waiting its turn at the governor: the loop that polls until it is the head and fits."""

    def __init__(self, cls, mem: Any, cores: Any, label: Any, kind: Any, me: Any, report: Any, force: Any=False, wait: Any=True, max_wait: Any=None) -> None:
        self.cls, self.mem, self.cores, self.label, self.kind = cls, mem, cores, label, kind
        self.me, self.report, self.force, self.wait, self.max_wait = me, report, force, wait, max_wait
        self.t0 = time.time()
        self.said, self.report_at, self.waited_on = False, 0.0, None
        self.idle = env_num("AGENTCTL_GOV_POLL", GOV_POLL)  # a waiter behind the head backs off to GOV_IDLE_MAX

    def verdict(self, tickets: Any, waiters: Any, head: Any) -> Any:
        """(fits, why not, held on memory) for the waiter in this poll."""
        if self.force:
            return True, None, False
        if not head:
            w = waiters[0] if waiters else {}
            return False, f"behind {w.get('class')} work ({w.get('label') or '?'}, pid {w.get('pid')})", False
        return gov_fits(self.cls, self.mem, tickets, gov_machine(), self.kind, self.cores)

    def check_timeout(self, why: Any) -> Any:
        if self.max_wait is not None and time.time() - self.t0 > self.max_wait:
            raise GovTimeout(f"the governor did not admit {self.cls} work ({self.label}) in "
                             f"{int(self.max_wait // 60)} min: {why}")

    def keep_leases(self) -> Any:
        try:  # a check waiting its turn holds its leases (bounded by max_wait and their max hold), never loses them
            lock_beat(f"gov-wait:{self.label}:{int(time.time() // 60)}")
        except LeaseLost as ex:
            raise GovTimeout(f"{ex} while waiting for the governor") from None

    def maybe_report(self, why: Any, tickets: Any) -> Any:
        if time.monotonic() < self.report_at:
            return
        self.report(f"{'still ' if self.said else ''}waiting for the governor ({self.cls}, {self.mem:g} GB, "
                    f"{self.cores:g} cores, {self.label}): {why}; {len(tickets)} admitted, "
                    f"{gov_committed(tickets):.0f}/{gov_budget():.0f} GB, {gov_charged(tickets):g} cores charged")
        self.said, self.report_at = True, time.monotonic() + GOV_REPORT

    def pause(self, head: Any, mark: Any) -> Any:
        if head:  # the head reads the machine every poll: memory and load free up with no event
            self.idle = env_num("AGENTCTL_GOV_POLL", GOV_POLL)
            time.sleep(self.idle)
        else:
            gov_wait_change(mark, self.idle)
            self.idle = min(self.idle * 2, env_num("AGENTCTL_GOV_IDLE_MAX", GOV_IDLE_MAX))

    def evicted(self, head: Any, on_mem: Any, why: Any) -> Any:
        """The head, held on memory, freed some by evicting idle work (the freed memory shows in vm_stat)."""
        if head and on_mem and self.cls != "measure" and gov_evict(f"{self.cls} {self.label} ({why})"):
            time.sleep(env_num("AGENTCTL_GOV_EVICT_SETTLE", 3))
            return True
        return False

    def run(self) -> Any:
        """True once it may start (self.waited_on: the last hold, for the record); False for a --try that may not."""
        while True:
            mark = gov_change_mark()
            tickets = gov_tickets()
            waiters = gov_waiters()
            head = bool(waiters) and os.path.abspath(waiters[0]["path"]) == os.path.abspath(self.me)
            ok, why, on_mem = self.verdict(tickets, waiters, head)
            if ok:
                return True
            self.waited_on = why  # `why` is None once it fits
            if self.evicted(head, on_mem, why):
                continue
            if not self.wait:
                return False
            self.check_timeout(why)
            self.keep_leases()
            self.maybe_report(why, tickets)
            self.pause(head, mark)


def gov_take_ticket(d: Any, t0: Any, ticket: Any, fd: Any) -> Any:
    """(ticket path, the open lock file or None): the admitted work's exclusive lock, on the inherited `fd` or a file."""
    path = ticket or os.path.join(d, f"t-{t0:.6f}-{os.getpid()}.lock")
    if fd is not None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return path, None
    f = open(path, "a+")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return path, f


def gov_admit(cls, mem=None, label="", kind="job", fd=None, ticket=None, wait=True, force=False, pid=None, cwd=None,
              report=None, max_wait=None, cores=None, job=None):
    """Admit one unit of work: a GovTicket once it may start (None for a --try that may not). With `fd`, the lock is
    taken on that inherited descriptor (a shell's, which then holds the ticket); else on a file this process keeps open.
    `cores`: what it is charged (default: its `job`'s, gov_job_cores)."""
    cls = cls if cls in GOV_CLASSES else "agent"
    mem = GOV_MEM_GB.get(cls, 5.0) if mem is None else float(mem)
    cores = gov_job_cores(job, cls, kind) if cores is None else float(cores)
    d = gov_dir()
    waiter = GovWait(cls, mem, cores, label, kind, None, report or err, force, wait, max_wait)
    t0 = waiter.t0
    waiter.me = me = os.path.join(d, f"w-{t0:.6f}-{os.getpid()}.json")
    write_json(me, {"class": cls, "mem": mem, "cores": cores, "job": job, "label": label, "kind": kind,
                    "pid": os.getpid(), "at": t0, "start": process_start(os.getpid())})
    try:
        if not waiter.run():
            return None
        # THE TICKET BEFORE THE WAITER GOES: the next head never looks between the two and misses this admission
        path, f = gov_take_ticket(d, t0, ticket, fd)
        waited = round(time.time() - t0, 1)
        rec = {"class": cls, "mem": mem, "cores": cores, "job": job, "label": label, "kind": kind,
               "pid": pid or os.getpid(), "cwd": cwd or os.getcwd(), "at": time.time(), "since": ts(), "waited": waited,
               "waited_on": waiter.waited_on if waited >= 1 else None, "id": os.path.basename(path)}
        gov_admit_write(d, path, rec)
        if waiter.said:
            waiter.report(f"admitted by the governor after {int(waited)} s ({cls}, {mem:g} GB, {cores:g} cores, {label})")
        return GovTicket(path, rec, f)
    finally:
        gov_admit_cleanup(me)


def gov_admit_write(d: Any, path: Any, rec: Any) -> Any:
    """The admission's record beside its ticket, and one line of the admits log."""
    write_json(path[:-5] + ".json", rec)
    with open(os.path.join(d, "admits.jsonl"), "a") as af:
        af.write(json.dumps({k: rec[k] for k in ("class", "mem", "cores", "job", "label", "kind", "at", "waited",
                                                 "waited_on", "id")}) + "\n")


def gov_admit_cleanup(waiter_file: Any) -> Any:
    """The waiter's file goes; the next waiter is the head now: it looks at once."""
    try:
        os.unlink(waiter_file)
    except OSError:
        pass
    gov_bump()


def cmd_suite(a):
    """`agentctl suite [tests/run.py args]`: agentctl's own test suite (tests/run.py: parallel, a timeout per test; in
    this directory). run.py takes its governor ticket itself (THE GOVERNED FAN-OUT, govpool.py: class `suite`, SUITE_CAP
    at once, charged its workers): parallel agents queue there instead of piling up. Arguments are run.py's (-j N,
    --timeout S, PATTERN...)."""
    return process_runner().run([sys.executable, os.path.join("tests", "run.py")] + list(a.args or [])).returncode


def gov_class_of(repo, commits, reds, st=None):
    """red when the commits' Fixes-Red names an open red -- the gate's (reds) or any other the store has open: the
    change's KIND (changekind.py classify_change) -- else check."""
    fixes = fixes_of(repo, commits, st)
    if not fixes:
        return "check"
    return "red" if classify_change({"fixes": sorted(fixes)}, queue_facts(reds, st))["red_fix"] else "check"



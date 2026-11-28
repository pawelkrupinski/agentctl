# agentctl, part `gov`: the governor: ONE admission control for the laptop's memory and cores: its capacity, its waiters, a ticket admitted or refused, idle sbt servers evicted
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import stopwatch, wall_epoch
import fcntl
import glob
import json
import os
import subprocess
import sys
import time
from typing import Any
from agentctl.procrunner import process_runner
from agentctl.core import attempt, print_error, pid_alive, process_cache
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json, note_failure, unlink_file
from agentctl.procs import process_alive, process_start
from agentctl.filelock import lock_held
from agentctl.machine import MachineProbe
from agentctl.filelog import file_event, note_event, note_events
from agentctl.causes import governor_cause
from agentctl.govtickets import gov_bump, gov_change_mark, gov_dir, gov_log, gov_log_end, gov_tickets, gov_wait_change
from agentctl.jvmreap import sbt_warm_state
from agentctl.govbudget import GOV_MEM_GB, gov_budget
from agentctl.leasetable import LeaseLost
from agentctl.leases import lock_beat
from agentctl.changekind import facts_for
from agentctl.redland import may_land_on_red
from agentctl.pressure import gov_busy_cores, laptop_pressure, pressure_reading
from agentctl.govusage import gov_job_cores
from agentctl.govfits import gov_capacity, gov_charged, gov_committed, gov_fits, gov_grant
from agentctl.govknobs import knob_gov_poll
from agentctl.govknobs import knob_laptop_pressure
from agentctl.govknobs import knob_gov_evict_settle, knob_gov_idle_max


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
GOV_EVICT_IDLE = 60     # a kept server unused this long (seconds) may be evicted
GOV_REPORT = 600


def gov_machine(machine: MachineProbe) -> dict[str, Any]:
    """{free, pressure, load1, cores, queues (1- and 5-min load), busy (gov_busy_cores), ram_gb, laptop}: live memory
    and CPU, and why the laptop is under pressure (laptop_pressure without the load: each class has its own)."""
    load1, _cpu, pressure, _swap = machine.sample()
    try:
        laptop = laptop_pressure(None if knob_laptop_pressure() is not None else pressure_reading(machine), load=False)
    except Exception:
        laptop = None
    return {"free": machine.free_gb(), "pressure": pressure, "load1": load1, "cores": machine.cores(),
            "queues": machine.load_averages(), "busy": gov_busy_cores(machine), "ram_gb": machine.ram_gb(),
            "laptop": laptop}


GOV_ALIVE_TTL = 60.0    # s a waiter's start time, once read, vouches for its pid
_gov_alive = process_cache({})


def gov_waiter_alive(rec):
    pid, start = int(rec.get("pid", 0) or 0), rec.get("start")
    if not pid or not pid_alive(pid):
        return False
    k = (pid, str(start))
    at = _gov_alive.get(k)
    if at is not None and wall_epoch() - at < GOV_ALIVE_TTL:
        return True
    ok = process_alive(pid, start)  # a reused pid is not the waiter
    if ok:
        _gov_alive[k] = wall_epoch()
    return ok


def gov_waiters():
    """The live waiters, in admission order: (class priority, arrival)."""
    out = []
    for p in glob.glob(os.path.join(gov_dir(), "w-*.json")):
        rec = read_record(p)
        if not rec or not gov_waiter_alive(rec):
            if unlink_file(p):
                gov_bump()
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
        idle = attempt(lambda: wall_epoch() - os.path.getmtime(prefix + ".used"), None, catch=(OSError,)) if root else None
        if idle is not None and idle >= min_idle:
            out.append((idle, root, prefix))
    return sorted(out, reverse=True)


def _stop_failed(script: str, root: str) -> str | None:
    """Why `sbt-warm --stop` did not stop the server of root (it timed out, or exited non-zero), or None."""
    try:
        r = process_runner().run([script, "--stop", "--root"], cwd=root, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return "sbt-warm --stop took over 3 min"
    if r.returncode != 0:
        return f"sbt-warm --stop failed (exit {r.returncode}): {(r.stderr or r.stdout or '').strip()[-160:]}"
    return None


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
            failed = _stop_failed(script, root)
            if failed:  # review 2026-11-26: a failed stop freed nothing, so it is no eviction
                gov_log(f"evict {root}: {failed}")
                continue
            gov_log(f"evicted the idle sbt server of {root} (unused {int(idle // 60)} min) for: {why}")
            note_event("sbt-server", root, "idle", "evicted", "governor", detail=why)
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
            unlink_file(p)
        try:
            gov_log_end(os.path.basename(self.path))
        except OSError as e:
            note_failure("governor log end not written", e)
        if self.f:
            self.f.close()
            self.f = None
        gov_bump()


class GovTimeout(RuntimeError):
    """The governor did not admit the work within its bound (audit #12: an unbounded wait hid a stuck queue entry)."""


class GovWait:
    """One unit of work waiting its turn at the governor: the loop that polls until it is the head and fits."""

    def __init__(self, cls, mem: Any, cores: Any, label: Any, kind: Any, me: Any, report: Any, force: Any = False,
                 wait: Any = True, max_wait: Any = None, floor: Any = None, *, machine) -> None:
        self.machine = machine
        self.cls, self.mem, self.cores, self.label, self.kind = cls, mem, cores, label, kind
        self.floor = None if floor is None else min(float(floor), cores)  # a pool that can run narrower: its least
        self.granted = cores
        self.me, self.report, self.force, self.wait, self.max_wait = me, report, force, wait, max_wait
        self.t0 = wall_epoch()
        self.said, self.report_due, self.waited_on = False, stopwatch(0), None
        self.idle = knob_gov_poll()  # a waiter behind the head backs off to GOV_IDLE_MAX

    def verdict(self, tickets: Any, waiters: Any, head: Any) -> Any:
        """(fits, why not, held on memory) for the waiter in this poll."""
        if self.force:
            return True, None, False
        if not head:
            w = waiters[0] if waiters else {}
            return False, f"behind {w.get('class')} work ({w.get('label') or '?'}, pid {w.get('pid')})", False
        m = gov_machine(self.machine)
        ok = gov_fits(self.cls, self.mem, tickets, m, self.kind, self.cores if self.floor is None else self.floor)
        if ok[0]:
            self.granted = gov_grant(self.cores, self.floor, gov_capacity(tickets, m))
        return ok

    def check_timeout(self, why: Any) -> Any:
        if self.max_wait is not None and wall_epoch() - self.t0 > self.max_wait:
            raise GovTimeout(f"the governor did not admit {self.cls} work ({self.label}) in "
                             f"{int(self.max_wait // 60)} min: {why}")

    def keep_leases(self) -> Any:
        try:  # a check waiting its turn holds its leases (bounded by max_wait and their max hold), never loses them
            lock_beat(f"gov-wait:{self.label}:{int(wall_epoch() // 60)}")
        except LeaseLost as ex:
            raise GovTimeout(f"{ex} while waiting for the governor") from None

    def maybe_report(self, why: Any, tickets: Any) -> Any:
        if not self.report_due.expired():
            return
        self.report(f"{'still ' if self.said else ''}waiting for the governor ({self.cls}, {self.mem:g} GB, "
                    f"{self.cores:g} cores, {self.label}): {why}; {len(tickets)} admitted, "
                    f"{gov_committed(tickets):.0f}/{gov_budget(self.machine.ram_gb()):.0f} GB, {gov_charged(tickets):g} cores charged")
        self.said, self.report_due = True, stopwatch(GOV_REPORT)

    def pause(self, head: Any, mark: Any) -> Any:
        if head:  # the head reads the machine every poll: memory and load free up with no event
            self.idle = knob_gov_poll()
            time.sleep(self.idle)  # wall: waits on and times real processes and threads
        else:
            gov_wait_change(mark, self.idle)
            self.idle = min(self.idle * 2, knob_gov_idle_max())

    def evicted(self, head: Any, on_mem: Any, why: Any) -> Any:
        """The head, held on memory, freed some by evicting idle work (the freed memory shows in vm_stat)."""
        if head and on_mem and self.cls != "measure" and gov_evict(f"{self.cls} {self.label} ({why})"):
            # wall: waits on and times real processes and threads
            time.sleep(knob_gov_evict_settle())
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
              report=None, max_wait=None, cores=None, job=None, min_cores=None, *, machine):
    """Admit one unit of work: a GovTicket once it may start (None for a --try that may not). With `fd`, the lock is
    taken on that inherited descriptor (a shell's, which then holds the ticket); else on a file this process keeps open.
    `cores`: what it is charged (default: its `job`'s, gov_job_cores); with `min_cores`, a pool admitted once that
    many fit and charged what it is granted (THE GRANTED WIDTH, govfits.gov_grant: the ticket record's `cores`)."""
    cls = cls if cls in GOV_CLASSES else "agent"
    mem = GOV_MEM_GB.get(cls, 5.0) if mem is None else float(mem)
    cores = gov_job_cores(job, cls, kind, machine_cores=machine.cores()) if cores is None else float(cores)
    d = gov_dir()
    waiter = GovWait(cls, mem, cores, label, kind, None, report or print_error, force, wait, max_wait, floor=min_cores,
                     machine=machine)
    t0 = waiter.t0
    waiter.me = me = os.path.join(d, f"w-{t0:.6f}-{os.getpid()}.json")
    write_json(me, {"class": cls, "mem": mem, "cores": cores, "job": job, "label": label, "kind": kind,
                    "pid": os.getpid(), "at": t0, "start": process_start(os.getpid())})
    try:
        if not waiter.run():
            return None
        # THE TICKET BEFORE THE WAITER GOES: the next head never looks between the two and misses this admission
        path, f = gov_take_ticket(d, t0, ticket, fd)
        waited, cores = round(wall_epoch() - t0, 1), waiter.granted
        rec = {"class": cls, "mem": mem, "cores": cores, "job": job, "label": label, "kind": kind,
               "pid": pid or os.getpid(), "cwd": cwd or os.getcwd(), "at": wall_epoch(), "since": ts(), "waited": waited,
               "waited_on": waiter.waited_on if waited >= 1 else None, "id": os.path.basename(path)}
        gov_admit_write(d, path, rec)
        if waiter.said:
            waiter.report(f"admitted by the governor after {int(waited)} s ({cls}, {mem:g} GB, {cores:g} cores, {label})")
        return GovTicket(path, rec, f)
    finally:
        gov_admit_cleanup(me)


def gov_admit_write(d: Any, path: Any, rec: Any) -> Any:
    """The admission's record beside its ticket, one line of the admits log, and its wait and admission as events
    (filelog.py: the ticket opened `waiting` when it asked, `admitted` now; why it waited the wait's cause)."""
    write_json(path[:-5] + ".json", rec)
    what = f"{rec['class']} {rec.get('job') or rec.get('kind')} ({rec.get('label')}, {rec['cores']:g} cores)"
    note_events([file_event("gov-ticket", rec["id"], None, "waiting", governor_cause(rec.get("waited_on")),
                            at=rec["at"] - rec["waited"], detail=rec.get("waited_on"), text=what, key="opened"),
                 file_event("gov-ticket", rec["id"], "waiting", "admitted", "governor", at=rec["at"], text=what,
                            key="admitted")])
    with open(os.path.join(d, "admits.jsonl"), "a") as af:
        af.write(json.dumps({k: rec[k] for k in ("class", "mem", "cores", "job", "label", "kind", "at", "waited",
                                                 "waited_on", "id")}) + "\n")


def gov_admit_cleanup(waiter_file: Any) -> Any:
    """The waiter's file goes; the next waiter is the head now: it looks at once."""
    unlink_file(waiter_file)
    gov_bump()


def cmd_suite(a):
    """`agentctl suite [tests/run.py args]`: agentctl's own test suite (tests/run.py: parallel, a timeout per test; in
    this directory). run.py takes its governor ticket itself (THE GOVERNED FAN-OUT, govpool.py: class `suite`, SUITE_CAP
    at once, charged its workers): parallel agents queue there instead of piling up. Arguments are run.py's (-j N,
    --timeout S, PATTERN...)."""
    return process_runner().run([sys.executable, os.path.join("tests", "run.py")] + list(a.args or []),
                                timeout=None).returncode  # the suite bounds itself (--suite-timeout)


def gov_class_of(repo, commits, reds, st=None, *, ctx):
    """red when the commits' Fixes-Red names an open red -- the gate's (reds) or any other the store has open: the
    change's red fix, THE RED-LANDING RULE's (redland.may_land_on_red) -- else check."""
    return "red" if may_land_on_red(facts_for(reds, st, ctx.caches.tick_facts), repo=repo, commits=commits, store=st, ctx=ctx).red_fix else "check"



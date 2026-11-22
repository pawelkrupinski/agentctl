# agentctl, part `gov`: the governor.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

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
GOV_MEM_GB = {"red": 5.0, "check": 5.0, "agent": 5.0, "measure": 6.0, "hook": 1.0, "suite": 1.0}
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
GOV_SATURATED = 0.95    # x the cores: the machine's measured CPU at or over it is starved, whatever is charged
GOV_SPARE = 0.5         # x the cores: under it, spare ($AGENTCTL_GOV_SPARE)
GOV_IDLE_CLAUDE = 10.0  # %CPU under which a Claude session is idle (not work)
GOV_CPU_FACTOR = 1.0    # charged cores (or the load) + a job's own may reach this x the cores ($AGENTCTL_GOV_CPU_FACTOR)
# A job's expected cores, by its name (scripts/jvm-gate.sh passes --job: sbt-warm classifies its command); measured
# history (usage.jsonl) replaces the number once a job has GOV_USAGE_MIN uncontended runs. `job`: a JVM of unknown work.
GOV_JOB_CORES = {"testUnit": 4.0, "test": 4.0, "what-if": 4.0, "measure": 4.0, "check": 4.0, "compile": 3.0,
                 "job": 3.0, "testOnly": 2.0, "slice": 2.0, "suite": 2.0, "serve": 1.0, "keeper": 0.0}
GOV_USAGE_MIN = 3       # uncontended runs of a job before its measured cores replace the default
GOV_USAGE_DAYS = 14
GOV_USAGE_MIN_WALL = 20  # seconds: a shorter call says nothing about a job's cores
GOV_EVICT_IDLE = 60     # a kept server unused this long (seconds) may be evicted
GOV_POLL = 2.0
GOV_REPORT = 600


def gov_dir():
    d = os.environ.get("AGENTCTL_GOV_DIR") or state_root("gov")
    os.makedirs(d, exist_ok=True)
    return d


def gov_log(msg):
    with open(os.path.join(gov_dir(), "gov.log"), "a") as f:
        f.write(f"{ts()} {msg}\n")


def read_meminfo(path="/proc/meminfo"):
    """{field: kB} of Linux's /proc/meminfo ({} elsewhere)."""
    try:
        with open(path) as f:
            return {m.group(1): int(m.group(2)) for m in re.finditer(r"^(\w+):\s+(\d+)", f.read(), re.M)}
    except OSError:
        return {}


# A FIXED MACHINE ($AGENTCTL_MACHINE, JSON): ram_gb, free_gb, pressure, swap_used_mb, swap_total_mb, load1, cores,
# free_pct, level -- read in place of this host's (sysctl, vm_stat, /proc/meminfo, the load, the CPU count). The tests'
# one seam: tests/harness.py pins an idle 12-core 36 GB machine, so no test depends on the runner it runs on
# (2026-10-31: GitHub's 4-core hosted runner, with its own load and RAM, failed a dozen governor, check-cap and land
# tests that pass on the laptop and on ci-runner). A test of a reader passes its own input or sets its own probe.
def machine_fixed():
    raw = os.environ.get("AGENTCTL_MACHINE")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def machine_load1():
    m = machine_fixed()
    return float(m["load1"]) if m and m.get("load1") is not None else os.getloadavg()[0]


def machine_cores():
    m = machine_fixed()
    return int(m["cores"]) if m and m.get("cores") else (os.cpu_count() or 1)


def mem_stats():
    """THE machine's memory, macOS or Linux alike: {ram_gb, free_gb, pressure (% of RAM not available), swap_used_mb,
    swap_total_mb}, each None when unreadable. macOS: sysctl, vm_stat, memory_pressure; Linux (ci-runner, where a check
    may fall back to running locally): /proc/meminfo (MemAvailable). Before, Linux read none of it: the budget assumed
    36 GB and free memory, pressure and swap were unknown, so the governor admitted blind."""
    out = dict.fromkeys(("ram_gb", "free_gb", "pressure", "swap_used_mb", "swap_total_mb"))
    fixed = machine_fixed()
    if fixed is not None:
        return {k: fixed.get(k) for k in out}
    mi = read_meminfo()
    if mi.get("MemTotal"):
        out["ram_gb"] = mi["MemTotal"] / 2 ** 20
        if "MemAvailable" in mi:
            out["free_gb"] = mi["MemAvailable"] / 2 ** 20
            out["pressure"] = round(100 * (1 - mi["MemAvailable"] / mi["MemTotal"]))
        if "SwapTotal" in mi and "SwapFree" in mi:
            out["swap_total_mb"] = mi["SwapTotal"] / 1024
            out["swap_used_mb"] = (mi["SwapTotal"] - mi["SwapFree"]) / 1024
        return out
    r = run_cmd(["sysctl", "-n", "hw.memsize"], timeout=10)
    if r.stdout.strip().isdigit():
        out["ram_gb"] = int(r.stdout.strip()) / 2 ** 30
    vm = run_cmd(["vm_stat"], timeout=10).stdout
    page = re.search(r"page size of (\d+) bytes", vm)
    if page:
        pages = sum(int(m.group(2)) for m in re.finditer(r"Pages (free|inactive|speculative):\s+(\d+)", vm))
        out["free_gb"] = pages * int(page.group(1)) / 2 ** 30
    lvl = run_cmd(["sysctl", "-n", "kern.memorystatus_level"], timeout=10).stdout.strip()  # memory_pressure's free %
    if lvl.isdigit():
        out["pressure"] = 100 - int(lvl)
    sw = run_cmd(["sysctl", "-n", "vm.swapusage"], timeout=10).stdout
    tot, used = re.search(r"total = ([\d.]+)M", sw), re.search(r"used = ([\d.]+)M", sw)
    if tot and used:
        out["swap_total_mb"], out["swap_used_mb"] = float(tot.group(1)), float(used.group(1))
    return out


def ram_gb():
    return mem_stats()["ram_gb"] or 36.0


def gov_budget():
    return env_num("AGENTCTL_GOV_BUDGET_GB", round(0.6 * ram_gb(), 1))


def free_gb():
    """GB of RAM a new heap can have now (free + inactive + speculative pages); $AGENTCTL_GOV_FREE_PROBE's output when
    set (the tests' seam); None when unreadable."""
    probe = os.environ.get("AGENTCTL_GOV_FREE_PROBE")
    if probe:
        try:
            return float(run_cmd(probe, shell=True).stdout.strip())
        except ValueError:
            return None
    return mem_stats()["free_gb"]


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


def gov_tickets():
    """The live tickets' records (their lock held), oldest first; a dead ticket's files are removed."""
    out = []
    for p in sorted(glob.glob(os.path.join(gov_dir(), "t-*.json"))):
        lock = p[:-5] + ".lock"
        if not lock_held(lock):
            try:
                if time.time() - os.path.getmtime(p) > 30:  # never one being written
                    os.unlink(p)
                    os.unlink(lock)
                    gov_log_end(os.path.basename(lock))
                    gov_bump()
            except OSError:
                pass
            continue
        rec = read_record(p)
        if rec:
            rec["path"] = lock
            out.append(rec)
    return sorted(out, key=lambda r: r.get("at", 0))


def gov_log_end(ticket_id, at=None):
    """A ticket's end, in admits.jsonl beside its admission (`gov replay` reads both): exact for a ticket released in
    process, the first look after it for one a shell held (the kernel freed it)."""
    with open(os.path.join(gov_dir(), "admits.jsonl"), "a") as af:
        af.write(json.dumps({"end": round(at or time.time(), 1), "id": ticket_id}) + "\n")


def gov_committed(tickets):
    return sum(float(t.get("mem") or 0) for t in tickets)


def gov_charged(tickets):
    """The cores the live tickets are charged (a ticket from before cores were charged: none)."""
    return sum(float(t.get("cores") or 0) for t in tickets)


def gov_usage(days=GOV_USAGE_DAYS, now=None):
    """usage.jsonl's records of the last `days`: {at, job, wall, cpu, cores, load1, machine_cores}."""
    cutoff = (now or time.time()) - days * 86400
    out = []
    try:
        with open(os.path.join(gov_dir(), "usage.jsonl")) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("at", 0) >= cutoff:
                    out.append(r)
    except OSError:
        pass
    return out


def gov_measured_cores(job, usage=None):
    """The job's p75 cores over its uncontended runs (the load at its end no more than the cores: a starved run uses
    fewer cores than it wants), to the half core; None under GOV_USAGE_MIN such runs."""
    runs = [float(r["cores"]) for r in (gov_usage() if usage is None else usage)
            if r.get("job") == job and r.get("cores") is not None and float(r.get("wall") or 0) >= GOV_USAGE_MIN_WALL
            and r.get("load1") is not None and float(r["load1"]) <= float(r.get("machine_cores") or machine_cores())]
    if len(runs) < GOV_USAGE_MIN:
        return None
    return min(float(machine_cores()), max(0.5, round(2 * pct(runs, .75)) / 2))


def gov_job_cores(job=None, cls="agent", kind="job"):
    """The cores a ticket is charged: its job's measured cores, else its default; with no job named, a check's (agentctl's
    own pre-commit check: the hook's sbt) or the suite's, else none (a caller from before cores were charged: memory
    only, as it was)."""
    if not job:
        job = "check" if kind == "check" else "suite" if cls == "suite" else None
        if not job:
            return 0.0
    if job == "keeper":
        return 0.0
    measured = gov_measured_cores(job)
    return measured if measured is not None else GOV_JOB_CORES.get(job, GOV_JOB_CORES["job"])


def gov_record_usage(job, wall, cpu, load1=None, label=""):
    """One run's cores (CPU seconds over wall seconds) for the job's measured history; a run under GOV_USAGE_MIN_WALL
    is not recorded."""
    if wall < GOV_USAGE_MIN_WALL:
        return None
    rec = {"at": time.time(), "job": job, "label": label, "wall": round(wall, 1), "cpu": round(cpu, 1),
           "cores": round(cpu / wall, 2), "load1": machine_load1() if load1 is None else load1,
           "machine_cores": machine_cores()}
    with open(os.path.join(gov_dir(), "usage.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def gov_ramp(tickets, now=None):
    now = now or time.time()
    return sum(float(t.get("mem") or 0) for t in tickets if now - t.get("at", 0) < GOV_RAMP)


# THE WHOLE MACHINE'S CPU (2026-11-22: top 0.24% idle -- 44% user, 56% sys -- at load 131-150 on 18 cores, while ps's
# %CPU summed to 6.1 cores and the governor saw no pressure). ps's %CPU is a decaying per-process average: it misses
# kernel time and the fork/exec churn of short-lived processes (git, test fake repos, run-script-tests trees,
# syspolicyd scanning each new executable). The busy cores are the machine's own CPU ticks -- user + sys + nice over
# a delta (host_statistics on macOS, /proc/stat on Linux) -- times the cores.
def cpu_ticks():
    """(busy ticks, all ticks) of the whole machine since boot, or None where unreadable."""
    try:
        if os.path.exists("/proc/stat"):
            with open("/proc/stat") as f:
                v = [int(x) for x in f.readline().split()[1:]]
            idle = v[3] + (v[4] if len(v) > 4 else 0)  # idle + iowait
            return sum(v[:8]) - idle, sum(v[:8])
        import ctypes, ctypes.util
        lib = ctypes.CDLL(ctypes.util.find_library("System"))
        lib.mach_host_self.restype = ctypes.c_uint
        info, count = (ctypes.c_uint * 4)(), ctypes.c_uint(4)
        if lib.host_statistics(lib.mach_host_self(), 3, ctypes.byref(info), ctypes.byref(count)) != 0:  # CPU_LOAD
            return None
        user, system, idle, nice = info
        return user + system + nice, user + system + idle + nice
    except (OSError, ValueError, AttributeError, IndexError):
        return None


CPU_SAMPLE_GAP = 0.25  # s between two tick samples when no recent one is kept
CPU_SAMPLE_KEEP = 120  # s a kept sample serves as the delta's start


def machine_busy_cores(n, read=None, sleep=None, state=None):
    """The cores the whole machine is busy on (user + sys + nice), from a tick delta: against the sample kept in
    <gov>/cpu-ticks.json when it is 0.5-120 s old, else two samples CPU_SAMPLE_GAP apart. None where unreadable."""
    read, path = read or cpu_ticks, state or os.path.join(gov_dir(), "cpu-ticks.json")
    prev, now_ = read_record(path) or {}, time.time()
    start = prev.get("ticks") if 0.5 <= now_ - float(prev.get("at") or 0) <= CPU_SAMPLE_KEEP else None
    if not start:
        start = read()
        (sleep or time.sleep)(CPU_SAMPLE_GAP)
    end = read()
    if not start or not end or end[1] <= start[1]:
        return None
    try:
        write_json(path, {"at": now_, "ticks": list(end)})
    except OSError:
        pass
    return round(n * (end[0] - start[0]) / (end[1] - start[1]), 2)


def gov_busy_cores():
    """The cores processes use now: ps's %CPU summed, less the governor's waiters (`agentctl gov admit`) and idle Claude
    sessions. Seams: $AGENTCTL_GOV_BUSY_PROBE (its output: the number), the tests' $AGENTCTL_GOV_LOAD_PROBE (read as
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
    if whole is not None:
        return whole
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


def gov_capacity(tickets=None, m=None):
    """{state: starved | ok | spare, busy, charged, measured, cores, load1}: the laptop's CPU now, with hysteresis (the
    last state kept in <gov>/capacity.json between GOV_RESUME and GOV_STARVED)."""
    tickets = gov_tickets() if tickets is None else tickets
    n = (m or {}).get("cores") or machine_cores()
    charged, measured = gov_charged(tickets), gov_busy_cores()
    busy = max(charged, measured)
    path = os.path.join(gov_dir(), "capacity.json")
    prev = (read_record(path) or {}).get("state", "ok")
    if busy > env_num("AGENTCTL_GOV_STARVED", GOV_STARVED) * n or measured >= GOV_SATURATED * n:
        state = "starved"  # charged past the cores, or the whole machine's CPU saturated (it never reads past them)
    elif busy < env_num("AGENTCTL_GOV_SPARE", GOV_SPARE) * n:
        state = "spare"
    elif busy < env_num("AGENTCTL_GOV_RESUME", GOV_RESUME) * n:
        state = "ok"
    else:
        state = "starved" if prev == "starved" else "ok"
    if state != prev:
        try:
            write_json(path, {"state": state, "busy": busy, "at": time.time()})
            gov_log(f"capacity {prev} -> {state}: {busy:.1f} of {n} cores busy ({charged:g} charged, {measured:.1f} "
                    "measured)")
        except OSError:
            pass
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


def gov_bump():
    """Mark that the queue changed (a waiter or a ticket left): every waiter behind the head looks again now."""
    try:
        with open(os.path.join(gov_dir(), "changed"), "a"):
            pass
        os.utime(os.path.join(gov_dir(), "changed"))
    except OSError:
        pass


def gov_change_mark():
    try:
        return os.stat(os.path.join(gov_dir(), "changed")).st_mtime_ns
    except OSError:
        return 0


def gov_wait_change(mark, secs):
    """Sleep up to secs, returning early when the change mark moves past `mark`."""
    end = time.monotonic() + secs
    step = min(env_num("AGENTCTL_GOV_POLL", GOV_POLL), secs)
    while time.monotonic() < end:
        time.sleep(max(0.01, min(step, end - time.monotonic())))
        if gov_change_mark() != mark:
            return


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


def idle_servers(min_idle=None):
    """The kept sbt servers the governor may evict, unused longest first: [(idle seconds, root, key prefix)]. Never the
    root checkout's (the owner's own), a serving one, or one a call is running in (its busy lock held)."""
    min_idle = GOV_EVICT_IDLE if min_idle is None else min_idle
    out = []
    for f in glob.glob(os.path.join(sbt_warm_state(), "*.root")):
        prefix = f[:-5]
        try:
            with open(f) as rf, open(prefix + ".pid") as pf:
                root, pid = rf.read().strip(), int(pf.read().strip())
        except (OSError, ValueError):
            continue
        if not os.path.isdir(root) or os.path.isdir(os.path.join(root, ".git")) or not pid_alive(pid):
            continue
        if os.path.exists(prefix + ".serving") or lock_held(prefix + ".busy"):
            continue
        try:
            idle = time.time() - os.path.getmtime(prefix + ".used")
        except OSError:
            continue
        if idle >= min_idle:
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
                subprocess.run([script, "--stop", "--root"], cwd=root, capture_output=True, text=True, timeout=180)
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


def gov_admit(cls, mem=None, label="", kind="job", fd=None, ticket=None, wait=True, force=False, pid=None, cwd=None,
              report=None, max_wait=None, cores=None, job=None):
    """Admit one unit of work: a GovTicket once it may start (None for a --try that may not). With `fd`, the lock is
    taken on that inherited descriptor (a shell's, which then holds the ticket); else on a file this process keeps open.
    `cores`: what it is charged (default: its `job`'s, gov_job_cores)."""
    cls = cls if cls in GOV_CLASSES else "agent"
    mem = GOV_MEM_GB.get(cls, 5.0) if mem is None else float(mem)
    cores = gov_job_cores(job, cls, kind) if cores is None else float(cores)
    d = gov_dir()
    t0 = time.time()
    me = os.path.join(d, f"w-{t0:.6f}-{os.getpid()}.json")
    write_json(me, {"class": cls, "mem": mem, "cores": cores, "job": job, "label": label, "kind": kind,
                    "pid": os.getpid(), "at": t0, "start": process_start(os.getpid())})
    said, report_at, why, waited_on = False, 0.0, None, None
    report = report or err
    idle = env_num("AGENTCTL_GOV_POLL", GOV_POLL)  # a waiter behind the head backs off from here to GOV_IDLE_MAX
    try:
        while True:
            mark = gov_change_mark()
            tickets = gov_tickets()
            waiters = gov_waiters()
            head = waiters and os.path.abspath(waiters[0]["path"]) == os.path.abspath(me)
            if force:
                ok, why, on_mem = True, None, False
            elif not head:
                w = waiters[0] if waiters else {}
                ok, on_mem = False, False
                why = f"behind {w.get('class')} work ({w.get('label') or '?'}, pid {w.get('pid')})"
            else:
                ok, why, on_mem = gov_fits(cls, mem, tickets, gov_machine(), kind, cores)
            if ok:
                break
            waited_on = why  # the last hold, for the record: `why` is None once it fits
            if head and on_mem and cls != "measure" and gov_evict(f"{cls} {label} ({why})"):
                time.sleep(env_num("AGENTCTL_GOV_EVICT_SETTLE", 3))  # the freed memory shows in vm_stat
                continue
            if not wait:
                return None
            if max_wait is not None and time.time() - t0 > max_wait:
                raise GovTimeout(f"the governor did not admit {cls} work ({label}) in {int(max_wait // 60)} min: {why}")
            try:  # a check waiting its turn holds its leases (bounded by max_wait and their max hold), never loses them
                lock_beat(f"gov-wait:{label}:{int(time.time() // 60)}")
            except LeaseLost as ex:
                raise GovTimeout(f"{ex} while waiting for the governor") from None
            if time.monotonic() >= report_at:
                report(f"{'still ' if said else ''}waiting for the governor ({cls}, {mem:g} GB, {cores:g} cores, {label}): "
                       f"{why}; {len(tickets)} admitted, {gov_committed(tickets):.0f}/{gov_budget():.0f} GB, "
                       f"{gov_charged(tickets):g} cores charged")
                said, report_at = True, time.monotonic() + GOV_REPORT
            if head:  # the head reads the machine every poll: memory and load free up with no event
                idle = env_num("AGENTCTL_GOV_POLL", GOV_POLL)
                time.sleep(idle)
            else:
                gov_wait_change(mark, idle)
                idle = min(idle * 2, env_num("AGENTCTL_GOV_IDLE_MAX", GOV_IDLE_MAX))
        # THE TICKET BEFORE THE WAITER GOES: the next head never looks between the two and misses this admission
        path = ticket or os.path.join(d, f"t-{t0:.6f}-{os.getpid()}.lock")
        f = None
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            f = open(path, "a+")
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        waited = round(time.time() - t0, 1)
        rec = {"class": cls, "mem": mem, "cores": cores, "job": job, "label": label, "kind": kind,
               "pid": pid or os.getpid(), "cwd": cwd or os.getcwd(), "at": time.time(), "since": ts(), "waited": waited,
               "waited_on": waited_on if waited >= 1 else None, "id": os.path.basename(path)}
        write_json(path[:-5] + ".json", rec)
        with open(os.path.join(d, "admits.jsonl"), "a") as af:
            af.write(json.dumps({k: rec[k] for k in ("class", "mem", "cores", "job", "label", "kind", "at", "waited",
                                                     "waited_on", "id")}) + "\n")
        if said:
            report(f"admitted by the governor after {int(waited)} s ({cls}, {mem:g} GB, {cores:g} cores, {label})")
        return GovTicket(path, rec, f)
    finally:
        try:
            os.unlink(me)
        except OSError:
            pass
        gov_bump()  # the next waiter is the head now: it looks at once


@contextlib.contextmanager
def gov_ticket(cls, mem=None, label="", kind="job", report=None, cores=None, job=None):
    t = gov_admit(cls, mem, label, kind=kind, report=report, cores=cores, job=job)
    try:
        yield t
    finally:
        t.release()


# THE ROUTE of a heavy job (the owner, 2026-11-18: "stop the laptop being the bottleneck"): a whole-module test run or
# a commit-gate proof (scripts/fail-before: two full runs) the governor would HOLD now goes to ci-runner's measure lane
# (scripts/identity-ci-run's identity-run.yml, which runs sbt there) when that lane is free -- no job waiting on its
# label in ci-runners-page's last good read (redseen.py's runner_queue) -- instead of queueing on the laptop. A red fix is
# never held, so it stays local; so does anything the lane cannot be read for.
MEASURE_LANE = "movies-measure"


def measure_lane_free(q=None):
    """(free?, why) of ci-runner's measure lane: free when the runners page is fresh and no job waits on its label."""
    q = runner_queue() if q is None else q
    if not q:
        return False, "ci-runners-page has no fresh read of ci-runner"
    waiting = [w for w in q.get("waiting_jobs") or [] if MEASURE_LANE in str(w.get("label") or "")]
    if waiting:
        return False, f"{len(waiting)} job(s) already wait on {MEASURE_LANE}"
    return True, f"no job waits on {MEASURE_LANE}"


def gov_route(job, cls="agent", mem=None, kind="job", q=None):
    """("ci" | "local", why) for a job about to ask the governor: ci when it would be held now and the lane is free."""
    if cls == "red":
        return "local", "a red fix is never held"
    cores = gov_job_cores(job, cls, kind)
    mem = GOV_MEM_GB.get(cls, 5.0) if mem is None else float(mem)
    ahead = [w for w in gov_waiters() if GOV_CLASSES.get(w.get("class"), 9) <= GOV_CLASSES.get(cls, 9)]
    if ahead:
        held = f"{len(ahead)} waiter(s) ahead of it"
    else:
        ok, held, _ = gov_fits(cls, mem, gov_tickets(), gov_machine(), kind, cores)
        if ok:
            return "local", f"the governor admits it now ({cores:g} cores)"
    free, lane = measure_lane_free(q)
    if free:
        return "ci", f"the governor would hold it ({held}); {lane}"
    return "local", f"the governor would hold it ({held}) but {lane}: it waits for the governor"


# THE REPLAY (`agentctl gov replay`): the day's admissions (admits.jsonl) again under the cores rule, to show what it
# would have admitted. Each admission is an arrival at at - waited, held until its end record (gov_log_end) or, where
# none was written (before 2026-11-18), for --hold minutes; its cores are its record's, else its job's by label. The
# replay admits oldest first while the charged cores + its own fit the cap (one at a time when none is charged).
def replay_cores(r):
    if r.get("cores") is not None:
        return float(r["cores"])
    label, cls = r.get("label") or "", r.get("class")
    job = ("what-if" if "what-if" in label else "slice" if "identity-slice" in label else "measure" if cls == "measure"
           else "check" if r.get("kind") == "check" or cls == "check" else "job")
    return GOV_JOB_CORES[job]


def gov_replay(rows, cap, at, hold=30 * 60):
    """{old: [(label, cores)] live at `at` as admitted, new: [...] live at `at` under the cores rule, waiting: [...],
    old_peak / new_peak: the most cores live at once}."""
    ends = {r["id"]: r["end"] for r in rows if "end" in r and r.get("id")}
    jobs = []
    for r in rows:
        if "end" in r or "at" not in r:
            continue
        start = float(r["at"])
        dur = ends.get(r.get("id"), start + hold) - start
        jobs.append({"label": r.get("label") or "?", "cores": replay_cores(r), "arrive": start - float(r.get("waited") or 0),
                     "admitted": start, "dur": max(dur, 1.0)})

    def live(js, t, key):
        return [j for j in js if j.get(key) is not None and j[key] <= t < j[key] + j["dur"]]

    def peak(js, key):
        return max((sum(x["cores"] for x in live(js, j[key], key)) for j in js if j.get(key) is not None), default=0.0)
    # the new rule, event by event: arrivals in order, each admitted once the charged cores leave it room
    queue, running, t = sorted((dict(j, new=None) for j in jobs), key=lambda j: j["arrive"]), [], None
    pending = list(queue)
    events = sorted({j["arrive"] for j in queue})
    while pending:
        t = events.pop(0) if events else t
        running = [j for j in running if j["new"] + j["dur"] > t]
        for j in [p for p in pending if p["arrive"] <= t]:
            charged = sum(x["cores"] for x in running)
            if j["cores"] == 0 or charged == 0 or charged + j["cores"] <= cap:
                j["new"] = t
                running.append(j)
                pending.remove(j)
            else:
                break  # oldest first: none overtakes the head
        nxt = [x["new"] + x["dur"] for x in running] + [p["arrive"] for p in pending if p["arrive"] > t]
        if pending and not nxt:
            break
        if nxt:
            events = sorted(set(events) | {min(nxt)})
    return {"old": [(j["label"], j["cores"]) for j in live(jobs, at, "admitted")],
            "new": [(j["label"], j["cores"]) for j in live(queue, at, "new")],
            "waiting": [(j["label"], j["cores"]) for j in queue if j["arrive"] <= at and (j["new"] is None or j["new"] > at)],
            "old_peak": peak(jobs, "admitted"), "new_peak": peak(queue, "new"),
            "new_wait_p50": pct([j["new"] - j["arrive"] for j in queue if j["new"] is not None], .5) or 0.0,
            "old_wait_p50": pct([j["admitted"] - j["arrive"] for j in jobs], .5) or 0.0}


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
LAPTOP_LOAD = 2.0             # holds other work while the 1-min load is over this x the cores
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


def run_queue():
    """The 1-min load average: the run queue, beside the busy cores (a saturated CPU is pressure only when it is
    oversubscribed too). None where unreadable."""
    try:
        return os.getloadavg()[0]
    except OSError:
        return None


def pressure_reading(sysctl_out=None, meminfo=None, load1=None, cores=None, now_=None):
    """{free_pct, level, load1, cores, swap_mb, swap_growth_mb, swap_span}: this machine now, None where unreadable.
    The readers are the tests' seams: sysctl_out (pressure_sysctl's text), meminfo (read_meminfo's dict)."""
    now_ = time.time() if now_ is None else now_
    # its CPU is the cores processes use (gov_busy_cores), never the load average, which counts threads waiting (46 on
    # 18 cores while 5.2 were busy, 2026-11-19); `load1` passed in (the tests' seam) stands for it
    r = {"free_pct": None, "level": None, "swap_mb": None, "load1": gov_busy_cores() if load1 is None else load1,
         "cores": cores or machine_cores(), "runq": run_queue() if load1 is None else None}
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


def cpu_pressure(r):
    """The CPU's part of laptop_pressure: busy cores past LAPTOP_LOAD x the cores (a reading charged past them), or the
    whole machine's CPU saturated (GOV_SATURATED, user + sys) with a run queue over LAPTOP_LOAD x the cores -- never
    a long run queue alone (46 threads waiting on 18 cores while cores sat idle, 2026-11-19)."""
    factor, n, busy, runq = env_num("AGENTCTL_LAPTOP_LOAD", LAPTOP_LOAD), r.get("cores"), r.get("load1"), r.get("runq")
    if busy is None or not n:
        return []
    if busy > factor * n:
        return [f"{busy:.1f} cores busy of {n} (holds over {factor * n:g})"]
    if busy >= GOV_SATURATED * n and runq is not None and runq > factor * n:
        return [f"CPU saturated: {busy:.1f} of {n} cores busy (user + sys), run queue {runq:.0f} (holds at "
                f"{GOV_SATURATED:.0%} busy with a run queue over {factor * n:g})"]
    return []


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


def cmd_suite(a):
    """`agentctl suite [tests/run.py args]`: agentctl's own test suite (tests/run.py: parallel, a timeout per test; in
    this directory) under a governor ticket of class `suite` (SUITE_CAP at once): parallel agents queue here instead of
    piling up. Arguments are run.py's (-j N, --timeout S, PATTERN...)."""
    args = list(a.args or [])
    with gov_ticket("suite", label=f"agentctl suite {os.path.basename(os.getcwd())}"):  # run.py then takes none
        return subprocess.run([sys.executable, os.path.join("tests", "run.py")] + args,
                              env={**os.environ, "AGENTCTL_SUITE_TICKET": "agentctl suite"}).returncode


def gov_class_of(repo, commits, reds, st=None):
    """red when the commits' Fixes-Red names an open red -- the gate's (reds) or any other the store has open: the
    change's KIND (changekind.py classify_change) -- else check."""
    fixes = fixes_of(repo, commits)
    if not fixes:
        return "check"
    return "red" if classify_change({"fixes": sorted(fixes)}, queue_facts(reds, st))["red_fix"] else "check"


def cmd_gov(a):
    if a.gcmd == "admit":
        t = gov_admit(a.cls, a.mem, a.label or "", kind=a.kind, fd=a.fd, ticket=a.ticket, wait=not a.try_,
                      force=a.force, pid=a.pid, cwd=a.cwd, cores=a.cores, job=a.job)
        return 0 if t else 1
    if a.gcmd == "usage":
        rec = gov_record_usage(a.job, a.wall, a.cpu, a.load1, a.label or "")
        print(f"{a.job}: {rec['cores']:g} cores over {a.wall:g} s" if rec else f"{a.job}: under {GOV_USAGE_MIN_WALL} s, not recorded")
        return 0
    if a.gcmd == "cores":
        print(f"{gov_job_cores(a.job, a.cls):g}")
        return 0
    if a.gcmd == "route":
        where, why = gov_route(a.job, a.cls, a.mem)
        print(f"{where}: {why}")
        return 0 if where == "ci" else 1
    if a.gcmd == "replay":
        rows = []
        try:
            with open(os.path.join(gov_dir(), "admits.jsonl")) as f:
                for line in f:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass
        at = dt.datetime.fromisoformat(a.at.replace("Z", "+00:00")).timestamp()
        rows = [r for r in rows if abs(float(r.get("at", r.get("end", 0))) - at) <= 12 * 3600]
        cap = env_num("AGENTCTL_GOV_CPU_FACTOR", GOV_CPU_FACTOR) * (a.cores or machine_cores())
        res = gov_replay(rows, cap, at, a.hold * 60)
        fmt = lambda js: ", ".join(f"{l} {c:g}" for l, c in js) or "-"
        print(f"replay at {a.at} (cap {cap:g} cores; a ticket with no end record held {a.hold:g} min):")
        print(f"  as admitted:    {len(res['old'])} live, {sum(c for _, c in res['old']):g} cores: {fmt(res['old'])}")
        print(f"  the cores rule: {len(res['new'])} live, {sum(c for _, c in res['new']):g} cores: {fmt(res['new'])}; "
              f"{len(res['waiting'])} waiting: {fmt(res['waiting'])}")
        print(f"  peak cores live: {res['old_peak']:g} as admitted, {res['new_peak']:g} under the rule; p50 wait "
              f"{res['old_wait_p50'] / 60:.0f} min as admitted, {res['new_wait_p50'] / 60:.0f} min under the rule")
        return 0
    if a.gcmd == "evict":
        if a.dry_run:
            for idle, root, _p in idle_servers():
                print(f"would evict {root} (unused {int(idle // 60)} min)")
            return 0
        root = gov_evict("by hand (agentctl gov evict)")
        print(f"evicted {root}" if root else "no idle sbt server to evict")
        return 0
    if a.gcmd == "capacity":
        cap, tickets = gov_capacity(), gov_tickets()
        print(f"{cap['state']}: {cap['busy']:.1f} of {cap['cores']} cores busy ({cap['charged']:g} charged, "
              f"{cap['measured']:.1f} measured, load {machine_load1():.1f}); {len(tickets)} admitted, "
              f"{len(gov_waiters())} waiting")
        return 0
    if a.gcmd == "status":
        tickets, m = gov_tickets(), gov_machine()
        cap = env_num("AGENTCTL_GOV_CPU_FACTOR", GOV_CPU_FACTOR) * m["cores"]
        print(f"governor: {len(tickets)} admitted, {gov_committed(tickets):.1f}/{gov_budget():.0f} GB committed, "
              f"{gov_charged(tickets):g}/{cap:g} cores charged, {gov_ramp(tickets):.0f} GB still growing; free {m['free'] if m['free'] is None else round(m['free'], 1)} "
              f"GB, pressure {m['pressure']}%, load {m['load1']:.1f}/{m['cores']} cores; check cap {checks_cap()}")
        for t in tickets:
            print(f"  {t.get('class'):<7} {t.get('mem'):>4} GB {float(t.get('cores') or 0):>4g} cores  {t.get('kind'):<5} "
                  f"{t.get('label')}  pid {t.get('pid')}  "
                  f"since {t.get('since')}  {t.get('cwd')}")
        for w in gov_waiters():
            print(f"  WAITING {w.get('class'):<7} {w.get('mem'):>4} GB {float(w.get('cores') or 0):>4g} cores  "
                  f"{w.get('label')}  pid {w.get('pid')}")
        idle = idle_servers()
        if idle:
            print("evictable: " + ", ".join(f"{os.path.basename(r)} ({int(i // 60)} min)" for i, r, _p in idle))
        why, summary = pressure_report()
        print(f"pressure: {why} -- holds all but red fixes [{summary}]" if why else f"pressure: none [{summary}]")
        return 0
    if a.gcmd == "stats":
        rc = cmd_checks(argparse.Namespace(ccmd="stats", days=a.days))
        cutoff = time.time() - a.days * 86400
        rows = []
        try:
            with open(os.path.join(gov_dir(), "admits.jsonl")) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("at", 0) >= cutoff:
                        rows.append(r)
        except OSError:
            pass
        print(f"{'class':>7} {'admits':>6} {'p50 wait':>9} {'p90 wait':>9} {'waited>1m':>9}")
        for cls in GOV_CLASSES:
            rs = [r for r in rows if r.get("class") == cls]
            if rs:
                w = [r.get("waited", 0) for r in rs]
                print(f"{cls:>7} {len(rs):>6} {pct(w, .5):>8.0f}s {pct(w, .9):>8.0f}s {sum(1 for x in w if x > 60):>9}")
        usage = gov_usage(a.days)
        for job in sorted({r.get("job") for r in usage if r.get("job")}):
            m_ = gov_measured_cores(job, usage)
            print(f"cores {job}: {sum(1 for r in usage if r.get('job') == job)} runs, charged "
                  f"{gov_job_cores(job):g} ({'measured p75' if m_ is not None else 'default'})")
        try:
            ev = sum(1 for l in open(os.path.join(gov_dir(), "gov.log")) if " evicted " in l)
        except OSError:
            ev = 0
        print(f"evictions (all time): {ev}")
        return rc
    return 2

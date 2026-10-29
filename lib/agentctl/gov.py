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
#   memory_pressure's free %) must be under 75% (90% for red).
# - CPU: checks wait while the 1-min load is over 4x the cores (a load of ~250 kept an sbt server from starting in 5 min
#   and failed two checks, 2026-10-27), agent work over 1.5x, measurements over 1x; red never.
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
GOV_PRESSURE_STOP = {"red": 90}  # memory pressure (%) at which a class waits; others: GOV_PRESSURE_DEFAULT
GOV_PRESSURE_DEFAULT = 75
GOV_LOAD = {"check": 4.0, "agent": 1.5, "measure": 1.0}  # waits while load1 > factor x cores; red never waits on CPU
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


def ram_gb():
    try:
        r = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=10)
        return int(r.stdout.strip()) / 2 ** 30
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 36.0


def gov_budget():
    return env_num("AGENTCTL_GOV_BUDGET_GB", round(0.6 * ram_gb(), 1))


def free_gb():
    """GB of RAM a new heap can have now (free + inactive + speculative pages); $AGENTCTL_GOV_FREE_PROBE's output when
    set (the tests' seam); None when unreadable."""
    probe = os.environ.get("AGENTCTL_GOV_FREE_PROBE")
    if probe:
        try:
            return float(subprocess.run(probe, shell=True, capture_output=True, text=True).stdout.strip())
        except ValueError:
            return None
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    page = re.search(r"page size of (\d+) bytes", out)
    pages = sum(int(m.group(2)) for m in re.finditer(r"Pages (free|inactive|speculative):\s+(\d+)", out))
    return pages * int(page.group(1)) / 2 ** 30 if page else None


def gov_machine():
    """{free, pressure, load1, cores}: live memory and CPU ($AGENTCTL_GOV_PRESSURE_PROBE / _LOAD_PROBE: the tests')."""
    load1, _cpu, pressure, _swap = machine_sample()
    if os.environ.get("AGENTCTL_GOV_PRESSURE_PROBE"):
        pressure = float(subprocess.run(os.environ["AGENTCTL_GOV_PRESSURE_PROBE"], shell=True, capture_output=True,
                                        text=True).stdout.strip() or 0)
    if os.environ.get("AGENTCTL_GOV_LOAD_PROBE"):
        load1 = float(subprocess.run(os.environ["AGENTCTL_GOV_LOAD_PROBE"], shell=True, capture_output=True,
                                     text=True).stdout.strip() or 0)
    return {"free": free_gb(), "pressure": pressure, "load1": load1, "cores": os.cpu_count() or 1}


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
            except OSError:
                pass
            continue
        rec = read_record(p)
        if rec:
            rec["path"] = lock
            out.append(rec)
    return sorted(out, key=lambda r: r.get("at", 0))


def gov_committed(tickets):
    return sum(float(t.get("mem") or 0) for t in tickets)


def gov_ramp(tickets, now=None):
    now = now or time.time()
    return sum(float(t.get("mem") or 0) for t in tickets if now - t.get("at", 0) < GOV_RAMP)


def gov_fits(cls, mem, tickets, m, kind="job"):
    """(True, None) when work of class cls expecting `mem` GB may start now; else (False, why, on_memory)."""
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
    stop = GOV_PRESSURE_STOP.get(cls, GOV_PRESSURE_DEFAULT)
    if m.get("pressure") is not None and m["pressure"] >= stop:
        return False, f"memory pressure {m['pressure']:.0f}% (waits at {stop}%)", True
    factor = GOV_LOAD.get(cls)
    if factor and m.get("load1") is not None and m["load1"] > factor * m["cores"]:
        return False, f"load {m['load1']:.0f} over {factor:g}x {m['cores']} cores", False
    return True, None, False


def gov_waiters():
    """The live waiters, in admission order: (class priority, arrival)."""
    out = []
    for p in glob.glob(os.path.join(gov_dir(), "w-*.json")):
        rec = read_record(p)
        if not rec or not pid_alive(int(rec.get("pid", 0))):
            try:
                os.unlink(p)
            except OSError:
                pass
            continue
        rec["path"] = p
        out.append(rec)
    return sorted(out, key=lambda r: (GOV_CLASSES.get(r.get("class"), 9), r.get("at", 0)))


def sbt_warm_state():
    return os.environ.get("KINOWO_SBT_WARM_STATE") or os.path.join(HOME, ".cache", "kinowo-sbt-warm")


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
        for p in (self.path[:-5] + ".json", self.path):
            try:
                os.unlink(p)
            except OSError:
                pass
        if self.f:
            self.f.close()
            self.f = None


class GovTimeout(RuntimeError):
    """The governor did not admit the work within its bound (audit #12: an unbounded wait hid a stuck queue entry)."""


GOV_ADMIT_MAX = 2 * 3600  # a check waits this long for the governor at most ($AGENTCTL_GOV_ADMIT_MAX)


def gov_admit(cls, mem=None, label="", kind="job", fd=None, ticket=None, wait=True, force=False, pid=None, cwd=None,
              report=None, max_wait=None):
    """Admit one unit of work: a GovTicket once it may start (None for a --try that may not). With `fd`, the lock is
    taken on that inherited descriptor (a shell's, which then holds the ticket); else on a file this process keeps open."""
    cls = cls if cls in GOV_CLASSES else "agent"
    mem = GOV_MEM_GB.get(cls, 5.0) if mem is None else float(mem)
    d = gov_dir()
    t0 = time.time()
    me = os.path.join(d, f"w-{t0:.6f}-{os.getpid()}.json")
    write_json(me, {"class": cls, "mem": mem, "label": label, "kind": kind, "pid": os.getpid(), "at": t0})
    said, report_at, why = False, 0.0, None
    report = report or err
    try:
        while True:
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
                ok, why, on_mem = gov_fits(cls, mem, tickets, gov_machine(), kind)
            if ok:
                break
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
                report(f"{'still ' if said else ''}waiting for the governor ({cls}, {mem:g} GB, {label}): {why}; "
                       f"{len(tickets)} admitted, {gov_committed(tickets):.0f}/{gov_budget():.0f} GB")
                said, report_at = True, time.monotonic() + GOV_REPORT
            time.sleep(env_num("AGENTCTL_GOV_POLL", GOV_POLL))
    finally:
        try:
            os.unlink(me)
        except OSError:
            pass
    path = ticket or os.path.join(d, f"t-{t0:.6f}-{os.getpid()}.lock")
    f = None
    if fd is not None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        f = open(path, "a+")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    waited = round(time.time() - t0, 1)
    rec = {"class": cls, "mem": mem, "label": label, "kind": kind, "pid": pid or os.getpid(), "cwd": cwd or os.getcwd(),
           "at": time.time(), "since": ts(), "waited": waited, "waited_on": why if waited >= 1 else None}
    write_json(path[:-5] + ".json", rec)
    with open(os.path.join(d, "admits.jsonl"), "a") as af:
        af.write(json.dumps({k: rec[k] for k in ("class", "mem", "label", "kind", "at", "waited", "waited_on")}) + "\n")
    if said:
        report(f"admitted by the governor after {int(waited)} s ({cls}, {mem:g} GB, {label})")
    return GovTicket(path, rec, f)


@contextlib.contextmanager
def gov_ticket(cls, mem=None, label="", kind="job", report=None):
    t = gov_admit(cls, mem, label, kind=kind, report=report)
    try:
        yield t
    finally:
        t.release()


def gov_pressure():
    """Why the laptop is under pressure now (memory pressure, free memory or load past the governor's limits for a
    check), or None: `land` runs a change's checks on ci-runner while it says so."""
    m = gov_machine()
    if m.get("pressure") is not None and m["pressure"] >= env_num("AGENTCTL_REMOTE_PRESSURE", GOV_PRESSURE_DEFAULT):
        return f"memory pressure {m['pressure']:.0f}%"
    if m.get("free") is not None and m["free"] < env_num("AGENTCTL_GOV_RESERVE_GB", GOV_RESERVE_GB) + GOV_MEM_GB["check"] \
            and not idle_servers():
        return f"{m['free']:.1f} GB free and no idle sbt server to evict"
    if m.get("load1") is not None and m["load1"] > env_num("AGENTCTL_REMOTE_LOAD", 2.0) * m["cores"]:
        return f"load {m['load1']:.0f} on {m['cores']} cores"
    return None


# NO NEW FIXER ON A LAPTOP UNDER PRESSURE (the owner's coordinator, 2026-10-29: load 56-72 on 14 cores, swap 30.4 of
# 31.7 GB). The dispatcher starts no fixer while swap used is over FIXER_SWAP_GB, the load over FIXER_LOAD x the cores,
# or free memory under FIXER_FREE_GB: the red stays offered (`waiting: laptop pressure (...)`), takes no slot, and
# dispatches when the pressure clears; its offer's own deadline still escalates it (RED UNCLAIMED / STARVED).
FIXER_SWAP_GB = 16.0    # $AGENTCTL_FIXER_SWAP_GB: swap USED, absolute -- macOS shrinks the swap total as it frees,
                        # so a share of the total never drops (15.0 GB total, 13.9 used read as 93% with 57% RAM free)
FIXER_LOAD = 3.0        # $AGENTCTL_FIXER_LOAD (x cores)
FIXER_FREE_GB = 4.0     # $AGENTCTL_FIXER_FREE_GB


def swap_usage():
    """(used MB, total MB) of swap, or (None, None)."""
    try:
        r = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=10)
        tot, used = re.search(r"total = ([\d.]+)M", r.stdout), re.search(r"used = ([\d.]+)M", r.stdout)
        return (float(used.group(1)), float(tot.group(1))) if tot and used else (None, None)
    except (OSError, subprocess.TimeoutExpired):
        return None, None


def laptop_pressure(m=None, swap=None):
    """Why no new fixer may start on this laptop now, or None (gov_machine's free memory and load, vm.swapusage).
    $AGENTCTL_LAPTOP_PRESSURE set (the tests'): its value is the answer, `-` or empty for none."""
    if m is None and swap is None and "AGENTCTL_LAPTOP_PRESSURE" in os.environ:
        return os.environ["AGENTCTL_LAPTOP_PRESSURE"].strip("-") or None  # `-` or empty: none
    m = m or gov_machine()
    used, total = swap or swap_usage()
    if used is not None and used / 1024 > env_num("AGENTCTL_FIXER_SWAP_GB", FIXER_SWAP_GB):
        return f"swap {used / 1024:.1f} of {total / 1024:.1f} GB used"
    if m.get("load1") is not None and m["load1"] > env_num("AGENTCTL_FIXER_LOAD", FIXER_LOAD) * m["cores"]:
        return f"load {m['load1']:.0f} on {m['cores']} cores"
    if m.get("free") is not None and m["free"] < env_num("AGENTCTL_FIXER_FREE_GB", FIXER_FREE_GB):
        return f"{m['free']:.1f} GB free"
    return None


def cmd_suite(a):
    """`agentctl suite [tests/run.py args]`: agentctl's own test suite (tests/run.py: parallel, a timeout per test; in
    this directory) under a governor ticket of class `suite` (SUITE_CAP at once): parallel agents queue here instead of
    piling up. Arguments are run.py's (-j N, --timeout S, PATTERN...)."""
    args = list(a.args or [])
    with gov_ticket("suite", label=f"agentctl suite {os.path.basename(os.getcwd())}"):
        return subprocess.run([sys.executable, os.path.join("tests", "run.py")] + args).returncode


def gov_class_of(repo, commits, reds):
    """red when a commit carries Fixes-Red for a red run, else check."""
    return "red" if reds and (fixes_of(repo, commits) & reds) else "check"


def cmd_gov(a):
    if a.gcmd == "admit":
        t = gov_admit(a.cls, a.mem, a.label or "", kind=a.kind, fd=a.fd, ticket=a.ticket, wait=not a.try_,
                      force=a.force, pid=a.pid, cwd=a.cwd)
        return 0 if t else 1
    if a.gcmd == "evict":
        if a.dry_run:
            for idle, root, _p in idle_servers():
                print(f"would evict {root} (unused {int(idle // 60)} min)")
            return 0
        root = gov_evict("by hand (agentctl gov evict)")
        print(f"evicted {root}" if root else "no idle sbt server to evict")
        return 0
    if a.gcmd == "status":
        tickets, m = gov_tickets(), gov_machine()
        print(f"governor: {len(tickets)} admitted, {gov_committed(tickets):.1f}/{gov_budget():.0f} GB committed, "
              f"{gov_ramp(tickets):.0f} GB still growing; free {m['free'] if m['free'] is None else round(m['free'], 1)} "
              f"GB, pressure {m['pressure']}%, load {m['load1']:.1f}/{m['cores']} cores; check cap {checks_cap()}")
        for t in tickets:
            print(f"  {t.get('class'):<7} {t.get('mem'):>4} GB  {t.get('kind'):<5} {t.get('label')}  pid {t.get('pid')}  "
                  f"since {t.get('since')}  {t.get('cwd')}")
        for w in gov_waiters():
            print(f"  WAITING {w.get('class'):<7} {w.get('mem'):>4} GB  {w.get('label')}  pid {w.get('pid')}")
        idle = idle_servers()
        if idle:
            print("evictable: " + ", ".join(f"{os.path.basename(r)} ({int(i // 60)} min)" for i, r, _p in idle))
        why = gov_pressure()
        print(f"pressure: {why} (no new fixer starts)" if why else "pressure: none")
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
        try:
            ev = sum(1 for l in open(os.path.join(gov_dir(), "gov.log")) if " evicted " in l)
        except OSError:
            ev = 0
        print(f"evictions (all time): {ev}")
        return rc
    return 2

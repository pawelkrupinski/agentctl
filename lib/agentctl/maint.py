# agentctl, part `maint`: orphans and stale JVMs.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- orphans
def ps():
    r = run_cmd(["ps", "-axo", "pid=,ppid=,etime=,command="])
    out = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            out.append((int(parts[0]), int(parts[1]), parts[2], parts[3]))
    return out


def loop_body(cmd):
    m = re.search(r"eval '(.*)' < /dev/null", cmd, re.S)
    return m.group(1) if m else cmd


def is_wait_loop(cmd):
    if re.search(r"\bagentctl wait\b", cmd):
        return True
    if not re.match(r"\S*(zsh|bash|sh) ", cmd):
        return False
    body = loop_body(cmd)
    return bool(re.search(r"\bsleep\b", body) and re.search(r"\b(until|while)\b", body))


def loop_targets(body):
    ids = set(re.findall(r"run (?:view|watch) (\d+)", body)) | set(re.findall(r"/runs/(\d+)", body))
    ids |= {i for m in re.findall(r"for \w+ in ((?:\d+\s+)*\d+)\s*;", body) for i in m.split()}
    m = re.search(r"(?:-R|--repo)[ =]([\w.-]+/[\w.-]+)", body) or re.search(r"agentctl wait run (\S+)", body)
    repo = None
    if m:
        try:
            repo = slug(m.group(1))
        except SystemExit:
            repo = None
    return repo, sorted(ids, key=int)


def missing_tree_paths(text):
    gone = set()
    for p in re.findall(rf"{re.escape(HOME)}/(?:projects|bitcashier)/[\w.-]+", text):
        if not os.path.exists(p):
            gone.add(p)
    return sorted(gone)


# STUCK SBT SERVERS (2026-10-27): a worktree's one server ran an orphaned `--serve` (its owner's shell died) for hours,
# and every later command queued behind it at 0% CPU. Besides a gone worktree, orphans/sweep reap a server whose
# serving command's sbt-warm or owner is dead (scripts/sbt-warm's $State/<key>.serving), and one with a command
# running (an sbtn client in its checkout) whose process tree used no CPU for SBT_STALL_MIN minutes, judged across
# runs from ~/.cache/agentctl/sbt-cpu.json. A thread dump (jcmd Thread.print) is saved first, under
# ~/.cache/agentctl/sbt-dumps/.
SBT_STALL_MIN = 20
# PROGRESS IS A RATE (2026-11-07, measured): idle server JVMs burn 0.04-0.12 CPU s/min in background threads, busy ones
# 5-155 s/min. A fixed tolerance on the CPU total since idling began was crossed by that background burn within one
# 15-min pass, so neither the stall nor the idle rule could ever fire; CPU since the previous pass is judged per minute.
SBT_PROGRESS_CPU_PER_MIN = 1.0  # CPU s/min since the previous pass above which an sbt server counts as working
# A RATE NEEDS A COMPARABLE SAMPLE: the laptop sleeps (1565 sleep/wakes since boot, pmset 2026-11-07), and wall time runs
# on while the JVM is frozen, so the first pass after a long sleep spread a busy server's few minutes of CPU over hours
# and read it as idle since before the sleep. A previous sample older than two 15-min passes (the pass not running), or
# from the future (another clock), says nothing about CPU: the server starts over.
SBT_SAMPLE_MAX_GAP_MIN = 30
# ...and a shorter sleep is no idle time either: 25 min asleep after a pass read a command's minute of CPU as no CPU for
# 26 min, over the 20-min stall limit. The samples are dated by the AWAKE clock (CLOCK_UPTIME_RAW: it stops while the
# laptop sleeps; Linux's CLOCK_MONOTONIC stops in suspend too), so a rate and an idle time count awake minutes only. A
# record from another clock (an older agentctl's wall time, a previous boot's) reads as from the future: it starts over.
SBT_SAMPLE_CLOCK = getattr(time, "CLOCK_UPTIME_RAW", time.CLOCK_MONOTONIC)


def awake_now():
    """Seconds of the awake clock the sbt reaper dates its CPU samples by (SBT_SAMPLE_CLOCK)."""
    return time.clock_gettime(SBT_SAMPLE_CLOCK)
# IDLE DETACHED SERVERS (2026-11-07): two lone servers (core-wt-red-822/824, -Xmx6000M each) sat idle for 3 h while
# swap stood at 40.1/40.96 GB and every other build crawled; no rule matched them (one server per checkout, under the
# hog CPU floor, no command running). A detached server (PPID 1) with no client and no CPU for SBT_IDLE_MIN is reaped,
# after SBT_IDLE_PRESSURE_MIN once swap free falls under SWAP_FREE_FLOOR_MB -- never one sbt-warm --keep keeps.
# Ending an idle server costs only the next command's warm-up.
SBT_IDLE_MIN = 120  # min a detached sbt server with no client may sit idle before the reaper ends it
SBT_IDLE_PRESSURE_MIN = 30  # that limit while swap free is under the floor
SWAP_FREE_FLOOR_MB = 2048  # swap free (MB) below which the shorter idle limit applies


def parse_swap_free_mb(text):
    """`sysctl vm.swapusage` -> free MB, or None when it cannot be read (then the normal idle limit applies)."""
    m = re.search(r"free\s*=\s*([0-9.]+)M", text or "")
    return float(m.group(1)) if m else None


def swap_free_mb():
    return parse_swap_free_mb(run_cmd(["sysctl", "vm.swapusage"]).stdout)


def sbt_warm_state():
    return os.environ.get("KINOWO_SBT_WARM_STATE") or os.path.join(HOME, ".cache", "kinowo-sbt-warm")


def sbt_serving():
    """{checkout root: (serving sbt-warm pid, owner pid or None, command)} from sbt-warm's .serving marks."""
    out = {}
    for path in glob.glob(os.path.join(sbt_warm_state(), "*.serving")):
        try:
            with open(path) as f:
                parts = f.read().split(None, 2)
            with open(path[: -len(".serving")] + ".root") as f:
                root = f.read().strip()
        except OSError:
            continue
        if len(parts) >= 2:
            owner = None if parts[1] == "-" else int(parts[1]) if parts[1].isdigit() else None
            out[root] = (int(parts[0]) if parts[0].isdigit() else 0, owner, parts[2].strip() if len(parts) > 2 else "")
    return out


def tree_cpu(pid, procs_cpu, children):
    """CPU seconds of pid and every descendant (the server and its forked test JVMs)."""
    total, todo = 0.0, [pid]
    while todo:
        p = todo.pop()
        total += procs_cpu.get(p, 0.0)
        todo += children.get(p, [])
    return total


def comparable(rec, now):
    """Whether the previous sample can be judged against: taken before now and at most SBT_SAMPLE_MAX_GAP_MIN ago. A
    record from before 'at' existed is dated by 'since'."""
    return 0 <= now - rec.get("at", rec["since"]) <= SBT_SAMPLE_MAX_GAP_MIN * 60


def working(rec, cpu, now):
    """Whether the server used CPU since the previous pass faster than an idle JVM does (or its pid was reused: less CPU
    than before)."""
    if cpu < rec.get("cpu", -1):
        return True
    minutes = max((now - rec.get("at", rec["since"])) / 60, 1 / 60)
    return (cpu - rec["cpu"]) / minutes > env_num("AGENTCTL_SBT_PROGRESS_CPU_PER_MIN", SBT_PROGRESS_CPU_PER_MIN)


def sbt_stuck_reasons(pid, cwd, cpu, clients, serving, state, now, ppid=None, kept=(), swap_free_mb=None):
    """Why the sbt server pid (checkout cwd, tree CPU seconds) is stuck: an orphaned serving command, a running
    command and no CPU for SBT_STALL_MIN, or (detached, ppid 1, not kept by sbt-warm) no client and no CPU for
    SBT_IDLE_MIN (SBT_IDLE_PRESSURE_MIN under low swap); "no CPU" is under SBT_PROGRESS_CPU_PER_MIN since the
    previous pass, judged only against a comparable sample (SBT_SAMPLE_MAX_GAP_MIN). Updates state[pid] = {cpu, at
    (that sample), since (idle from)}."""
    reasons = []
    serve = serving.get(cwd)
    if serve:
        spid, owner, cmd = serve
        if not pid_alive(spid) or (owner and not pid_alive(owner)):
            reasons.append(f"serving `{cmd[:60]}` for {'sbt-warm pid ' + str(spid) if not pid_alive(spid) else 'owner pid ' + str(owner)}, which ended")
    rec = state.get(str(pid))
    if not rec or not comparable(rec, now) or working(rec, cpu, now):
        state[str(pid)] = {"cpu": cpu, "at": now, "since": now}
        return reasons
    rec.update(cpu=cpu, at=now)
    mins = (now - rec["since"]) / 60
    if clients.get(cwd):
        if mins >= env_num("AGENTCTL_SBT_STALL_MIN", SBT_STALL_MIN):
            reasons.append(f"a command running (sbtn pid {clients[cwd][0]}) and no CPU for {int(mins)} min")
    elif ppid == 1 and cwd not in kept:
        floor = env_num("AGENTCTL_SWAP_FREE_FLOOR_MB", SWAP_FREE_FLOOR_MB)
        pressed = swap_free_mb is not None and swap_free_mb < floor
        if pressed:
            limit = env_num("AGENTCTL_SBT_IDLE_PRESSURE_MIN", SBT_IDLE_PRESSURE_MIN)
        else:
            limit = env_num("AGENTCTL_SBT_IDLE_MIN", SBT_IDLE_MIN)
        if mins >= limit:
            reasons.append(f"detached, no client and idle for {int(mins)} min"
                           + (f" (swap free {int(swap_free_mb)} MB)" if pressed else ""))
    return reasons


def thread_dump(pid, cwd):
    d = state_root("sbt-dumps")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{pid}-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}.txt")
    try:
        r = subprocess.run(["jcmd", str(pid), "Thread.print"], capture_output=True, text=True, timeout=60)
        with open(path, "w") as f:
            f.write(f"# sbt server pid {pid}, checkout {cwd}\n{r.stdout}{r.stderr}")
        return path
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"(no thread dump: {e})"


def is_sbt(cmd):
    return ("java" in cmd.split(" ", 1)[0] and re.search(r"xsbt\.boot|sbt-launch|sbt\.boot|\bsbt\.server", cmd)) or bool(
        re.search(r"scripts/sbt-warm --keep", cmd)
    )


def cmd_watch(a):
    a.inbox_cwd = a.agent_cwd or a.close or a.repo or safe_cwd()  # its inbox entry routes to the agent's worktree
    repo = main_clone(a.repo or safe_cwd())
    os.chdir(repo)  # not the worktree: --close removes it, and so may its agent
    return watch_landed(repo, a.sha, a.pushed_at, close=a.close, fixes=a.fixes or ())


def owner_orphans_dead_only():
    """Cancel a DEAD owner's runs and drop its record (what --kill does to it), and log it; live owners untouched."""
    for rec in owner_records():
        if rec["alive"]:
            continue
        for o_r, rid in rec.get("runs") or []:
            cancel_run(o_r, rid, f"its owner pid {rec.get('pid')} died")
        for ext in (".json", ".verdict"):
            try:
                os.unlink(rec["path"][: -len(".json")] + ext)
            except FileNotFoundError:
                pass
        reap_log(f"removed dead owner {rec.get('kind')} {rec.get('key')} pid {rec.get('pid')} (runs {rec.get('runs')})")


def owner_orphans(kill):
    """Owner records that are strays: a dead process's (its runs never cancelled), an owner on old code, an owner whose
    cwd is gone. With kill: a dead one's runs are cancelled and its record removed; a live one gets SIGTERM, whose
    handler cancels its runs."""
    found = []
    current = code_version()
    for rec in owner_records():
        what = f"{rec.get('kind')} {rec.get('key')} ({rec.get('owner')}, since {rec.get('started')})"
        runs = " ".join(str(r[1]) for r in rec.get("runs") or [])
        if not rec["alive"]:
            found.append(("owner", rec.get("pid"), "process gone, record left" + (f"; runs {runs} never cancelled" if runs
                                                                                     else ""), what))
            if kill:
                for o_r, rid in rec.get("runs") or []:
                    cancel_run(o_r, rid, f"its owner pid {rec.get('pid')} died")
                for ext in (".json", ".verdict"):
                    try:
                        os.unlink(rec["path"][: -len(".json")] + ext)
                    except FileNotFoundError:
                        pass
                print(f"removed the dead owner's record {rec['path']}")
            continue
        reasons = []
        if current and rec.get("code") and rec["code"] != current:
            reasons.append(f"running old code ({rec['code'][:8]}, now {current[:8]}): it restarts at its next poll")
        if rec.get("cwd") and not os.path.isdir(rec["cwd"]):
            reasons.append(f"cwd gone: {rec['cwd']}")
        if reasons:
            found.append(("owner", rec.get("pid"), "; ".join(reasons), what))
            if kill and rec.get("pid"):
                try:
                    os.kill(int(rec["pid"]), signal.SIGTERM)
                    print(f"sent SIGTERM to owner pid {rec['pid']} ({rec.get('kind')})")
                except ProcessLookupError:
                    pass
    return found


REAP_LOG = None  # a test's override; else reap_log_path(): under the state root, resolved at each use


def reap_log_path():
    return REAP_LOG or state_root("reap.log")


def reap_log(line):
    os.makedirs(os.path.dirname(reap_log_path()), exist_ok=True)
    with open(reap_log_path(), "a") as f:
        f.write(f"{ts()} {line}\n")


# ---------------------------------------------------------------- stale JVMs (folded in from reap-stale-jvms.sh)
# The owner (2026-10-27): ONE reaper. Its rules, unchanged from ~/.local/bin/reap-stale-jvms.sh (movies'
# scripts/reap-stale-jvms.sh, spec scripts/reap-stale-jvms-test.sh), which ran as launchd dev.kinowo.jvm-reaper:
#   orphan  an sbt worker / forked-test JVM whose parent is gone (PPID 1), 10 min or older: killed whatever its CPU;
#   hog     any sbt/java process 1.5 h or older that burned >= 5% of a core over its LIFETIME (cputime / elapsed, not ps's
#           decaying %CPU), with no tty (a human's terminal can Ctrl-C it), never a server a live `sbt-warm --keep`
#           keeps in its directory (2026-10-19 the hog rule killed one mid-use);
#   dup-sbt-server / dup-sbt-gone-dir   detached `sbt-launch --detach-stdio` servers (PPID 1) grouped by cwd: all but the
#           newest are reaped once 10 min old and IDLE now (recent %CPU <= 5: 2026-10-15 one was killed at 120% mid-run),
#           and a server whose cwd is gone (a `<dir>.part` whose `<dir>` exists is a finished rename, not gone).
# Spared always: IDEs and the protected names (PROTECT_REGEX), a listener on a protected port (9000) under 50% of a core,
# this reaper's own process group, and -- agentctl's rule -- any process descended from a live Claude Code session or a
# running land / check / waiting processor (another agent's working process). Before killing: the list; an sbt server
# gets a thread dump first; TERM, then KILL after 10 s; every kill one line of ~/.cache/agentctl/reap.log.
JVM_PROTECT = r"kinowo\.longrun|scripts\.IdentityCalibrate|JetBrains|IntelliJ|IDEA|Android Studio|jetbrains|toolbox|/Applications/|GradleDaemon|KotlinCompileDaemon|jdk.jcmd|VisualVM|jconsole"
JVM_SCOPE = r"(^|/)java( |$)|(^|/)java$|sbt-launch\.jar|org\.scala-sbt|sbt\.internal|sbt\.ForkMain"
JVM_ORPHAN = r"sbt\.internal\.worker[0-9]*\.WorkerMain|sbt\.ForkMain|org\.scala\.sbt\.ForkMain"
AGENT_WORK = re.compile(r"(^|/)claude( |$)|agentctl (land|waiting run|red fixer)\b|scripts/hooks/pre-push|scripts/sbt-warm")


def jvm_knob(name, default):
    """The old script's tunables, by the same names ($HOG_MIN_AGE_SECONDS, ...)."""
    return os.environ.get(name, default)


def tosec(t):
    """ps's [[dd-]hh:]mm:ss[.frac] (etime) or unbounded mm:ss (cputime) -> seconds."""
    dd = 0
    t = str(t)
    if "-" in t:
        d_, t = t.split("-", 1)
        dd = int(d_ or 0)
    s = 0.0
    for part in t.split(":"):
        try:
            s = s * 60 + float(part)
        except ValueError:
            return 0.0
    return s + dd * 86400


def jvm_candidates(rows, cwd, kept, port_pids, self_pid, isdir=os.path.isdir):
    """rows: [(pid, ppid, tty, etime, cputime, args)] -> [(pid, ppid, age, cpu, pct, rule, args)]: the old script's
    rules A (orphan), B (hog) and C (dup-sbt-server, dup-sbt-gone-dir), exactly."""
    hog_age, hog_cpu = float(jvm_knob("HOG_MIN_AGE_SECONDS", 5400)), float(jvm_knob("HOG_MIN_CPU_PERCENT", 5))
    orphan_age = float(jvm_knob("ORPHAN_MIN_AGE_SECONDS", 600))
    no_tty = str(jvm_knob("HOG_REQUIRE_NO_TTY", "1")) == "1"
    port_max = float(jvm_knob("PROTECTED_PORT_MAX_CPU_PERCENT", 50))
    protect, scope = re.compile(jvm_knob("PROTECT_REGEX", JVM_PROTECT)), re.compile(jvm_knob("SCOPE_REGEX", JVM_SCOPE))
    orphan_re = re.compile(jvm_knob("ORPHAN_REGEX", JVM_ORPHAN))
    out, servers = [], {}
    for pid, ppid, tty, etime, cputime, args in rows:
        if pid == self_pid or pid <= 1 or not scope.search(args) or protect.search(args):
            continue
        age, cpu = tosec(etime), tosec(cputime)
        pct = cpu * 100.0 / age if age > 0 else 0.0
        if pid in port_pids and pct <= port_max:
            continue
        rule = None
        if orphan_re.search(args) and ppid == 1 and age >= orphan_age:
            rule = "orphan"
        elif age >= hog_age and pct >= hog_cpu and not ("--detach-stdio" in args and pid in cwd and cwd[pid] in kept):
            if no_tty and tty not in ("??", "?", "-"):
                continue
            rule = "hog"
        if rule is None and ppid == 1 and "sbt-launch.jar" in args and "--detach-stdio" in args and pid in cwd:
            servers[pid] = (age, cpu, pct, args)
            continue
        if rule:
            out.append((pid, ppid, age, cpu, pct, rule, args))
    dirs, newest = {}, {}
    for pid in servers:
        d = re.sub(r"\.part$", "", cwd[pid])
        dirs[pid] = d
        n = newest.get(d)
        if n is None or servers[pid][0] < servers[n][0] or (servers[pid][0] == servers[n][0] and pid > n):
            newest[d] = pid
    for pid, (age, cpu, pct, args) in servers.items():
        d = dirs[pid]
        if not isdir(d) and not isdir(d + ".part"):
            rule = "dup-sbt-gone-dir"
        elif newest[d] != pid and age >= orphan_age:
            rule = "dup-sbt-server"
        else:
            continue
        out.append((pid, 1, age, cpu, pct, rule, f"{args} cwd={d}"))
    return sorted(out)


def jvm_scan():
    """The live process table for jvm_candidates ($AGENTCTL_JVM_PS / _CWD / _KEPT: fixture files for the tests)."""
    def read(env, cmd):
        if os.environ.get(env):
            with open(os.environ[env]) as f:
                return f.read()
        return run_cmd(cmd, shell=True).stdout
    rows = []
    for line in read("AGENTCTL_JVM_PS", "ps -Ao pid=,ppid=,tty=,etime=,cputime=,args=").splitlines():
        f = line.split(None, 5)
        if len(f) == 6 and f[0].isdigit() and f[1].isdigit():
            rows.append((int(f[0]), int(f[1]), f[2], f[3], f[4], f[5]))
    cwd = {}
    if os.environ.get("AGENTCTL_JVM_CWD"):
        read_cwd = read("AGENTCTL_JVM_CWD", "").splitlines()
    else:  # pid-scoped: the java processes of the ps rows above, never a machine-wide lsof
        jp = [r_[0] for r_ in rows if re.search(r"(^|/)java(\s|$)", r_[5].split(" ", 1)[0] if r_[5] else "")]
        read_cwd = [f"{p_} {d_}" for p_, d_ in lsof_cwds(jp).items()] if jp else []
    for line in read_cwd:
        p, _, d = line.partition(" ")
        if p.isdigit() and allowed_path(d):
            cwd[int(p)] = d
    if os.environ.get("AGENTCTL_JVM_KEPT"):
        kept = {l.strip() for l in read("AGENTCTL_JVM_KEPT", "").splitlines() if l.strip() and allowed_path(l.strip())}
    else:
        kp = [r_[0] for r_ in rows if "sbt-warm --keep" in r_[5]]
        kept = set(lsof_cwds(kp).values()) if kp else set()
    ports = set()
    if not os.environ.get("AGENTCTL_JVM_PS"):
        for port in str(jvm_knob("PROTECTED_PORTS", "9000")).split():
            r = run_cmd(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"])
            ports |= {int(x) for x in r.stdout.split() if x.isdigit()}
    return rows, cwd, kept, ports


def agent_work(pid, procs):
    """The live Claude session or land / check / waiting processor pid descends from, if any: another agent's work."""
    parents = {p: (pp, c) for p, pp, _e, c in procs}
    seen, p = set(), parents.get(pid, (None, ""))[0]
    while p and p > 1 and p not in seen:
        seen.add(p)
        pp, c = parents.get(p, (None, ""))
        if AGENT_WORK.search(c or ""):
            return f"pid {p} ({(c or '')[:60]})"
        p = pp
    return None


def jvm_reap(cands, kill, procs):
    """List the candidates; with kill, end them under the guards (idle duplicate, alive, not this group, no agent's
    work), sbt servers after a thread dump, TERM then KILL. Returns the lines printed."""
    lines = []
    grace = float(jvm_knob("TERM_GRACE_SECONDS", 10))
    own_pgid = os.getpgid(0)
    for pid, ppid, age, cpu, pct, rule, args in cands:
        short = re.sub(r"-Duser\.(country|language)=[A-Za-z]+ ?", "", args)[:150]
        detail = f"pid={pid} ppid={ppid} rule={rule} age={int(age // 3600)}h{int(age % 3600 // 60)}m cpu={pct:.1f}%-of-a-core cmd=[{short}]"
        lines.append(f"jvm       {detail}")
    for l in lines:
        print(l)
    if not kill:
        return lines
    for pid, ppid, age, cpu, pct, rule, args in cands:
        why = None
        if rule == "dup-sbt-server":
            r = run_cmd(["ps", "-o", "%cpu=", "-p", str(pid)]).stdout.strip()
            try:
                if float(r) > float(jvm_knob("DUP_MAX_RECENT_CPU_PERCENT", 5)):
                    why = f"busy now ({r}% CPU), not idle"
            except ValueError:
                pass
        if why is None and not pid_alive(pid):
            why = "already gone"
        if why is None:
            try:
                if os.getpgid(pid) == own_pgid:
                    why = "own process group"
            except OSError:
                why = "already gone"
        if why is None:
            w = agent_work(pid, procs)
            if w:
                why = f"another agent's working process (under {w})"
        if why:
            print(f"skip jvm pid {pid} ({rule}): {why}")
            reap_log(f"skip jvm pid {pid} ({rule}): {why}")
            continue
        dump = ""
        if "sbt-launch" in args or "xsbt.boot" in args:
            dump = f"; thread dump {thread_dump(pid, '')}"
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        waited = 0.0
        while waited < grace and pid_alive(pid):
            time.sleep(0.5)
            waited += 0.5
        how = f"SIGTERM, {waited:.0f}s"
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            how = f"SIGKILL after {grace:.0f}s"
        line = f"killed jvm pid {pid} ({how}) rule={rule}{dump}: {args[:150]}"
        print(line)
        reap_log(line)
    return lines


MAINTAIN_SWEEP_HOUR = 9  # local time: the daily sweep runs at the first maintain pass from 09:00 ($AGENTCTL_SWEEP_HOUR)

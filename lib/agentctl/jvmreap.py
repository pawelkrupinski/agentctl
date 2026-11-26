# agentctl, part `jvmreap`: stale JVMs: a stuck or idle sbt server, an orphaned sbt or test JVM, and their reap
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.ids import stamp
import glob
import os
import re
import signal
import subprocess
import time
from contextlib import suppress
from typing import Any
from agentctl.procrunner import process_runner
from agentctl.core import HOME, pid_alive, run_cmd, state_root
from agentctl.timefmt import ts
from agentctl.procs import lsof_cwds, ps_rows
from agentctl.psparse import tosec
from agentctl.allowedpaths import allowed_path
from agentctl.govknobs import knob_sbt_idle_min, knob_sbt_idle_pressure_min, knob_sbt_progress_cpu_per_min, knob_sbt_stall_min, knob_swap_free_floor_mb
from agentctl.govknobs import knob_jvm_cwd, knob_jvm_kept, knob_jvm_ps


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
    return time.clock_gettime(SBT_SAMPLE_CLOCK)  # wall: waits on and times real processes and threads


def parse_swap_free_mb(text):
    """`sysctl vm.swapusage` -> free MB, or None when it cannot be read (then the normal idle limit applies)."""
    m = re.search(r"free\s*=\s*([0-9.]+)M", text or "")
    return float(m.group(1)) if m else None


def swap_free_mb():
    return parse_swap_free_mb(run_cmd(["sysctl", "vm.swapusage"]).stdout)


def sbt_warm_state():
    return os.environ.get("KINOWO_SBT_WARM_STATE") or os.path.join(HOME, ".cache", "kinowo-sbt-warm")


def _read_serving(path: Any) -> Any:
    """(checkout root, the fields of its .serving mark) of one sbt-warm mark; None when it cannot be read."""
    try:
        with open(path) as f:
            parts = f.read().split(None, 2)
        with open(path[: -len(".serving")] + ".root") as f:
            return f.read().strip(), parts
    except OSError:
        return None


def _serving_row(parts: Any) -> Any:
    owner = int(parts[1]) if parts[1].isdigit() else None  # `-`: no owner
    return int(parts[0]) if parts[0].isdigit() else 0, owner, parts[2].strip() if len(parts) > 2 else ""


def sbt_serving():
    """{checkout root: (serving sbt-warm pid, owner pid or None, command)} from sbt-warm's .serving marks."""
    out = {}
    for path in glob.glob(os.path.join(sbt_warm_state(), "*.serving")):
        seen = _read_serving(path)
        if seen and len(seen[1]) >= 2:
            out[seen[0]] = _serving_row(seen[1])
    return out


def tree_cpu(pid: int, procs_cpu: dict[int, float], children: dict[int, list[int]]) -> float:
    """CPU seconds of pid and every descendant (the server and its forked test JVMs)."""
    total, todo = 0.0, [pid]
    while todo:
        p = todo.pop()
        total += procs_cpu.get(p, 0.0)
        todo += children.get(p, [])
    return total


def comparable(rec: dict[str, Any], now: float) -> bool:
    """Whether the previous sample can be judged against: taken before now and at most SBT_SAMPLE_MAX_GAP_MIN ago. A
    record from before 'at' existed is dated by 'since'."""
    return bool(0 <= now - rec.get("at", rec["since"]) <= SBT_SAMPLE_MAX_GAP_MIN * 60)


def working(rec: dict[str, Any], cpu: float, now: float) -> bool:
    """Whether the server used CPU since the previous pass faster than an idle JVM does (or its pid was reused: less CPU
    than before)."""
    if cpu < rec.get("cpu", -1):
        return True
    minutes = max((now - rec.get("at", rec["since"])) / 60, 1 / 60)
    return bool((cpu - rec["cpu"]) / minutes > knob_sbt_progress_cpu_per_min())


def _ended_serving(serve: tuple[int, int | None, str]) -> list[str]:
    """The reason the serving command (sbt-warm pid, owner pid, command) is orphaned -- its sbt-warm or owner ended."""
    spid, owner, cmd = serve
    if not pid_alive(spid) or (owner and not pid_alive(owner)):
        return [f"serving `{cmd[:60]}` for {'sbt-warm pid ' + str(spid) if not pid_alive(spid) else 'owner pid ' + str(owner)}, which ended"]
    return []


def _detached_idle(mins: float, swap_free_mb: float | None) -> list[str]:
    """A detached server with no client: idle for SBT_IDLE_MIN (SBT_IDLE_PRESSURE_MIN under low swap)."""
    floor = knob_swap_free_floor_mb()
    pressed = swap_free_mb is not None and swap_free_mb < floor
    if pressed:
        limit = knob_sbt_idle_pressure_min()
    else:
        limit = knob_sbt_idle_min()
    if mins < limit:
        return []
    return [f"detached, no client and idle for {int(mins)} min" + (f" (swap free {int(swap_free_mb or 0)} MB)" if pressed else "")]


def _no_cpu_reasons(mins: float, cwd: str, clients: dict[str, Any], ppid: int | None, kept: Any,
                    swap_free_mb: float | None) -> list[str]:
    """What the server's CPU-less minutes mean: a stalled command (a client), or a detached idle server (none)."""
    if clients.get(cwd):
        if mins >= knob_sbt_stall_min():
            return [f"a command running (sbtn pid {clients[cwd][0]}) and no CPU for {int(mins)} min"]
        return []
    if ppid == 1 and cwd not in kept:
        return _detached_idle(mins, swap_free_mb)
    return []


def sbt_stuck_reasons(pid: int, cwd: str, cpu: float, clients: dict[str, Any], serving: dict[str, Any],
                      state: dict[str, Any], now: float, ppid: int | None = None, kept: Any = (),
                      swap_free_mb: float | None = None) -> list[str]:
    """Why the sbt server pid (checkout cwd, tree CPU seconds) is stuck: an orphaned serving command, a running
    command and no CPU for SBT_STALL_MIN, or (detached, ppid 1, not kept by sbt-warm) no client and no CPU for
    SBT_IDLE_MIN (SBT_IDLE_PRESSURE_MIN under low swap); "no CPU" is under SBT_PROGRESS_CPU_PER_MIN since the
    previous pass, judged only against a comparable sample (SBT_SAMPLE_MAX_GAP_MIN). Updates state[pid] = {cpu, at
    (that sample), since (idle from)}."""
    serve = serving.get(cwd)
    reasons = _ended_serving(serve) if serve else []
    rec = state.get(str(pid))
    if not rec or not comparable(rec, now) or working(rec, cpu, now):
        state[str(pid)] = {"cpu": cpu, "at": now, "since": now}
        return reasons
    rec.update(cpu=cpu, at=now)
    return reasons + _no_cpu_reasons((now - rec["since"]) / 60, cwd, clients, ppid, kept, swap_free_mb)


def thread_dump(pid: int, cwd: str) -> str:
    d = state_root("sbt-dumps")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{pid}-{stamp('file')}.txt")
    try:
        r = process_runner().run(["jcmd", str(pid), "Thread.print"], capture_output=True, text=True, timeout=60)
        with open(path, "w") as f:
            f.write(f"# sbt server pid {pid}, checkout {cwd}\n{r.stdout}{r.stderr}")
        return path
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"(no thread dump: {e})"


def is_sbt(cmd):
    return ("java" in cmd.split(" ", 1)[0] and re.search(r"xsbt\.boot|sbt-launch|sbt\.boot|\bsbt\.server", cmd)) or bool(
        re.search(r"scripts/sbt-warm --keep", cmd)
    )


REAP_LOG = None  # a test's override; else reap_log_path(): under the state root, resolved at each use


def reap_log_path() -> str:
    return REAP_LOG or state_root("reap.log")


def reap_log(line: str) -> None:
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


def jvm_knob(name: str, default: Any) -> Any:
    """The old script's tunables, by the same names ($HOG_MIN_AGE_SECONDS, ...)."""
    return os.environ.get(name, default)


NO_TTY = ("??", "?", "-")  # a process with none of these has a terminal: a human can Ctrl-C it


class JvmRules:
    """The old reaper script's rules A (orphan), B (hog) and C (dup-sbt-server, dup-sbt-gone-dir) with their tunables
    read once ($HOG_MIN_AGE_SECONDS, ...), applied to a process table."""

    def __init__(self) -> None:
        self.hog_age, self.hog_cpu = float(jvm_knob("HOG_MIN_AGE_SECONDS", 5400)), float(jvm_knob("HOG_MIN_CPU_PERCENT", 5))
        self.orphan_age = float(jvm_knob("ORPHAN_MIN_AGE_SECONDS", 600))
        self.no_tty = str(jvm_knob("HOG_REQUIRE_NO_TTY", "1")) == "1"
        self.port_max = float(jvm_knob("PROTECTED_PORT_MAX_CPU_PERCENT", 50))
        self.protect, self.scope = re.compile(jvm_knob("PROTECT_REGEX", JVM_PROTECT)), re.compile(jvm_knob("SCOPE_REGEX", JVM_SCOPE))
        self.orphan_re = re.compile(jvm_knob("ORPHAN_REGEX", JVM_ORPHAN))

    def ignored(self, pid: int, args: str, self_pid: int) -> bool:
        """The reaper's own process, init, anything out of scope or protected."""
        return pid == self_pid or pid <= 1 or not self.scope.search(args) or bool(self.protect.search(args))

    def is_orphan(self, ppid: int, args: str, age: float) -> bool:
        return bool(self.orphan_re.search(args)) and ppid == 1 and age >= self.orphan_age

    def is_hog(self, pid: int, args: str, age: float, pct: float, cwd: dict[int, str], kept: Any) -> bool:
        """Old and busy over its lifetime, and not the server a live `sbt-warm --keep` keeps."""
        return age >= self.hog_age and pct >= self.hog_cpu and not ("--detach-stdio" in args and pid in cwd and cwd[pid] in kept)

    def verdict(self, pid: int, ppid: int, tty: str, args: str, age: float, pct: float, cwd: dict[int, str], kept: Any) -> str | None:
        """'orphan', 'hog', 'server' (a detached sbt server: judged with its duplicates) or None."""
        if self.is_orphan(ppid, args, age):
            return "orphan"
        if self.is_hog(pid, args, age, pct, cwd, kept):
            return "hog" if not self.no_tty or tty in NO_TTY else None  # a hog with a terminal is spared, not a server
        if ppid == 1 and "sbt-launch.jar" in args and "--detach-stdio" in args and pid in cwd:
            return "server"
        return None

    def duplicates(self, servers: dict[int, tuple[Any, ...]], cwd: dict[int, str], isdir: Any) -> list[tuple[Any, ...]]:
        """Rows for the detached servers whose directory is gone, or that an idle-enough newer one in their directory outlives."""
        dirs = {pid: re.sub(r"\.part$", "", cwd[pid]) for pid in servers}
        newest = self.newest_per_dir(servers, dirs)
        out = []
        for pid, (age, cpu, pct, args) in servers.items():
            d = dirs[pid]
            if not isdir(d) and not isdir(d + ".part"):
                rule = "dup-sbt-gone-dir"
            elif newest[d] != pid and age >= self.orphan_age:
                rule = "dup-sbt-server"
            else:
                continue
            out.append((pid, 1, age, cpu, pct, rule, f"{args} cwd={d}"))
        return out

    @staticmethod
    def newest_per_dir(servers: dict[int, tuple[Any, ...]], dirs: dict[int, str]) -> dict[str, int]:
        """{directory: pid of its youngest server (the higher pid on a tie)}."""
        newest: dict[str, int] = {}
        for pid in servers:
            n = newest.get(dirs[pid])
            if n is None or servers[pid][0] < servers[n][0] or (servers[pid][0] == servers[n][0] and pid > n):
                newest[dirs[pid]] = pid
        return newest

    def judge(self, row: tuple[int, int, str, str, str, str], cwd: dict[int, str], kept: Any, port_pids: Any,
              self_pid: int) -> tuple[Any, ...] | None:
        """(pid, ppid, age, cpu, pct, rule, args) of a process the rules name (rule 'server' for a detached server), else None."""
        pid, ppid, tty, etime, cputime, args = row
        if self.ignored(pid, args, self_pid):
            return None
        age, cpu = tosec(etime), tosec(cputime)
        pct = cpu * 100.0 / age if age > 0 else 0.0
        if pid in port_pids and pct <= self.port_max:
            return None
        rule = self.verdict(pid, ppid, tty, args, age, pct, cwd, kept)
        return (pid, ppid, age, cpu, pct, rule, args) if rule else None

    def candidates(self, rows: list[tuple[int, int, str, str, str, str]], cwd: dict[int, str], kept: Any, port_pids: Any,
                   self_pid: int, isdir: Any) -> list[tuple[Any, ...]]:
        out: list[tuple[Any, ...]] = []
        servers: dict[int, tuple[Any, ...]] = {}
        for row in rows:
            c = self.judge(row, cwd, kept, port_pids, self_pid)
            if c and c[5] == "server":
                servers[c[0]] = (c[2], c[3], c[4], c[6])
            elif c:
                out.append(c)
        return sorted(out + self.duplicates(servers, cwd, isdir))


def jvm_candidates(rows: list[tuple[int, int, str, str, str, str]], cwd: dict[int, str], kept: Any, port_pids: Any,
                   self_pid: int, isdir: Any = os.path.isdir) -> list[tuple[Any, ...]]:
    """rows: [(pid, ppid, tty, etime, cputime, args)] -> [(pid, ppid, age, cpu, pct, rule, args)]: the old script's
    rules A (orphan), B (hog) and C (dup-sbt-server, dup-sbt-gone-dir), exactly."""
    return JvmRules().candidates(rows, cwd, kept, port_pids, self_pid, isdir)


def _scan_read(fixture: str, cmd: str) -> str:
    """The fixture file `fixture` names (a test's), else cmd's output."""
    if fixture:
        with open(fixture) as f:
            return f.read()
    return str(run_cmd(cmd, shell=True).stdout)


def _scan_rows() -> list[tuple[int, int, str, str, str, str]]:
    rows = []
    for line in _scan_read(knob_jvm_ps(), "ps -Ao pid=,ppid=,tty=,etime=,cputime=,args=").splitlines():
        f = line.split(None, 5)
        if len(f) == 6 and f[0].isdigit() and f[1].isdigit():
            rows.append((int(f[0]), int(f[1]), f[2], f[3], f[4], f[5]))
    return rows


def _scan_cwds(rows: list[tuple[int, int, str, str, str, str]]) -> dict[int, str]:
    if knob_jvm_cwd():
        read_cwd = _scan_read(knob_jvm_cwd(), "").splitlines()
    else:  # pid-scoped: the java processes of the ps rows above, never a machine-wide lsof
        jp = [r_[0] for r_ in rows if re.search(r"(^|/)java(\s|$)", r_[5].split(" ", 1)[0] if r_[5] else "")]
        read_cwd = [f"{p_} {d_}" for p_, d_ in lsof_cwds(jp).items()] if jp else []
    cwd = {}
    for line in read_cwd:
        p, _, d = line.partition(" ")
        if p.isdigit() and allowed_path(d):
            cwd[int(p)] = d
    return cwd


def _scan_kept(rows: list[tuple[int, int, str, str, str, str]]) -> set[str]:
    """The checkouts a live `sbt-warm --keep` keeps a server in."""
    if knob_jvm_kept():
        return {l.strip() for l in _scan_read(knob_jvm_kept(), "").splitlines() if l.strip() and allowed_path(l.strip())}
    kp = [r_[0] for r_ in rows if "sbt-warm --keep" in r_[5]]
    return set(lsof_cwds(kp).values()) if kp else set()


def _scan_ports() -> set[int]:
    """The pids listening on a protected port (none under the tests' fixture table)."""
    ports: set[int] = set()
    if not knob_jvm_ps():
        for port in str(jvm_knob("PROTECTED_PORTS", "9000")).split():
            r = run_cmd(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"])
            ports |= {int(x) for x in r.stdout.split() if x.isdigit()}
    return ports


def jvm_scan() -> tuple[list[tuple[int, int, str, str, str, str]], dict[int, str], set[str], set[int]]:
    """The live process table for jvm_candidates ($AGENTCTL_JVM_PS / _CWD / _KEPT: fixture files for the tests)."""
    rows = _scan_rows()
    return rows, _scan_cwds(rows), _scan_kept(rows), _scan_ports()


def agent_work(pid: int, procs: Any) -> str | None:
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


def _busy_duplicate(pid: int, rule: str) -> str | None:
    """Why a dup-sbt-server is not reaped: it is busy now (recent CPU above DUP_MAX_RECENT_CPU_PERCENT)."""
    if rule != "dup-sbt-server":
        return None
    for row in ps_rows(("pcpu",), pids=(pid,)):
        if row.pcpu > float(jvm_knob("DUP_MAX_RECENT_CPU_PERCENT", 5)):
            return f"busy now ({row.pcpu}% CPU), not idle"
    return None


def _own_group(pid: int, own_pgid: int) -> str | None:
    try:
        return "own process group" if os.getpgid(pid) == own_pgid else None
    except OSError:
        return "already gone"


def _reap_guard(pid: int, rule: str, own_pgid: int, procs: Any) -> str | None:
    """Why the candidate is spared: a busy duplicate, gone, this group's, or another agent's working process."""
    why = _busy_duplicate(pid, rule) or ("already gone" if not pid_alive(pid) else None) or _own_group(pid, own_pgid)
    if why:
        return why
    w = agent_work(pid, procs)
    return f"another agent's working process (under {w})" if w else None


def _end_jvm(pid: int, rule: str, args: str, grace: float) -> str | None:
    """SIGTERM (after a thread dump for an sbt server), SIGKILL after `grace` s; the line saying how, None if it was gone."""
    dump = f"; thread dump {thread_dump(pid, '')}" if "sbt-launch" in args or "xsbt.boot" in args else ""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return None
    waited = 0.0
    while waited < grace and pid_alive(pid):
        time.sleep(0.5)  # wall: waits on and times real processes and threads
        waited += 0.5
    how = f"SIGTERM, {waited:.0f}s"
    if pid_alive(pid):
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        how = f"SIGKILL after {grace:.0f}s"
    return f"killed jvm pid {pid} ({how}) rule={rule}{dump}: {args[:150]}"


def _jvm_line(cand: tuple[Any, ...]) -> str:
    pid, ppid, age, _cpu, pct, rule, args = cand
    short = re.sub(r"-Duser\.(country|language)=[A-Za-z]+ ?", "", args)[:150]
    return f"jvm       pid={pid} ppid={ppid} rule={rule} age={int(age // 3600)}h{int(age % 3600 // 60)}m cpu={pct:.1f}%-of-a-core cmd=[{short}]"


def jvm_reap(cands: list[tuple[Any, ...]], kill: bool, procs: Any) -> list[str]:
    """List the candidates; with kill, end them under the guards (idle duplicate, alive, not this group, no agent's
    work), sbt servers after a thread dump, TERM then KILL. Returns the lines printed."""
    grace = float(jvm_knob("TERM_GRACE_SECONDS", 10))
    own_pgid = os.getpgid(0)
    lines = [_jvm_line(c) for c in cands]
    for l in lines:
        print(l)
    if not kill:
        return lines
    for pid, _ppid, _age, _cpu, _pct, rule, args in cands:
        why = _reap_guard(pid, rule, own_pgid, procs)
        if why:
            print(f"skip jvm pid {pid} ({rule}): {why}")
            reap_log(f"skip jvm pid {pid} ({rule}): {why}")
            continue
        line = _end_jvm(pid, rule, args, grace)
        if line:
            print(line)
            reap_log(line)
    return lines

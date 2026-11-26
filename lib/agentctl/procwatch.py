# agentctl, part `procwatch`: the watchdog's machine invariants WP (a runaway process) and WT (temp churn).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# 2026-11-22: fseventsd held 21.5 GB and over 100% CPU for days, and mds_stores ran hot beside it, fed by the file
# churn of 174 leaked test roots (/private/tmp/agentctl-t-*, 197 MB of fake repos) -- and nothing said so. WP watches
# EVERY process, the system's daemons too: an RSS over WP_RSS_GB that keeps growing, or a CPU over WP_CPU_PCT held for
# WP_CPU_MIN minutes, from samples kept between passes. It names the process, its RSS and CPU trend, and the fix --
# for a system daemon the OWNER's command (agentctl never stops or reconfigures a process or setting it did not start:
# `sudo killall fseventsd`, which launchd restarts, and the Spotlight exclusions), for one of ours the worktree it runs
# in. WT counts the churn itself: test suite roots past their bound (upkeep's sweep removes the ones no runner holds)
# and the worktrees holding build output.
#
# 2026-11-22 16:15Z, a false alarm: "java pid 6811: RSS 4.6 GB (from 0.5 GB at 16:14Z)" -- a bitcashier fail-before sbt
# (-Xms1500M -Xmx6000M) growing its heap toward its own -Xmx, a JVM's normal warm-up, called a runaway from one jump
# between two samples. So the RSS rule is now: past the process's OWN limit, and growing for WP_PERSIST_MIN minutes
# (both halves of the kept window grew: one jump is no trend). The limit is WP_RSS_GB; a JVM's is its -Xmx plus
# WP_JVM_MARGIN of it plus WP_JVM_NONHEAP_GB (metaspace, code cache, threads, direct buffers); a process under a live
# governor ticket (sbt-warm, gov-run, fail-before: the shell holding it, or its descendants) may also use the memory and
# cores it was charged.
#
# 17:10Z the same day: "mongod pid 65521: RSS 9.2 GB (from 9.1 GB at 16:25Z), past 4 GB, growing for 45 min" -- the
# local prod mirror warming WiredTiger's cache (no cacheSizeGB: 50% of (RAM - 1 GB), ~68 GB here). So a process with a
# known memory CEILING is judged against it (CEILINGS: a JVM's -Xmx, a mongod's cache, a VM's guest memory), and growth must reach
# WP_GROWTH_PCT or WP_GROWTH_MB over the window: 1% of a big long-lived server is its noise.

from agentctl.clock import epoch_now, wall_epoch
import glob
import os
import re
import time
from typing import Any, Callable, Optional
from agentctl.core import HOME, run_cmd, state_root, tilde
from agentctl.procs import ps_rows
from agentctl.psparse import parse_ps
from agentctl.statefile import read_record, read_text_or, write_json
from agentctl.machine import machine_fixed, ram_gb
from agentctl.govtickets import gov_tickets
from agentctl.testtmp import TEST_TMP_MAX_AGE
from agentctl.watchknobs import knob_wp_rss_by_name


WP_RSS_GB = 4.0
WP_CPU_PCT = 90.0
WP_CPU_MIN = 20
WP_PERSIST_MIN = 30  # minutes the RSS growth must have held before it is a trend
WP_WINDOW_MIN = 120  # minutes of RSS samples kept a process
WP_SAMPLE_GAP = 300  # s between kept RSS samples (the newest always the latest pass's)
WP_GROWTH_MB = 500  # RSS growth over the kept window that is a trend (or WP_GROWTH_PCT, the more); half in each half
WP_GROWTH_PCT = 5.0
WP_CEILING_MARGIN = 0.25  # x a ceiling (-Xmx, a mongod's cache) its RSS may exceed it by, besides the headroom
WP_CEILING_HEADROOM_GB = 1.0  # what lives beside it: a JVM's metaspace, code cache, threads; a mongod's connections
WT_STALE_MAX = 10  # suite roots past twice the sweep's bound before WT is violated
SPOTLIGHT_EXCLUDE = ("System Settings > Spotlight > Search Privacy: add ~/projects and /private/tmp (or `sudo mdutil "
                     "-i off /private/tmp`)")
SYSTEM_FIXES = {
    "fseventsd": "the owner's: `sudo killall fseventsd` (launchd restarts it), then " + SPOTLIGHT_EXCLUDE,
    "mds_stores": "the owner's: " + SPOTLIGHT_EXCLUDE + "; `sudo killall mds_stores` (launchd restarts it)",
    "mds": "the owner's: " + SPOTLIGHT_EXCLUDE,
    "mdworker_shared": "the owner's: " + SPOTLIGHT_EXCLUDE,
}
OURS = re.compile(r"^(java|python3?(\.\d+)?|claude|node|sbt|bash|git)$")
XMX = re.compile(r"(?:^|\s)-Xmx(\d+)([kKmMgGtT]?)(?=\s|$)")
MONGOD_CACHE_ARG = re.compile(r"--wiredTigerCacheSizeGB[=\s]+([\d.]+)")
MONGOD_CONFIG_ARG = re.compile(r"(?:--config|-f)[=\s]+(\S+)")
MONGOD_CACHE_CONF = re.compile(r"^\s*cacheSizeGB\s*:\s*([\d.]+)", re.M)
ProcRow = tuple[int, int, int, float, str]  # (pid, ppid, rss KB, %cpu, command name)


def proc_rows(ps_out: Optional[str] = None) -> list[ProcRow]:
    """[(pid, ppid, rss KB, %cpu, command name)] of every process (ps), or of `ps_out` (the tests')."""
    fixed = machine_fixed() if ps_out is None else None  # the tests' fixed machine: its own processes, else none
    cols = ("pid", "ppid", "rss", "pcpu", "comm")
    if ps_out is None and fixed is None:
        found = ps_rows(cols)
    else:
        found = parse_ps(ps_out if ps_out is not None else str((fixed or {}).get("ps") or ""), cols)
    return [(r.pid, r.ppid, r.rss, r.pcpu, os.path.basename(r.comm)) for r in found]


def jvm_xmx_kb(args: str) -> Optional[int]:
    """The KB of a JVM command line's -Xmx (its last one wins, as in java), or None."""
    found = XMX.findall(args or "")
    if not found:
        return None
    n, unit = found[-1]
    return int(n) * {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}[unit.lower()] // 1024


def mongod_cache_kb(args: str, ram: float) -> int:
    """The KB of a mongod's WiredTiger cache: --wiredTigerCacheSizeGB, else its config's cacheSizeGB, else WiredTiger's
    default, 50% of (RAM - 1 GB) and at least 256 MB."""
    found = MONGOD_CACHE_ARG.search(args or "")
    conf = MONGOD_CONFIG_ARG.search(args or "")
    if not found and conf:
        found = MONGOD_CACHE_CONF.search(read_text_or(os.path.expanduser(conf.group(1)), ""))
    gb = float(found.group(1)) if found else max(0.25, 0.5 * (ram - 1))
    return int(gb * 1024 * 1024)


LIMA_MEMORY = re.compile(r"^memory:\s*[\"']?(\d+(?:\.\d+)?)\s*([KMGT]?)i?B?[\"']?\s*$", re.M)
DOCKER_MEMORY = re.compile(r"\"[Mm]emoryMiB\"\s*:\s*(\d+)()")
SIZE_UNITS = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
# Where a Linux VM's guest memory is configured, under HOME: (glob, its memory line, the bytes of a bare number).
VM_MEMORY_CONFIGS = (
    ("Library/Application Support/rancher-desktop/lima/*/lima.yaml", LIMA_MEMORY, 1),
    (".lima/*/lima.yaml", LIMA_MEMORY, 1),
    (".colima/_lima/*/lima.yaml", LIMA_MEMORY, 1),
    (".colima/*/colima.yaml", LIMA_MEMORY, 1024 ** 3),
    ("Library/Group Containers/group.com.docker/settings-store.json", DOCKER_MEMORY, 1024 ** 2),
    ("Library/Group Containers/group.com.docker/settings.json", DOCKER_MEMORY, 1024 ** 2),
)


def _configured_kb(path: str, line: re.Pattern[str], bare: int) -> Optional[int]:
    """The KB of the memory `line` reads in config `path` (a bare number counted in `bare` bytes), or None."""
    m = line.search(read_text_or(path, ""))  # an unreadable config: nothing found
    return int(float(m.group(1)) * (SIZE_UNITS[m.group(2).upper()] if m.group(2) else bare)) // 1024 if m else None


def vm_memory_kb(home: str) -> Optional[int]:
    """The KB of the largest guest memory a Linux VM here is configured with (Rancher Desktop, lima, colima, Docker
    Desktop: VM_MEMORY_CONFIGS), or None. A VM process names no instance, so the largest bounds them all."""
    found = [_configured_kb(path, line, bare) for pattern, line, bare in VM_MEMORY_CONFIGS
             for path in glob.glob(os.path.join(home, pattern))]
    return max((kb for kb in found if kb), default=None)


def _jvm_ceiling(args: str, ram: float, home: str) -> Optional[tuple[int, str]]:
    xmx = jvm_xmx_kb(args)
    return (xmx, f"its -Xmx {xmx / 1024 / 1024:.1f} GB") if xmx else None


def _mongod_ceiling(args: str, ram: float, home: str) -> Optional[tuple[int, str]]:
    cache = mongod_cache_kb(args, ram)
    return cache, f"its WiredTiger cache {cache / 1024 / 1024:.1f} GB"


def _vm_ceiling(args: str, ram: float, home: str) -> Optional[tuple[int, str]]:
    guest = vm_memory_kb(home)
    return (guest, f"its guest memory {guest / 1024 / 1024:.1f} GB") if guest else None


# The process kinds that set their own memory ceiling, and how to read it from a command line, the RAM GB and HOME:
# a JVM's -Xmx, a mongod's WiredTiger cache, a Linux VM's configured guest memory (Docker / colima / Rancher Desktop's,
# which `run-script-tests --linux` and the containers use: its RSS fills toward it as the guest touches pages). Any
# other process is held to WP_RSS_GB.
CEILINGS: tuple[tuple[re.Pattern[str], Callable[[str, float, str], Optional[tuple[int, str]]]], ...] = (
    (re.compile(r"^java$"), _jvm_ceiling),
    (re.compile(r"^mongod$"), _mongod_ceiling),
    (re.compile(r"^(com\.apple\.Virtualization\.VirtualMachine|qemu-system-\w+|vfkit|krunkit)$"), _vm_ceiling),
)


def ceiling_kind(name: str) -> Optional[Callable[[str, float, str], Optional[tuple[int, str]]]]:
    """The reader of a process kind's own memory ceiling (CEILINGS), or None: an unknown kind."""
    return next((read for kind, read in CEILINGS if kind.match(name)), None)


def proc_ceiling(name: str, args: str, ram: float, home: str) -> Optional[tuple[int, str]]:
    """(KB, what it is) of the memory ceiling a process of `name` run with `args` sets itself, or None (CEILINGS)."""
    read = ceiling_kind(name)
    return read(args, ram, home) if read else None


def proc_args(pid: int) -> str:
    """The process's command line (ps), or ''."""
    try:
        return str(run_cmd(["ps", "-ww", "-o", "args=", "-p", str(pid)], timeout=10).stdout).strip()
    except OSError:
        return ""


def no_args(pid: int) -> str:
    """No command line: the tests' processes, unless they give one."""
    return ""


def no_ceiling(pid: int, name: str) -> Optional[tuple[int, str]]:
    return None


def _ticket_of(pid: int, ppids: dict[int, int], tickets: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The live governor ticket held by `pid` or one of its ancestors, or None."""
    by_pid = {int(t.get("pid") or 0): t for t in tickets}
    for _ in range(32):
        if pid in by_pid:
            return by_pid[pid]
        if pid <= 1 or pid not in ppids:
            return None
        pid = ppids[pid]
    return None


def wp_rss_gb(name: str) -> float:
    """The RSS limit in GB of the process called `name`: its `name=GB` entry of AGENTCTL_WP_RSS_BY_NAME, else WP_RSS_GB."""
    for entry in knob_wp_rss_by_name().split(","):
        key, _, gb = entry.partition("=")
        try:
            if key.strip() == name:
                return float(gb)
        except ValueError:
            continue
    return WP_RSS_GB


def _limits(p: dict[str, Any], ticket: Optional[dict[str, Any]]) -> tuple[float, str, float]:
    """(RSS KB past which process record `p` may not grow, what that limit is, the %CPU it may hold)."""
    gb = 1024 * 1024
    ceiling = p.get("ceiling")
    rss, said = (ceiling[0] * (1 + WP_CEILING_MARGIN) + WP_CEILING_HEADROOM_GB * gb, f"past {ceiling[1]} + margin") \
        if ceiling else (wp_rss_gb(p["name"]) * gb, f"past {wp_rss_gb(p['name']):g} GB")
    cpu = WP_CPU_PCT
    if ticket:
        charged = float(ticket.get("mem") or 0) * gb
        if charged > rss:
            rss, said = charged, f"past its governor charge {charged / gb:g} GB"
        cpu = max(cpu, float(ticket.get("cores") or 0) * 100)
    return rss, said, cpu


def _growing(rss: list[list[float]]) -> bool:
    """The kept samples span WP_PERSIST_MIN and grew in both halves: a climb that held, not one jump."""
    if len(rss) < 3 or rss[-1][0] - rss[0][0] < WP_PERSIST_MIN * 60:
        return False
    mid = min(rss[1:-1], key=lambda s: abs(s[0] - (rss[0][0] + rss[-1][0]) / 2))
    step = max(WP_GROWTH_MB * 1024, rss[0][1] * WP_GROWTH_PCT / 100) / 2  # 500 MB or 5% in all: less is noise
    return mid[1] - rss[0][1] >= step and rss[-1][1] - mid[1] >= step


def _proc_finding(p: dict[str, Any], now_: float, ticket: Optional[dict[str, Any]]) -> Optional[str]:
    """Why kept process record `p` is a runaway now, or None."""
    rss = p["rss"]
    limit, past, _cpu = _limits(p, ticket)
    big = rss[-1][1] > limit and _growing(rss)
    hot = p.get("hot_since") is not None and now_ - p["hot_since"] >= WP_CPU_MIN * 60
    if not (big or hot):
        return None
    gb = lambda kb: f"{kb / 1024 / 1024:.1f} GB"  # noqa: E731
    said = [f"RSS {gb(rss[-1][1])} (from {gb(rss[0][1])} at {time.strftime('%H:%MZ', time.gmtime(rss[0][0]))}), "
            f"{past} ({gb(limit)}), growing for {(rss[-1][0] - rss[0][0]) / 60:.0f} min"] if big else []
    if hot:
        said.append(f"CPU {p['cpu']:.0f}%, over {WP_CPU_PCT:g}% for {(now_ - p['hot_since']) / 60:.0f} min")
    return ", ".join(said)


def _keep_rss(kept: list[list[float]], rss: int, now_: float) -> list[list[float]]:
    """The kept RSS samples moved on by one: WP_SAMPLE_GAP apart (the newest replaced until then), WP_WINDOW_MIN long."""
    kept = kept[:-1] if len(kept) >= 2 and now_ - kept[-2][0] < WP_SAMPLE_GAP else kept
    return [s for s in kept if now_ - s[0] <= WP_WINDOW_MIN * 60] + [[now_, rss]]


def _proc_sample(state: dict[str, Any], row: ProcRow, now_: float, ticket: Optional[dict[str, Any]],
                 ceiling_of: Callable[[int, str], Optional[tuple[int, str]]]) -> dict[str, Any]:
    """Process pid's kept record moved on by one sample (a new one when the pid now runs another command)."""
    pid, _ppid, rss, cpu, name = row
    key = str(pid)
    p = state.get(key) if (state.get(key) or {}).get("name") == name else None
    p = p or {"name": name, "rss": [], "hot_since": None}
    if "ceiling" not in p and ceiling_kind(name):
        p["ceiling"] = ceiling_of(pid, name)
    p["rss"] = _keep_rss(p["rss"], rss, now_)
    p["cpu"] = cpu
    p["hot_since"] = (now_ if p["hot_since"] is None else p["hot_since"]) if cpu > _limits(p, ticket)[2] else None
    state[key] = p
    return p


def proc_watch(rows: list[ProcRow], state: dict[str, Any], now_: float, tickets: Optional[list[dict[str, Any]]] = None,
               ceiling_of: Optional[Callable[[int, str], Optional[tuple[int, str]]]] = None) -> list[tuple[int, str, str]]:
    """The kept samples `state` ({pid: record}) moved on by `rows`; [(pid, name, why)] of the runaways now. `tickets`:
    the live governor tickets; `ceiling_of`: a process's own memory ceiling (proc_ceiling), by pid and name."""
    ppids = {pid: ppid for pid, ppid, *_ in rows}
    seen, found = set(), []
    for row in rows:
        seen.add(str(row[0]))
        ticket = _ticket_of(row[0], ppids, tickets or [])
        p = _proc_sample(state, row, now_, ticket, ceiling_of or no_ceiling)
        why = _proc_finding(p, now_, ticket)
        if why:
            found.append((row[0], row[4], why))
    for key in set(state) - seen:  # a process gone: its episode with it
        del state[key]
    return found


def proc_cwd(pid: int) -> Optional[str]:
    """The process's working directory (lsof), or None."""
    try:
        out = run_cmd(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], timeout=10).stdout
    except OSError:
        return None
    return next((l[1:] for l in out.splitlines() if l.startswith("n")), None)


def proc_fix(pid: int, name: str, cwd_of: Optional[Callable[[int], Optional[str]]] = None) -> str:
    """What to do about runaway `name`: a system daemon's owner fix, or one of ours named by where it runs."""
    if name in SYSTEM_FIXES:
        return SYSTEM_FIXES[name]
    cwd = (cwd_of or proc_cwd)(pid) if OURS.match(name) else None
    ours = tuple(os.path.join(HOME, d) for d in ("projects", "bitcashier", "bin")) + ("/private/tmp", "/tmp")
    if cwd and cwd.startswith(ours):
        return f"ours: it runs in {tilde(cwd)} (that worktree's session: `agentctl status`, `agentctl orphans`)"
    return "the owner's to look at (agentctl never stops a process it did not start)"


def _wp_machine(ps_out: Optional[str], tickets: Optional[list[dict[str, Any]]], args_of: Optional[Callable[[int], str]],
                ram: Optional[float], home: Optional[str]
                ) -> tuple[list[dict[str, Any]], Callable[[int, str], Optional[tuple[int, str]]]]:
    """(the live governor tickets, a process's memory ceiling by pid and name): from the given tickets, command lines,
    RAM GB and HOME (a VM's config), else the real machine's -- no tickets nor command lines when the processes are the tests' (`ps_out`, or
    a fixed machine)."""
    real = ps_out is None and machine_fixed() is None
    args, gb = args_of or (proc_args if real else no_args), ram if ram is not None else ram_gb()
    return (tickets if tickets is not None else gov_tickets() if real else []), \
        lambda pid, name: proc_ceiling(name, args(pid), gb, home or HOME)


def inv_wp(ps_out: Optional[str] = None, now_: Optional[float] = None, state_path: Optional[str] = None,
           cwd_of: Optional[Callable[[int], Optional[str]]] = None, args_of: Optional[Callable[[int], str]] = None,
           tickets: Optional[list[dict[str, Any]]] = None, ram: Optional[float] = None,
           home: Optional[str] = None) -> tuple[bool, str]:
    """WP: no process past its own RSS limit (WP_RSS_GB; a ceiling it sets itself plus headroom: a JVM's -Xmx, a mongod's
    WiredTiger cache, a VM's guest memory; a governor ticket's charge) and
    growing for WP_PERSIST_MIN minutes, or over WP_CPU_PCT (a ticket's charged cores) for WP_CPU_MIN minutes (any
    process: a system daemon too). Its samples are kept in procwatch.json between passes. The tests give `ps_out`
    (or a fixed machine), and with it `args_of`, `tickets`, `ram` and `home`: none read from the real machine but its RAM and HOME."""
    path, t = state_path or state_root("procwatch.json"), now_ if now_ is not None else epoch_now()
    state = read_record(path) or {}
    found = proc_watch(proc_rows(ps_out), state, t, *_wp_machine(ps_out, tickets, args_of, ram, home))
    try:
        write_json(path, state)
    except OSError:
        pass
    if not found:
        return True, "no runaway process"
    return False, "; ".join(f"{name} pid {pid}: {why} -- {proc_fix(pid, name, cwd_of)}" for pid, name, why in found[:5])


def _wt_places(tmp_root: Optional[str], projects: Optional[str]) -> tuple[str, str]:
    """(the temp root, the projects dir) WT reads: the given ones, the tests' fixed machine's (none unless it names
    them), else /tmp and ~/projects."""
    fixed = machine_fixed() if tmp_root is None else None
    if fixed is not None:
        return (fixed.get("tmp_root") or os.path.join(HOME, ".no-tmp"),
                projects or fixed.get("projects") or os.path.join(HOME, ".no-projects"))
    return tmp_root or "/tmp", projects or os.path.join(HOME, "projects")


def _older_than(p: str, t: float, secs: float) -> bool:
    try:
        return t - os.lstat(p).st_mtime > secs
    except OSError:
        return False


def inv_wt(tmp_root: Optional[str] = None, now_: Optional[float] = None,
           projects: Optional[str] = None) -> tuple[bool, str]:
    """WT: the test suite's roots past twice the sweep's bound stay under WT_STALE_MAX (upkeep removes the ones no
    runner holds); said with them, how many worktrees hold build output (target/, node_modules)."""
    t = now_ if now_ is not None else wall_epoch()  # against directory mtimes
    tmp_root, projects = _wt_places(tmp_root, projects)
    stale = [p for p in glob.glob(os.path.join(tmp_root, "agentctl-t-*")) if _older_than(p, t, 2 * TEST_TMP_MAX_AGE)]
    trees = glob.glob(os.path.join(projects, "movies-*"))
    built = sum(1 for w in trees if any(os.path.isdir(os.path.join(w, d)) for d in ("target", "node_modules")))
    said = f"{len(stale)} test roots in {tmp_root} past {2 * TEST_TMP_MAX_AGE / 3600:g} h; {built} worktrees hold build output"
    if len(stale) <= WT_STALE_MAX:
        return True, said
    return False, (f"{said} -- file churn feeds fseventsd: upkeep's sweep removes the roots no runner holds "
                   "(`agentctl maintain` runs it now); a root it keeps is held by a live test")

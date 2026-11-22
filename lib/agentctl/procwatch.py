# agentctl, part `procwatch`: the watchdog's machine invariants WP (a runaway process) and WT (temp churn).
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).
#
# 2026-11-22: fseventsd held 21.5 GB and over 100% CPU for days, and mds_stores ran hot beside it, fed by the file
# churn of 174 leaked test roots (/private/tmp/agentctl-t-*, 197 MB of fake repos) -- and nothing said so. WP watches
# EVERY process, the system's daemons too: an RSS over WP_RSS_GB that keeps growing, or a CPU over WP_CPU_PCT held for
# WP_CPU_MIN minutes, from samples kept between passes. It names the process, its RSS and CPU trend, and the fix --
# for a system daemon the OWNER's command (agentctl never stops or reconfigures a process or setting it did not start:
# `sudo killall fseventsd`, which launchd restarts, and the Spotlight exclusions), for one of ours the worktree it runs
# in. WT counts the churn itself: test suite roots past their bound (upkeep's sweep removes the ones no runner holds)
# and the worktrees holding build output.
WP_RSS_GB = 4.0
WP_CPU_PCT = 90.0
WP_CPU_MIN = 20
WP_KEEP = 6  # RSS samples kept a process
WP_GROWTH_MB = 100  # RSS growth over the kept samples that is a trend (and at least 1%)
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


def proc_rows(ps_out: Optional[str] = None) -> list[tuple[int, int, float, str]]:
    """[(pid, rss KB, %cpu, command name)] of every process (ps), or of `ps_out` (the tests')."""
    fixed = machine_fixed() if ps_out is None else None  # the tests' fixed machine: its own processes, else none
    out = ps_out if ps_out is not None else str(fixed.get("ps") or "") if fixed is not None else \
        run_cmd(["ps", "-A", "-o", "pid=,rss=,pcpu=,comm="]).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        try:
            rows.append((int(parts[0]), int(parts[1]), float(parts[2]), os.path.basename(parts[3].strip())))
        except (ValueError, IndexError):
            continue
    return rows


def _proc_finding(p: dict[str, Any], now_: float) -> Optional[str]:
    """Why kept process record `p` is a runaway now, or None."""
    rss = p["rss"]
    grew = rss[-1][1] - rss[0][1] >= max(WP_GROWTH_MB * 1024, rss[0][1] / 100)  # 100 MB or 1%: noise is no trend
    big = rss[-1][1] > WP_RSS_GB * 1024 * 1024 and len(rss) > 1 and grew
    hot = p.get("hot_since") is not None and now_ - p["hot_since"] >= WP_CPU_MIN * 60
    if not (big or hot):
        return None
    gb = lambda kb: f"{kb / 1024 / 1024:.1f} GB"  # noqa: E731
    said = [f"RSS {gb(rss[-1][1])} (from {gb(rss[0][1])} at {time.strftime('%H:%MZ', time.gmtime(rss[0][0]))})"] \
        if big else []
    if hot:
        said.append(f"CPU {p['cpu']:.0f}%, over {WP_CPU_PCT:g}% for {(now_ - p['hot_since']) / 60:.0f} min")
    return ", ".join(said)


def _proc_sample(state: dict[str, Any], pid: int, rss: int, cpu: float, name: str, now_: float) -> dict[str, Any]:
    """Process pid's kept record moved on by one sample (a new one when the pid now runs another command)."""
    key = str(pid)
    p = state.get(key) if (state.get(key) or {}).get("name") == name else None
    p = p or {"name": name, "rss": [], "hot_since": None}
    p["rss"] = (p["rss"] + [[now_, rss]])[-WP_KEEP:]
    p["cpu"] = cpu
    p["hot_since"] = (now_ if p["hot_since"] is None else p["hot_since"]) if cpu > WP_CPU_PCT else None
    state[key] = p
    return p


def proc_watch(rows: list[tuple[int, int, float, str]], state: dict[str, Any],
               now_: float) -> list[tuple[int, str, str]]:
    """The kept samples `state` ({pid: record}) moved on by `rows`; [(pid, name, why)] of the runaways now."""
    seen, found = set(), []
    for pid, rss, cpu, name in rows:
        seen.add(str(pid))
        why = _proc_finding(_proc_sample(state, pid, rss, cpu, name, now_), now_)
        if why:
            found.append((pid, name, why))
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
        return f"ours: it runs in {cwd.replace(HOME, '~')} (that worktree's session: `agentctl status`, `agentctl orphans`)"
    return "the owner's to look at (agentctl never stops a process it did not start)"


def inv_wp(ps_out: Optional[str] = None, now_: Optional[float] = None, state_path: Optional[str] = None,
           cwd_of: Optional[Callable[[int], Optional[str]]] = None) -> tuple[bool, str]:
    """WP: no process past WP_RSS_GB and growing, or over WP_CPU_PCT for WP_CPU_MIN minutes (any process: a system
    daemon too). Its samples are kept in procwatch.json between passes."""
    path, t = state_path or state_root("procwatch.json"), now_ if now_ is not None else time.time()
    state = read_record(path) or {}
    found = proc_watch(proc_rows(ps_out), state, t)
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
    t = now_ if now_ is not None else time.time()
    tmp_root, projects = _wt_places(tmp_root, projects)
    stale = [p for p in glob.glob(os.path.join(tmp_root, "agentctl-t-*")) if _older_than(p, t, 2 * TEST_TMP_MAX_AGE)]
    trees = glob.glob(os.path.join(projects, "movies-*"))
    built = sum(1 for w in trees if any(os.path.isdir(os.path.join(w, d)) for d in ("target", "node_modules")))
    said = f"{len(stale)} test roots in {tmp_root} past {2 * TEST_TMP_MAX_AGE / 3600:g} h; {built} worktrees hold build output"
    if len(stale) <= WT_STALE_MAX:
        return True, said
    return False, (f"{said} -- file churn feeds fseventsd: upkeep's sweep removes the roots no runner holds "
                   "(`agentctl maintain` runs it now); a root it keeps is held by a live test")

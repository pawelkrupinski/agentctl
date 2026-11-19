# agentctl, part `land`: land.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- land / env
SDKMAN_BINS = [f"{HOME}/.sdkman/candidates/sbt/current/bin", f"{HOME}/.sdkman/candidates/java/current/bin"]
RACE = re.compile(r"non-fast-forward|cannot lock ref|fetch first|failed to update ref|stale info|incorrect old value|"
                  r"reference already exists|remote rejected.*(lock|update)", re.I)
# The hook REFUSING for red (its "is RED on main" warnings also print when tools-only commits land while red: not a refusal)
REDGATE = re.compile(r"main is red: only a fix|carries no Fixes-Red")
TUNNEL_TROUBLE = re.compile(r"GitOps manifests STALE|Connection to localhost closed|"
                            r"Could not read from remote repository|Connection refused", re.I)
LAND_RETRIES = 3
# One lander per repository: with no pre-merge CI left (movies, 2026-10-26) a landing is the hook's local tests --
# minutes for an identity change -- and two landers would each test a base the other is about to move, then
# race, rebase, and test again. Agents wait for the lock instead (land runs in the background), and it caps the
# laptop at one landing hook's JVM at a time.
LAND_LOCK_TIMEOUT = 4 * 3600
# NO STALLED HOLDER (the owner, 2026-10-27): one land's hook hung 2.5 h in sbt-warm and blocked every lander behind the
# lock. A step land runs (the hook's dry run, the push) that makes NO PROGRESS -- its output does not grow and its
# process group uses no CPU -- for LAND_STALL_MIN minutes ($AGENTCTL_LAND_STALL_MIN) is ended (its process group,
# TERM then KILL) and land says `NOT LANDED: stalled <step>`. And the long checks run BEFORE the lock: under it, only a
# fetch, a check that main has not moved since, and the push (whose hook reuses the dry run's pass). Waiters print
# how long the holder has held the lock, and what it is doing, every LAND_WAIT_REPORT_MIN minutes.
LAND_STALL_MIN = 20
LAND_WAIT_REPORT_MIN = 10
LAND_PRECHECK_ROUNDS = 2  # main moved under the lock this often: the next check runs under it, so a lander finishes


def group_cpu_seconds(pgid):
    """The CPU seconds the step's work has used: every live process of process group `pgid` AND every descendant of its
    leader, whatever group it runs in (ps's cumulative TIME), or 0. ISSUE 20261108T232313-7384d0: the movies hook's
    run-script-tests runs each self-test in a process group of its own, so a push whose hook ran scripttests for 19
    silent minutes read "no output and no CPU" and was ended twice."""
    r = run_cmd(["ps", "-A", "-o", "pid=,ppid=,pgid=,time=,stat="])
    rows = [line.split() for line in r.stdout.splitlines()]
    rows = [x if len(x) == 5 else x + [""] for x in rows if len(x) in (4, 5)]
    kids = {}
    for pid, ppid, _g, _t, _s in rows:
        kids.setdefault(ppid, []).append(pid)
    tree, todo = set(), [str(pgid)]
    while todo:
        cur = todo.pop()
        if cur not in tree:
            tree.add(cur)
            todo += kids.get(cur, [])
    mine = [x for x in rows if x[2] == str(pgid) or x[0] in tree]
    # A RUNNABLE PROCESS IS PROGRESS (as movies' stall-watch.sh counts it): under a load of 75 on 18 cores a busy child
    # is granted ~0.25 CPU-s a second, under the judge's 1 s a window, yet it is working, not stalled. Each sample with
    # one of the tree running or waiting for a CPU (state R) is credited STALL_CPU_SECONDS.
    # A process new since the last sample is no progress (a hook's helper loop forks one every second).
    seen = _tree_seen.get(pgid, set())
    if any(x[4].startswith("R") and x[0] in seen for x in mine):
        _runnable_credit[pgid] = _runnable_credit.get(pgid, 0.0) + env_num("AGENTCTL_STALL_CPU_SECONDS", STALL_CPU_SECONDS)
    _tree_seen[pgid] = {x[0] for x in mine}
    # ps's d-hh:mm:ss: a day is 86400 s (the old split read it as 60 s)
    return sum(tosec(x[3]) for x in mine) + _runnable_credit.get(pgid, 0.0)


_runnable_credit, _tree_seen = process_cache({}), process_cache({})


STALL_CPU_SECONDS = 1.0  # CPU that counts as progress per window: a watchdog's polls are not work


def stall_clock():
    """The stall judge's clock (tests drive it, with group_cpu_seconds, so no verdict depends on the machine's load)."""
    return time.monotonic()


WATCHED_GROUPS = set()  # process groups run_watched started and that may still run: the watchdog ends them first


def run_watched(argv, cwd, env, log_path, step, stall=None, sampler=None, cpu=None, clock=None, wall=None,
                quiet_ok=False, hook_stall=None, hooks=None):
    """Run argv (its own process group, stdin closed), its output into a file beside log_path. (returncode, output);
    returncode None when it STALLED: no output and no CPU in its process group for `stall` seconds, after which the
    group is ended. Waits with Popen.wait, never time.sleep. `cpu` (pgid -> CPU seconds) and `clock` are the samples the
    verdict reads: group_cpu_seconds and stall_clock unless given. quiet_ok: a step that legitimately waits quietly
    (a remote run polled without output) renews the leases this process holds at every poll, so its own `stall`
    bound ends it, not the leases' TTL. hook_stall (a push's): while a pre-push hook runs in the group (`hooks`, pgid
    -> bool: group_runs_hook unless given), it is judged as quiet_ok with hook_stall as its bound -- the hook bounds
    itself -- and `stall` judges the transport around it. A stalled run's output ends with its STALL_LINE (why)."""
    cpu_of, clock = cpu or group_cpu_seconds, clock or stall_clock
    hooks = hooks or group_runs_hook
    stall = stall if stall is not None else env_num("AGENTCTL_LAND_STALL_MIN", LAND_STALL_MIN) * 60
    out_path = f"{log_path}.{re.sub(r'[^A-Za-z0-9_-]', '_', step)}.out"
    with open(out_path, "w") as out:
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
    WATCHED_GROUPS.add(p.pid)
    try:
        stalled = _watch_group(p, out_path, step, stall, sampler, cpu_of, clock, wall, quiet_ok,
                               hook_stall, hooks)
    except BaseException:  # a store error, a signal: never a child group left running, untracked
        _end_group(p)
        WATCHED_GROUPS.discard(p.pid)
        raise
    WATCHED_GROUPS.discard(p.pid)
    with open(out_path, errors="replace") as f:
        output = f.read()
    try:
        os.unlink(out_path)
    except OSError:
        pass
    if stalled:
        output += f"{'' if not output or output.endswith(chr(10)) else chr(10)}{STALL_LINE}{stalled}\n"
    return (None if stalled else p.returncode), output


STALL_LINE = "agentctl: ended, "  # run_watched's last output line of a run it ended: STALL_LINE + why


def stall_reason(output):
    """Why run_watched ended a run (its output's STALL_LINE), or None."""
    for line in reversed((output or "").splitlines()):
        if line.startswith(STALL_LINE):
            return line[len(STALL_LINE):].strip()
    return None


HOOK_COMMAND = re.compile(r"(^|[\s/])hooks/pre-push(\s|$)")


def group_runs_hook(pgid):
    """Whether a live process of group `pgid` is a git pre-push hook (`.../hooks/pre-push <remote> <url>`)."""
    r = run_cmd(["ps", "-A", "-o", "pgid=,command="])
    for line in r.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] == str(pgid) and HOOK_COMMAND.search(parts[1]):
            return True
    return False


def _end_group(p):
    for sig, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(p.pid, sig)
        except ProcessLookupError:
            return
        try:
            p.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def beat_mark(step, size, cpu, polls=0, quiet_ok=False):
    """The progress mark a poll renews the leases with: output bytes and whole CPU seconds -- and, for a quiet_ok step,
    the poll count (every poll is progress: its own stall bound judges it)."""
    return f"{step}:{size}:{int(cpu or 0)}" + (f":{polls}" if quiet_ok else "")


def _watch_group(p, out_path, step, stall, sampler, cpu_of, clock, wall, quiet_ok=False, hook_stall=None, hooks=None):
    """run_watched's wait: why it ended the group (it stalled, lost a lease, passed its wall bound), or None when the
    group exited by itself."""
    size, cpu, changed, polls, in_hook = -1, None, clock(), 0, False
    poll = max(0.2, min(15.0, stall / 4))
    need = env_num("AGENTCTL_STALL_CPU_SECONDS", STALL_CPU_SECONDS)
    t_start = clock()  # the wall bound reads the same clock: tests drive it, so no verdict depends on the load
    while True:
        try:
            p.wait(timeout=poll)
            break
        except subprocess.TimeoutExpired:
            pass
        if sampler:
            sampler()
        now_size, now_cpu = os.path.getsize(out_path), cpu_of(p.pid)
        was_hook, in_hook = in_hook, hook_stall is not None and hooks(p.pid)
        why = None
        try:  # the landing lease's holder renews on real progress only (output bytes, whole CPU seconds)
            polls += 1
            lock_beat(beat_mark(step, now_size, now_cpu, polls, quiet_ok or in_hook))
        except LeaseLost as ex:
            why = f"{ex} (no progress within its TTL)"
        except StoreUnavailable as ex:  # no verdict: renewed at the next poll (a lease that lapsed meanwhile is lost)
            err(f"{step}: the store did not answer the lease renewal ({str(ex)[:120]})")
        # progress: output grew, or the group used a real second of CPU since the last progress (the hook idle in
        # `wait` with only its watchdog polling used ~0.08 s in 50 min, 2026-10-27: that is not progress); a hook
        # ending is progress too (the transport's bound starts there)
        now_c = clock()
        bound = hook_stall if in_hook else stall
        if now_size != size or cpu is None or now_cpu - cpu >= need or was_hook != in_hook:
            size, cpu, changed = now_size, now_cpu, now_c
        elif now_c - changed > bound:
            why = why or (f"no output and no CPU for {bound:g} s" + (" while its pre-push hook ran" if in_hook else ""))
        if wall is not None and now_c - t_start > wall:  # busy or not: the wall bound (a push's)
            why = why or f"past its wall bound of {wall:g} s"
        if why:
            err(f"{why}: ending {step}")
            _end_group(p)
            return why
    return None

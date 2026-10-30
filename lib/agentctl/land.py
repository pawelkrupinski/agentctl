# agentctl, part `land`: land.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- land / env
SDKMAN_BINS = [f"{HOME}/.sdkman/candidates/sbt/current/bin", f"{HOME}/.sdkman/candidates/java/current/bin"]
RACE = re.compile(r"non-fast-forward|cannot lock ref|fetch first|failed to update ref|stale info|incorrect old value|"
                  r"reference already exists|remote rejected.*(lock|update)", re.I)
# The hook REFUSING for red (its "is RED on main" warnings also print when tools-only commits land while red: not a refusal)
REDGATE = re.compile(r"main is red: only a fix|carries no Fixes-Red")
TUNNEL_TROUBLE = re.compile(r"GitOps manifests STALE|Connection to localhost closed|forgejo\.internal.*(refused|unreachable)|"
                            r"Could not read from remote repository|Connection refused", re.I)
LAND_RETRIES = 3
# One lander per repository: with no pre-merge CI left (movies, 2026-10-26) a landing is the hook's local tests --
# minutes for an identity or page change -- and two landers would each test a base the other is about to move, then
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
    """The CPU seconds every live process of process group `pgid` has used (ps's cumulative TIME), or 0."""
    r = run_cmd(["ps", "-A", "-o", "pgid=,time="])
    total = 0.0
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[0] != str(pgid):
            continue
        secs = 0.0
        for field in re.split(r"[:-]", parts[1]):
            try:
                secs = secs * 60 + float(field)
            except ValueError:
                pass
        total += secs
    return total


STALL_CPU_SECONDS = 1.0  # CPU that counts as progress per window: a watchdog's polls are not work


def stall_clock():
    """The stall judge's clock (tests drive it, with group_cpu_seconds, so no verdict depends on the machine's load)."""
    return time.monotonic()


WATCHED_GROUPS = set()  # process groups run_watched started and that may still run: the watchdog ends them first


def run_watched(argv, cwd, env, log_path, step, stall=None, sampler=None, cpu=None, clock=None, wall=None):
    """Run argv (its own process group, stdin closed), its output into a file beside log_path. (returncode, output);
    returncode None when it STALLED: no output and no CPU in its process group for `stall` seconds, after which the
    group is ended. Waits with Popen.wait, never time.sleep. `cpu` (pgid -> CPU seconds) and `clock` are the samples the
    verdict reads: group_cpu_seconds and stall_clock unless given."""
    cpu_of, clock = cpu or group_cpu_seconds, clock or stall_clock
    stall = stall if stall is not None else env_num("AGENTCTL_LAND_STALL_MIN", LAND_STALL_MIN) * 60
    out_path = f"{log_path}.{re.sub(r'[^A-Za-z0-9_-]', '_', step)}.out"
    with open(out_path, "w") as out:
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
    WATCHED_GROUPS.add(p.pid)
    size, cpu, changed, stalled = -1, None, clock(), False
    poll = max(0.2, min(15.0, stall / 4))
    need = env_num("AGENTCTL_STALL_CPU_SECONDS", STALL_CPU_SECONDS)
    t_start = time.monotonic()
    while True:
        try:
            p.wait(timeout=poll)
            break
        except subprocess.TimeoutExpired:
            pass
        if sampler:
            sampler()
        now_size, now_cpu = os.path.getsize(out_path), cpu_of(p.pid)
        try:  # the landing lease's holder renews on real progress only (output bytes, whole CPU seconds)
            lock_beat(f"{step}:{now_size}:{int(now_cpu or 0)}")
        except LeaseLost as ex:
            err(f"{ex}: ending {step}")
            stalled = True
        # progress: output grew, or the group used a real second of CPU since the last progress (the hook idle in
        # `wait` with only its watchdog polling used ~0.08 s in 50 min, 2026-10-27: that is not progress)
        now_c = clock()
        if now_size != size or cpu is None or now_cpu - cpu >= need:
            size, cpu, changed = now_size, now_cpu, now_c
        elif now_c - changed > stall:
            stalled = True
        if wall is not None and time.monotonic() - t_start > wall:  # busy or not: the wall bound (a push's)
            stalled = True
        if stalled:
            for sig, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
                try:
                    os.killpg(p.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    p.wait(timeout=grace)
                    break
                except subprocess.TimeoutExpired:
                    continue
            break
    WATCHED_GROUPS.discard(p.pid)
    with open(out_path, errors="replace") as f:
        output = f.read()
    try:
        os.unlink(out_path)
    except OSError:
        pass
    return (None if stalled else p.returncode), output

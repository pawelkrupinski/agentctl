# agentctl, part `slots`: check slots, the landing lease, the known-red baseline.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- check slots
# The owner (2026-10-27): "just limit the precommit checks to a reasonable number", and "keep measuring to adjust that
# number". Every pre-commit check run (land's dry run, the `waiting` batch check, the known-red baseline) is admitted by
# THE GOVERNOR (above) as a check-kind ticket of class `check` (`red` for a change fixing a red run, which skips the
# cap): at most N run at once. Its ticket counts a JVM's 5 GB: the hook's `sbt -batch` takes no ticket of its own; an
# sbt server it starts through sbt-warm does (at the check's priority, KINOWO_GOV_CLASS), counted twice for a while. Its
# cores are the check's (GOV_JOB_CORES["check"]): the hook's sbt-warm calls take no cores ticket (KINOWO_GOV_CPU_HELD).
# N: $AGENTCTL_MAX_CHECKS if set, else the automatic cap (checks/cap.json, default 3, at most the budget's worth of sbt
# servers). A waiter prints why it waits. Each run appends to checks/runs.jsonl: its duration, its wait, the cap and the
# number of checks running, the load average, CPU % and memory pressure / swap sampled during it. After each run the cap
# is adjusted from the data (adjust_cap), at most once a day, each change logged and sent to the inbox.
CHECKS_DEFAULT_CAP = 3


def checks_dir():
    d = os.environ.get("AGENTCTL_CHECKS_DIR") or state_root("checks")
    os.makedirs(d, exist_ok=True)
    return d


def gov_max_checks():
    """The most checks the budget holds: one sbt server's worth of memory each."""
    return max(1, int(gov_budget() // GOV_MEM_GB["check"]))


def cap_state():
    return read_record(os.path.join(checks_dir(), "cap.json"), {"cap": CHECKS_DEFAULT_CAP})


def checks_cap():
    if os.environ.get("AGENTCTL_MAX_CHECKS"):
        return max(1, int(os.environ["AGENTCTL_MAX_CHECKS"]))
    return max(1, min(gov_max_checks(), int(cap_state().get("cap", CHECKS_DEFAULT_CAP))))


def machine_sample():
    """(load1, cpu %, memory pressure %, swap used MB) of this machine, cheaply; None where unreadable."""
    load1 = machine_load1()
    cpu = min(100.0, 100.0 * load1 / machine_cores())
    m = mem_stats()
    return load1, cpu, m["pressure"], m["swap_used_mb"]


class Slot:
    def __init__(self, label, cls="check"):
        self.label, self.cls, self.ticket, self.waited, self.t0 = label, cls, None, 0.0, None
        self.samples, self.running, self.cap, self._last_sample = [], 0, 0, 0.0

    def sample(self, force=False):
        if force or time.monotonic() - self._last_sample >= 30:
            self._last_sample = time.monotonic()
            try:
                self.samples.append(machine_sample())
            except Exception:
                pass

    def done(self, rc, out):
        self.rc, self.out = rc, out

    def env(self, env):
        """The hook's environment: its sbt JVM is admitted at this check's priority, and its sbt-warm calls take no cores
        ticket of their own (KINOWO_GOV_CPU_HELD): this check's ticket is charged the hook's cores already."""
        return dict(env, KINOWO_GOV_CLASS=self.cls, KINOWO_GOV_CPU_HELD="1")


def slot_holders(cap=None):
    return [f"pid {t.get('pid')} {t.get('label')} since {t.get('since')} ({t.get('class')})"
            for t in gov_tickets() if t.get("kind") == "check"]


@contextlib.contextmanager
def check_slot(repo, label, cls="check"):
    """Hold a check ticket of the governor for a pre-commit check run (waiting, with a line saying why, until it is
    admitted). Records the run in runs.jsonl and adjusts the cap afterwards."""
    slot = Slot(label, cls)
    slot.ticket = gov_admit(cls, GOV_MEM_GB["check"], label, kind="check",
                            max_wait=env_num("AGENTCTL_GOV_ADMIT_MAX", GOV_ADMIT_MAX))
    slot.waited, slot.cap = slot.ticket.waited, checks_cap()
    slot.running = len(slot_holders())
    slot.t0, slot.rc = time.monotonic(), None
    slot.sample(force=True)
    try:
        yield slot
    finally:
        slot.sample(force=True)
        record_check(slot, time.monotonic() - slot.t0)
        slot.ticket.release()
        try:
            maybe_adjust_cap()
        except Exception as e:
            err(f"could not adjust the check cap: {e}")


def record_check(slot, duration):
    def mx(i):
        vals = [x[i] for x in slot.samples if x and x[i] is not None]
        return max(vals) if vals else None
    rec = {"at": time.time(), "label": slot.label, "duration": round(duration, 1), "waited": round(slot.waited, 1),
           "cap": slot.cap, "running": slot.running, "rc": slot.rc, "load1": mx(0), "cpu": mx(1), "pressure": mx(2),
           "swap_mb": mx(3), "swap_mb_start": slot.samples[0][3] if slot.samples and slot.samples[0] else None}
    with open(os.path.join(checks_dir(), "runs.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")


def check_runs(days=7, now=None):
    now = now or time.time()
    out = []
    try:
        with open(os.path.join(checks_dir(), "runs.jsonl")) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("at", 0) >= now - days * 86400:
                    out.append(r)
    except OSError:
        pass
    return out


def pct(vals, q):
    vals = sorted(vals)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(round(q * (len(vals) - 1))))]


SLOT_WAIT_COMMON = 0.3        # share of the day's runs that waited over a minute for a slot
PRESSURE_LOW, PRESSURE_HIGH = 50, 75
SWAP_GROWTH_HIGH_MB = 512


def adjust_cap(runs, cap, max_cap, last_change, now):
    """(new cap, reason) from the last day's check runs, or (cap, None). At most one change a day. Lower by 1 (to 1)
    when memory pressure or swap rises, or when p90 duration at the current level is over 1.5x the p90 at the level
    below; raise by 1 (to max_cap) when slot waits are common and memory pressure and swap stay low."""
    if now - (last_change or 0) < 86400:
        return cap, None
    day = [r for r in runs if r.get("at", 0) >= now - 86400]
    if not day:
        return cap, None
    pressures = [r["pressure"] for r in day if r.get("pressure") is not None]
    growth = [(r["swap_mb"] or 0) - (r.get("swap_mb_start") or 0) for r in day if r.get("swap_mb") is not None]
    if (pressures and max(pressures) >= PRESSURE_HIGH) or (growth and max(growth) >= SWAP_GROWTH_HIGH_MB):
        if cap > 1:
            return cap - 1, (f"memory pressure up to {max(pressures) if pressures else '?'}% or swap growing up to "
                             f"{int(max(growth)) if growth else 0} MB during a check")
        return cap, None
    at = [r["duration"] for r in runs if r.get("running") == cap]
    below = [r["duration"] for r in runs if r.get("running") == cap - 1]
    if cap > 1 and len(at) >= 5 and len(below) >= 5 and pct(at, 0.9) > 1.5 * pct(below, 0.9):
        return cap - 1, f"p90 check duration with {cap} running is {pct(at, 0.9):.0f}s, over 1.5x the {pct(below, 0.9):.0f}s with {cap - 1}"
    waited = [r for r in day if r.get("waited", 0) > 60]
    if (cap < max_cap and len(day) >= 5 and len(waited) / len(day) >= SLOT_WAIT_COMMON
            and (not pressures or max(pressures) < PRESSURE_LOW) and (not growth or max(growth) < 64)):
        return cap + 1, (f"{len(waited)} of {len(day)} checks today waited over a minute for a slot, memory pressure "
                         f"under {PRESSURE_LOW}% and swap steady")
    return cap, None


def maybe_adjust_cap(now=None):
    if os.environ.get("AGENTCTL_MAX_CHECKS"):
        return None
    now = now or time.time()
    st = cap_state()
    cap = int(st.get("cap", CHECKS_DEFAULT_CAP))
    new, why = adjust_cap(check_runs(7, now), cap, gov_max_checks(), st.get("changed_at"), now)
    if why is None or new == cap:
        return None
    st = {"cap": new, "changed_at": now, "reason": why, "previous": cap}
    write_json(os.path.join(checks_dir(), "cap.json"), st, indent=None)
    line = f"CHECK CAP {cap} -> {new}: {why}"
    with open(os.path.join(checks_dir(), "adjust.log"), "a") as f:
        f.write(f"{ts()} {line}\n")
    inbox_write("checks", line, [line], [], 0)
    return line


def cmd_checks(a):
    if a.ccmd == "stats":
        runs = check_runs(a.days)
        cap = checks_cap()
        st = cap_state()
        print(f"check slots: cap {cap}" + (" ($AGENTCTL_MAX_CHECKS)" if os.environ.get("AGENTCTL_MAX_CHECKS") else
                                          f" (auto; last change: {st.get('reason', 'none')})")
              + f"; running now: {len(slot_holders(cap))}; {len(runs)} check run(s) in {a.days} d")
        print(f"{'running':>7} {'runs':>5} {'p50 dur':>8} {'p90 dur':>8} {'p50 wait':>9} {'p90 wait':>9} {'max mem%':>8} {'max swap':>8}")
        for level in sorted({r.get("running", 0) for r in runs}):
            rs = [r for r in runs if r.get("running", 0) == level]
            dur, wait = [r["duration"] for r in rs], [r["waited"] for r in rs]
            mem = [r["pressure"] for r in rs if r.get("pressure") is not None]
            swp = [r["swap_mb"] for r in rs if r.get("swap_mb") is not None]
            print(f"{level:>7} {len(rs):>5} {pct(dur, .5):>7.0f}s {pct(dur, .9):>7.0f}s {pct(wait, .5):>8.0f}s "
                  f"{pct(wait, .9):>8.0f}s {max(mem) if mem else '-':>8} {int(max(swp)) if swp else '-':>8}")
        return 0
    if a.ccmd == "status":
        cap = checks_cap()
        holders = slot_holders(cap)
        print(f"checks: {len(holders)}/{cap} running (the governor's check cap; `agentctl gov status` for the rest)"
              + "".join(f"\n  {h}" for h in holders))
        return 0
    return 2


def stalled(step, output, log_path):
    why = stall_reason(output) or f"no output and no CPU for {env_num('AGENTCTL_LAND_STALL_MIN', LAND_STALL_MIN):g} min"
    print(f"NOT LANDED: stalled {step}: {why}, so it was ended (the landing lock is free again). Last lines (full log "
          f"{log_path}):\n  {tail_of(output)}")
    return 1


# EVERY PUSH IS BOUNDED (the owner, 2026-10-28, "fix root cause in a structural way": a queued change's `git push` over
# the ssh tunnel died with "Connection to localhost closed by remote host" when the hotspot dropped, then hung -- no
# progress, no exit -- holding the landing lock 30+ min while a checked batch waited behind it with the gate green): a
# push gets a no-progress bound (no output and no CPU) of PUSH_STALL_SECS and a wall bound of PUSH_WALL_MIN, its group
# killed on either; a stalled or dead-transport push is infra (re-queued), never a hang.
# THE HOOK INSIDE A PUSH IS JUDGED BY THE HOOK'S BOUND (2026-11-16: movies 040934987's push lost its landing lease
# 3 min in -- a gate run had completed since the check outside the lock, so the hook could not reuse that pass and ran
# its checks again, silent, their CPU in a kept sbt server and a Docker container, out of the push's process group): while
# a pre-push hook process runs in the push's group, its leases renew at every poll and the hook's own bound (the check
# outside the lock's, LAND_STALL_MIN; movies' hook bounds each step itself) applies; the transport around it keeps
# PUSH_STALL_SECS and progress-only renewal, so a push hung on a dead tunnel still loses the lock within minutes.
PUSH_STALL_SECS = 180
PUSH_WALL_MIN = 45


def push_bounds():
    """A push's bounds: `stall` for its transport; while its pre-push hook runs, the hook's own -- the check outside the
    lock's (LAND_STALL_MIN), its leases renewed at every poll (run_watched hook_stall); `wall` over all of it."""
    return {"stall": env_num("AGENTCTL_PUSH_STALL_SECS", PUSH_STALL_SECS),
            "wall": env_num("AGENTCTL_PUSH_WALL_MIN", PUSH_WALL_MIN) * 60,
            "hook_stall": env_num("AGENTCTL_LAND_STALL_MIN", LAND_STALL_MIN) * 60}


def push_argv(remote, src, dst="main"):
    argv = ["git", "push", remote, f"{src}:refs/heads/{dst}"]
    assert not any(a in ("--no-verify", "-n", "--force", "-f") or a.startswith("--force") for a in argv)
    return argv


class Interrupted(BaseException):
    """A signal received inside exit_guard: unwinds like an exception, so every cleanup on the way runs."""


@contextlib.contextmanager
def exit_guard(signals=(signal.SIGTERM, signal.SIGHUP, signal.SIGINT)):
    """SIGTERM / SIGHUP / SIGINT raise Interrupted inside the block (main thread only; elsewhere a no-op)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def raise_it(sig, _frame):
        raise Interrupted(signal.Signals(sig).name)

    saved = {sg: signal.signal(sg, raise_it) for sg in signals}
    try:
        yield
    finally:
        for sg, h in saved.items():
            signal.signal(sg, h)


class Stash:
    """A NAMED stash (git's stash list is shared by every worktree of a clone): found again by its own sha, never by
    position, and restored exactly once."""

    def __init__(self, repo):
        self.repo, self.name, self.sha = repo, f"agentctl-land-{os.getpid()}-{int(time.time())}", None

    def save(self):
        git(self.repo, "stash", "push", "--include-untracked", "-m", self.name)
        for line in git(self.repo, "stash", "list", "--format=%H %gs").splitlines():
            sha, _, msg = line.partition(" ")
            if msg.endswith(self.name):
                self.sha = sha
                return
        raise RuntimeError(f"stash {self.name} not found after saving it")

    def restore(self):
        if not self.sha:
            return True
        r = git(self.repo, "stash", "apply", "--index", self.sha, check=False)
        if r.returncode != 0:
            r = git(self.repo, "stash", "apply", self.sha, check=False)
        if r.returncode != 0:
            err(f"could not re-apply stash {self.name} ({self.sha[:10]}): {r.stderr.strip()[:200]}; it is kept: "
                f"`git stash list | grep {self.name}`")
            return False
        for i, line in enumerate(git(self.repo, "stash", "list", "--format=%H").splitlines()):
            if line.strip() == self.sha:
                git(self.repo, "stash", "drop", f"stash@{{{i}}}", check=False)
                break
        self.sha = None
        return True


def landed(repo, sha, main_ref):
    if git_ok(repo, "merge-base", "--is-ancestor", sha, main_ref):
        return "ancestor"
    commits, left = cherry_landed(repo, main_ref, sha)
    if commits and not left:
        return "patch-equivalent"
    return None


# THE LANDING LOCK IS A LEASE (design §6, phase 1 step 3; the owner, 2026-10-28: a push hung on a dropped tunnel held
# the flock 30+ min and a checked batch waited behind it). `land/<repo>` in the store: TTL LAND_LEASE_TTL, renewed only
# on real progress (run_watched's output / CPU), capped at LAND_LEASE_MAX; a holder that stops progressing simply
# loses it, and a waiter takes it. No flock, no heartbeat file, no killing another process: expiry IS the release.
# A holder that lost the lease stops its work (run_watched ends its group: LeaseLost).
LAND_LEASE_TTL = 120
LAND_LEASE_MAX = 45 * 60  # the design's 300 s once checks no longer run in the push's hook under the lease (step 6)


def land_lease_name(repo):
    """One lease per REPOSITORY (its GitHub slug): every clone and worktree of it, on every host -- keyed by the
    local clone's path, the laptop's `land --follow` and ci-runner's pushes took different leases on one store. A
    checkout without the remote: its git common dir."""
    try:
        key = slug(repo)
    except (SystemExit, RuntimeError):
        key = os.path.realpath(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    return "land/" + re.sub(r"[^A-Za-z0-9._-]", "_", key.strip("/"))


@contextlib.contextmanager
def land_lock(repo, timeout=None):
    """Hold the repository's landing lease, waiting for it with a line saying who holds it and doing what, repeated
    every LAND_WAIT_REPORT_MIN minutes. Yields True when held, False when the wait timed out (timeout s; default
    LAND_LOCK_TIMEOUT -- the tick's pushes pass 0: busy now is tried again next tick, never a tick held for hours)."""
    h = LeaseHold(land_lease_name(repo), env_num("AGENTCTL_LAND_LEASE_TTL", LAND_LEASE_TTL),
                  env_num("AGENTCTL_LAND_LEASE_MAX", LAND_LEASE_MAX), meta={"repo": repo})
    if not h.acquire():
        def said(r):
            meta = json.loads(r.get("meta") or "{}")
            return f"{r['holder']} (pid {meta.get('pid')}), {meta.get('note') or 'working'}; expires in {int(r['expires_at'] - time.time())} s"
        err(f"waiting for the landing lease {h.name}, held by {said(h.held)}")
        deadline = time.monotonic() + (env_num("AGENTCTL_LAND_LOCK_TIMEOUT", LAND_LOCK_TIMEOUT) if timeout is None
                                       else timeout)
        every = env_num("AGENTCTL_LAND_WAIT_REPORT_MIN", LAND_WAIT_REPORT_MIN) * 60
        report_at = time.monotonic() + every
        while not h.acquire():
            if time.monotonic() > deadline:
                yield False
                return
            if time.monotonic() > report_at:
                err(f"still waiting for the landing lease: {said(h.held)}")
                report_at = time.monotonic() + every
            time.sleep(2)
        err("got the landing lease")
    _landing[0] = h
    try:
        yield True
    finally:
        _landing[0] = None
        h.release()


LAND_SUBCOMMANDS = {"status": "status", "hold": "pause", "release": "resume", "cancel": "cancel"}
WAITING_RENAMED = {"status": "land status", "pause": "land hold", "resume": "land release", "cancel": "land cancel <id>",
                   "run": "tick", "service": "tick service"}
ALIASES_UNTIL = "2026-11-05"  # the old command names (design §14.5) answer until then, each naming its new one
# TODO(2026-11-05): drop `waiting` and WAITING_RENAMED. NOT BEFORE the laptop's launchd job stops calling `waiting run
# --service` (as installed 2026-10-29): the owner reinstalls it with `agentctl tick service install` (which kickstarts
# it and needs the token handed over again), then the alias goes -- removing it first would stop the sync.


# ONE WAITING LAND, ONE OUTPUT FILE (2026-11-18): two subagents of one session each ran `land --until-done >
# $SCRATCH/land.log 2>&1` -- one scratchpad, so one file. The second's shell truncated the first's log, and the first's
# agent read the second's `NOT LANDED: the fast precheck refused <its id>` there as its own verdict (its own entry was
# still checking, and landed). land itself followed only its own entry; the file mixed the two. A land that waits for
# its verdict (--until-done, --follow) now holds an exclusive flock on its output file while it runs; a second one
# writing the same file does not start (exit LAND_SHARED_OUTPUT), in words no verdict uses.
LAND_SHARED_OUTPUT = 5


def output_file_shared(stream):
    """When `stream` is a regular file: lock it for this process (flock, released at exit). The file's path when another
    running process holds that lock; None otherwise (a pipe, a terminal, a StringIO, or now this process's own)."""
    try:
        fd = stream.fileno()
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
    except (OSError, ValueError, AttributeError):
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return None
    except BlockingIOError:
        pass
    except OSError:  # a filesystem without flock: nothing to tell
        return None
    try:
        return (fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024).rstrip(b"\0").decode() if hasattr(fcntl, "F_GETPATH")
                else os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return "its output file"


def cmd_land(a):
    if getattr(a, "lcmd", None):  # land status | hold | release | cancel <id>: the queue (design §14.5)
        if a.lcmd == "cancel" and not a.entry:
            print("usage: agentctl land cancel <id>")
            return 2
        w = argparse.Namespace(wcmd=LAND_SUBCOMMANDS[a.lcmd], repo=a.repo, entry=a.entry, renamed=True,
                               hold_for=getattr(a, "hold_for", None), hold_why=getattr(a, "hold_why", None))
        return cmd_waiting(w)
    shared = (getattr(a, "until_done", False) or getattr(a, "follow", False)) and output_file_shared(sys.stdout)
    if shared:
        err(f"land did not start: another running land writes its output file {shared}, so their verdicts would mix "
            "(2026-11-18). Run it again into a file of its own -- best, no redirect at all: the Bash tool's "
            "run_in_background output is the task's own")
        return LAND_SHARED_OUTPUT
    if os.environ.get("PREPUSH_SKIP"):
        print("NOT LANDED: PREPUSH_SKIP is set in the environment; agentctl land never skips checks. `unset PREPUSH_SKIP`.")
        return 1
    if a.repo and not os.path.isdir(a.repo):  # `--repo dashboard` (issue 20261104T154024-971c60): a verdict, never a
        # traceback -- land's --repo is the CHECKOUT to land (a short name would name a root clone, never landed from)
        print(f"NOT LANDED: --repo {a.repo} is no directory: land's --repo is the checkout (worktree) to land, a path; "
              "run land from inside your worktree, or pass its path")
        return 2
    repo = git(a.repo or os.getcwd(), "rev-parse", "--show-toplevel").strip()
    try:
        owner_only = red_owner_only(slug(repo))
    except (Exception, SystemExit):
        owner_only = False
    if owner_only and in_fixer():  # red-repos.json owner-only: a fixer's push there is a deploy, and the owner's
        print(f"NOT LANDED: {slug(repo)} is owner-only (red-repos.json): a red fixer never lands or pushes there. File "
              "your prepared branch with `~/bin/agentctl red owner-push <run> --note \"<what; which units restart>\"` and "
              "end with `BLOCKED ON OWNER: <its one-liner>`.")
        return 1
    with exit_guard(), WorktreeGuard(repo):  # EVERY EXIT PUTS THE WORKTREE BACK: its rebase, stash and lease
        try:
            return land_in_worktree(repo, a)
        except Interrupted as ex:
            print(f"NOT LANDED: interrupted by {ex}: its stash, rebase and landing lease were put back; land again",
                  flush=True)
            return 130


LAND_FETCH_TIMEOUT = 120  # s: one fetch of main proving a push (net_run tries it again through a blip)


def land_in_worktree(repo, a):
    """cmd_land's work in the worktree (stash, rebase, checks, push, watch), under its exit guards."""
    main_ref = f"refs/remotes/{remote()}/main"
    env = tool_env()
    log_path = os.path.join(scratch(), f"agentctl-land-{os.path.basename(repo)}-{os.getpid()}.log")

    def sh(*args):
        return subprocess.run(args, cwd=repo, capture_output=True, text=True, env=env)

    dirty = git(repo, "status", "--porcelain", "--untracked-files=no").strip()
    stash = None
    if dirty and not a.stash:
        print(f"NOT LANDED: the working tree has uncommitted changes ({len(dirty.splitlines())} file(s)). Commit them, or "
              "pass --stash to set them aside in a named stash and restore them afterwards.")
        return 1
    if a.branch:
        r = sh("git", "rev-parse", "--abbrev-ref", "HEAD")
        if r.stdout.strip() != a.branch:
            if dirty:
                print("NOT LANDED: --branch with a dirty tree; commit first")
                return 1
            r = sh("git", "switch", a.branch)
            if r.returncode:
                print(f"NOT LANDED: cannot switch to {a.branch}: {r.stderr.strip()}")
                return 1
    try:
        if dirty:
            stash = Stash(repo)
            stash.save()
            err(f"stashed local changes as {stash.name} ({stash.sha[:10]})")
        if getattr(a, "until_done", False):  # the whole tail: submit, wait, clean up (queueuntil.py LAND --UNTIL-DONE)
            if not queue_mode(repo, a):
                print("NOT LANDED: --until-done is the land queue's (movies); it takes no --follow / --no-watch")
                return 1
            return land_until_done(repo, main_ref, sh, a)
        if queue_mode(repo, a):  # THE LAND QUEUE: rebase, guard, QUEUED -- the checks and the push are the processor's
            return enqueue(repo, main_ref, sh, a)
        a.landed_sha, a.pushed_at, a.waiting = None, None, None
        rc = None
        for round_ in range(LAND_PRECHECK_ROUNDS + 1):
            # The long checks first, WITHOUT the lock (the last round: under it, so a lander always finishes).
            a.prechecked = None
            if round_ < LAND_PRECHECK_ROUNDS:
                rc = precheck(repo, main_ref, sh, log_path, a)
                if rc is not None:
                    break
            with land_lock(repo) as held:
                if not held:
                    print("NOT LANDED: timed out waiting for the landing lock (another land is running its checks); "
                          "land again.")
                    return 1
                rc = land_loop(repo, main_ref, sh, log_path, a)
            if rc != MAIN_MOVED:
                break
            err(f"{remote()}/main moved since the check: checking again on it, outside the lock")
    finally:
        if stash and not stash.restore():
            print(f"WARNING: local changes are in stash {stash.name}, not restored")
    if rc == 0 and a.waiting and not getattr(a, "follow", False):
        return 0  # the processor lands it; LANDED / NOT LANDED and the WATCH verdict go to `agentctl inbox`
    if rc == 0 and a.waiting:  # --follow: wait here for the batch, then watch
        rc, a.landed_sha, a.pushed_at = await_entry(repo, a.waiting["id"], close=None if getattr(a, "keep_worktree", False) else repo)
    if rc != 0 or not a.landed_sha or getattr(a, "no_watch", False):
        return rc
    if not getattr(a, "follow", False):  # land ends at LANDED; its watch item's verdict goes to the inbox
        start_watch(repo, a.landed_sha, a.pushed_at, worktree=repo,
                    close=None if getattr(a, "keep_worktree", False) else repo,
                    session=os.environ.get("CLAUDE_CODE_SESSION_ID"))
        print(f"watching {a.landed_sha[:10]}: its WATCH verdict goes to `agentctl inbox`", flush=True)
        return 0
    # the lock is free again: the next lander goes while this one watches main's CI; on WATCH OK the worktree it ran
    # from is closed when proven merged (close_worktree), unless --keep-worktree
    return watch_landed(repo, a.landed_sha, a.pushed_at, close=None if getattr(a, "keep_worktree", False) else repo,
                        fixes=getattr(a, "fixes", None) or ())


MAIN_MOVED = "main moved"  # land_loop: the check outside the lock no longer covers origin/main


def rebase_in_progress(repo):
    """The worktree's rebase-merge / rebase-apply directory when a rebase is in progress there, else None."""
    for name in ("rebase-merge", "rebase-apply"):
        p = git(repo, "rev-parse", "--path-format=absolute", "--git-path", name, check=False).stdout.strip()
        if p and os.path.isdir(p):
            return p
    return None


# EVERY NOT LANDED LEAVES NO REBASE BEHIND (issue 20261119T162652-494caa, movies-wt-owner-outside-gate 2026-11-19: a land
# ended NOT LANDED with its rebase onto origin/main in progress; later commits went onto the rebase's detached HEAD, and
# the next land's `rebase --abort` reset the branch to the pre-rebase commit, dropping them). A land that finds a rebase
# already in progress refuses -- it is the agent's to finish or abort, never land's to discard -- and a rebase land
# started is aborted on whatever path land leaves it unfinished.
def sync(repo, main_ref, sh, release_hold=False):
    """sync_onto_main, never leaving a rebase it started in progress; refused when one is in progress already."""
    pending = rebase_in_progress(repo)
    if pending:
        print(f"NOT LANDED: a rebase is in progress in this worktree ({pending}): finish it (`git rebase --continue`) "
              "or abort it (`git rebase --abort`) yourself -- commits made since it started are on its detached HEAD, "
              "and an abort resets the branch -- then land again")
        return 1, None
    with WorktreeGuard(repo):
        return sync_onto_main(repo, main_ref, sh, release_hold)


def sync_onto_main(repo, main_ref, sh, release_hold=False):
    """Fetch, rebase onto origin/main, the squash guard: (rc, None) when land is decided here, else (None, head).
    A commit with a `Hold: <why>` trailer is refused unless release_hold (`land --release-hold`: the owner's go)."""
    r = sh("git", "fetch", "--quiet", remote(), "main")
    if r.returncode:  # a tunnel restart is seconds long: one more try before giving up
        err(f"git fetch failed ({r.stderr.strip()[:120]}); retrying once in 15 s")
        time.sleep(15)
        r = sh("git", "fetch", "--quiet", remote(), "main")
    if r.returncode:
        print(f"NOT LANDED: git fetch {remote()} main failed: {r.stderr.strip()[:300]} (tunnel? `agentctl env --check`)")
        return 1, None
    head = git(repo, "rev-parse", "HEAD").strip()
    if landed(repo, head, main_ref):
        print(f"LANDED {head} (already on {remote()}/main)")
        return 0, None
    why = do_not_land(repo, waiting_commits(repo, f"{remote()}/main", head), branch_of(repo))
    if why:
        print(f"NOT LANDED: {why}. Throwaway work never lands; remove the marker (rename the branch, reword the commit, "
              "drop the Do-Not-Land: trailer) only if the owner wants it landed.")
        return 1, None
    # policy: hold-trailer
    why = None if release_hold else held_commit(repo, waiting_commits(repo, f"{remote()}/main", head))
    if why:
        print(f"NOT LANDED: {why}. Held work lands only on the owner's go: `agentctl land --release-hold` then (or "
              "drop the trailer).")
        return 1, None
    # policy: blame-dead
    why = blame_land_refusal(repo, waiting_commits(repo, f"{remote()}/main", head), repo,
                             records=blame_records_quiet())
    if why:
        print(f"NOT LANDED: {why}. A withdrawn or refuted blame revert is dead: drop this worktree "
              "(`git worktree remove`), never land it.")
        return 1, None
    # agentctl's own rewrites never sign (issue 20261115T112749-d27361): main's commits are unsigned, and a global
    # commit.gpgsign=true with an empty ssh-agent (an unattended fixer) failed every rebase behind main
    r = sh("git", *NO_SIGN, "rebase", "--quiet", f"{remote()}/main")
    for _ in range(50):  # each stop whose conflicts are docs or generated files only: resolved (conflict_resolve), and on
        if not r.returncode or not conflict_resolve(repo):
            break
        r = sh("git", *NO_SIGN, "-c", "core.editor=true", "rebase", "--continue")
    if r.returncode:
        conflicts = git(repo, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
        sh("git", "rebase", "--abort")
        if conflicts:
            print(f"NOT LANDED: rebase onto {remote()}/main conflicts in: {', '.join(conflicts)}. "
                  f"Resolve by hand (git rebase {remote()}/main), then land again.")
        else:  # no conflicted file: git itself failed (a hook, signing, a lock), never told as a conflict
            print(f"NOT LANDED: rebase onto {remote()}/main failed (no conflict): {r.stderr.strip()[:300]}")
        return 1, None
    head = git(repo, "rev-parse", "HEAD").strip()
    if landed(repo, head, main_ref):
        print(f"LANDED {head} (the rebase left nothing: already on {remote()}/main)")
        return 0, None
    head = land_before_queue(repo, head)
    if fix_replay_blocks(repo, head):
        return 1, None
    problems = check_range(repo, f"{remote()}/main", head, [], int(env_num("AGENTCTL_MAX_FOREIGN", MAX_FOREIGN)))
    if problems:
        print("NOT LANDED: prepush-check refused:\n  " + "\n  ".join(problems))
        return 1, None
    return None, head


# THE REPO'S OWN STEP AFTER THE REBASE (2026-11-20, the movies stamp carry: a proven-equivalent identity change keeps its
# stamps only if a lane remembers to write the carry rows, and 3-7 of 37 stamp-moving deploys would have): a repo's
# executable LAND_BEFORE_QUEUE runs with <origin/main> <head> on the rebased change, and may commit onto it -- the commit
# then rides the same push, judged by the same checks. It never refuses a land: a failure or a timeout is said and the
# head stays as it was.
LAND_BEFORE_QUEUE = "scripts/land-before-queue"
LAND_BEFORE_QUEUE_SECONDS = 1200


def land_before_queue(repo, head):
    """The head after the repo's LAND_BEFORE_QUEUE step: head itself, or a descendant the step committed onto it."""
    tool = os.path.join(repo, LAND_BEFORE_QUEUE)
    if not os.access(tool, os.X_OK):
        return head
    base = git(repo, "rev-parse", f"{remote()}/main").strip()
    branch = git(repo, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    try:
        r = subprocess.run([tool, base, head], cwd=repo, capture_output=True, text=True, env=tool_env(),
                           timeout=env_num("AGENTCTL_LAND_BEFORE_QUEUE_SECONDS", LAND_BEFORE_QUEUE_SECONDS))
        for line in (r.stdout + r.stderr).strip().splitlines()[-12:]:
            err(f"{LAND_BEFORE_QUEUE}: {line}")
    except subprocess.TimeoutExpired:
        err(f"{LAND_BEFORE_QUEUE}: timed out; the change lands as it was")
    now = git(repo, "rev-parse", "HEAD").strip()
    if now != head and git(repo, "merge-base", "--is-ancestor", head, now, check=False).returncode != 0:
        err(f"{LAND_BEFORE_QUEUE}: left HEAD off the change ({now[:10]}): put back on {head[:10]}")
        if branch:
            git(repo, "checkout", "-q", "--force", branch)
        git(repo, "reset", "-q", "--hard", head)
        return head
    if now != head:
        err(f"{LAND_BEFORE_QUEUE}: added {git(repo, 'rev-list', '--count', f'{head}..{now}').strip()} commit(s) to the change")
    return now


# FIX REPLAY at land (redreplay.py): on the rebased head, agentctl runs each bare `Fix-Replay: <command>` of a Fixes-Red push
# at origin/main and at head (once per base, head and command), keeps the proof for the submission (change_fields'
# `replays`), and refuses a gate red's fix it does not prove; a red outside the gate only warns.
_fix_replay_proofs = process_cache({})  # (base, head) -> [proof]


def fix_replay_proofs(repo, head):
    """agentctl's replays of the push origin/main..head (run_fix_replays), run once per base and head."""
    base = git(repo, "rev-parse", f"{remote()}/main").strip()
    if (base, head) not in _fix_replay_proofs:
        _fix_replay_proofs[(base, head)] = run_fix_replays(repo, base, head, waiting_commits(repo, base, head))
    return _fix_replay_proofs[(base, head)]


def fix_replay_blocks(repo, head):
    """True (its NOT LANDED printed) when FIX REPLAY refuses the push origin/main..head."""
    commits = waiting_commits(repo, f"{remote()}/main", head)
    verdict = fix_replay_refusal(repo, commits, proofs=fix_replay_proofs(repo, head))
    if not verdict:
        return False
    pid, why = verdict
    if pid == "fix-replay":
        blocks = policy_blocks("fix-replay", None, None, why[:160])  # policy: fix-replay
    else:
        blocks = policy_blocks("fix-replay-outside", None, None, why[:160])  # policy: fix-replay-outside
    if blocks:
        print(f"NOT LANDED: {why}. A red fix lands only once a replay proved it (or says why none can).")
    return blocks


def dry_run(repo, hook, head, log_path, attempt, mode="--range", rechecked=False, cls="check", a=None, judged=None):
    """The repo hook's dry run over origin/main..head, admitted by the governor as a check (`red` for a fix of a red
    run), watched for a stall: None when it passed, else land's rc (0: handed to the queue as infra)."""
    err(f"checking {head[:10]}: {os.path.relpath(hook, repo)} {mode} {remote()}/main..HEAD (log: {log_path})")
    lock_note(f"checks of {head[:10]}")
    try:
        with check_slot(repo, f"land {os.path.basename(repo)} {head[:10]}", cls) as slot:
            rc, out = run_watched([hook, mode, f"{remote()}/main..{head}"], repo, slot.env(tool_env()), log_path,
                                  "checks", sampler=slot.sample)
            slot.done(rc, out)
    except GovTimeout as ex:  # the machine stayed too busy for a check: a verdict, never a traceback
        print(f"NOT LANDED: {ex}; nothing of the change was judged -- land again once `agentctl gov status` has room",
              flush=True)
        return 1
    with open(log_path, "a") as f:
        f.write(f"=== check {attempt} {ts()} {head}\n{out}\n")
    if rc is None:
        return stalled("checks (scripts/hooks/pre-push --range)", out, log_path)
    if rc != 0:
        if not REDGATE.search(out) and os.environ.get("AGENTCTL_BASELINE", "1") != "0":
            main_at = git(repo, "rev-parse", f"{remote()}/main", check=False).stdout.strip()
            new, known = judge_against_baseline(repo, hook, head, out, log_path)
            if known is NETWORK_SHAPED:
                return network_requeue(repo, head, out, log_path, a)
            if known and not new:
                checks = ", ".join(sorted(known))
                print(f"KNOWN RED: {checks} fail{'s' if len(known) == 1 else ''} the same way on plain {remote()}/main "
                      f"(see `agentctl ci-why`, `Local checks`): not held against this change", flush=True)
                # the hook again, now with main's baseline written: it passes known reds (stored). MAIN MOVED UNDER THE
                # BASELINE (issue 20261107T133552-2c7673: 35 min on 875d0d6bd7 while main moved on; the hook looked up the
                # new main's baseline, which nobody had run): judged again on the main it reads, a bounded few times
                moved = rechecked and judged and main_at != judged and int(rechecked) < BASELINE_RECHECKS
                if not rechecked or moved:
                    return dry_run(repo, hook, head, log_path, f"{attempt}, against the known-red baseline", mode,
                                   rechecked=int(rechecked) + 1, cls=cls, a=a, judged=main_at)
                return refused(repo, out, log_path, f"the pre-push checks failed only where plain {remote()}/main fails "
                               f"too ({checks}; a known red, claimable as `local`), and the hook still held them "
                               "against it (no baseline mode, or no baseline for this main), so this change lands once "
                               "they are fixed on main (or with the fix)")
            if known:
                print(f"KNOWN RED (not held against this change): {', '.join(sorted(known))}; NEW with this change: "
                      f"{', '.join(sorted(new))}", flush=True)
        return refused(repo, out, log_path, "the pre-push checks failed")
    if os.path.exists(reds_dir("local.json")):  # passing checks clear main's known red
        main_ = git(repo, "rev-parse", f"{remote()}/main").strip()
        had = bool(local_red().get("checks"))
        note_local_red(main_, {}, check_passes(out))
        if had and not os.path.exists(reds_dir("local.json")):
            try:
                local_cleared(repo, main_)
            except (Exception, SystemExit) as e:
                err(f"local red cleared, but releasing it failed ({str(e)[:120]})")
    return None


BASELINE_RECHECKS = 3  # dry runs against a known-red baseline at most, main moving under each


# THE KNOWN-RED BASELINE (the owner, 2026-10-27: "pre-push fails on plain origin/main" is itself a claimed red, and
# while it is open a change is judged against main's known-failing checks). When a change's dry run fails, the same
# checks run on plain origin/main's tree: a commit with main's tree on top of the change (`git commit-tree
# main^{tree} -p <head>`), checked with `--checks-only <head>..<that>` -- the change's paths select the checks, main's
# tree is checked. Checks failing there the same way (the same normalized failure lines) are main's own red: recorded
# in reds/local.json, shown by ci-why as `Local checks`, claimable (`red claim local`) and dispatched like any red, and
# land says which failures are the change's own. The hook's own output there is kept as `reds/baseline-out/<main sha>`
# (first line `pre-push baseline <main sha>`), the file movies' pre-push reads through $PREPUSH_BASELINE_DIR (tool_env):
# land's dry run then runs the hook once more, which passes a check failing only as it fails on main (its pass stored,
# so the push reuses it). No file for main: the hook blocks every failure (fail-closed).
HOOK_STATUS = re.compile(r"pre-push (\S+) (ok|FAILED) \(\d+s\)")
# NETWORK-SHAPED CHECK FAILURES ARE INFRA, NOT A KNOWN RED (2026-10-29 ~18:50: during ci-runner packet loss #823's land
# recorded "Local checks" as a KNOWN RED -- fetch-gitops reached neither Forgejo nor GitHub, on the change and on plain
# main alike). A dry run failing with one of these signatures, and its baseline on plain main too, is no verdict on
# either: no reds/local.json, no baseline; the change is handed to the land queue as an infra re-queue (requeue_infra:
# the same reason QUEUE_INFRA_RETRIES times or INFRA_SAME_MAX opens its STALLED condition), checked again later.
NETWORK_SHAPED = "network-shaped"
NETWORK_REASON = "its checks could not reach the network"


def network_requeue(repo, head, out, log_path, a=None):
    """A dry run that failed on the network, on the change and on plain main: the change goes to the land queue as an
    infra re-queue (bounded by requeue_infra's STALLED condition). A repo without a queue: NOT LANDED, nothing recorded."""
    sig = network_failure(out)
    why = f"{NETWORK_REASON} ({sig}), on the change and on plain {remote()}/main"
    with open(log_path, "a") as f:
        f.write(f"=== infra {ts()} {head}: {why}\n")
    if not batching(repo):
        return refused(repo, out, log_path, f"the pre-push checks failed on the network, not on the change ({why}); "
                       "land again once the remote answers")
    d, _clone, _wt = waiting_state(repo)
    if host_role() == "sync":  # ONE QUEUE: ci-runner's (its checks run there, its own network)
        return submit_to_server(repo, d, change_fields(repo, f"{remote()}/main", head, a), note=f" after {why}")
    e, _ahead = queue_change(d, change_fields(repo, f"{remote()}/main", head, a))
    submit_ref(repo, d, e)
    requeue_infra(d, e, why, reason=NETWORK_REASON, detail=out, repo=repo)
    e = read_record(os.path.join(d, "entries", f"{e['id']}.json")) or e
    left = max(0, int((float(e.get("retry_after") or time.time()) - time.time()) // 60))
    print(f"QUEUED {e['id']} ({head[:10]}): infra, not a known red -- {why}; checked again in {left} min; "
          f"{queued_tail()}", flush=True)
    spawn_waiting_processor(repo)
    return 0


def check_failures(out):
    """{failed check: {normalized failure lines}} of a hook run's output."""
    fails, seg = {}, []
    for raw in (out or "").splitlines():
        line = ANSI.sub("", raw)
        m = HOOK_STATUS.search(line)
        if m:
            if m.group(2) == "FAILED":
                fails[m.group(1)] = {norm_line(l) for l in seg if STRONG.search(l)} or {"(no failure line)"}
            seg = []
            continue
        seg.append(line)
    return fails


def check_passes(out):
    return {m.group(1) for m in (HOOK_STATUS.search(ANSI.sub("", l)) for l in (out or "").splitlines())
            if m and m.group(2) == "ok"}


def local_red():
    return read_record(reds_dir("local.json")) or {}


def baseline_failures(repo, hook, head, log_path):
    """The change's checks run on plain origin/main's tree: {check: lines} failing there (cached per main tree and
    change paths), or None when it could not be run."""
    import hashlib
    main = git(repo, "rev-parse", f"{remote()}/main").strip()
    paths = git(repo, "diff", "--name-only", f"{remote()}/main...{head}").split()
    key = hashlib.sha1("\n".join([git(repo, "rev-parse", f"{main}^{{tree}}").strip()] + sorted(paths)).encode()).hexdigest()[:16]
    cache = reds_dir("baseline", f"{key}.json")
    rec = read_record(cache)
    if rec is not None:
        if rec.get("main") and rec["main"] != main:  # another main with the same tree (main moved by a commit that left
            # it alone): the hook looks this main's baseline up by its sha, so it gets the same output (2c7673)
            try:
                with open(reds_dir("baseline-out", rec["main"])) as f:
                    old = f.read().split("\n", 1)[1]
                if not os.path.exists(reds_dir("baseline-out", main)):
                    write_baseline_out(main, old)
            except (OSError, IndexError):
                pass
        return {k: set(v) for k, v in rec["fails"].items()}
    synth = git(repo, "commit-tree", f"{main}^{{tree}}", "-p", head, "-m", f"agentctl baseline: {remote()}/main {main[:10]}",
                check=False)
    if synth.returncode != 0:
        return None
    synth = synth.stdout.strip()
    _d, clone, _w = waiting_state(repo)
    tree = f"{clone}-wt-baseline-{os.getpid()}"
    if git(repo, "worktree", "add", "-q", "--detach", mark_internal(tree), synth, check=False).returncode != 0:
        return None
    try:
        err(f"the change's checks failed: running them on plain {remote()}/main {main[:10]} (the known-red baseline)")
        with check_slot(repo, f"baseline {main[:10]}") as slot:
            rc, out = run_watched([os.path.join(tree, os.path.relpath(hook, repo)), "--checks-only", f"{head}..{synth}"],
                                  tree, {**slot.env(tool_env()), "PREPUSH_BASELINE_DIR": ""}, log_path, "baseline",
                                  sampler=slot.sample)
            slot.done(rc, out)
    finally:
        git(repo, "worktree", "remove", "--force", tree, check=False)
    if rc is None:
        return None
    if rc and network_failure(out):  # the network's failure, not main's: no baseline, no cache, no reds/local.json
        err(f"the baseline on plain {remote()}/main {main[:10]} could not reach the network ({network_failure(out)})")
        return NETWORK_SHAPED
    write_baseline_out(main, out)
    fails = check_failures(out) if rc else {}
    write_json(cache, {"main": main, "at": ts(), "fails": {k: sorted(v) for k, v in fails.items()},
                       "passes": sorted(check_passes(out))})
    note_local_red(main, fails, check_passes(out))
    return fails


def write_baseline_out(main, out):
    """reds/baseline-out/<main>: the hook's output on plain main (appended to a run for other paths on the same main),
    for the hook's known-red baseline; files older than 3 days go."""
    d = reds_dir("baseline-out")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, main)
    try:
        with open(path) as f:
            old = f.read()
    except OSError:
        old = ""
    if not old.startswith(f"pre-push baseline {main}\n"):
        old = f"pre-push baseline {main}\n"
    tmp = f"{path}.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(old + (out or "") + ("" if (out or "").endswith("\n") else "\n"))
    os.replace(tmp, path)
    for name in os.listdir(d):
        p = os.path.join(d, name)
        try:
            if time.time() - os.path.getmtime(p) > 3 * 86400:
                os.unlink(p)
        except OSError:
            pass


@contextlib.contextmanager
def local_lock():
    """One writer of reds/local.json at a time."""
    os.makedirs(reds_dir(), exist_ok=True)
    with open(reds_dir("local.lock"), "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def note_local_red(main, fails, passes):
    """Keep reds/local.json: the checks failing on plain origin/main (added), those seen passing (cleared)."""
    with local_lock():
        rec = local_red()
        checks = rec.get("checks") or {}
        for c in passes:
            checks.pop(c, None)
        for c, lines in fails.items():
            old = checks.get(c) or {"since": ts(), "first_main": main}
            checks[c] = {**old, "lines": sorted(lines)[:20], "main": main}
        rec.update(checks=checks, main=main, at=ts())
        if checks:
            write_json(reds_dir("local.json"), rec)
        elif os.path.exists(reds_dir("local.json")):
            os.unlink(reds_dir("local.json"))


# A STALE LOCAL RED (2026-10-27: `Local checks: RED on plain origin/main ac65cbbd7e` outlived the commit that fixed it).
# reds/local.json is keyed to the main sha it was computed on. Once origin/main moves past it the record is stale: shown as
# `stale, rechecking` (never RED), and the failing checks are run again on the new main's tree -- in the background by the
# processor's patrol (the governor's check class), synchronously by `red patrol`, or by the next land's dry run. A pass
# clears the red, releases its claims, stops its dispatched fixer (process groups) and puts FIX OK in the inbox; the
# fixer's worktrees whose commits are patch-equivalent on main are removed, any other is listed in the inbox.
def recheck_local(repo, wait=True):
    """Recompute a stale local red on the current origin/main. Returns action lines (when it ran synchronously)."""
    main = local_stale(repo)
    if not main:
        return release_local_leftovers(repo)
    if not wait:
        import threading
        t = _bounded.get("local-recheck")
        if t and t.is_alive():
            return []
        t = threading.Thread(target=lambda: _recheck_guarded(repo, main), name="local-recheck", daemon=True)
        _bounded["local-recheck"] = t
        t.start()
        return []
    return _recheck_guarded(repo, main)


def _recheck_guarded(repo, main):
    """One recheck at a time across processes (the processor's and a `red patrol`): the second one leaves it."""
    os.makedirs(reds_dir("local"), exist_ok=True)
    try:
        with open(reds_dir("local", "recheck.lock"), "a+") as lf:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return []
            return run_local_recheck(repo, main)
    except (Exception, SystemExit) as e:
        err(f"local recheck: skipped ({str(e).replace('agentctl: ', '')[:200]})")
        return []


def run_local_recheck(repo, main):
    """Run the recorded failing checks on main's tree (the hook over <recorded main>..main, every other check skipped)."""
    failing = sorted(local_red().get("checks") or {})
    hook = precheck_hook(repo)
    if not hook:
        return []
    # The base is the main the red FIRST appeared on (first_main; the record's `main` moves with every recheck): the checks judge what changed since (shellcheck fails only on warnings
    # a change ADDS, so against an empty base the whole tree would count). Unknown to this clone: the empty tree.
    firsts = [str(c.get("first_main") or "") for c in (local_red().get("checks") or {}).values()]
    old_main = next((f for f in firsts if f and git_ok(repo, "cat-file", "-e", f"{f}^{{commit}}")), "")
    if old_main and git_ok(repo, "cat-file", "-e", f"{old_main}^{{commit}}"):
        base = old_main
    else:
        empty = git(repo, "hash-object", "-t", "tree", "-w", "--stdin", input="").strip()
        base = git(repo, "commit-tree", empty, "-m", "agentctl recheck base").strip()
    synth = git(repo, "commit-tree", f"{main}^{{tree}}", "-p", base, "-m", f"agentctl recheck: {remote()}/main {main[:10]}").strip()
    _d, clone, _w = waiting_state(repo)
    for w in worktrees(repo)[1:]:  # a killed recheck's tree (its pid is gone)
        m = re.fullmatch(re.escape(clone) + r"-wt-recheck-(\d+)", w["worktree"])
        if m and not pid_alive(int(m.group(1))):
            git(repo, "worktree", "remove", "--force", w["worktree"], check=False)
    tree = f"{clone}-wt-recheck-{os.getpid()}"
    if git(repo, "worktree", "add", "-q", "--detach", mark_internal(tree), synth, check=False).returncode != 0:
        return []
    log_path = reds_dir("local", "recheck.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    tree_hook = os.path.join(tree, os.path.relpath(hook, repo))
    try:
        err(f"local checks were recorded on an older main: rechecking {', '.join(failing)} on {remote()}/main {main[:10]}")
        with check_slot(repo, f"local recheck {main[:10]}", "check") as slot:
            env = {**slot.env(tool_env()), "PREPUSH_BASELINE_DIR": "",
                   "PREPUSH_SKIP": " ".join(c for c in hook_checks(tree_hook) if c not in failing)}
            rc, out = run_watched([tree_hook, "--checks-only", f"{base}..{synth}"],
                                  tree, env, log_path, "recheck", sampler=slot.sample)
            slot.done(rc, out)
    finally:
        git(repo, "worktree", "remove", "--force", tree, check=False)
        git(repo, "gc", "--auto", "--quiet", check=False)
    if rc is None:
        return []  # stalled: stays stale, rechecked next time
    return apply_local_recheck(repo, main, rc, out)


def apply_local_recheck(repo, main, rc, out):
    """Record a recheck's outcome (also what land's passing dry run does): still failing -> re-keyed to main; passed ->
    cleared and released."""
    failing = dict(local_red().get("checks") or {})
    passes = check_passes(out)
    fails = check_failures(out) if rc else {}
    for c, v in failing.items():  # neither seen passing nor failing (not run): still the old failure, on the new main
        if c not in passes and c not in fails:
            fails[c] = set(v.get("lines") or [])
    write_baseline_out(main, out)
    note_local_red(main, fails, passes)
    if os.path.exists(reds_dir("local.json")):
        return [f"local checks still fail on {remote()}/main {main[:10]}: {', '.join(sorted(fails))}"]
    return local_cleared(repo, main)


def local_cleared(repo, main):
    """The local red is gone (its red item clears at the next tick, its holder told to stop): FIX OK to the inbox, the
    fixer's worktrees tidied."""
    line = f"FIX OK: Local checks pass on {remote()}/main {main[:10]}; the red is cleared"
    inbox_write("red", line, [line], [], 0)
    return [line] + release_local_leftovers(repo)


def release_local_leftovers(repo):
    """With no local red: each ended local fixer's worktree is removed when its commits are patch-equivalent on main
    (git cherry); anything else is listed in the inbox, never deleted. Once per fixer record."""
    if local_red().get("checks"):
        return []
    acts = []
    for r in fixer_records():
        if r.get("workflow") != LOCAL_FLOW or r["alive"] or r.get("tidied"):
            continue
        wt = r.get("worktree")
        if not wt or not os.path.isdir(wt):
            msg = None
        else:
            msg = close_worktree(wt)
            if not msg.startswith("closed"):
                line = f"RED LEFTOVER: the local fixer's {wt.replace(HOME, '~')} was kept ({msg.split(': ', 1)[-1]}); not patch-equivalent on main, review it"
                inbox_write("red", line, [line], [], 1)
                acts.append(line)
                msg = None
        if msg:
            acts.append(f"removed the local fixer's worktree: {msg}")
        rec = read_record(r["path"]) or {}
        rec["tidied"] = ts()
        write_json(r["path"], rec)
    return acts


def judge_against_baseline(repo, hook, head, out, log_path):
    """(new failures, known): the change's failures that plain origin/main does not share, and those it does.
    known is NETWORK_SHAPED when both runs failed with a network signature (infra: no verdict on either)."""
    mine = check_failures(out)
    if not mine:
        return None, {}
    base = baseline_failures(repo, hook, head, log_path)
    if base is NETWORK_SHAPED:
        return None, (NETWORK_SHAPED if network_failure(out) else {})
    if base is None:
        return None, {}
    new = {c: l for c, l in mine.items() if c not in base or not (l <= base[c])}
    return new, {c: l for c, l in mine.items() if c not in new}


def current_main(repo):
    r = git(repo, "rev-parse", "--verify", "-q", f"{remote()}/main", check=False) if repo else None
    return r.stdout.strip() if r is not None and r.returncode == 0 else None


def local_stale(repo):
    """origin/main's sha when the recorded local red was computed on an older one (the record is stale), else None."""
    rec = local_red()
    if not rec.get("checks"):
        return None
    cur = current_main(repo)
    return cur if cur and cur != rec.get("main") else None


def local_units(repo=None):
    """main's own local-checks red (reds/local.json) as red units: one per failing check, run `local`. A record
    computed on an older main is stale: no units (shown as `stale, rechecking`, never RED)."""
    if repo and local_stale(repo):
        return []
    rec = local_red()
    units = []
    for check, c in sorted((rec.get("checks") or {}).items()):
        line = (c.get("lines") or [""])[0]
        s = {"job": check, "kind": "test", "line": line, "sig": signature(LOCAL_FLOW, check, line)}
        run = {"number": "local", "databaseId": f"local-{str(c.get('main'))[:10]}", "headSha": c.get("main"),
               "status": "completed", "conclusion": "failure"}
        units += red_units("?", LOCAL_FLOW, run, [], [s], True)
    return units


def precheck(repo, main_ref, sh, log_path, a):
    """Before the lock: sync and the hook's dry run (the long part). None when land goes on to the lock (with
    a.prechecked = (head, base) when the dry run passed there), else land's rc."""
    rc, head = sync(repo, main_ref, sh, getattr(a, "release_hold", False))
    if rc is not None:
        return rc
    hook = precheck_hook(repo)
    if not hook:
        return None
    # The change's OWN checks first, red main or green (the owner, 2026-10-27: "once the checks are done but build is
    # red, merge to waiting"): main red and the change one that must wait, without the gate (--checks-only), since it
    # will join `waiting` and land with the batch at green.
    reds = gate_reds(repo) if waiting_supported(repo) else set()
    commits = waiting_commits(repo, f"{remote()}/main", head)
    joining = bool(reds) and must_wait(repo, commits, reds)
    rc = dry_run(repo, hook, head, log_path, "before the lock", mode="--checks-only" if joining else "--range",
                 cls=gov_class_of(repo, commits, reds), a=a)
    if rc is not None:
        return rc
    a.prechecked = (head, git(repo, "rev-parse", main_ref).strip())
    return None


def proven_push(repo, head, main_ref, log_path, a):
    """A push that exited 0, proven on main by a fetch (net_run: through a blip): LANDED (0), NOT LANDED (1) -- or,
    the fetch still failing the network's way, UNKNOWN (2): the network's, never NOT LANDED for a change on main."""
    f = net_run("git fetch main", ["git", "fetch", "--quiet", remote(), "main"], cwd=repo, capture_output=True,
                text=True, env=tool_env(), timeout=LAND_FETCH_TIMEOUT)
    if f.returncode != 0 and is_transient_network(f"{f.stderr}\n{f.stdout}"):
        print(f"UNKNOWN: {head[:10]} was pushed (the push exited 0) but {remote()}/main could not be fetched to prove "
              f"it -- the network's ({(f.stderr or f.stdout).strip()[-160:]}), not the change's; `git fetch "
              f"{remote()} main` later shows it; see {log_path}", flush=True)
        return 2
    how = landed(repo, head, main_ref)
    if how:
        print(f"LANDED {head}" + ("" if how == "ancestor" else f" ({how})"), flush=True)
        a.landed_sha = head
        lander_note_direct(repo, head, getattr(a, "lander_keys", None))
        return 0
    print(f"NOT LANDED: the push exited 0 but {head[:10]} is not on {remote()}/main after a fetch; see {log_path}")
    return 1


def land_loop(repo, main_ref, sh, log_path, a):
    for attempt in range(LAND_RETRIES + 1):
        lock_note("fetch and rebase")
        rc, head = sync(repo, main_ref, sh, getattr(a, "release_hold", False))
        if rc is not None:
            return rc
        reds = gate_reds(repo) if waiting_supported(repo) else set()
        if reds and must_wait(repo, waiting_commits(repo, f"{remote()}/main", head), reds):
            # checked already (before the lock, without the gate): only the cherry-pick onto `waiting` here
            return join(repo, main_ref, sh, log_path, a, head=head)
        hook = precheck_hook(repo)
        prechecked = getattr(a, "prechecked", None)
        if hook and prechecked and prechecked != (head, git(repo, "rev-parse", main_ref).strip()):
            # main moved since the check outside the lock: give the lock back and check again outside it
            a.prechecked = None
            return MAIN_MOVED
        if hook and not prechecked:
            # CHECK FIRST: the hook's dry run over exactly what the push will carry (its pass is stored, so the push's own
            # hook run reuses it in seconds). A long hook inside `git push` holds the push's ssh connection idle, and a
            # tunnel drop kills it after "all clear" (2026-10-23); the area tests make long hooks the norm.
            rc = dry_run(repo, hook, head, log_path, attempt + 1,
                         cls=gov_class_of(repo, waiting_commits(repo, f"{remote()}/main", head), reds), a=a)
            if rc is not None:
                return rc
        a.prechecked = None  # a retry after a push race checks its new base again
        try:  # the runs the pushed commits claim to fix: the watch verifies them
            a.fixes = sorted(fixes_of(repo, waiting_commits(repo, f"{remote()}/main", head)))
        except Exception:
            a.fixes = []
        try:  # THE LANDER (lander.py): what this push carries, keyed so the commits are found on main again
            a.lander_keys = commit_keys(repo, f"{remote()}/main", head)
        except Exception:
            a.lander_keys = []
        err(f"pushing {head[:10]} to {remote()}/main (attempt {attempt + 1}/{LAND_RETRIES + 1}; hook log: {log_path})")
        lock_note(f"push of {head[:10]}")
        a.pushed_at = time.time()
        rc, out = run_watched(push_argv(remote(), head), repo, tool_env(), log_path, "push", **push_bounds())
        if rc == 0:
            mark_gate_dirty()
        with open(log_path, "a") as f:
            f.write(f"=== attempt {attempt + 1} {ts()} {head}\n{out}\n")
        if rc is None:
            return stalled("push (git push and its pre-push hook)", out, log_path)
        if rc == 0:
            return proven_push(repo, head, main_ref, log_path, a)
        if RACE.search(out) and attempt < LAND_RETRIES:
            err(f"push race ({RACE.search(out).group(0)}): refetch, rebase, retry")
            time.sleep(min(30, 2 + 3 * attempt))
            continue
        if RACE.search(out) and not REDGATE.search(out):
            print(f"NOT LANDED: still racing after {LAND_RETRIES} retries ({RACE.search(out).group(0)}); try again shortly.")
            return 1
        remote_fault = TRANSIENT_PUSH.search(out or "")
        if remote_fault and attempt < LAND_RETRIES:  # the remote's (or the network's) fault: retried, never the change's
            err(f"push failed remote-side ({remote_fault.group(0)}): retry")
            time.sleep(min(60, 5 + 10 * attempt))
            continue
        return refused(repo, out, log_path, f"the push failed remote-side ({remote_fault.group(0)}), {LAND_RETRIES} "
                       "times: the forge's fault, not the change's -- land again later" if remote_fault else
                       "the push failed (pre-push hook or remote)")
    return 1


def refused(repo, out, log_path, what):
    """Print the NOT LANDED line for a hook or push that said no: the red gate explained by ci-why, a tunnel hinted."""
    tail = "\n  ".join([l for l in out.splitlines() if l.strip()][-25:])
    if REDGATE.search(out):
        print(f"NOT LANDED: the red gate refused the push (main is red).\n  {tail}")
        try:
            print(ci_why(slug(repo), 4, repo=repo)[1])
        except Exception as e:
            print(f"(agentctl ci-why failed: {e})")
        return 1
    hint = ""
    if TUNNEL_TROUBLE.search(out):
        hint = ("\n  HINT: the hook could not reach the remote (network?): `~/bin/agentctl env --check`, "
                "`gh auth status`, then land again.")
    print(f"NOT LANDED: {what}. Last lines (full log {log_path}):\n  {tail}{hint}")
    return 1

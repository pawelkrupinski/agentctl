# agentctl, part `owned`: owned long-running modes.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- owned: every long-running mode
# The owner (2026-10-26), after a day of strays: a zombie processor blocking the next one, duplicate waiters, a processor
# still running old code after a fix, orphaned shells, abandoned CI runs, landing slots held by dead loops. So every
# long-running mode -- `wait`, land's watch and its wait on `waiting`, the `waiting` processor -- runs OWNED:
#   * one flock per (kind, key) under ~/.cache/agentctl/owners/ ($AGENTCTL_OWNERS_DIR): the kernel frees it when the
#     process dies, however it dies, so a second instance can never run beside a live one and never waits on a dead one;
#   * beside it a record (<kind>-<key>.json): owner (the Claude session, else user@cwd), pid, the process's start time,
#     argv, cwd, the code version (agentctl's sha256) and the CI runs it started;
#   * the runs it started are cancelled when it ends -- by its SIGTERM/SIGHUP/SIGINT handler, or, after a SIGKILL, by
#     the next owner of the same key, which finds the dead one's record;
#   * at each poll it compares agentctl's file with the version it started on and, when a fix has landed, re-execs
#     itself on the new code with the same pid (its runs adopted, not cancelled);
#   * `agentctl orphans` reads the records: a dead process's record, an owner on old code, an owner whose cwd is gone.
OWNERS_RESTART_CHECK = 30  # seconds between code-version checks ($AGENTCTL_RESTART_CHECK)
TOOL_PATH = os.path.realpath(__file__)
if TOOL_PATH.endswith(os.path.join("lib", "agentctl", "__init__.py")):  # imported as a package: the entry beside lib/
    TOOL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(TOOL_PATH))), "agentctl")


def owners_dir():
    return os.environ.get("AGENTCTL_OWNERS_DIR") or state_root("owners")


def code_files(path=TOOL_PATH):
    """agentctl's code: the entry script and every part of its package beside it (lib/agentctl/*.py), as
    (relative path, absolute path), in a fixed order."""
    root = os.path.dirname(path)
    lib = os.path.join(root, "lib", "agentctl")
    parts = sorted(f for f in os.listdir(lib) if f.endswith(".py")) if os.path.isdir(lib) else []
    return [(os.path.basename(path), path)] + [(f"lib/agentctl/{f}", os.path.join(lib, f)) for f in parts]


def code_version(path=TOOL_PATH):
    """A hash of ALL of agentctl's code (code_files): a change to any part is new code."""
    import hashlib
    h = hashlib.sha256()
    try:
        for rel, p in code_files(path):
            with open(p, "rb") as f:
                h.update(rel.encode() + b"\0" + f.read() + b"\0")
    except OSError:
        return ""
    return h.hexdigest()[:16]


START_CODE = code_version()


def process_start(pid):
    """The process's start time as ps prints it: with the pid, it names one process even after the pid is reused."""
    # LC_ALL=C: one format whoever asks (2026-10-27: launchd's processor wrote "Tue Oct 27 …", a session's en_GB ps read
    # "Tue 27 Oct …", and a live red fixer looked dead)
    r = run_cmd(["ps", "-o", "lstart=", "-p", str(pid)], env=dict(os.environ, LC_ALL="C"))
    return r.stdout.strip()


def read_record(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def cancel_run(o_r, rid, why):
    """Cancel a CI run this tool started (a cancel is a no-op on a finished run, on either forge)."""
    try:
        r = subprocess.run(forge_argv(o_r, "run", "cancel", str(rid)), capture_output=True, text=True,
                           env=forge_env(o_r), timeout=120)
        err(f"cancelled {o_r} run {rid} ({why})" if r.returncode == 0 else
            f"could not cancel {o_r} run {rid} ({why}): {r.stderr.strip()[:160]}")
    except (Exception, SystemExit) as e:
        err(f"could not cancel {o_r} run {rid} ({why}): {e}")


def lock_held(lockpath):
    """Whether a live process holds the flock at lockpath (tried without blocking, released at once)."""
    if not os.path.exists(lockpath):
        return False
    with open(lockpath, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


class Held(Exception):
    def __init__(self, holder):
        super().__init__(f"held by pid {holder.get('pid')} ({holder.get('owner')}) since {holder.get('started')}")
        self.holder = holder


class Owned:
    """One long-running instance of (kind, key): see the section comment."""

    def __init__(self, kind, key, argv=None):
        self.kind, self.key = kind, re.sub(r"[^\w.-]", "_", str(key))[-150:]
        base = os.path.join(owners_dir(), f"{kind}-{self.key}")
        self.lockpath, self.record = base + ".lock", base + ".json"
        self.argv = list(sys.argv[1:] if argv is None else argv)
        self.f, self.holder, self.runs, self.started, self._old = None, None, [], None, {}

    def acquire(self):
        os.makedirs(owners_dir(), exist_ok=True)
        f = open(self.lockpath, "a+")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            self.holder = read_record(self.record) or {}
            return False
        self.f = f
        prev = read_record(self.record)
        if prev and prev.get("pid") == os.getpid():  # our own re-exec on new code: its runs are still ours
            self.runs, self.started = [tuple(r) for r in prev.get("runs") or []], prev.get("started")
        elif prev:
            for o_r, rid in prev.get("runs") or []:
                cancel_run(o_r, rid, f"its owner, {prev.get('kind')} pid {prev.get('pid')}, died without ending it")
        self.started = self.started or ts()
        self._write()
        self._signals()
        return True

    def _write(self):
        rec = {"kind": self.kind, "key": self.key, "pid": os.getpid(), "started": self.started,
               "process_start": process_start(os.getpid()),
               "owner": os.environ.get("CLAUDE_CODE_SESSION_ID") or f"{os.environ.get('USER', '?')}@{os.getcwd()}",
               "cwd": os.getcwd(), "argv": self.argv, "code": code_version(), "runs": [list(r) for r in self.runs]}
        tmp = f"{self.record}.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(rec, f)
        os.replace(tmp, self.record)

    def _signals(self):
        def end(signum, frame):
            raise Terminated(128 + signum)
        for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            try:
                self._old[s] = signal.signal(s, end)
            except ValueError:  # not the main thread: the caller's own handling stands
                pass

    def add_run(self, o_r, rid):
        self.runs.append((o_r, int(rid)))
        self._write()

    def drop_run(self, rid):
        self.runs = [r for r in self.runs if r[1] != int(rid)]
        self._write()

    def release(self, cancel=True, keep_record=False):
        if not self.f:
            return
        if cancel:
            for o_r, rid in self.runs:
                cancel_run(o_r, rid, f"its {self.kind} ended")
            self.runs = []
        if not keep_record:
            try:
                os.unlink(self.record)
            except FileNotFoundError:
                pass
        for s, h in self._old.items():
            signal.signal(s, h)
        self._old = {}
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()
        self.f = None

    def __enter__(self):
        if not self.acquire():
            raise Held(self.holder)
        return self

    def __exit__(self, *exc):
        self.release()
        return False


_last_code_check = [0.0]


_restart_refused = [None]
_smoked = process_cache({})


def smoke_check(path):
    """(ok, why): the code compiles (the entry and its package's parts) and `--selftest` (import, parser, fixture
    reads) passes, in a process of its own."""
    r = subprocess.run([sys.executable, "-m", "py_compile"] + [p for _r, p in code_files(path)], capture_output=True,
                       text=True, timeout=60)
    if r.returncode:
        return False, "py_compile: " + (r.stderr.strip().splitlines() or ["failed"])[-1][:160]
    try:
        r = subprocess.run([sys.executable, path, "--selftest"], capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, "--selftest timed out"
    if r.returncode:
        return False, "--selftest: " + ((r.stdout + r.stderr).strip().splitlines() or ["failed"])[-1][:160]
    return True, ""


def restart_gate():
    """(ok, why) for a re-exec onto the code on disk: only COMMITTED code (the file equals ~/bin's HEAD version) that
    passes the smoke check. A dirty working-tree edit is never picked up; a commit that fails the smoke check is refused
    once, with one inbox entry naming it. Not a git checkout: allowed (nothing to judge by)."""
    d = os.path.dirname(TOOL_PATH)
    head = git(d, "rev-parse", "--verify", "-q", "HEAD", check=False)
    if head.returncode:
        return True, ""
    head = head.stdout.strip()
    shown = {}
    for rel, p in code_files(TOOL_PATH):  # every part, each equal to HEAD's: a dirty or untracked part is no code
        r = run_cmd(["git", "-C", d, "show", f"HEAD:{rel}"], text=False)
        try:
            with open(p, "rb") as f:
                same = r.returncode == 0 and f.read() == r.stdout
        except OSError:
            same = False
        if not same:
            return False, f"the working-tree {rel} is not ~/bin's HEAD {head[:9]} (uncommitted edit)"
        shown[rel] = r.stdout
    if head not in _smoked:
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            for rel, data in shown.items():  # the whole code, as committed, in a tree of its own
                os.makedirs(os.path.dirname(os.path.join(t, rel)), exist_ok=True)
                with open(os.path.join(t, rel), "wb") as f:
                    f.write(data)
            _smoked[head] = smoke_check(os.path.join(t, "agentctl"))
        if not _smoked[head][0]:
            line = f"CODE REFUSED: agentctl commit {head[:9]} fails its smoke check ({_smoked[head][1]}); services keep running the old code"
            inbox_write("code", line, [line], [], 1)
    ok, why = _smoked[head]
    return ok, ("" if ok else f"commit {head[:9]} fails its smoke check ({why})")


def selftest():
    """Fast smoke check of the code a service would re-exec onto: the parser builds, the failure parsers and the
    snapshot readers work on fixtures, a fixture gate reads green."""
    snap = {"at": time.time(), "repo": "x/y", "outside_error": None, "outside": [],
            "gate": [{"name": "Main", "decided": {"databaseId": 1, "number": 1, "status": "completed",
                                                  "conclusion": "success"}, "newer": None, "since": None, "count": 0,
                      "runs": []}]}
    assert gate_from_snap(snap)[0][0] == "Main" and outside_from_snap(snap) == []
    assert set(check_failures("! pre-push sbt FAILED (3s)\n")) == {"sbt"}
    assert red_for("2026-01-01T00:00:00Z", dt.datetime(2026, 1, 1, 1, 5, tzinfo=dt.timezone.utc)) == "1h 5m"
    assert callable(recheck_local) and callable(restart_gate)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            main(["ci-why", "--help"])
    except SystemExit as e:
        assert e.code == 0
    print("selftest ok")
    return 0


def restart_if_changed(owned=(), argv=None):
    """Re-exec this process on agentctl's new code (same pid; argv: given, else the first owner's, else this process's)
    once the file on disk changed.
    The owned locks are released without cancelling: the new process takes them back and adopts their runs."""
    every = env_num("AGENTCTL_RESTART_CHECK", OWNERS_RESTART_CHECK)
    if time.time() - _last_code_check[0] < every:
        return
    _last_code_check[0] = time.time()
    now_code = code_version()
    if not now_code or now_code == START_CODE:
        return
    ok, why = restart_gate()
    if not ok:  # a dirty edit, or a commit that fails the smoke check: the old code keeps running
        if why and why != _restart_refused[0]:
            _restart_refused[0] = why
            err(f"agentctl changed on disk but not restarted: {why}")
        return
    err(f"agentctl changed on disk ({START_CODE[:8]} -> {now_code[:8]}): restarting on the new code, pid {os.getpid()}")
    for o in owned:
        o.release(cancel=False, keep_record=True)
    sys.stdout.flush()
    sys.stderr.flush()
    # The owner's own command (land's watch resumes as `agentctl watch`, not as `land`), else this process's.
    if argv is None:
        argv = owned[0].argv if owned else sys.argv[1:]
    os.execv(sys.executable, [sys.executable, TOOL_PATH] + list(argv))


def owner_records():
    """Every owner record, each with `alive` (its lock is held) and `path`."""
    out = []
    for p in sorted(glob.glob(os.path.join(owners_dir(), "*.json"))):
        rec = read_record(p)
        if rec is None:
            continue
        rec.update(path=p, alive=lock_held(p[: -len(".json")] + ".lock"))
        out.append(rec)
    return out


def default_repo():
    try:
        return slug(os.getcwd())
    except (SystemExit, RuntimeError):
        return SHORT["movies"]


def backoff(floor, cap):
    """Poll delays: floor, x1.5 each time, never above cap (<= 300 s)."""
    d = floor
    while True:
        yield d
        d = min(cap, d * 1.5)


class Waiter:
    """One target waited on: OWNS it (and polls) or attaches to the live owner's verdict file. The verdict sits beside the
    owner record, so a waiter in any session finds it."""

    def __init__(self, key, check, argv=None):
        self.key, self.check = key, check
        self.own = Owned("wait", key, argv)
        self.verdict = self.own.record[: -len(".json")] + ".verdict"
        self.owned, self.holder, self.result = False, None, None

    def acquire(self):
        """True if we own the target now; False if a live waiter does (self.holder = its record)."""
        if self.own.acquire():
            self.owned = True
            try:
                os.unlink(self.verdict)
            except FileNotFoundError:
                pass
            return True
        self.holder = self.own.holder
        return False

    def poll_owned(self):
        try:
            done, ok, text = self.check()
        except Exception as e:  # transient API trouble: report and keep waiting
            done, ok, text = False, None, f"check failed: {e}"
        if done:
            tmp = self.verdict + f".{os.getpid()}"
            with open(tmp, "w") as f:
                json.dump({"ok": ok, "line": text, "at": ts()}, f)
            os.replace(tmp, self.verdict)
            self.result = (ok, text)
        return text

    def poll_attached(self):
        """Owner gone (its lock free): read its verdict, or take over when it left none."""
        if lock_held(self.own.lockpath):
            return
        try:
            v = json.load(open(self.verdict))
            self.result = (v["ok"], v["line"])
        except (OSError, ValueError, KeyError):
            err(f"{self.key}: waiter pid {(self.holder or {}).get('pid')} ended without a verdict; taking over")
            self.holder = None
            self.acquire()

    def release(self):
        if self.owned:
            self.own.release()
            self.owned = False


def wait_all(waiters, timeout, floor, cap):
    """Block until every waiter has a verdict. Prints each verdict as it lands. Exit 0 all ok, 1 any failed, 2 timeout."""
    deadline = time.time() + timeout
    attach_poll = env_num("AGENTCTL_ATTACH_POLL", 15)
    for w in waiters:
        if not w.acquire():
            err(f"{w.key}: attached to existing waiter pid {w.holder.get('pid')} (since {w.holder.get('started')}); not polling GitHub")
    delays = backoff(floor, cap)
    next_check, last = 0.0, {}
    try:
        while True:
            pending = [w for w in waiters if w.result is None]
            if time.time() >= next_check:
                for w in pending:
                    if w.owned:
                        last[w.key] = w.poll_owned()
                        if w.result:
                            print(w.result[1], flush=True)
                next_check = time.time() + next(delays)
            for w in pending:
                if w.result is None and not w.owned:
                    w.poll_attached()
                    if w.result:
                        print(w.result[1], flush=True)
            pending = [w for w in waiters if w.result is None]
            if not pending:
                return 0 if all(w.result[0] for w in waiters) else 1
            restart_if_changed([w.own for w in waiters if w.owned])
            if time.time() >= deadline:
                for w in pending:
                    print(f"TIMEOUT after {timeout}s; last: {last.get(w.key, 'attached to another waiter')}", flush=True)
                return 2
            step = next_check - time.time()
            if any(not w.owned for w in pending):
                step = min(step, attach_poll)
            time.sleep(max(0.05, min(step, deadline - time.time())))
    finally:
        for w in waiters:
            w.release()


def cmd_wait(a):
    floor = env_num("AGENTCTL_MIN_INTERVAL", 60)
    cap = max(floor, min(300, a.interval))
    if a.kind == "run":
        ids = list(a.ids)
        repo = a.repo
        if ids and not ids[0].isdigit():  # legacy form: wait run <repo> <id>...
            repo = repo or ids[0]
            ids = ids[1:]
        if not ids or not all(i.isdigit() for i in ids):
            raise SystemExit("agentctl: wait run needs numeric run ids (action_run.id, the GITHUB_RUN_ID)")
        o_r = slug(repo) if repo else default_repo()
        waiters = []
        for i in dict.fromkeys(ids):
            others = foreign_loops_on(i)
            if others:
                err(f"note: other loops already poll run {i}: pids {others} (see `agentctl orphans`)")
            key = f"run-{o_r.replace('/', '_')}-{i}" + ("-landable" if a.until == "landable" else "")
            waiters.append(Waiter(key, (lambda i=i: run_verdict(o_r, int(i), a.until, a.tail))))
        return wait_all(waiters, a.timeout, floor, cap)
    path = os.path.abspath(a.target)
    key = "file-" + re.sub(r"[^\w.-]", "_", path)[-150:]
    return wait_all([Waiter(key, lambda: file_verdict(path, a.contains))], a.timeout, min(floor, a.interval), max(min(floor, a.interval), min(300, a.interval)))
